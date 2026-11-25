from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tx_checker.analyze.amounts import scale_amount
from tx_checker.config import Chain, lookup_token
from tx_checker.models import (
    EvmFee,
    FailureReason,
    Network,
    ReportWarning,
    Status,
    TokenTransfer,
    TxReport,
    WarningCode,
)
from tx_checker.providers.etherscan import EvmRawData

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
NATIVE_DECIMALS = 18


def analyze_evm(
    raw: EvmRawData, chain: Chain, required_confirmations: int | None = None
) -> TxReport:
    """Turn raw Etherscan data into a TxReport. Pure: no network, no clock."""
    tx = raw.transaction
    tx_hash = str(tx["hash"]).lower()
    required = (
        required_confirmations
        if required_confirmations is not None
        else chain.required_confirmations
    )
    value = _to_int(tx.get("value"))
    to = tx.get("to")

    report = TxReport(
        hash=tx_hash,
        status=Status.PENDING,
        network=Network.EVM,
        chain_id=chain.chain_id,
        chain_name=chain.name,
        explorer_url=chain.tx_url(tx_hash),
        from_address=tx.get("from"),
        to_address=to,
        native_amount=scale_amount(value, NATIVE_DECIMALS),
        native_symbol=chain.native_symbol,
        required_confirmations=required,
        confirmations=0,
        raw={"transaction": tx, "receipt": raw.receipt},
    )
    if tx.get("blockNumber") is None:
        return report

    receipt = raw.receipt or {}
    receipt_status = receipt.get("status")
    if receipt_status not in ("0x0", "0x1"):
        report.status = Status.PROVIDER_ERROR
        report.message = "The provider returned a transaction receipt without a clear status."
        report.confirmations = None
        return report

    report.block_number = _to_int(tx["blockNumber"])
    report.confirmations = _confirmations(report.block_number, raw.latest_block)
    report.timestamp = _timestamp(raw.block)
    report.status = Status.SUCCESS if receipt_status == "0x1" else Status.FAILED

    gas_limit = _to_int(tx.get("gas"))
    gas_used = _to_int(receipt.get("gasUsed"))
    price = _to_int(receipt.get("effectiveGasPrice") or tx.get("gasPrice"))
    report.fee = EvmFee(
        gas_used=gas_used,
        gas_limit=gas_limit,
        effective_gas_price_gwei=scale_amount(price, 9),
        fee_native=scale_amount(gas_used * price, NATIVE_DECIMALS),
    )
    if report.status is Status.FAILED:
        # A transaction that used its whole gas limit most likely ran out of gas.
        out_of_gas = gas_used == gas_limit
        report.failure_reason = FailureReason.OUT_OF_GAS if out_of_gas else FailureReason.REVERTED

    if to is not None and raw.to_code is not None:
        report.to_is_contract = raw.to_code not in ("0x", "0x0", "")

    report.token_transfers = _token_transfers(receipt.get("logs") or [], chain.chain_id)
    report.warnings = _warnings(report, value)
    return report


def _warnings(report: TxReport, native_value: int) -> list[ReportWarning]:
    warnings: list[ReportWarning] = []
    if native_value > 0 and report.to_is_contract:
        warnings.append(
            ReportWarning(
                WarningCode.TO_IS_CONTRACT, "Native coins were sent directly to a contract address."
            )
        )
    if (
        report.status is Status.SUCCESS
        and native_value == 0
        and not report.token_transfers
        and report.to_is_contract is False
    ):
        warnings.append(
            ReportWarning(WarningCode.ZERO_VALUE, "The transaction moved no coins and no tokens.")
        )
    for contract in dict.fromkeys(
        t.token_contract for t in report.token_transfers if t.decimals is None
    ):
        warnings.append(
            ReportWarning(
                WarningCode.UNKNOWN_TOKEN,
                f"Token {contract} is not in the registry, so its amount is shown as a raw number.",
            )
        )
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


def _token_transfers(logs: list[dict[str, Any]], chain_id: int) -> list[TokenTransfer]:
    transfers: list[TokenTransfer] = []
    for log in logs:
        topics = log.get("topics") or []
        # ERC-20 has 3 topics. ERC-721 uses the same event with 4 topics, skip those.
        if len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        contract = str(log["address"]).lower()
        amount_raw = _to_int(log.get("data"))
        token = lookup_token(chain_id, contract)
        transfers.append(
            TokenTransfer(
                token_contract=contract,
                from_address="0x" + topics[1][-40:].lower(),
                to_address="0x" + topics[2][-40:].lower(),
                amount=scale_amount(amount_raw, token.decimals) if token else Decimal(amount_raw),
                token_symbol=token.symbol if token else None,
                decimals=token.decimals if token else None,
            )
        )
    return transfers


def _confirmations(block_number: int, latest_block: str | None) -> int | None:
    if latest_block is None:
        return None
    # Etherscan's node can be a block behind, a mined transaction has at least one.
    return max(1, _to_int(latest_block) - block_number + 1)


def _timestamp(block: dict[str, Any] | None) -> datetime | None:
    if not block or block.get("timestamp") is None:
        return None
    return datetime.fromtimestamp(_to_int(block["timestamp"]), tz=UTC)


def _to_int(value: str | None) -> int:
    """Parse a 0x hex quantity. Empty data ("0x") and missing values count as zero."""
    if not value or value == "0x":
        return 0
    return int(value, 16)
