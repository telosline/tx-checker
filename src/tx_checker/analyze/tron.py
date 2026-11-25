import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from tx_checker.analyze.amounts import scale_amount
from tx_checker.config import (
    PROTECTED_SYMBOLS,
    TRON_NAME,
    TRON_REQUIRED_CONFIRMATIONS,
    TRON_SYMBOL,
    Token,
    lookup_tron_token,
    tron_tx_url,
)
from tx_checker.models import (
    FailureReason,
    Network,
    ReportWarning,
    Status,
    TokenTransfer,
    TronFee,
    TxReport,
    WarningCode,
)

SUN_DECIMALS = 6
TRX_TRANSFER = 1
SMART_CONTRACT_CALL = 31
# Used only when the response does not carry its own price per unit.
DEFAULT_ENERGY_PRICE_SUN = 100
DEFAULT_BANDWIDTH_PRICE_SUN = 1000

_FAILURE_REASONS = {
    "OUT_OF_ENERGY": FailureReason.OUT_OF_ENERGY,
    "REVERT": FailureReason.REVERTED,
    "UNKNOWN": FailureReason.UNKNOWN,
}

# Capital letters from other alphabets that pass for the Latin ones in a token symbol.
_HOMOGLYPHS = str.maketrans({"Т": "T", "Τ": "T", "С": "C", "Ѕ": "S", "Ԁ": "D"})


def analyze_tron(info: dict[str, Any], required_confirmations: int | None = None) -> TxReport:
    """Turn a Tronscan transaction-info response into a TxReport. Pure: no network, no clock."""
    tx_hash = str(info["hash"]).lower()
    required = (
        required_confirmations
        if required_confirmations is not None
        else TRON_REQUIRED_CONFIRMATIONS
    )
    contract_type = info.get("contractType")
    to = info.get("toAddress")
    native_sun = _native_sun(info, contract_type)

    report = TxReport(
        hash=tx_hash,
        status=Status.PENDING,
        network=Network.TRON,
        chain_name=TRON_NAME,
        explorer_url=tron_tx_url(tx_hash),
        from_address=info.get("ownerAddress"),
        to_address=to,
        to_is_contract=_to_is_contract(info, to, contract_type),
        native_amount=scale_amount(native_sun, SUN_DECIMALS) if native_sun is not None else None,
        native_symbol=TRON_SYMBOL,
        required_confirmations=required,
        confirmations=0,
        raw=info,
    )
    if not info.get("block"):
        return report

    result = info.get("contractRet")
    if not isinstance(result, str):
        report.status = Status.PROVIDER_ERROR
        report.message = "The provider returned a transaction without a clear result."
        report.confirmations = None
        return report

    report.block_number = _as_int(info["block"])
    report.confirmations = _confirmations(info)
    report.timestamp = _timestamp(info)
    report.status = Status.SUCCESS if result == "SUCCESS" else Status.FAILED
    if report.status is Status.FAILED:
        report.failure_reason = _FAILURE_REASONS.get(result, FailureReason.OTHER)

    cost = info.get("cost")
    if isinstance(cost, dict):
        report.fee = _fee(cost)

    transfers, token_warnings = _token_transfers(info.get("trc20TransferInfo"))
    # A failed call still lists the transfer it tried to make, but nothing moved.
    if report.status is Status.SUCCESS:
        report.token_transfers = transfers
    else:
        report.attempted_transfers = transfers

    report.warnings = _warnings(report, native_sun, contract_type, token_warnings)
    return report


def _warnings(
    report: TxReport,
    native_sun: int | None,
    contract_type: Any,
    token_warnings: list[ReportWarning],
) -> list[ReportWarning]:
    warnings: list[ReportWarning] = []
    plain_trx_send = contract_type == TRX_TRANSFER
    # Token transfers and contract calls go through a contract by design, only a plain
    # TRX transfer to a contract is odd.
    if plain_trx_send and native_sun and report.to_is_contract:
        warnings.append(
            ReportWarning(
                WarningCode.TO_IS_CONTRACT, "Native coins were sent directly to a contract address."
            )
        )
    if (
        report.status is Status.SUCCESS
        and plain_trx_send
        and native_sun == 0
        and not report.token_transfers
        and report.to_is_contract is False
    ):
        warnings.append(
            ReportWarning(WarningCode.ZERO_VALUE, "The transaction moved no coins and no tokens.")
        )
    warnings.extend(token_warnings)
    if (
        report.status is Status.SUCCESS
        and report.confirmations is not None
        and report.required_confirmations is not None
        and report.confirmations < report.required_confirmations
    ):
        warnings.append(
            ReportWarning(
                WarningCode.LOW_CONFIRMATIONS,
                "The transaction succeeded but has fewer confirmations than required.",
            )
        )
    return warnings


