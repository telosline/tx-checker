import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tx_checker.analyze.evm import TRANSFER_TOPIC, analyze_evm, scale_amount
from tx_checker.config import get_chain
from tx_checker.models import (
    EvmFee,
    FailureReason,
    Network,
    Status,
    TxReport,
    WarningCode,
)
from tx_checker.providers import UnsupportedChainError
from tx_checker.providers.etherscan import BASE_URL, EtherscanClient, EvmRawData

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
USDT_CONTRACT = "0xdac17f958d2ee523a2206206994597c13d831ec7"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def fetch(name: str) -> EvmRawData | None:
    """Replay a recorded fixture through the real client, so parsing is covered too."""
    data = load(name)
    responses = data["responses"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses[request.url.params["action"]])

    with respx.mock(assert_all_called=False) as router:
        router.get(BASE_URL).mock(side_effect=handler)
        client = EtherscanClient("TESTKEY", http=httpx.Client(), sleep=lambda _: None)
        return client.fetch(data["_meta"]["tx_hash"], data["_meta"]["chain_id"])


def report_for(name: str, **kwargs: Any) -> TxReport:
    raw = fetch(name)
    assert raw is not None
    return analyze_evm(raw, get_chain(load(name)["_meta"]["chain_id"]), **kwargs)


def test_eth_transfer() -> None:
    report = report_for("eth_transfer")
    assert report.status is Status.SUCCESS
    assert report.network is Network.EVM
    assert report.chain_id == 1
    assert report.chain_name == "Ethereum"
    assert report.hash == load("eth_transfer")["_meta"]["tx_hash"]
    assert report.explorer_url == f"https://etherscan.io/tx/{report.hash}"
    assert report.block_number == 18_000_000
    assert report.timestamp == datetime(2023, 8, 26, 16, 21, 35, tzinfo=UTC)
    assert report.confirmations == 8_488_005
    assert report.required_confirmations == 12
    assert report.is_final
    assert report.from_address == "0x93aa6246c8a067907ff6ebdcd5e96ea2cfb625fe"
    assert report.to_address == "0x59302f15c6c604d3f43d2902337444388119a1d1"
    assert report.to_is_contract is False
    assert report.native_amount == Decimal("0.08")
    assert report.native_symbol == "ETH"
    assert report.token_transfers == []
    assert report.failure_reason is None
    assert report.warnings == []


def test_eth_transfer_fee_math_is_exact() -> None:
    fee = report_for("eth_transfer").fee
    assert fee == EvmFee(
        gas_used=21000,
        gas_limit=21000,
        effective_gas_price_gwei=Decimal("555"),
        fee_native=Decimal("0.011655"),
    )


def test_usdt_transfer() -> None:
    report = report_for("usdt_transfer")
    assert report.status is Status.SUCCESS
    assert report.native_amount == Decimal("0")
    assert report.to_is_contract is True
    assert len(report.token_transfers) == 1
    transfer = report.token_transfers[0]
    assert transfer.token_symbol == "USDT"
    assert transfer.decimals == 6
    assert transfer.token_contract == USDT_CONTRACT
    assert transfer.from_address == "0x28c6c06298d514db089934071355e5743bf21d60"
    assert transfer.to_address == "0x7e7bdf8b9bc6bdeeb936e694b3e721424c364e90"
    assert transfer.amount == Decimal("337.095759")
    assert report.warnings == []


