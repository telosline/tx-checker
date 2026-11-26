import threading
from typing import Any

import httpx
import pytest
import respx

from tx_checker import config
from tx_checker.config import MissingApiKeyError
from tx_checker.deposit_check import parse_expected
from tx_checker.detect import detect
from tx_checker.guard import (
    ETHERSCAN,
    TRONSCAN,
    DemoBudget,
    Outcome,
    RateWindow,
    SharedGuard,
    TtlCache,
    cache_seconds,
    estimate_calls,
    guarded_check,
    guarded_deposit,
    new_session_window,
)
from tx_checker.models import Network, Status, TxReport
from tx_checker.providers.etherscan import BASE_URL
from tx_checker.providers.tronscan import BASE_URL as TRONSCAN_URL


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def tron_hash(n: int) -> str:
    return f"{n:064x}"


def evm_hash(n: int) -> str:
    return "0x" + tron_hash(n)


class FakeLookup:
    """Stands in for check(): counts calls and returns a settled success.

    It reports as many provider requests as the guard reserved, unless told otherwise.
    """

    def __init__(
        self, status: Status = Status.SUCCESS, requests: dict[str, int] | None = None
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.status = status
        self.requests = requests

    def __call__(self, raw: str, **kwargs: Any) -> TxReport:
        on_request = kwargs.pop("on_request", None)
        self.calls.append((raw, kwargs))
        if on_request:
            requests = self.requests or estimate_calls(detect(raw, kwargs["network"]))
            for provider, count in requests.items():
                for _ in range(count):
                    on_request(provider)
        return TxReport(hash=raw, status=self.status, confirmations=100, required_confirmations=12)


def make(clock: Clock, **budget: int) -> tuple[SharedGuard, Any]:
    shared = SharedGuard(
        budget=DemoBudget(
            per_minute=budget.get("per_minute", 60),
            per_day=budget.get("per_day", 5000),
            clock=clock,
        ),
        cache=TtlCache(clock=clock),
    )
    return shared, new_session_window(clock)


def test_window_allows_the_limit_and_then_refuses() -> None:
    clock = Clock()
    window = RateWindow(3, 60, clock)
    assert [window.try_acquire() for _ in range(4)] == [True, True, True, False]


def test_window_frees_room_when_old_events_expire() -> None:
    clock = Clock()
    window = RateWindow(2, 60, clock)
    window.try_acquire()
    clock.advance(30)
    window.try_acquire()
    assert not window.has_room()
    clock.advance(30)  # the first event is exactly one window old
    assert window.has_room()
    assert window.try_acquire()
    assert not window.has_room()


def test_window_counts_costs_and_checking_does_not_spend() -> None:
    window = RateWindow(10, 60, Clock())
    assert window.has_room(10)
    assert window.has_room(10)
    assert window.try_acquire(6)
    assert not window.has_room(5)
    assert window.has_room(4)


def test_each_provider_has_its_own_minute_budget() -> None:
    budget = DemoBudget(per_minute=5, per_day=100, clock=Clock())
    assert budget.try_spend({ETHERSCAN: 5})
    assert not budget.try_spend({ETHERSCAN: 1})
    assert budget.try_spend({TRONSCAN: 5})


def test_a_refused_spend_takes_nothing_from_any_provider() -> None:
    budget = DemoBudget(per_minute=5, per_day=100, clock=Clock())
    assert budget.try_spend({TRONSCAN: 5})
    assert not budget.try_spend({ETHERSCAN: 5, TRONSCAN: 1})
    assert budget.try_spend({ETHERSCAN: 5})


def test_daily_cap_holds_after_the_minute_window_has_passed() -> None:
    clock = Clock()
    budget = DemoBudget(per_minute=10, per_day=10, clock=clock)
    assert budget.try_spend({ETHERSCAN: 10})
    clock.advance(120)
    assert not budget.try_spend({ETHERSCAN: 1})
    assert budget.try_spend({TRONSCAN: 1})  # the other provider is untouched
    clock.advance(24 * 60 * 60)
    assert budget.try_spend({ETHERSCAN: 10})


def test_budget_is_exact_under_concurrent_use() -> None:
    budget = DemoBudget(per_minute=50, per_day=5000, clock=Clock())
    results: list[bool] = []

    def spend() -> None:
        results.append(budget.try_spend({TRONSCAN: 1}))

    threads = [threading.Thread(target=spend) for _ in range(200)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 50


def test_cache_entries_expire_on_their_own_schedule() -> None:
    clock = Clock()
    cache = TtlCache(clock=clock)
    cache.set("short", 1, 30)
    cache.set("long", 2, 600)
    clock.advance(31)
    assert cache.get("short") is None
    assert cache.get("long") == 2
    clock.advance(600)
    assert cache.get("long") is None


def test_cache_drops_the_least_recently_used_entry() -> None:
    cache = TtlCache(max_entries=2, clock=Clock())
    cache.set("a", 1, 100)
    cache.set("b", 2, 100)
    cache.get("a")
    cache.set("c", 3, 100)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


@pytest.mark.parametrize(
    ("status", "confirmations", "expected"),
    [
        (Status.SUCCESS, 12, config.CACHE_SECONDS_SETTLED),
        (Status.SUCCESS, 11, config.CACHE_SECONDS_WAITING),
        (Status.FAILED, 50, config.CACHE_SECONDS_SETTLED),
        (Status.FAILED, 2, config.CACHE_SECONDS_WAITING),
        (Status.PENDING, 0, config.CACHE_SECONDS_WAITING),
        (Status.NOT_FOUND, None, config.CACHE_SECONDS_NOT_FOUND),
        (Status.PROVIDER_ERROR, None, None),
        (Status.INVALID_INPUT, None, None),
    ],
)
def test_cache_lifetime_depends_on_how_final_the_result_is(
    status: Status, confirmations: int | None, expected: float | None
) -> None:
    report = TxReport(
        hash="x", status=status, confirmations=confirmations, required_confirmations=12
    )
    assert cache_seconds(report) == expected


def test_call_estimates_per_provider() -> None:
    assert estimate_calls(detect(evm_hash(1))) == {ETHERSCAN: config.EVM_CALLS_PER_LOOKUP}
    assert estimate_calls(detect(tron_hash(1), Network.TRON)) == {
        TRONSCAN: config.TRON_CALLS_PER_LOOKUP
    }
    assert estimate_calls(detect(tron_hash(1))) == {
        TRONSCAN: config.TRON_CALLS_PER_LOOKUP,
        ETHERSCAN: config.EVM_CALLS_PER_LOOKUP,
    }


def test_bad_input_costs_nothing_and_never_needs_the_budget() -> None:
    shared, session = make(Clock(), per_minute=0)
    lookup = FakeLookup()
    for raw in ("hello", "", "0x1234"):
        result = guarded_check(raw, shared=shared, session=session, lookup=lookup)
        assert result.outcome is Outcome.OK
    assert session.has_room(config.SESSION_LOOKUPS_PER_MINUTE)  # nothing was taken
    assert len(lookup.calls) == 3


@pytest.mark.parametrize("kwargs", [{"chain_id": 999999}, {"network": Network.TRON, "chain_id": 1}])
def test_unusable_options_are_answered_without_spending(kwargs: dict[str, Any]) -> None:
    shared, session = make(Clock(), per_minute=0)
    lookup = FakeLookup()
    raw = evm_hash(1) if "chain_id" in kwargs and "network" not in kwargs else tron_hash(1)
    result = guarded_check(raw, shared=shared, session=session, lookup=lookup, **kwargs)
    assert result.outcome is Outcome.OK
    assert len(lookup.calls) == 1


def test_a_repeated_lookup_is_served_from_the_cache() -> None:
    shared, session = make(Clock())
    lookup = FakeLookup()
    first = guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup)
    second = guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup)
    assert (first.cached, second.cached) == (False, True)
    assert second.report is first.report
    assert len(lookup.calls) == 1