def _token_transfers(items: Any) -> tuple[list[TokenTransfer], list[ReportWarning]]:
    """Parse TRC-20 transfers. Symbol and decimals come from our registry, never the provider."""
    transfers: list[TokenTransfer] = []
    warnings: list[ReportWarning] = []
    warned: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type", "Transfer") != "Transfer":
            continue
        contract = item.get("contract_address")
        try:
            raw_amount = int(str(item["amount_str"]))
        except (KeyError, ValueError):
            continue
        if not isinstance(contract, str):
            continue
        token = lookup_tron_token(contract)
        transfers.append(
            TokenTransfer(
                token_contract=contract,
                from_address=str(item.get("from_address", "")),
                to_address=str(item.get("to_address", "")),
                amount=scale_amount(raw_amount, token.decimals) if token else Decimal(raw_amount),
                token_symbol=token.symbol if token else None,
                decimals=token.decimals if token else None,
            )
        )
        if contract not in warned:
            warned.add(contract)
            if token is None:
                warnings.extend(_unknown_token_warnings(contract, item.get("symbol")))
            elif token.legacy:
                warnings.append(_legacy_token_warning(contract, token))
    return transfers, warnings


def _unknown_token_warnings(contract: str, claimed_symbol: Any) -> list[ReportWarning]:
    warnings = [
        ReportWarning(
            WarningCode.UNKNOWN_TOKEN,
            f"Token {contract} is not in the registry, so its amount is shown as a raw number.",
        )
    ]
    official = _normalized_symbol(claimed_symbol)
    if official in PROTECTED_SYMBOLS:
        warnings.append(
            ReportWarning(
                WarningCode.LOOKALIKE_TOKEN,
                f"Token {contract} uses the name {official} but is not the official {official} "
                "contract known to this tool.",
            )
        )
    return warnings


def _legacy_token_warning(contract: str, token: Token) -> ReportWarning:
    base = token.symbol.removesuffix(" (legacy)")
    seen_as = f", shown as {token.explorer_symbol} on Tronscan" if token.explorer_symbol else ""
    return ReportWarning(
        WarningCode.LEGACY_TOKEN,
        f"Token {contract} is an older version of {base} on TRON{seen_as}. "
        "Check whether the receiving platform still accepts it.",
    )


def _normalized_symbol(symbol: Any) -> str:
    if not isinstance(symbol, str):
        return ""
    text = unicodedata.normalize("NFKC", symbol).upper().translate(_HOMOGLYPHS)
    return "".join(ch for ch in text if ch.isalnum())


def _fee(cost: dict[str, Any]) -> TronFee:
    energy_price = _as_int(cost.get("energy_fee_cost")) or DEFAULT_ENERGY_PRICE_SUN
    bandwidth_price = _as_int(cost.get("net_fee_cost")) or DEFAULT_BANDWIDTH_PRICE_SUN
    energy_from_stake = _as_int(cost.get("energy_usage"))
    energy_from_contract = _as_int(cost.get("origin_energy_usage"))
    energy_burned_sun = _as_int(cost.get("energy_fee"))
    bandwidth_burned_sun = _as_int(cost.get("net_fee"))

    energy_used = (
        _as_int(cost["energy_usage_total"])
        if "energy_usage_total" in cost
        else energy_from_stake + energy_from_contract + energy_burned_sun // energy_price
    )
    other_burned_sun = sum(
        _as_int(cost.get(key)) for key in ("account_create_fee", "memoFee", "multi_sign_fee")
    )
    # `fee` is the total in list responses but 0 in transaction-info, so never go below it.
    total_sun = max(
        energy_burned_sun + bandwidth_burned_sun + other_burned_sun, _as_int(cost.get("fee"))
    )
    return TronFee(
        energy_used=energy_used,
        energy_from_stake=energy_from_stake,
        energy_from_contract=energy_from_contract,
        energy_burned_trx=scale_amount(energy_burned_sun, SUN_DECIMALS),
        bandwidth_used=_as_int(cost.get("net_usage")) + bandwidth_burned_sun // bandwidth_price,
        bandwidth_burned_trx=scale_amount(bandwidth_burned_sun, SUN_DECIMALS),
        fee_total_trx=scale_amount(total_sun, SUN_DECIMALS),
    )


def _native_sun(info: dict[str, Any], contract_type: Any) -> int | None:
    """TRX moved by the transaction in sun. None for contract types this tool does not read."""
    data = info.get("contractData") or {}
    if contract_type == TRX_TRANSFER:
        return _as_int(data.get("amount"))
    if contract_type == SMART_CONTRACT_CALL:
        trigger = info.get("trigger_info") or {}
        return _as_int(trigger.get("call_value", data.get("call_value")))
    return None


def _to_is_contract(info: dict[str, Any], to: Any, contract_type: Any) -> bool | None:
    if contract_type == SMART_CONTRACT_CALL:
        return True
    known = info.get("contract_map")
    if isinstance(known, dict) and to in known:
        return bool(known[to])
    return None


def _confirmations(info: dict[str, Any]) -> int | None:
    value = info.get("confirmations")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _timestamp(info: dict[str, Any]) -> datetime | None:
    millis = info.get("timestamp")
    if not isinstance(millis, int):
        return None
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=millis)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