@pytest.mark.parametrize(
    ("contract", "symbol"),
    [
        ("0x55d398326f99059ff775485246999027b3197955", "USDT"),
        ("0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "USDC"),
    ],
)
def test_bnb_chain_tokens_scale_by_18_decimals(contract: str, symbol: str) -> None:
    raw = fetch("usdt_transfer")
    assert raw is not None and raw.receipt is not None
    log = {
        "address": contract,
        "topics": [
            TRANSFER_TOPIC,
            "0x" + "0" * 24 + "11" * 20,
            "0x" + "0" * 24 + "22" * 20,
        ],
        # 1.5 tokens with 18 decimals. With 6 decimals this would read as 1500000000000.
        "data": hex(1_500_000_000_000_000_000),
    }
    bsc_raw = replace(raw, receipt={**raw.receipt, "logs": [log]})
    report = analyze_evm(bsc_raw, get_chain(56))
    assert len(report.token_transfers) == 1
    transfer = report.token_transfers[0]
    assert transfer.token_symbol == symbol
    assert transfer.decimals == 18
    assert transfer.amount == Decimal("1.5")
    assert all(w.code is not WarningCode.UNKNOWN_TOKEN for w in report.warnings)


def test_usdt_transfer_fee_math_is_exact() -> None:
    fee = report_for("usdt_transfer").fee
    assert fee == EvmFee(
        gas_used=46109,
        gas_limit=220436,
        effective_gas_price_gwei=Decimal("23.721091641"),
        fee_native=Decimal("0.001093755814474869"),
    )


def test_failed_out_of_gas() -> None:
    report = report_for("failed_out_of_gas")
    assert report.status is Status.FAILED
    assert report.failure_reason is FailureReason.OUT_OF_GAS
    assert report.fee is not None
    assert report.fee.gas_used == report.fee.gas_limit == 80000
    assert report.fee.fee_native == Decimal("0.00172521949728")
    assert report.token_transfers == []
    assert not report.is_final


def test_failed_reverted() -> None:
    report = report_for("failed_reverted")
    assert report.status is Status.FAILED
    assert report.failure_reason is FailureReason.REVERTED
    assert report.fee is not None
    assert report.fee.gas_used == 26439
    assert report.fee.gas_limit == 31160
    assert report.fee.fee_native == Decimal("0.000554374212267813")
    assert not report.is_final


def test_pending() -> None:
    report = report_for("pending")
    assert report.status is Status.PENDING
    assert report.confirmations == 0
    assert report.block_number is None
    assert report.timestamp is None
    assert report.fee is None
    assert report.failure_reason is None
    assert report.native_amount == Decimal("0.08")
    assert not report.is_final
    assert report.explorer_url


def test_not_found() -> None:
    assert fetch("not_found") is None


def test_unsupported_chain_is_a_provider_error() -> None:
    with pytest.raises(UnsupportedChainError, match="not supported for this chain"):
        fetch("provider_error_unsupported_chain")


def test_raw_data_is_attached_and_has_no_key() -> None:
    report = report_for("eth_transfer")
    assert report.raw is not None
    assert "TESTKEY" not in json.dumps(report.raw)
    assert report.raw["transaction"]["hash"] == report.hash


def test_low_confirmations_warning() -> None:
    raw = fetch("eth_transfer")
    assert raw is not None
    close = replace(raw, latest_block=hex(18_000_000 + 4))
    report = analyze_evm(close, get_chain(1))
    assert report.confirmations == 5
    assert not report.is_final
    assert [w.code for w in report.warnings] == [WarningCode.LOW_CONFIRMATIONS]


def test_required_confirmations_override() -> None:
    raw = fetch("eth_transfer")
    assert raw is not None
    close = replace(raw, latest_block=hex(18_000_000 + 4))
    report = analyze_evm(close, get_chain(1), required_confirmations=5)
    assert report.required_confirmations == 5
    assert report.is_final
    assert report.warnings == []


def test_node_a_block_behind_still_counts_one_confirmation() -> None:
    raw = fetch("eth_transfer")
    assert raw is not None
    behind = replace(raw, latest_block=hex(17_999_990))
    assert analyze_evm(behind, get_chain(1)).confirmations == 1


def test_zero_value_warning_only_for_plain_zero_transfers() -> None:
    raw = fetch("eth_transfer")
    assert raw is not None
    zero_tx = {**raw.transaction, "value": "0x0"}
    report = analyze_evm(replace(raw, transaction=zero_tx), get_chain(1))
    assert [w.code for w in report.warnings] == [WarningCode.ZERO_VALUE]

    to_contract = replace(raw, transaction=zero_tx, to_code="0x6060")
    assert analyze_evm(to_contract, get_chain(1)).warnings == []


def test_native_coins_sent_to_a_contract_warns() -> None:
    raw = fetch("eth_transfer")
    assert raw is not None
    report = analyze_evm(replace(raw, to_code="0x6060"), get_chain(1))
    assert report.to_is_contract is True
    assert [w.code for w in report.warnings] == [WarningCode.TO_IS_CONTRACT]


def test_payable_call_to_a_contract_does_not_warn() -> None:
    raw = fetch("eth_transfer")
    assert raw is not None
    call = {**raw.transaction, "input": "0xd0e30db0"}
    report = analyze_evm(replace(raw, transaction=call, to_code="0x6060"), get_chain(1))
    assert report.to_is_contract is True
    assert report.native_amount == Decimal("0.08")
    assert report.warnings == []


def test_token_transfer_to_the_token_contract_does_not_warn() -> None:
    report = report_for("usdt_transfer")
    assert report.to_is_contract is True
    assert WarningCode.TO_IS_CONTRACT not in [w.code for w in report.warnings]


def test_contract_creation_has_no_recipient() -> None:
    raw = fetch("eth_transfer")
    assert raw is not None
    creation = replace(raw, transaction={**raw.transaction, "to": None}, to_code=None)
    report = analyze_evm(creation, get_chain(1))
    assert report.to_address is None
    assert report.to_is_contract is None


def log_for(contract: str, topics: list[str], data: str) -> dict[str, Any]:
    return {"address": contract, "topics": topics, "data": data}


def pad(address: str) -> str:
    return "0x" + address[2:].rjust(64, "0")


def with_logs(logs: list[dict[str, Any]]) -> EvmRawData:
    raw = fetch("usdt_transfer")
    assert raw is not None
    assert raw.receipt is not None
    return replace(raw, receipt={**raw.receipt, "logs": logs})


def test_unknown_token_keeps_the_raw_amount_and_warns() -> None:
    unknown = "0x" + "11" * 20
    sender, receiver = "0x" + "aa" * 20, "0x" + "bb" * 20
    logs = [log_for(unknown, [TRANSFER_TOPIC, pad(sender), pad(receiver)], hex(1_500_000))]
    report = analyze_evm(with_logs(logs), get_chain(1))
    transfer = report.token_transfers[0]
    assert transfer.token_symbol is None
    assert transfer.decimals is None
    assert transfer.amount == Decimal(1_500_000)
    assert transfer.from_address == sender
    assert transfer.to_address == receiver
    assert [w.code for w in report.warnings] == [WarningCode.UNKNOWN_TOKEN]
    assert unknown in report.warnings[0].text


def test_nft_transfers_are_ignored() -> None:
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    topics = [TRANSFER_TOPIC, pad("0x" + "aa" * 20), pad("0x" + "bb" * 20), hex(7)]
    report = analyze_evm(with_logs([log_for(usdt, topics, "0x")]), get_chain(1))
    assert report.token_transfers == []


def test_several_transfers_in_one_transaction() -> None:
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    one, two = pad("0x" + "aa" * 20), pad("0x" + "bb" * 20)
    logs = [
        log_for(usdt, [TRANSFER_TOPIC, one, two], hex(2_000_000)),
        log_for(usdc, [TRANSFER_TOPIC, two, one], hex(250_000)),
        log_for(usdc, ["0x" + "12" * 32], "0x"),
    ]
    report = analyze_evm(with_logs(logs), get_chain(1))
    assert [(t.token_symbol, t.amount) for t in report.token_transfers] == [
        ("USDT", Decimal("2")),
        ("USDC", Decimal("0.25")),
    ]


def test_receipt_without_status_becomes_a_provider_error_report() -> None:
    raw = fetch("eth_transfer")
    assert raw is not None
    assert raw.receipt is not None
    broken = replace(raw, receipt={k: v for k, v in raw.receipt.items() if k != "status"})
    report = analyze_evm(broken, get_chain(1))
    assert report.status is Status.PROVIDER_ERROR
    assert report.message


def test_same_raw_amount_gives_different_results_for_6_and_18_decimals() -> None:
    raw_amount = 1_500_000
    six = scale_amount(raw_amount, 6)
    eighteen = scale_amount(raw_amount, 18)
    assert six == Decimal("1.5")
    assert eighteen == Decimal("0.0000000000015")
    assert six != eighteen


@pytest.mark.parametrize(
    ("raw_amount", "decimals", "expected"),
    [
        (0, 6, "0"),
        (1, 18, "0.000000000000000001"),
        (100, 0, "100"),
        (1_000_000, 6, "1"),
        (10**30, 6, "1000000000000000000000000"),
        (2**256 - 1, 0, str(2**256 - 1)),
    ],
)
def test_scale_amount_is_exact(raw_amount: int, decimals: int, expected: str) -> None:
    assert format(scale_amount(raw_amount, decimals), "f") == expected


def test_to_dict_of_a_real_report_is_json_safe() -> None:
    data = report_for("usdt_transfer").to_dict()
    json.dumps(data)
    assert data["token_transfers"][0]["amount"] == "337.095759"
    assert data["fee"]["fee_native"] == "0.001093755814474869"


def test_fixtures_contain_no_key_material() -> None:
    for path in FIXTURES.glob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        assert "apikey" not in text and "api_key" not in text, path.name


def test_fixture_meta_marks_derived_files() -> None:
    assert load("pending")["_meta"]["derived"] is True
    assert load("eth_transfer")["_meta"]["derived"] is False
