import logging
import os
from dataclasses import dataclass
from decimal import Decimal

import streamlit as st

from tx_checker import config
from tx_checker.config import CHAINS, DEMO_CHAIN_IDS, MissingApiKeyError
from tx_checker.explain.explain import explain, short_address
from tx_checker.guard import Outcome, SharedGuard, guarded_check, new_session_window
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
from tx_checker.redact import redact

log = logging.getLogger("tx_checker.app")

LANGUAGES = {"en": "English", "uk": "Українська", "ru": "Русский"}

# Public mainnet transactions, so a visitor can try the tool without a hash at hand.
EXAMPLES = (
    ("TRON: USDT transfer", "6b614edb1f73ff5eacc28d4ca84b2add6b68376d00c7c41154c5a39c514077e8"),
    (
        "TRON: failed, out of energy",
        "02603687d24c4f3d9a2af90171aae7cad94f33594b2c9c05d33b77785dfac5aa",
    ),
    (
        "Ethereum: ETH transfer",
        "0x1d5807ff7c727bb1a1f69b7561622118186900eb84f3fa494d17dfc8f2e2d1b0",
    ),
    (
        "Ethereum: failed, out of gas",
        "0x2ae10539f56fbe3642a97da1d495bc8289c603d2526970d406c107f35179a6bc",
    ),
)

# Label -> (network, chain id). Only chains the free Etherscan plan serves are offered.
NETWORK_CHOICES: dict[str, tuple[Network | None, int | None]] = {
    "Auto-detect": (None, None),
    "TRON": (Network.TRON, None),
    **{CHAINS[chain_id].name: (None, chain_id) for chain_id in DEMO_CHAIN_IDS},
}

MSG_SESSION_LIMIT = (
    f"You have reached the limit of {config.SESSION_LOOKUPS_PER_MINUTE} lookups per minute. "
    "Please wait a little and try again."
)
MSG_BUSY = "The demo is busy right now. Please try again in a minute."
MSG_NO_KEY = "The demo is not configured: an API key is missing."
MSG_ERROR = "Something went wrong. Please try again later."

FAILURE_PHRASES = {
    FailureReason.OUT_OF_GAS: "likely out of gas",
    FailureReason.REVERTED: "likely rejected by the contract (revert)",
    FailureReason.OUT_OF_ENERGY: "out of energy",
    FailureReason.FEE_LIMIT_EXCEEDED: "fee limit exceeded",
    FailureReason.OTHER: "other reason",
    FailureReason.UNKNOWN: "unknown reason",
}
HEADLINES = {
    Status.PENDING: ("Pending: not in a block yet", "orange"),
    Status.NOT_FOUND: ("Not found", "gray"),
    Status.INVALID_INPUT: ("Invalid input", "red"),
    Status.PROVIDER_ERROR: ("Service temporarily unavailable", "red"),
}


@dataclass(frozen=True)
class View:
    """What the last lookup left to show: a report, or a notice such as a limit message."""

    report: TxReport | None = None
    notice: str | None = None
    level: str = "info"


def load_secrets_into_env() -> None:
    """Streamlit Cloud keeps keys in st.secrets. Hand them to the core, which reads the env."""
    for name in ("ETHERSCAN_API_KEY", "TRONSCAN_API_KEY"):
        if os.environ.get(name):
            continue
        try:
            value = st.secrets.get(name)
        except Exception:  # no secrets file locally, the core falls back to .env
            continue
        if value:
            os.environ[name] = str(value)


@st.cache_resource
def shared_guard() -> SharedGuard:
    """One guard per process: the budget and the cache are shared by every visitor."""
    return SharedGuard()


def run_lookup(
    query: str, network: Network | None, chain_id: int | None, required: int | None
) -> View:
    if "limiter" not in st.session_state:
        st.session_state["limiter"] = new_session_window()
    try:
        with st.spinner("Looking up the transaction..."):
            result = guarded_check(
                query,
                shared=shared_guard(),
                session=st.session_state["limiter"],
                network=network,
                chain_id=chain_id,
                required_confirmations=required,
            )
    except MissingApiKeyError as exc:
        log.error("API key missing: %s", exc)
        return View(notice=MSG_NO_KEY, level="error")
    except Exception as exc:
        # The visitor gets a plain message. The log gets a line with no URLs or keys.
        log.error("Unexpected error: %s: %s", type(exc).__name__, redact(str(exc)))
        return View(notice=MSG_ERROR, level="error")
    if result.outcome is Outcome.SESSION_LIMIT:
        return View(notice=MSG_SESSION_LIMIT, level="warning")
    if result.outcome is Outcome.BUSY:
        return View(notice=MSG_BUSY, level="info")
    return View(report=result.report)


