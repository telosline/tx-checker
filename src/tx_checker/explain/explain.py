from dataclasses import dataclass
from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum
from typing import Any

from tx_checker.config import CHAINS, TRON_NAME
from tx_checker.deposit_check import Asset, DepositResult, Reason, Sent, Verdict
from tx_checker.detect import MSG_ADDRESS
from tx_checker.explain.templates import (
    DEPOSIT_INTERNAL,
    DEPOSIT_TEMPLATES,
    INTERNAL,
    TEMPLATES,
)
from tx_checker.models import (
    EvmFee,
    FailureReason,
    Network,
    Status,
    TokenTransfer,
    TronFee,
    TxReport,
    WarningCode,
)

LANGS = tuple(TEMPLATES)

# The one warning a client sees, most important first. Others stay in the internal summary.
CLIENT_WARNING_PRIORITY = (
    WarningCode.LOOKALIKE_TOKEN,
    WarningCode.LEGACY_TOKEN,
    WarningCode.UNKNOWN_TOKEN,
    WarningCode.TO_IS_CONTRACT,
    WarningCode.NETWORK_UNCONFIRMED,
    WarningCode.NETWORK_GUESSED,
    WarningCode.ZERO_VALUE,
)

NO_ADDRESS = "—"
FEE_STEP = Decimal("0.000001")


class Scenario(StrEnum):
    SUCCESS_FINAL = "success_final"
    SUCCESS_WAITING = "success_waiting"
    PENDING = "pending"
    FAILED_OUT_OF_GAS = "failed_out_of_gas"
    FAILED_OUT_OF_ENERGY = "failed_out_of_energy"
    FAILED_REVERTED = "failed_reverted"
    FAILED_OTHER = "failed_other"
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    INVALID_ADDRESS = "invalid_address"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True)
class Explanation:
    scenario: Scenario
    internal: str
    client: str


def explain(report: TxReport, lang: str = "en") -> Explanation:
    """Build the internal summary (English) and the client message (`lang`) for a report."""
    if lang not in TEMPLATES:
        raise ValueError(f"Unsupported language {lang!r}. Use one of: {', '.join(LANGS)}.")
    scenario = pick_scenario(report)
    return Explanation(
        scenario=scenario,
        internal=internal_summary(report),
        client=client_text(report, scenario, lang),
    )


def pick_scenario(report: TxReport) -> Scenario:
    match report.status:
        case Status.SUCCESS:
            return Scenario.SUCCESS_FINAL if report.is_final else Scenario.SUCCESS_WAITING
        case Status.PENDING:
            return Scenario.PENDING
        case Status.FAILED:
            return _FAILED_SCENARIOS.get(report.failure_reason, Scenario.FAILED_OTHER)
        case Status.NOT_FOUND:
            return Scenario.NOT_FOUND
        case Status.INVALID_INPUT:
            if report.message == MSG_ADDRESS:
                return Scenario.INVALID_ADDRESS
            return Scenario.INVALID_INPUT
        case _:
            return Scenario.PROVIDER_ERROR


_FAILED_SCENARIOS = {
    FailureReason.OUT_OF_GAS: Scenario.FAILED_OUT_OF_GAS,
    FailureReason.OUT_OF_ENERGY: Scenario.FAILED_OUT_OF_ENERGY,
    FailureReason.REVERTED: Scenario.FAILED_REVERTED,
}

# Nothing to look up for these, so no explorer link.
_NO_LINK = {Scenario.INVALID_INPUT, Scenario.INVALID_ADDRESS}


def client_text(report: TxReport, scenario: Scenario, lang: str) -> str:
    """The message for the client: a few sentences, then the explorer link on its own line."""
    texts = TEMPLATES[lang]
    values = _values(report, scenario, texts)
    sentences = [part.format(**values) for part in texts["scenarios"][scenario.value]]
    text = " ".join(s for s in sentences if s)
    if report.explorer_url and scenario not in _NO_LINK:
        text += "\n" + texts["snippets"]["link_line"].format(link=report.explorer_url)
    return text


def _values(report: TxReport, scenario: Scenario, texts: dict[str, Any]) -> dict[str, str]:
    snippets = texts["snippets"]
    network = report.chain_name or ""
    return {
        "network": network,
        "native": report.native_symbol or "",
        "confirmations": "?" if report.confirmations is None else str(report.confirmations),
        "required": "?"
        if report.required_confirmations is None
        else str(report.required_confirmations),
        "details": _details(report, scenario, texts),
        "fee_line": _fee_line(report, snippets),
        "warning": _warning(report, texts, network),
        "not_found_line": _not_found_line(report, snippets),
    }