def test_cache_hits_cost_neither_budget_nor_session_limit() -> None:
    shared, session = make(Clock(), per_minute=5)
    lookup = FakeLookup()
    for _ in range(config.SESSION_LOOKUPS_PER_MINUTE + 5):
        result = guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup)
        assert result.outcome is Outcome.OK
    assert len(lookup.calls) == 1


def test_the_cache_is_shared_between_sessions() -> None:
    shared, first_session = make(Clock())
    second_session = new_session_window()
    lookup = FakeLookup()
    guarded_check(evm_hash(1), shared=shared, session=first_session, lookup=lookup)
    result = guarded_check(evm_hash(1), shared=shared, session=second_session, lookup=lookup)
    assert result.cached
    assert second_session.has_room(config.SESSION_LOOKUPS_PER_MINUTE)


def test_different_chain_or_threshold_is_a_different_lookup() -> None:
    shared, session = make(Clock())
    lookup = FakeLookup()
    guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup)
    guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup, chain_id=137)
    guarded_check(
        evm_hash(1), shared=shared, session=session, lookup=lookup, required_confirmations=3
    )
    assert len(lookup.calls) == 3


def test_session_limit_stops_a_session_before_it_spends_the_shared_budget() -> None:
    clock = Clock()
    shared, session = make(clock)
    lookup = FakeLookup()
    for n in range(config.SESSION_LOOKUPS_PER_MINUTE):
        assert (
            guarded_check(
                tron_hash(n), shared=shared, session=session, lookup=lookup, network=Network.TRON
            ).outcome
            is Outcome.OK
        )
    blocked = guarded_check(
        tron_hash(99), shared=shared, session=session, lookup=lookup, network=Network.TRON
    )
    assert blocked.outcome is Outcome.SESSION_LIMIT
    assert blocked.report is None
    assert len(lookup.calls) == config.SESSION_LOOKUPS_PER_MINUTE
    clock.advance(61)
    assert (
        guarded_check(
            tron_hash(99), shared=shared, session=session, lookup=lookup, network=Network.TRON
        ).outcome
        is Outcome.OK
    )


