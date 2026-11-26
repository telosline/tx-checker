"""Deposit check: compare a looked-up transaction with what the client was expected to send."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from tx_checker.check import check
from tx_checker.config import (
    CHAINS,
    MissingApiKeyError,
    Token,
    lookup_token,
    lookup_tron_token,
)
from tx_checker.detect import Detection, detect
from tx_checker.models import Network, Status, TokenTransfer, TxReport, jsonable

TOKEN_TICKERS = ("USDT", "USDC")
# Network name used in asset names -> (network, chain id).
ASSET_NETWORKS: dict[str, tuple[Network, int | None]] = {
    "TRON": (Network.TRON, None),
    "ETHEREUM": (Network.EVM, 1),
    "POLYGON": (Network.EVM, 137),
    "ARBITRUM": (Network.EVM, 42161),
}
NETWORK_ALIASES = {"TRC20": "TRON", "ERC20": "ETHEREUM"}
# Native coin -> the network it lives on.
NATIVE_ASSETS = {"ETH": "ETHEREUM", "TRX": "TRON", "POL": "POLYGON"}

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_TRON_ADDRESS = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")
_AMOUNT = re.compile(r"\d+(\.\d+)?")
_MAX_ECHO = 40


class LookupStoppedError(Exception):
    """A lookup was refused before it started, for example because a budget ran out."""


class DepositInputError(ValueError):
    """The expected asset, address or amount is not valid. Raised before any API call."""


class Verdict(StrEnum):
    MATCH = "match"
    WAITING = "waiting"
    MISMATCH = "mismatch"
    INCONCLUSIVE = "inconclusive"


class Reason(StrEnum):
    TX_FAILED = "tx_failed"
    WRONG_NETWORK = "wrong_network"
    WRONG_TOKEN = "wrong_token"
    WRONG_ADDRESS = "wrong_address"
    AMOUNT_LOWER = "amount_lower"
    AMOUNT_HIGHER = "amount_higher"


@dataclass(frozen=True)
class Asset:
    name: str
    ticker: str
    network: Network
    chain_id: int | None
    native: bool = False


@dataclass(frozen=True)
class Expected:
    asset: Asset
    address: str
    amount: Decimal


@dataclass(frozen=True)
class Sent:
    """Something a transaction moved instead of the expected asset. No symbol: the token is not
    in the registry and the amount is raw."""

    amount: Decimal
    symbol: str | None = None
    contract: str | None = None


@dataclass(kw_only=True)
class DepositResult:
    verdict: Verdict
    expected: Expected
    report: TxReport
    reasons: list[Reason] = field(default_factory=list)
    # What reached the expected address. Set once the token and the address matched.
    actual_amount: Decimal | None = None
    # WRONG_ADDRESS: who received the expected asset instead.
    actual_recipient: str | None = None
    # The symbol the matching token has when it is not the plain ticker (USDT0 for USDT).
    sent_as: str | None = None
    # WRONG_TOKEN: what the transaction moved instead of the expected asset.
    sent_instead: list[Sent] = field(default_factory=list)
    # WRONG_NETWORK: the chain where the hash was found.
    found_on: str | None = None
    similar_address: bool = False
    # Not found on the networks that were checked, and the search stopped before the others.
    search_stopped: bool = False
    # Why the other networks were not searched, when that is a setup problem and not a limit.
    unchecked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict, like `TxReport.to_dict`."""
        data = jsonable(self)
        data["report"] = self.report.to_dict()
        return data


@dataclass(frozen=True)
class _Move:
    recipient: str
    amount: Decimal
    symbol: str | None = None


def supported_assets() -> list[str]:
    """Asset names in display order: tokens per network, then native coins."""
    tokens = [f"{ticker}-{network}" for network in ASSET_NETWORKS for ticker in TOKEN_TICKERS]
    return [*tokens, *NATIVE_ASSETS]


def parse_asset(text: str) -> Asset:
    """Accept names such as USDT-TRON, USDT-TRC20 (alias) and ETH, in any case."""
    ticker, _, where = text.strip().upper().partition("-")
    if not where and ticker in NATIVE_ASSETS:
        return _asset(ticker, NATIVE_ASSETS[ticker], native=True)
    where = NETWORK_ALIASES.get(where, where)
    if ticker in TOKEN_TICKERS and where in ASSET_NETWORKS:
        return _asset(ticker, where)
    raise DepositInputError(
        f"Unknown asset '{text.strip()[:_MAX_ECHO]}'. Supported assets: "
        f"{', '.join(supported_assets())}."
    )


