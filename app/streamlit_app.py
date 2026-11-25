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

BADGES = {
    Status.FAILED: ("Failed", "red"),
    Status.PENDING: ("Pending", "orange"),
    Status.NOT_FOUND: ("Not found", "gray"),
    Status.INVALID_INPUT: ("Invalid input", "red"),
    Status.PROVIDER_ERROR: ("Service unavailable", "red"),
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


def badge_for(report: TxReport) -> tuple[str, str]:
    if report.status is Status.SUCCESS:
        if report.is_final:
            return "Success, fully confirmed", "green"
        return "Success, waiting for confirmations", "orange"
    return BADGES[report.status]


def key_facts(report: TxReport) -> list[tuple[str, str]]:
    facts = [("Network", report.chain_name or "")]
    if report.status is Status.PENDING:
        facts.append(("Confirmations", "Not in a block yet"))
    elif report.confirmations is not None:
        done = report.status is Status.SUCCESS and report.is_final
        facts.append(
            (
                "Confirmations",
                "Fully confirmed"
                if done
                else f"{report.confirmations} of {report.required_confirmations}",
            )
        )
    moved = [amount_text(t) for t in report.token_transfers]
    if report.native_amount and report.native_amount > 0:
        moved.append(f"{plain(report.native_amount)} {report.native_symbol}")
    if moved:
        facts.append(("Amount", ", ".join(moved)))
    tried = [amount_text(t) for t in report.attempted_transfers]
    if tried:
        facts.append(("Tried to send (not executed)", ", ".join(tried)))
    if report.timestamp is not None:
        facts.append(("Time (UTC)", report.timestamp.strftime("%Y-%m-%d %H:%M")))
    if report.block_number is not None:
        facts.append(("Block", str(report.block_number)))
    facts.append(("From", short_address(report.from_address)))
    facts.append(("To", short_address(report.to_address)))
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


def show_details(report: TxReport) -> None:
    if report.fee:
        with st.expander("Fee breakdown"):
            st.markdown("\n".join(f"- {line}" for line in fee_lines(report)))
    if report.token_transfers:
        st.caption("Token transfers")
        st.dataframe(transfer_rows(report.token_transfers), hide_index=True, width="stretch")
    if report.attempted_transfers:
        st.caption("Attempted transfers, not executed")
        st.dataframe(transfer_rows(report.attempted_transfers), hide_index=True, width="stretch")
    with st.expander("Full hash and addresses"):
        for label, value in (
            ("Hash", report.hash),
            ("From", report.from_address),
            ("To", report.to_address),
        ):
            if value:
                st.caption(label)
                st.code(value, language=None, wrap_lines=True)


def show_report(report: TxReport, lang: str) -> None:
    label, color = badge_for(report)
    st.badge(label, color=color)
    if report.status in (Status.SUCCESS, Status.FAILED, Status.PENDING):
        show_facts(report)
        show_details(report)
    for warning in report.warnings:
        if warning.code is WarningCode.LOOKALIKE_TOKEN:
            st.warning(warning.text)
        else:
            st.info(warning.text)

    explanation = explain(report, lang)
    st.subheader("Internal summary")
    st.code(explanation.internal, language=None, wrap_lines=True)
    st.subheader("Message for the client")
    st.caption(f"Language: {LANGUAGES[lang]}. Use the copy button in the corner of the box.")
    st.code(explanation.client, language=None, wrap_lines=True)
    with st.expander("Raw API data"):
        st.json(report.raw or {}, expanded=False)


def pick_example(tx_hash: str) -> None:
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

    lang = (
        st.pills(
            "Language of the client message",
            list(LANGUAGES),
            default="en",
            format_func=LANGUAGES.get,
        )
        or "en"
    )

    with st.expander("Options"):
        choice = st.selectbox(
            "Network",
            list(NETWORK_CHOICES),
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
    with st.expander("Try an example"):
        for label, tx_hash in EXAMPLES:
            st.button(
                label,
                key=f"example-{tx_hash[:10]}",
                on_click=pick_example,
                args=(tx_hash,),
                width="stretch",
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
            show_report(view.report, lang)

    st.divider()
    st.caption(
        "Data from Etherscan and Tronscan APIs. The demo runs on free API keys, so it can be "
        "busy at times."
    )


main()