def plain(value: Decimal) -> str:
    return format(value, "f")


def amount_text(transfer: TokenTransfer) -> str:
    if transfer.decimals is None:
        return f"{plain(transfer.amount)} (raw units)"
    return f"{plain(transfer.amount)} {transfer.token_symbol}"


def headline(report: TxReport) -> tuple[str, str]:
    """The big status line: what happened and, for a failure, the likely reason."""
    if report.status is Status.SUCCESS:
        if report.is_final:
            return "Success: fully confirmed", "green"
        return "Success: waiting for confirmations", "orange"
    if report.status is Status.FAILED:
        phrase = FAILURE_PHRASES.get(report.failure_reason, "unknown reason")  # type: ignore[arg-type]
        return f"Failed: {phrase}", "red"
    return HEADLINES[report.status]


def confirmations_text(report: TxReport) -> str | None:
    if report.status is Status.PENDING:
        return "Not in a block yet"
    if report.confirmations is None:
        return None
    count = f"{report.confirmations:,}"
    if report.required_confirmations is None:
        return count
    if report.confirmations >= report.required_confirmations:
        return f"Final ({count})"
    return f"{count} of {report.required_confirmations:,}"


def fee_text(report: TxReport) -> str | None:
    match report.fee:
        case EvmFee(fee_native=paid) | TronFee(fee_total_trx=paid):
            return f"{plain(paid)} {report.native_symbol}"
    return None


def symbol_label(transfer: TokenTransfer) -> str:
    if transfer.token_symbol is None:
        return "unknown token"
    return transfer.token_symbol.replace(" (legacy)", ", legacy")


def key_facts(report: TxReport) -> list[tuple[str, str]]:
    facts = [("Network", report.chain_name or "")]
    if (confirmations := confirmations_text(report)) is not None:
        facts.append(("Confirmations", confirmations))
    moved = [amount_text(t) for t in report.token_transfers]
    if report.native_amount and report.native_amount > 0:
        moved.append(f"{plain(report.native_amount)} {report.native_symbol}")
    if moved:
        facts.append(("Amount", ", ".join(moved)))
    tried = [amount_text(t) for t in report.attempted_transfers]
    if tried:
        facts.append(("Tried to send (not executed)", ", ".join(tried)))
    if (fee := fee_text(report)) is not None:
        facts.append(("Fee", fee))
    if report.timestamp is not None:
        facts.append(("Time (UTC)", report.timestamp.strftime("%Y-%m-%d %H:%M")))
    if report.block_number is not None:
        facts.append(("Block", f"{report.block_number:,}"))
    facts.append(("From", short_address(report.from_address)))
    facts.extend(destination_facts(report))
    return facts


def destination_facts(report: TxReport) -> list[tuple[str, str]]:
    """For a token transfer the transaction goes to the token contract, not to the recipient."""
    transfers = report.token_transfers or report.attempted_transfers
    to = report.to_address
    contract = next((t for t in transfers if to and t.token_contract.lower() == to.lower()), None)
    facts = [
        (f"Token contract ({symbol_label(contract)})" if contract else "To", short_address(to))
    ]
    if transfers:
        recipients = ", ".join(dict.fromkeys(short_address(t.to_address) for t in transfers))
        label = "Recipient" if report.token_transfers else "Intended recipient"
        facts.append((label, recipients))
    return facts


def fee_lines(report: TxReport) -> list[str]:
    match report.fee:
        case EvmFee() as fee:
            return [
                f"Fee: {plain(fee.fee_native)} {report.native_symbol}",
                f"Gas used: {fee.gas_used} of {fee.gas_limit}",
                f"Gas price: {plain(fee.effective_gas_price_gwei)} gwei",
            ]
        case TronFee() as fee:
            burned = max(0, fee.energy_used - fee.energy_from_stake - fee.energy_from_contract)
            return [
                f"TRX burned in total: {plain(fee.fee_total_trx)}",
                f"Energy used: {fee.energy_used} ({fee.energy_from_stake} from staked TRX, "
                f"{fee.energy_from_contract} paid by the contract, {burned} paid by burning "
                f"{plain(fee.energy_burned_trx)} TRX)",
                f"Bandwidth used: {fee.bandwidth_used} ({plain(fee.bandwidth_burned_trx)} TRX "
                "burned)",
            ]
    return []