def _asset(ticker: str, where: str, native: bool = False) -> Asset:
    network, chain_id = ASSET_NETWORKS[where]
    name = ticker if native else f"{ticker}-{where}"
    return Asset(name, ticker, network, chain_id, native)


def parse_amount(text: str) -> Decimal:
    value = text.strip()
    if not _AMOUNT.fullmatch(value) or Decimal(value) <= 0:
        raise DepositInputError(
            "The expected amount must be a positive number written with a dot, such as 100 or 12.5."
        )
    return Decimal(value)


def parse_address(text: str, network: Network) -> str:
    address = text.strip()
    if network is Network.TRON:
        if not _is_tron_address(address):
            raise DepositInputError(
                "This asset needs a TRON address (34 characters, starts with T). "
                "Please check the address."
            )
    elif not _EVM_ADDRESS.fullmatch(address):
        raise DepositInputError(
            "This asset needs an EVM address (0x and 40 hex characters). Please check the address."
        )
    return address


def _is_tron_address(address: str) -> bool:
    """Base58check: version byte 0x41 and a double SHA-256 checksum."""
    if not _TRON_ADDRESS.fullmatch(address):
        return False
    number = 0
    for char in address:
        number = number * 58 + _BASE58.index(char)
    raw = number.to_bytes(25, "big")
    checksum = hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4]
    return raw[0] == 0x41 and raw[21:] == checksum


def parse_expected(address: str, amount: str, asset: str) -> Expected:
    parsed = parse_asset(asset)
    return Expected(parsed, parse_address(address, parsed.network), parse_amount(amount))


def check_deposit(report: TxReport, expected: Expected) -> DepositResult:
    """Compare a report with the expectation. Pure: no network calls."""
    result = DepositResult(verdict=Verdict.INCONCLUSIVE, expected=expected, report=report)
    if report.status not in (Status.SUCCESS, Status.FAILED, Status.PENDING):
        return result

    reasons = result.reasons
    if report.status is Status.FAILED:
        reasons.append(Reason.TX_FAILED)
    if not _on_expected_network(report, expected.asset):
        reasons.append(Reason.WRONG_NETWORK)
        result.found_on = report.chain_name
    if report.status is Status.PENDING and not reasons:
        # Not in a block yet: the transfer details are not known, so nothing can be compared.
        return result
    if report.status is Status.SUCCESS and not reasons:
        _compare_transfer(result)

    if reasons:
        result.verdict = Verdict.MISMATCH
    else:
        result.verdict = Verdict.MATCH if report.is_final else Verdict.WAITING
    return result


def _on_expected_network(report: TxReport, asset: Asset) -> bool:
    return report.network is asset.network and (
        asset.chain_id is None or report.chain_id == asset.chain_id
    )


def _compare_transfer(result: DepositResult) -> None:
    report, expected = result.report, result.expected
    moves = _moves(report, expected.asset)
    if not moves:
        result.reasons.append(Reason.WRONG_TOKEN)
        result.sent_instead = _sent_instead(report)
        return

    network = expected.asset.network
    to_expected = [m for m in moves if _same_address(m.recipient, expected.address, network)]
    if not to_expected:
        result.reasons.append(Reason.WRONG_ADDRESS)
        result.actual_recipient = moves[0].recipient or None
        result.similar_address = any(
            _similar_address(m.recipient, expected.address, network) for m in moves
        )
        return

    result.actual_amount = sum((m.amount for m in to_expected), Decimal(0))
    result.sent_as = next(
        (m.symbol for m in to_expected if m.symbol and m.symbol != expected.asset.ticker), None
    )
    if result.actual_amount < expected.amount:
        result.reasons.append(Reason.AMOUNT_LOWER)
    elif result.actual_amount > expected.amount:
        result.reasons.append(Reason.AMOUNT_HIGHER)


def _moves(report: TxReport, asset: Asset) -> list[_Move]:
    """Transfers of the expected asset in the transaction."""
    if asset.native:
        value = report.native_amount or Decimal(0)
        if value == 0 and report.token_transfers:
            return []
        # A native send with no value is kept: it shows up as an amount that is too low.
        return [_Move(report.to_address or "", value)]
    return [
        _Move(transfer.to_address, transfer.amount, symbol)
        for transfer in report.token_transfers
        if (symbol := _registry_symbol(report, transfer, asset.ticker)) is not None
    ]