def test_a_new_session_does_not_get_around_the_shared_budget() -> None:
    shared, _ = make(Clock(), per_minute=10)
    lookup = FakeLookup()
    outcomes = [
        guarded_check(evm_hash(n), shared=shared, session=new_session_window(), lookup=lookup)
        for n in range(3)
    ]
    assert [o.outcome for o in outcomes] == [Outcome.OK, Outcome.OK, Outcome.BUSY]
    assert len(lookup.calls) == 2


def test_a_busy_answer_does_not_use_up_the_session_limit() -> None:
    shared, session = make(Clock(), per_minute=0)
    lookup = FakeLookup()
    result = guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup)
    assert result.outcome is Outcome.BUSY
    assert session.has_room(config.SESSION_LOOKUPS_PER_MINUTE)
    assert lookup.calls == []


def test_one_provider_cannot_use_up_the_other() -> None:
    shared, session = make(Clock(), per_minute=5)
    lookup = FakeLookup()
    evm = [
        guarded_check(evm_hash(n), shared=shared, session=new_session_window(), lookup=lookup)
        for n in range(2)
    ]
    assert [r.outcome for r in evm] == [Outcome.OK, Outcome.BUSY]
    tron = guarded_check(
        tron_hash(5), shared=shared, session=session, lookup=lookup, network=Network.TRON
    )
    assert tron.outcome is Outcome.OK


def test_a_bare_hash_reserves_for_the_evm_fallback_too() -> None:
    shared, session = make(Clock(), per_minute=4)
    result = guarded_check(tron_hash(1), shared=shared, session=session, lookup=FakeLookup())
    assert result.outcome is Outcome.BUSY


def test_daily_cap_gives_the_same_busy_answer() -> None:
    clock = Clock()
    shared, session = make(clock, per_day=5)
    lookup = FakeLookup()
    assert guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup).outcome is (
        Outcome.OK
    )
    clock.advance(120)
    assert guarded_check(evm_hash(2), shared=shared, session=session, lookup=lookup).outcome is (
        Outcome.BUSY
    )


def test_provider_errors_are_not_cached() -> None:
    shared, session = make(Clock())
    lookup = FakeLookup(Status.PROVIDER_ERROR)
    guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup)
    result = guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup)
    assert not result.cached
    assert len(lookup.calls) == 2


