from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tx_checker.models import (
    EvmFee,
    FailureReason,
    Network,
    ReportWarning,
    Status,
    TokenTransfer,
    TronFee,
    TxReport,
    WarningCode,
)

HASH = "0x" + "ab12cd34" * 8


def make_report(**overrides: object) -> TxReport:
    values: dict[str, object] = {"hash": HASH, "status": Status.SUCCESS, "network": Network.EVM}
    values.update(overrides)
    return TxReport(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "confirmations", "required", "expected"),
    [
        (Status.SUCCESS, 12, 12, True),
        (Status.SUCCESS, 30, 12, True),
        (Status.SUCCESS, 11, 12, False),
        (Status.SUCCESS, None, 12, False),
        (Status.SUCCESS, 12, None, False),
        (Status.FAILED, 50, 12, False),
        (Status.PENDING, 0, 12, False),
    ],
)
def test_is_final(
    status: Status, confirmations: int | None, required: int | None, expected: bool
) -> None:
    report = make_report(
        status=status, confirmations=confirmations, required_confirmations=required
    )
    assert report.is_final is expected


def test_is_final_follows_threshold_override() -> None:
    report = make_report(confirmations=5, required_confirmations=12)
    assert not report.is_final
    report.required_confirmations = 5
    assert report.is_final


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_report(timestamp=datetime(2026, 1, 1, 12, 0))


def test_minimal_report_for_error_status() -> None:
    report = TxReport(hash="abc", status=Status.INVALID_INPUT, message="bad input")
    assert report.network is None
    assert report.token_transfers == []
    assert report.warnings == []
    assert not report.is_final


def test_default_lists_are_not_shared() -> None:
    first = make_report()
    second = make_report()
    first.warnings.append(ReportWarning(WarningCode.ZERO_VALUE, "zero"))
    assert second.warnings == []


def test_to_dict_serializes_decimals_as_plain_strings() -> None:
    report = make_report(
        native_amount=Decimal("0.000021"),
        native_symbol="ETH",
        fee=EvmFee(
            gas_used=21000,
            gas_limit=21000,
            effective_gas_price_gwei=Decimal("0.0000001"),
            fee_native=Decimal("0.000000000021"),
        ),
    )
    data = report.to_dict()
    assert data["native_amount"] == "0.000021"
    assert data["fee"]["effective_gas_price_gwei"] == "0.0000001"
    assert data["fee"]["fee_native"] == "0.000000000021"
    assert data["fee"]["gas_used"] == 21000


def test_to_dict_serializes_enums_datetimes_and_nested_objects() -> None:
    report = make_report(
        status=Status.FAILED,
        failure_reason=FailureReason.OUT_OF_GAS,
        timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        confirmations=3,
        required_confirmations=12,
        token_transfers=[
            TokenTransfer(
                token_contract="0xtoken",
                from_address="0xfrom",
                to_address="0xto",
                amount=Decimal("1500000"),
            )
        ],
        warnings=[ReportWarning(WarningCode.UNKNOWN_TOKEN, "unknown token")],
        raw={"result": {"status": "0x0"}},
    )
    data = report.to_dict()
    assert data["network"] == "evm"
    assert data["status"] == "failed"
    assert data["failure_reason"] == "out_of_gas"
    assert data["timestamp"] == "2026-01-02T03:04:05+00:00"
    assert data["is_final"] is False
    assert data["token_transfers"] == [
        {
            "token_contract": "0xtoken",
            "from_address": "0xfrom",
            "to_address": "0xto",
            "amount": "1500000",
            "token_symbol": None,
            "decimals": None,
        }
    ]
    assert data["warnings"] == [{"code": "unknown_token", "text": "unknown token"}]
    assert data["raw"] == {"result": {"status": "0x0"}}


def test_to_dict_tron_fee() -> None:
    report = make_report(
        network=Network.TRON,
        fee=TronFee(
            energy_used=65000,
            energy_from_stake=65000,
            energy_burned_trx=Decimal("0"),
            bandwidth_used=345,
            bandwidth_burned_trx=Decimal("0.345"),
            fee_total_trx=Decimal("0.345"),
        ),
    )
    fee = report.to_dict()["fee"]
    assert fee["energy_burned_trx"] == "0"
    assert fee["fee_total_trx"] == "0.345"