def _registry_symbol(report: TxReport, transfer: TokenTransfer, ticker: str) -> str | None:
    """The registry symbol if this contract is the expected ticker. The provider's symbol is
    never used."""
    token = _registry_token(report, transfer)
    return token.symbol if token is not None and token.ticker == ticker else None


def _registry_token(report: TxReport, transfer: TokenTransfer) -> Token | None:
    if report.network is Network.TRON:
        return lookup_tron_token(transfer.token_contract)
    if report.chain_id is None:
        return None
    return lookup_token(report.chain_id, transfer.token_contract)


def _sent_instead(report: TxReport) -> list[Sent]:
    sent: list[Sent] = []
    if report.native_amount:
        sent.append(Sent(report.native_amount, report.native_symbol))
    for transfer in report.token_transfers:
        token = _registry_token(report, transfer)
        symbol = token.symbol if token else None
        sent.append(Sent(transfer.amount, symbol, transfer.token_contract))
    return sent


def _same_address(actual: str, expected: str, network: Network) -> bool:
    if network is Network.TRON:
        return actual == expected
    return actual.lower() == expected.lower()


def _similar_address(actual: str, expected: str, network: Network) -> bool:
    """Same first and last 4 characters but not the same address (address poisoning pattern)."""
    if _same_address(actual, expected, network):
        return False
    if network is Network.EVM:
        actual, expected = actual.lower().removeprefix("0x"), expected.lower().removeprefix("0x")
    return actual[:4] == expected[:4] and actual[-4:] == expected[-4:] and len(actual) >= 8


def find_deposit(
    raw_input: str,
    expected: Expected,
    *,
    required_confirmations: int | None = None,
    lookup: Callable[..., TxReport] = check,
) -> DepositResult:
    """Look the hash up where the asset says it should be, then search the other networks.

    `lookup` has the signature of `check`. The demo passes one that counts provider calls.
    """
    plan = _search_plan(detect(raw_input), expected.asset)
    first: TxReport | None = None
    looked: list[str] = []
    key_missing = False
    stopped = False
    for index, (network, chain_id) in enumerate(plan):
        try:
            report = lookup(
                raw_input,
                network=network,
                chain_id=chain_id,
                required_confirmations=required_confirmations,
            )
        except MissingApiKeyError:
            # Only the extra search is optional. The expected network must be reachable.
            if index == 0:
                raise
            key_missing = True
            break
        except LookupStoppedError:
            if index == 0:
                raise
            stopped = True
            break
        if report.status is not Status.NOT_FOUND:
            return check_deposit(report, expected)
        first = first or report
        looked.append(report.chain_name or "")

    assert first is not None  # the plan is never empty and the first lookup returned or raised
    first.networks_checked = looked
    if stopped:
        first.message = (
            f"Not found on {', '.join(looked)}. The other networks could not be checked right now."
        )
    elif len(looked) > 1:
        first.message = f"Not found on {', '.join(looked)}. All of them were checked."
    result = check_deposit(first, expected)
    result.search_stopped = stopped
    if key_missing:
        result.unchecked_reason = "no Etherscan key configured"
    return result


def _search_plan(detection: Detection, asset: Asset) -> list[tuple[Network | None, int | None]]:
    """Where to look, in order. The first entry is the best guess, the rest is the search."""
    if not detection.ok:
        # One lookup is enough: it comes back as an invalid-input report.
        return [(None, None)]
    evm = [(Network.EVM, chain_id) for chain_id in _evm_chains(detection.chain_id, asset)]
    tron: tuple[Network | None, int | None] = (Network.TRON, None)
    if detection.network is Network.EVM:
        return evm
    if not detection.guessed:
        return [tron]
    return [tron, *evm] if asset.network is Network.TRON else [*evm[:1], tron, *evm[1:]]


def _evm_chains(link_chain: int | None, asset: Asset) -> list[int]:
    """Chain of the link first, then the expected chain, then the free-tier mainnets."""
    mainnets = [c.chain_id for c in CHAINS.values() if c.free_tier and not c.testnet]
    ordered = [link_chain, asset.chain_id, *mainnets]
    return list(dict.fromkeys(chain_id for chain_id in ordered if chain_id is not None))