def _details(report: TxReport, scenario: Scenario, texts: dict[str, Any]) -> str:
    snippets = texts["snippets"]
    when = _when(report, snippets)
    if scenario in (Scenario.SUCCESS_FINAL, Scenario.SUCCESS_WAITING):
        amounts = _amounts(report.token_transfers, report, snippets)
        recipients = _recipients(report.token_transfers) or short_address(report.to_address)
        key = "details_transfer" if amounts else "details_none"
        return snippets[key].format(amount=amounts, to=recipients, when=when)
    if scenario is Scenario.PENDING:
        to = short_address(report.to_address)
        if _native(report):
            amounts = _amounts([], report, snippets)
            return snippets["details_pending_amount"].format(amount=amounts, to=to)
        if report.to_address:
            return snippets["details_pending_plain"].format(to=to)
        return ""
    if report.status is Status.FAILED:
        attempted = _amounts(report.attempted_transfers, report, snippets)
        to = _recipients(report.attempted_transfers) or short_address(report.to_address)
        if attempted:
            return snippets["details_failed_attempt"].format(to=to, when=when, attempted=attempted)
        return snippets["details_failed"].format(to=to, when=when)
    return ""


def _amounts(transfers: list[TokenTransfer], report: TxReport, snippets: dict[str, str]) -> str:
    parts = [_asset(t, snippets) for t in transfers]
    native = _native(report)
    if native:
        parts.append(f"{_plain(native)} {report.native_symbol or ''}".strip())
    return ", ".join(parts)


def _asset(transfer: TokenTransfer, snippets: dict[str, str]) -> str:
    if transfer.decimals is None:
        return snippets["raw_units"].format(amount=_plain(transfer.amount))
    return f"{_plain(transfer.amount)} {transfer.token_symbol}"


def _native(report: TxReport) -> Decimal | None:
    if report.native_amount is not None and report.native_amount > 0:
        return report.native_amount
    return None


def _recipients(transfers: list[TokenTransfer]) -> str:
    unique = dict.fromkeys(short_address(t.to_address) for t in transfers)
    return ", ".join(unique)


def short_address(address: str | None) -> str:
    """First 6 and last 4 characters, enough for the client to recognise the address."""
    if not address:
        return NO_ADDRESS
    if len(address) <= 12:
        return address
    return f"{address[:6]}…{address[-4:]}"


def _when(report: TxReport, snippets: dict[str, str]) -> str:
    if report.timestamp is None:
        return snippets["time_unknown"]
    return report.timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _fee_line(report: TxReport, snippets: dict[str, str]) -> str:
    fee = _fee_text(report)
    return snippets["fee_charged"].format(fee=fee) if fee else ""


def _fee_text(report: TxReport) -> str:
    """The fee as 'amount SYMBOL', or an empty string when nothing was paid."""
    match report.fee:
        case EvmFee(fee_native=paid) if paid > 0:
            return f"{_rounded(paid)} {report.native_symbol}"
        case TronFee(fee_total_trx=paid) if paid > 0:
            return f"{_rounded(paid)} {report.native_symbol}"
    return ""


def _rounded(value: Decimal) -> str:
    """At most 6 decimals for the client, marked with an approximation sign when rounded."""
    with localcontext() as context:
        context.prec = 100
        rounded = value.quantize(FEE_STEP, rounding=ROUND_HALF_UP)
        if rounded == value:
            return _plain(value)
        if rounded == 0:
            return f"< {_plain(FEE_STEP)}"
        return f"\u2248 {_plain(rounded.normalize())}"


def _warning(report: TxReport, texts: dict[str, Any], network: str) -> str:
    present = {w.code for w in report.warnings}
    for code in CLIENT_WARNING_PRIORITY:
        if code in present:
            return texts["warnings"][code.value].format(network=network)
    return ""


def _not_found_line(report: TxReport, snippets: dict[str, str]) -> str:
    checked = report.networks_checked or [report.chain_name or ""]
    if len(checked) == 1:
        return snippets["not_found_one"].format(checked=checked[0])
    items = snippets["not_found_join"].join(
        snippets["not_found_item"].format(name=n) for n in checked
    )
    return snippets["not_found_many"].format(checked=items)