def test_not_found_is_cached_only_briefly() -> None:
    clock = Clock()
    shared, session = make(clock)
    lookup = FakeLookup(Status.NOT_FOUND)
    guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup)
    assert guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup).cached
    clock.advance(config.CACHE_SECONDS_NOT_FOUND + 1)
    assert not guarded_check(evm_hash(1), shared=shared, session=session, lookup=lookup).cached


def test_a_missing_key_is_not_swallowed() -> None:
    shared, session = make(Clock())

    def no_key(raw: str, **kwargs: Any) -> TxReport:
        raise MissingApiKeyError("ETHERSCAN_API_KEY is not set.")

    with pytest.raises(MissingApiKeyError):
        guarded_check(evm_hash(1), shared=shared, session=session, lookup=no_key)


def test_options_reach_the_lookup() -> None:
    shared, session = make(Clock())
    lookup = FakeLookup()
    guarded_check(
        evm_hash(1),
        shared=shared,
        session=session,
        lookup=lookup,
        chain_id=137,
        required_confirmations=7,
    )
    assert lookup.calls[0][1] == {
        "network": None,
        "chain_id": 137,
        "required_confirmations": 7,
    }


def test_demo_lists_only_free_tier_chains() -> None:
    assert config.DEMO_CHAIN_IDS == (1, 11155111, 137, 42161)
    for chain_id in (56, 10, 8453):
        assert not config.CHAINS[chain_id].free_tier
        assert chain_id in config.CHAINS  # still available in the CLI


def test_limits_are_the_agreed_example_values() -> None:
    assert config.SESSION_LOOKUPS_PER_MINUTE == 10
    assert config.PROVIDER_CALLS_PER_DAY == 5000


def room(budget: DemoBudget, provider: str, limit: int) -> int:
    """How many calls the provider has left, found by taking them."""
    left = 0
    while left < limit and budget.try_spend({provider: 1}):
        left += 1
    return left


def test_a_reservation_gives_back_what_was_not_used() -> None:
    budget = DemoBudget(per_minute=10, per_day=100)
    reservation = budget.reserve({ETHERSCAN: 5})
    assert reservation is not None
    reservation.settle({ETHERSCAN: 1})
    assert room(budget, ETHERSCAN, 10) == 9


def test_a_reservation_charges_calls_beyond_the_estimate() -> None:
    budget = DemoBudget(per_minute=10, per_day=100)
    reservation = budget.reserve({ETHERSCAN: 1})
    assert reservation is not None
    reservation.settle({ETHERSCAN: 3})
    assert room(budget, ETHERSCAN, 10) == 7


def test_settling_a_reservation_that_already_expired_changes_nothing() -> None:
    clock = Clock()
    budget = DemoBudget(per_minute=10, per_day=100, clock=clock)
    reservation = budget.reserve({ETHERSCAN: 5})
    assert reservation is not None
    clock.advance(61)
    reservation.settle({ETHERSCAN: 0})
    assert room(budget, ETHERSCAN, 10) == 10


def test_guarded_check_charges_the_calls_that_were_made() -> None:
    # Each lookup needs 5 free calls to start, but only the 1 it makes stays spent.
    shared, session = make(Clock(), per_minute=9)
    lookup = FakeLookup(requests={ETHERSCAN: 1})
    outcomes = [
        guarded_check(evm_hash(n), shared=shared, session=new_session_window(), lookup=lookup)
        for n in range(5)
    ]
    assert [o.outcome for o in outcomes] == [Outcome.OK] * 5
    assert guarded_check(evm_hash(9), shared=shared, session=session, lookup=lookup).outcome is (
        Outcome.BUSY
    )


def test_a_lookup_that_fails_before_any_call_gets_its_reservation_back() -> None:
    shared, session = make(Clock(), per_minute=5)

    def no_key(raw: str, **kwargs: Any) -> TxReport:
        raise MissingApiKeyError("ETHERSCAN_API_KEY is not set")

    with pytest.raises(MissingApiKeyError):
        guarded_check(evm_hash(1), shared=shared, session=session, lookup=no_key)
    assert room(shared.budget, ETHERSCAN, 5) == 5


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETHERSCAN_API_KEY", "TESTKEY")
    monkeypatch.setenv("TRONSCAN_API_KEY", "TESTKEY")