def transfer_rows(transfers: list[TokenTransfer]) -> list[dict[str, str]]:
    return [
        {
            "Token": t.token_symbol or "unknown token",
            "Amount": amount_text(t),
            "From": short_address(t.from_address),
            "To": short_address(t.to_address),
        }
        for t in transfers
    ]


def show_facts(report: TxReport) -> None:
    facts = key_facts(report)
    for start in range(0, len(facts), 2):
        # One row per pair keeps the order readable when the columns stack on a phone.
        for column, (label, value) in zip(st.columns(2), facts[start : start + 2], strict=False):
            column.caption(label)
            column.markdown(value)


def show_details(report: TxReport, explanation_internal: str) -> None:
    """Everything the grid already summarises, in collapsed boxes so the result stays short."""
    if report.token_transfers:
        with st.expander("Token transfers"):
            st.dataframe(transfer_rows(report.token_transfers), hide_index=True, width="stretch")
    if report.attempted_transfers:
        with st.expander("Attempted transfers (not executed)"):
            st.dataframe(
                transfer_rows(report.attempted_transfers), hide_index=True, width="stretch"
            )
    if report.fee:
        with st.expander("Fee breakdown"):
            st.markdown("\n".join(f"- {line}" for line in fee_lines(report)))
    with st.expander("Full hash and addresses"):
        for label, value in (
            ("Hash", report.hash),
            ("From", report.from_address),
            ("To", report.to_address),
        ):
            if value:
                st.caption(label)
                st.code(value, language=None, wrap_lines=True)
    with st.expander("Internal summary"):
        st.code(explanation_internal, language=None, wrap_lines=True)
    with st.expander("Raw API data"):
        st.json(report.raw or {}, expanded=False)


def show_report(report: TxReport) -> None:
    text, color = headline(report)
    st.header(f":{color}[{text}]", anchor=False)
    if report.status in (Status.SUCCESS, Status.FAILED, Status.PENDING):
        show_facts(report)
    for warning in report.warnings:
        if warning.code is WarningCode.LOOKALIKE_TOKEN:
            st.warning(warning.text)
        else:
            st.info(warning.text)

    st.subheader("Message for the client", anchor=False)
    lang = (
        st.pills(
            "Language",
            list(LANGUAGES),
            default="en",
            key="lang",
            format_func=LANGUAGES.get,
            label_visibility="collapsed",
        )
        or "en"
    )
    explanation = explain(report, lang)
    st.code(explanation.client, language=None, wrap_lines=True)
    show_details(report, explanation.internal)


def pick_example() -> None:
    label = st.session_state.get("example_choice")
    tx_hash = dict(EXAMPLES).get(label)
    if tx_hash:
        st.session_state["query"] = tx_hash
        st.session_state["example"] = tx_hash


def main() -> None:
    st.set_page_config(page_title="Transaction checker", layout="centered")
    load_secrets_into_env()

    st.title("Transaction checker")
    st.write(
        "Paste a transaction hash or an explorer link. You get the facts for a support agent "
        "and a message that can be sent to the client."
    )

    with st.form("lookup"):
        text = st.text_input(
            "Transaction hash or explorer link",
            key="query",
            max_chars=300,
            placeholder="0x... or 64 hex characters, or a link from Etherscan or Tronscan",
        )
        submitted = st.form_submit_button("Check", width="stretch")

    # A selectbox, not buttons in an expander: an expander stays open after a click inside it.
    st.selectbox(
        "Try an example",
        [label for label, _ in EXAMPLES],
        index=None,
        placeholder="Pick a public transaction",
        key="example_choice",
        on_change=pick_example,
    )
    with st.expander("Options"):
        choice = st.selectbox(
            "Network",
            list(NETWORK_CHOICES),
            key="network",
            help="Auto-detect works for most hashes and links. Chains that need a paid "
            "Etherscan plan are not offered in the demo.",
        )
        required = st.number_input(
            "Confirmations required",
            min_value=1,
            max_value=100000,
            value=None,
            step=1,
            placeholder="Default for the network",
            help="Example thresholds only. Every exchange and payment provider sets its own.",
        )

    network, chain_id = NETWORK_CHOICES[choice]
    query = text if submitted else st.session_state.pop("example", None)
    if query is not None:
        st.session_state["view"] = run_lookup(
            query, network, chain_id, int(required) if required is not None else None
        )

    view: View | None = st.session_state.get("view")
    if view is not None:
        if view.notice:
            getattr(st, view.level)(view.notice)
        if view.report is not None:
            show_report(view.report)

    st.divider()
    st.caption(
        "Data from Etherscan and Tronscan APIs. The demo runs on free API keys, so it can be "
        "busy at times."
    )


main()