def _plain(value: Decimal) -> str:
    # "f" avoids scientific notation such as 1E-7
    return format(value, "f")


def internal_summary(report: TxReport) -> str:
    """Up to six short English lines with the facts for the support agent."""
    lines = [
        _status_line(report),
        _route_line(report),
        _moved_line(report),
        _fee_detail_line(report),
        _notes_line(report),
        f"Link: {report.explorer_url}" if report.explorer_url else "",
    ]
    return "\n".join(line for line in lines if line)


def _status_line(report: TxReport) -> str:
    status = report.status.name.replace("_", " ")
    if report.failure_reason is not None:
        status += f" ({INTERNAL['reasons'][report.failure_reason.value]})"
    parts = []
    if report.chain_name:
        chain = f" (chain {report.chain_id})" if report.chain_id is not None else ""
        parts.append(f"Network: {report.chain_name}{chain}")
    parts.append(f"Status: {status}")
    if report.status in (Status.SUCCESS, Status.FAILED, Status.PENDING):
        parts.append(_confirmations_text(report))
    if report.status is Status.INVALID_INPUT and report.hash:
        parts.append(f"Input: {report.hash}")
    return " | ".join(parts)


def _confirmations_text(report: TxReport) -> str:
    if report.confirmations is None:
        return f"Confirmations: {INTERNAL['unknown']}"
    text = f"Confirmations: {report.confirmations}/{report.required_confirmations}"
    if report.status is Status.SUCCESS:
        text += f" ({INTERNAL['final'] if report.is_final else INTERNAL['not_final']})"
    return text