def etherscan_misses() -> respx.Route:
    return respx.get(BASE_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
    )


@respx.mock
@pytest.mark.usefixtures("keys")
def test_a_miss_on_one_chain_costs_one_etherscan_call() -> None:
    route = etherscan_misses()
    shared, session = make(Clock(), per_minute=20)

    result = guarded_check(evm_hash(1), shared=shared, session=session, chain_id=137)

    assert result.report is not None and result.report.status is Status.NOT_FOUND
    assert route.call_count == 1
    assert room(shared.budget, ETHERSCAN, 20) == 19


@respx.mock
@pytest.mark.usefixtures("keys")
def test_a_deposit_search_that_misses_everywhere_costs_one_call_per_chain() -> None:
    route = etherscan_misses()
    shared, session = make(Clock(), per_minute=20)
    expected = parse_expected("0x" + "ab" * 20, "10", "USDT-ETHEREUM")

    result = guarded_deposit(evm_hash(1), expected, shared=shared, session=session)

    assert result.deposit is not None
    assert result.deposit.report.networks_checked == ["Ethereum", "Polygon", "Arbitrum One"]
    assert route.call_count == 3
    assert room(shared.budget, ETHERSCAN, 20) == 17


@respx.mock
@pytest.mark.usefixtures("keys")
def test_a_bare_hash_search_misses_on_tron_then_costs_one_call_per_evm_chain() -> None:
    etherscan = etherscan_misses()
    tronscan = respx.get(f"{TRONSCAN_URL}/transaction-info").mock(
        return_value=httpx.Response(200, json={})
    )
    shared, session = make(Clock(), per_minute=20)
    expected = parse_expected("0x" + "ab" * 20, "10", "USDT-ETHEREUM")

    result = guarded_deposit(tron_hash(1), expected, shared=shared, session=session)

    assert result.deposit is not None
    assert (tronscan.call_count, etherscan.call_count) == (1, 3)
    assert room(shared.budget, TRONSCAN, 20) == 19
    assert room(shared.budget, ETHERSCAN, 20) == 17


def test_one_deposit_search_takes_one_session_slot() -> None:
    shared, session = make(Clock())
    expected = parse_expected("0x" + "ab" * 20, "10", "USDT-ETHEREUM")
    lookup = FakeLookup(Status.NOT_FOUND, requests={ETHERSCAN: 1})

    guarded_deposit(evm_hash(1), expected, shared=shared, session=session, lookup=lookup)

    assert len(lookup.calls) == 3
    taken = 0
    while session.has_room():
        session.add()
        taken += 1
    assert taken == config.SESSION_LOOKUPS_PER_MINUTE - 1


def test_a_deposit_search_that_cannot_start_is_refused_as_a_whole() -> None:
    shared, session = make(Clock(), per_minute=4)
    expected = parse_expected("0x" + "ab" * 20, "10", "USDT-ETHEREUM")
    lookup = FakeLookup()

    result = guarded_deposit(evm_hash(1), expected, shared=shared, session=session, lookup=lookup)

    assert result.outcome is Outcome.BUSY
    assert result.deposit is None
    assert lookup.calls == []


def test_a_search_that_runs_out_of_budget_reports_what_it_checked() -> None:
    # The first step reserves 5 and uses 4. A second reservation of 5 no longer fits.
    shared, session = make(Clock(), per_minute=5)
    expected = parse_expected("0x" + "ab" * 20, "10", "USDT-ETHEREUM")
    lookup = FakeLookup(Status.NOT_FOUND, requests={ETHERSCAN: 4})

    result = guarded_deposit(evm_hash(1), expected, shared=shared, session=session, lookup=lookup)

    assert result.outcome is Outcome.OK
    assert result.deposit is not None
    assert result.deposit.search_stopped
    assert len(result.deposit.report.networks_checked) == 1
