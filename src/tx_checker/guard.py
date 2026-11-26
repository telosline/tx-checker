"""Protection for the public demo: limits, a result cache and the guarded lookup.

Nothing here knows about Streamlit. The app keeps one `SharedGuard` for the whole process and
one `RateWindow` per browser session.
"""

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from tx_checker import config
from tx_checker.check import check
from tx_checker.deposit_check import DepositResult, Expected, LookupStoppedError, find_deposit
from tx_checker.detect import Detection, detect
from tx_checker.models import Network, Status, TxReport

Clock = Callable[[], float]

ETHERSCAN = "etherscan"
TRONSCAN = "tronscan"
MINUTE = 60.0
DAY = 24 * 60 * 60.0


@dataclass
class _Event:
    cost: int
    at: float
    expired: bool = False


class RateWindow:
    """Sliding window: at most `limit` units in the last `seconds` seconds."""

    def __init__(self, limit: int, seconds: float, clock: Clock = time.monotonic) -> None:
        self._limit = limit
        self._seconds = seconds
        self._clock = clock
        self._events: deque[_Event] = deque()
        self._used = 0

    def has_room(self, cost: int = 1) -> bool:
        self._expire()
        return self._used + cost <= self._limit

    def add(self, cost: int = 1) -> _Event:
        event = _Event(cost, self._clock())
        self._events.append(event)
        self._used += cost
        return event

    def try_acquire(self, cost: int = 1) -> bool:
        if not self.has_room(cost):
            return False
        self.add(cost)
        return True

    def resize(self, event: _Event, cost: int) -> None:
        """Change what an earlier `add` took. An event that already left the window is gone."""
        self._expire()
        if event.expired:
            return
        self._used += cost - event.cost
        event.cost = cost

    def _expire(self) -> None:
        cutoff = self._clock() - self._seconds
        while self._events and self._events[0].at <= cutoff:
            event = self._events.popleft()
            event.expired = True
            self._used -= event.cost


class Reservation:
    """Calls taken up front from the provider budgets. `settle` makes them match what was used."""

    def __init__(
        self,
        taken: dict[str, list[tuple[RateWindow, _Event]]],
        lock: threading.Lock,
    ) -> None:
        self._taken = taken
        self._lock = lock

    def settle(self, used: Mapping[str, int]) -> None:
        """Refund what was reserved but not used. Calls beyond the reservation are charged."""
        with self._lock:
            for name, entries in self._taken.items():
                for window, event in entries:
                    window.resize(event, used.get(name, 0))


class DemoBudget:
    """Provider calls for the whole app, per minute and per rolling day, one budget per provider."""

    def __init__(
        self,
        providers: tuple[str, ...] = (ETHERSCAN, TRONSCAN),
        per_minute: int | None = None,
        per_day: int | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        # Read the limits here, not at import, so a deployment or a test can change them.
        per_minute = config.PROVIDER_CALLS_PER_MINUTE if per_minute is None else per_minute
        per_day = config.PROVIDER_CALLS_PER_DAY if per_day is None else per_day
        self._windows = {
            name: (RateWindow(per_minute, MINUTE, clock), RateWindow(per_day, DAY, clock))
            for name in providers
        }
        self._lock = threading.Lock()

    def reserve(self, costs: Mapping[str, int]) -> Reservation | None:
        """Take the calls from every provider named, or from none if any of them is out."""
        with self._lock:
            windows = [(name, self._windows[name], cost) for name, cost in costs.items()]
            if not all(w.has_room(cost) for _, pair, cost in windows for w in pair):
                return None
            taken = {
                name: [(window, window.add(cost)) for window in pair]
                for name, pair, cost in windows
            }
            return Reservation(taken, self._lock)

    def try_spend(self, costs: Mapping[str, int]) -> bool:
        return self.reserve(costs) is not None


class TtlCache:
    """Small in-memory cache. Each entry carries its own lifetime."""

    def __init__(self, max_entries: int | None = None, clock: Clock = time.monotonic) -> None:
        self._max = config.CACHE_MAX_ENTRIES if max_entries is None else max_entries
        self._clock = clock
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if entry[0] <= self._clock():
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return entry[1]

    def set(self, key: Any, value: Any, seconds: float) -> None:
        with self._lock:
            self._items[key] = (self._clock() + seconds, value)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)


@dataclass
class SharedGuard:
    """State shared by every session of the app."""

    budget: DemoBudget = field(default_factory=DemoBudget)
    cache: TtlCache = field(default_factory=TtlCache)


class Outcome(StrEnum):
    OK = "ok"
    SESSION_LIMIT = "session_limit"
    BUSY = "busy"


@dataclass(frozen=True)
class GuardResult:
    outcome: Outcome
    report: TxReport | None = None
    cached: bool = False
    deposit: DepositResult | None = None


def new_session_window(clock: Clock = time.monotonic) -> RateWindow:
    return RateWindow(config.SESSION_LOOKUPS_PER_MINUTE, MINUTE, clock)


def estimate_calls(detection: Detection) -> dict[str, int]:
    """Worst-case provider calls for one lookup, known before anything is sent."""
    if detection.network is Network.TRON:
        costs = {TRONSCAN: config.TRON_CALLS_PER_LOOKUP}
        if detection.guessed:
            costs[ETHERSCAN] = config.EVM_CALLS_PER_LOOKUP
        return costs
    return {ETHERSCAN: config.EVM_CALLS_PER_LOOKUP}