def _route_line(report: TxReport) -> str:
    if report.from_address is None and report.to_address is None:
        return ""
    line = f"From: {report.from_address or NO_ADDRESS} -> To: {report.to_address or NO_ADDRESS}"
    extras = []
    if report.block_number is not None:
        extras.append(f"block {report.block_number}")
    if report.timestamp is not None:
        extras.append(report.timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"))
    return f"{line} | {', '.join(extras)}" if extras else line


def _moved_line(report: TxReport) -> str:
    native = _native(report)
    if report.status is Status.PENDING:
        return (
            f"Amount (not in a block yet): {_plain(native)} {report.native_symbol}"
            if native
            else ""
        )
    parts = [_transfer_text(t) for t in report.token_transfers]
    if native and report.status is not Status.FAILED:
        parts.append(f"{_plain(native)} {report.native_symbol}")
    moved = f"Moved: {'; '.join(parts)}" if parts else ""
    attempts = [_transfer_text(t) for t in report.attempted_transfers]
    if native and report.status is Status.FAILED:
        attempts.append(f"{_plain(native)} {report.native_symbol}")
    attempted = f"Attempted transfer: {'; '.join(attempts)}, not executed" if attempts else ""
    return " | ".join(part for part in (moved, attempted) if part)


def _transfer_text(transfer: TokenTransfer) -> str:
    route = f"{transfer.from_address} -> {transfer.to_address}"
    if transfer.decimals is None:
        return (
            f"{_plain(transfer.amount)} raw units of contract {transfer.token_contract} "
            f"(decimals unknown, {route})"
        )
    return f"{_plain(transfer.amount)} {transfer.token_symbol} ({route})"


def _fee_detail_line(report: TxReport) -> str:
    match report.fee:
        case EvmFee() as fee:
            return (
                f"Fee: {_plain(fee.fee_native)} {report.native_symbol} "
                f"(gas {fee.gas_used}/{fee.gas_limit}, {_plain(fee.effective_gas_price_gwei)} gwei)"
            )
        case TronFee() as fee:
            burned_units = max(
                0, fee.energy_used - fee.energy_from_stake - fee.energy_from_contract
            )
            return (
                f"Fee: {_plain(fee.fee_total_trx)} TRX burned (energy {fee.energy_used}: "
                f"{fee.energy_from_stake} from stake, {fee.energy_from_contract} paid by the "
                f"contract, {burned_units} burned = {_plain(fee.energy_burned_trx)} TRX; "
                f"bandwidth {fee.bandwidth_used}, {_plain(fee.bandwidth_burned_trx)} TRX burned)"
            )
    return ""


def _notes_line(report: TxReport) -> str:
    notes = []
    if report.message:
        notes.append(report.message)
    if report.networks_checked and report.status is Status.NOT_FOUND:
        notes.append(f"Checked: {', '.join(report.networks_checked)}")
    notes.extend(f"{w.code.name}: {w.text}" for w in report.warnings)
    return f"Notes: {' | '.join(notes)}" if notes else ""


class DepositScenario(StrEnum):
    """Deposit results that need their own text. Failed, pending, not found and error results
    use the `Scenario` texts of the transaction itself."""

    MATCH = "match"
    WAITING = "waiting"
    WRONG_NETWORK = "wrong_network"
    WRONG_TOKEN = "wrong_token"
    WRONG_ADDRESS = "wrong_address"
    AMOUNT_LOWER = "amount_lower"
    AMOUNT_HIGHER = "amount_higher"


@dataclass(frozen=True)
class DepositExplanation:
    scenario: DepositScenario | Scenario
    internal: str
    client: str


_REASON_SCENARIOS = {
    Reason.WRONG_NETWORK: DepositScenario.WRONG_NETWORK,
    Reason.WRONG_TOKEN: DepositScenario.WRONG_TOKEN,
    Reason.WRONG_ADDRESS: DepositScenario.WRONG_ADDRESS,
    Reason.AMOUNT_LOWER: DepositScenario.AMOUNT_LOWER,
    Reason.AMOUNT_HIGHER: DepositScenario.AMOUNT_HIGHER,
}


def explain_deposit(result: DepositResult, lang: str = "en") -> DepositExplanation:
    """Build the internal summary (English) and the client message (`lang`) for a deposit check."""
    if lang not in DEPOSIT_TEMPLATES:
        raise ValueError(f"Unsupported language {lang!r}. Use one of: {', '.join(LANGS)}.")
    scenario = pick_deposit_scenario(result)
    return DepositExplanation(
        scenario=scenario,
        internal=deposit_internal_summary(result),
        client=deposit_client_text(result, scenario, lang),
    )


def pick_deposit_scenario(result: DepositResult) -> DepositScenario | Scenario:
    match result.verdict:
        case Verdict.MATCH:
            return DepositScenario.MATCH
        case Verdict.WAITING:
            return DepositScenario.WAITING
        case Verdict.MISMATCH if result.reasons[0] is not Reason.TX_FAILED:
            return _REASON_SCENARIOS[result.reasons[0]]
    # A failed transaction and every inconclusive result are explained as the transaction itself.
    return pick_scenario(result.report)


def deposit_client_text(
    result: DepositResult, scenario: DepositScenario | Scenario, lang: str
) -> str:
    if isinstance(scenario, Scenario):
        return client_text(result.report, scenario, lang)
    texts, base = DEPOSIT_TEMPLATES[lang], TEMPLATES[lang]
    values = _deposit_values(result, scenario, texts["snippets"], base)
    sentences = [part.format(**values) for part in texts["scenarios"][scenario.value]]
    text = " ".join(s for s in sentences if s)
    if result.report.explorer_url:
        text += "\n" + base["snippets"]["link_line"].format(link=result.report.explorer_url)
    return text


def _deposit_values(
    result: DepositResult,
    scenario: DepositScenario,
    snippets: dict[str, str],
    base: dict[str, Any],
) -> dict[str, str]:
    report, expected = result.report, result.expected
    ticker = expected.asset.ticker
    actual = result.actual_amount
    values = {
        "network": report.chain_name or "",
        "expected_network": _expected_network(expected.asset),
        "asset": ticker,
        "confirmations": "?" if report.confirmations is None else str(report.confirmations),
        "required": "?"
        if report.required_confirmations is None
        else str(report.required_confirmations),
        "amount": f"{_plain(actual)} {result.sent_as or ticker}" if actual is not None else "",
        "expected_amount": f"{_plain(expected.amount)} {ticker}",
        "expected_address": expected.address,
        "actual_address": result.actual_recipient or NO_ADDRESS,
        "similar_line": snippets["similar_line"] if result.similar_address else "",
        "sent_line": _sent_line(result.sent_instead, snippets),
        "warning": _warning(report, base, report.chain_name or ""),
    }
    if scenario is DepositScenario.WRONG_ADDRESS and result.similar_address:
        # The hint about similar addresses matters more, and the text stays within 5 sentences.
        values["warning"] = ""
    when = _when(report, base["snippets"])
    values["details"] = ""
    if scenario in (DepositScenario.MATCH, DepositScenario.WAITING):
        values["details"] = snippets["details_match"].format(
            amount=values["amount"], to=short_address(expected.address), when=when
        )
    elif scenario is DepositScenario.WRONG_NETWORK:
        as_if = Scenario.PENDING if report.status is Status.PENDING else Scenario.SUCCESS_FINAL
        values["details"] = _details(report, as_if, base)
    return values


def _expected_network(asset: Asset) -> str:
    return TRON_NAME if asset.network is Network.TRON else CHAINS[asset.chain_id or 0].name


def _sent_line(sent: list[Sent], snippets: dict[str, str]) -> str:
    if not sent:
        return snippets["sent_none"]
    items = []
    for item in sent:
        if item.symbol is None:
            items.append(snippets["sent_unknown"].format(contract=short_address(item.contract)))
        else:
            items.append(f"{_plain(item.amount)} {item.symbol}")
    return snippets["sent_line"].format(sent=snippets["sent_join"].join(items))


def deposit_internal_summary(result: DepositResult) -> str:
    """The verdict, what was expected and what was found, then the usual status lines."""
    report = result.report
    lines = [
        _verdict_line(result),
        f"Expected: {_plain(result.expected.amount)} {result.expected.asset.name} "
        f"to {result.expected.address}",
        _found_line(result),
        f"Note: {DEPOSIT_INTERNAL['similar_address']}" if result.similar_address else "",
        _status_line(report),
        _notes_line(report),
        f"Link: {report.explorer_url}" if report.explorer_url else "",
    ]
    return "\n".join(line for line in lines if line)


def _verdict_line(result: DepositResult) -> str:
    labels = DEPOSIT_INTERNAL["reasons"]
    match result.verdict:
        case Verdict.WAITING:
            report = result.report
            detail = (
                f"{DEPOSIT_INTERNAL['waiting']}: {report.confirmations}/"
                f"{report.required_confirmations}"
            )
        case Verdict.MISMATCH:
            detail = "; ".join(_reason_text(result, reason, labels) for reason in result.reasons)
        case Verdict.INCONCLUSIVE:
            detail = DEPOSIT_INTERNAL["inconclusive"][_inconclusive_key(result.report.status)]
        case _:
            return "Deposit check: MATCH"
    return f"Deposit check: {result.verdict.name} ({detail})"


def _reason_text(result: DepositResult, reason: Reason, labels: dict[str, str]) -> str:
    label = labels[reason.value]
    if reason is Reason.WRONG_NETWORK:
        return f"{label}, found on {result.found_on}"
    return label


def _inconclusive_key(status: Status) -> str:
    if status in (Status.NOT_FOUND, Status.PENDING, Status.INVALID_INPUT):
        return status.value
    return "provider_error"


def _found_line(result: DepositResult) -> str:
    expected = result.expected
    ticker = expected.asset.ticker
    if result.verdict is Verdict.INCONCLUSIVE:
        return ""
    if Reason.TX_FAILED in result.reasons:
        return "Found: the transaction failed, nothing was transferred"
    if Reason.WRONG_NETWORK in result.reasons:
        return f"Found on: {result.found_on} (expected {_expected_network(expected.asset)})"
    if Reason.WRONG_TOKEN in result.reasons:
        found = "; ".join(_sent_text(item) for item in result.sent_instead)
        return f"Found: {found or f'no transfer of {ticker}'}"
    if Reason.WRONG_ADDRESS in result.reasons:
        return f"Found: {ticker} sent to {result.actual_recipient or NO_ADDRESS}"

    actual = result.actual_amount if result.actual_amount is not None else Decimal(0)
    symbol = result.sent_as or ticker
    line = f"Found: {_plain(actual)} {symbol} to {expected.address}"
    if result.sent_as:
        line += f" ({DEPOSIT_INTERNAL['sent_as'].format(symbol=result.sent_as, ticker=ticker)})"
    if actual != expected.amount:
        direction = "lower" if actual < expected.amount else "higher"
        line += f", {direction} than expected by {_plain(abs(expected.amount - actual))}"
    return line


def _sent_text(item: Sent) -> str:
    if item.symbol is None:
        return f"unknown token {item.contract} (raw amount {_plain(item.amount)})"
    return f"{_plain(item.amount)} {item.symbol}"