def cache_seconds(report: TxReport) -> float | None:
    """How long a result may be reused. None means do not cache."""
    match report.status:
        case Status.SUCCESS | Status.FAILED:
            settled = (
                report.confirmations is not None
                and report.required_confirmations is not None
                and report.confirmations >= report.required_confirmations
            )
            return config.CACHE_SECONDS_SETTLED if settled else config.CACHE_SECONDS_WAITING
        case Status.PENDING:
            return config.CACHE_SECONDS_WAITING
        case Status.NOT_FOUND:
            return config.CACHE_SECONDS_NOT_FOUND
    return None


class SessionGate(Protocol):
    """What a lookup needs from the session limit: ask for room, then take it."""

    def has_room(self) -> bool: ...

    def add(self) -> object: ...


class _OncePerSearch:
    """A deposit search is one lookup for the visitor, however many networks it tries."""

    def __init__(self, session: RateWindow) -> None:
        self._session = session
        self._charged = False

    def has_room(self) -> bool:
        return self._charged or self._session.has_room()

    def add(self) -> None:
        if not self._charged:
            self._session.add()
            self._charged = True


def guarded_check(
    raw_input: str,
    *,
    shared: SharedGuard,
    session: RateWindow,
    network: Network | None = None,
    chain_id: int | None = None,
    required_confirmations: int | None = None,
    lookup: Callable[..., TxReport] = check,
) -> GuardResult:
    """Validate, reuse a cached result, then spend limits and only then call the providers."""
    return _guarded_step(
        raw_input,
        shared=shared,
        session=session,
        network=network,
        chain_id=chain_id,
        required_confirmations=required_confirmations,
        lookup=lookup,
    )


def guarded_deposit(
    raw_input: str,
    expected: Expected,
    *,
    shared: SharedGuard,
    session: RateWindow,
    required_confirmations: int | None = None,
    lookup: Callable[..., TxReport] = check,
) -> GuardResult:
    """A deposit check where every provider call of the search goes through the same limits.

    The session limit is charged once per search. A search that cannot start is answered like
    a refused lookup. One that runs out of budget half way stops and reports what it checked.
    """
    gate = _OncePerSearch(session)
    refused: list[Outcome] = []

    def step(raw: str, **options: Any) -> TxReport:
        result = _guarded_step(raw, shared=shared, session=gate, lookup=lookup, **options)
        if result.outcome is not Outcome.OK or result.report is None:
            refused.append(result.outcome)
            raise LookupStoppedError
        return result.report

    try:
        deposit = find_deposit(
            raw_input, expected, required_confirmations=required_confirmations, lookup=step
        )
    except LookupStoppedError:
        return GuardResult(refused[0])
    return GuardResult(Outcome.OK, deposit=deposit)


def _guarded_step(
    raw_input: str,
    *,
    shared: SharedGuard,
    session: SessionGate,
    network: Network | None = None,
    chain_id: int | None = None,
    required_confirmations: int | None = None,
    lookup: Callable[..., TxReport],
) -> GuardResult:
    """One provider lookup: cache, session limit, budget reservation, call, settle."""

    def run(**extra: Any) -> TxReport:
        return lookup(
            raw_input,
            network=network,
            chain_id=chain_id,
            required_confirmations=required_confirmations,
            **extra,
        )

    detection = detect(raw_input, _effective_network(network, chain_id))
    chosen_chain = _chain_for(detection, chain_id)
    if _is_free_to_answer(detection, network, chain_id, chosen_chain):
        # Bad input is answered without any provider call, so it costs nothing.
        return GuardResult(Outcome.OK, run())

    key = (
        detection.network,
        detection.tx_hash,
        chosen_chain,
        detection.chain_unconfirmed and chain_id is None,
        detection.guessed,
        required_confirmations,
    )
    cached = shared.cache.get(key)
    if cached is not None:
        return GuardResult(Outcome.OK, cached, cached=True)

    if not session.has_room():
        return GuardResult(Outcome.SESSION_LIMIT)
    # Reserve the worst case, then give back what the lookup did not need: a hash that is
    # not found costs one call, not five.
    reservation = shared.budget.reserve(estimate_calls(detection))
    if reservation is None:
        return GuardResult(Outcome.BUSY)
    session.add()

    used: dict[str, int] = {}

    def count(provider: str) -> None:
        used[provider] = used.get(provider, 0) + 1

    try:
        report = run(on_request=count)
    finally:
        reservation.settle(used)
    seconds = cache_seconds(report)
    if seconds is not None:
        shared.cache.set(key, report, seconds)
    return GuardResult(Outcome.OK, report)


def _effective_network(network: Network | None, chain_id: int | None) -> Network | None:
    if chain_id is not None and network is not Network.TRON:
        return Network.EVM
    return network


def _chain_for(detection: Detection, chain_id: int | None) -> int | None:
    if detection.network is not Network.EVM:
        return None
    for candidate in (chain_id, detection.chain_id):
        if candidate is not None:
            return candidate
    return config.DEFAULT_CHAIN_ID


def _is_free_to_answer(
    detection: Detection, network: Network | None, chain_id: int | None, chosen_chain: int | None
) -> bool:
    if not detection.ok or detection.tx_hash is None:
        return True
    if chain_id is not None and network is Network.TRON:
        return True
    return chosen_chain is not None and chosen_chain not in config.CHAINS
