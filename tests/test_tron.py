import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tx_checker.analyze.tron import analyze_tron
from tx_checker.config import TRON_TOKENS, Token
from tx_checker.models import (
    FailureReason,
    Network,
    Status,
    TronFee,
    TxReport,
    WarningCode,
)
from tx_checker.providers.tronscan import BASE_URL, TronscanClient

FIXTURES = Path(__file__).parent / "fixtures" / "tron"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
OTHER_CONTRACT = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def fetch(name: str) -> dict[str, Any] | None:
    """Replay a recorded fixture through the real client, so parsing is covered too."""
    body = load(name)
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/transaction-info").mock(return_value=httpx.Response(200, json=body))
        client = TronscanClient("TESTKEY", http=httpx.Client(), sleep=lambda _: None)
        return client.fetch(body.get("hash", "ab" * 32))


def report_for(name: str, **kwargs: Any) -> TxReport:
    info = fetch(name)
    assert info is not None
    return analyze_tron(info, **kwargs)


def changed(name: str, **top: Any) -> dict[str, Any]:
    info = copy.deepcopy(load(name))
    info.update(top)
    return info


def with_transfer(fixture: str, **fields: Any) -> dict[str, Any]:
    info = copy.deepcopy(load(fixture))
    info["trc20TransferInfo"][0].update(fields)
    return info


def codes(report: TxReport) -> list[WarningCode]:
    return [w.code for w in report.warnings]


def test_trx_transfer() -> None:
    report = report_for("trx_transfer")
    assert report.status is Status.SUCCESS
    assert report.network is Network.TRON
    assert report.chain_id is None
    assert report.chain_name == "TRON"
    assert report.hash == load("trx_transfer")["hash"]
    assert report.explorer_url == f"https://tronscan.org/#/transaction/{report.hash}"
    assert report.block_number == 88_223_301
    assert report.timestamp == datetime(2026, 11, 22, 10, 40, 42, tzinfo=UTC)
    assert report.confirmations == 86_698
    assert report.required_confirmations == 19
    assert report.is_final
    assert report.from_address == "TGFRRV96uabvdGRS1GDXmfHvRR1zfavo9g"
    assert report.to_address == "TBnyUEJwkLjy2LgMM5QXeMvMCi9DFFFFVS"
    assert report.to_is_contract is False
    assert report.native_amount == Decimal("7")
    assert report.native_symbol == "TRX"
    assert report.token_transfers == []
    assert report.attempted_transfers == []
    assert report.failure_reason is None
    assert report.warnings == []


def test_trx_transfer_paid_with_free_bandwidth() -> None:
    assert report_for("trx_transfer").fee == TronFee(
        energy_used=0,
        energy_from_stake=0,
        energy_from_contract=0,
        energy_burned_trx=Decimal("0"),
        bandwidth_used=268,
        bandwidth_burned_trx=Decimal("0"),
        fee_total_trx=Decimal("0"),
    )


def test_usdt_transfer() -> None:
    report = report_for("usdt_transfer")
    assert report.status is Status.SUCCESS
    assert report.native_amount == Decimal("0")
    assert report.to_address == USDT_CONTRACT
    assert report.to_is_contract is True
    assert len(report.token_transfers) == 1
    transfer = report.token_transfers[0]
    assert transfer.token_symbol == "USDT"
    assert transfer.decimals == 6
    assert transfer.token_contract == USDT_CONTRACT
    assert transfer.from_address == "TWsLQpfLw8BJmzdUh2JdEvQung2qjnRpMA"
    assert transfer.to_address == "THobV9PPMgzxJtrHAUNLJ88CWWLeJF5iNb"
    assert transfer.amount == Decimal("1529.315")
    assert report.is_final


def test_usdt_transfer_fee_math_is_exact() -> None:
    assert report_for("usdt_transfer").fee == TronFee(
        energy_used=64285,
        energy_from_stake=64285,
        energy_from_contract=0,
        energy_burned_trx=Decimal("0"),
        bandwidth_used=345,
        bandwidth_burned_trx=Decimal("0.345"),
        fee_total_trx=Decimal("0.345"),
    )


def test_a_token_transfer_to_the_token_contract_is_not_a_contract_warning() -> None:
    report = report_for("usdt_transfer")
    assert report.to_is_contract is True
    assert WarningCode.TO_IS_CONTRACT not in codes(report)


def test_failed_out_of_energy_still_burns_trx() -> None:
    report = report_for("failed_out_of_energy")
    assert report.status is Status.FAILED
    assert report.failure_reason is FailureReason.OUT_OF_ENERGY
    assert report.fee == TronFee(
        energy_used=23040,
        energy_from_stake=17880,
        energy_from_contract=0,
        energy_burned_trx=Decimal("0.516"),
        bandwidth_used=378,
        bandwidth_burned_trx=Decimal("0.378"),
        fee_total_trx=Decimal("0.894"),
    )
    assert not report.is_final


def test_failed_transaction_keeps_the_attempt_apart_from_real_transfers() -> None:
    report = report_for("failed_out_of_energy")
    assert report.token_transfers == []
    assert len(report.attempted_transfers) == 1
    attempt = report.attempted_transfers[0]
    assert attempt.token_symbol == "USDT"
    assert attempt.amount == Decimal("22")
    assert attempt.from_address == "TQ2A9KV2oMpW8TbFTgnzNNPKJt7jLAFobD"
    assert attempt.to_address == "TJVKZXswjk77V3DTzTv1uvSYCyz3cVR5iK"


def test_failed_reverted_ignores_the_misleading_revert_flag() -> None:
    info = load("failed_reverted")
    assert info["revert"] is False
    report = analyze_tron(info)
    assert report.status is Status.FAILED
    assert report.failure_reason is FailureReason.REVERTED
    assert report.fee is not None
    assert report.fee.fee_total_trx == Decimal("0.345")
    assert report.token_transfers == []
    assert report.attempted_transfers[0].amount == Decimal("1000")


def test_other_contract_results_are_failures_with_reason_other() -> None:
    report = analyze_tron(changed("failed_reverted", contractRet="BAD_JUMP_DESTINATION"))
    assert report.status is Status.FAILED
    assert report.failure_reason is FailureReason.OTHER


def test_in_a_block_but_not_confirmed_is_success_with_low_confirmations() -> None:
    report = report_for("unconfirmed_trx")
    assert report.status is Status.SUCCESS
    assert report.confirmations == 2
    assert not report.is_final
    assert codes(report) == [WarningCode.LOW_CONFIRMATIONS]


def test_required_confirmations_override() -> None:
    report = report_for("unconfirmed_trx", required_confirmations=2)
    assert report.required_confirmations == 2
    assert report.is_final
    assert report.warnings == []


def test_not_found() -> None:
    assert fetch("not_found") is None


def test_no_block_means_pending() -> None:
    info = changed("unconfirmed_trx", block=None, confirmations=0)
    report = analyze_tron(info)
    assert report.status is Status.PENDING
    assert report.confirmations == 0
    assert report.block_number is None
    assert report.fee is None
    assert report.explorer_url


def test_missing_result_becomes_a_provider_error_report() -> None:
    info = changed("trx_transfer")
    del info["contractRet"]
    report = analyze_tron(info)
    assert report.status is Status.PROVIDER_ERROR
    assert report.message


@pytest.mark.parametrize("name", ["trx_transfer", "usdt_transfer", "failed_out_of_energy"])
def test_energy_parts_add_up_to_the_total(name: str) -> None:
    fee = report_for(name).fee
    assert isinstance(fee, TronFee)
    burned_units = int(fee.energy_burned_trx * 10_000)  # 1 unit costs 100 sun
    assert fee.energy_from_stake + fee.energy_from_contract + burned_units == fee.energy_used


def test_energy_paid_by_the_contract_owner_is_counted_separately() -> None:
    # cost block of a real meta-transaction: the caller paid no energy at all
    cost = {
        "net_fee_cost": 1000,
        "fee": 0,
        "energy_fee_cost": 100,
        "net_usage": 1979,
        "multi_sign_fee": 0,
        "net_fee": 0,
        "energy_penalty_total": 364045,
        "energy_usage": 0,
        "energy_fee": 0,
        "energy_usage_total": 710025,
        "memoFee": 0,
        "origin_energy_usage": 710025,
    }
    fee = analyze_tron(changed("usdt_transfer", cost=cost)).fee
    assert isinstance(fee, TronFee)
    assert fee.energy_used == 710025
    assert fee.energy_from_stake == 0
    assert fee.energy_from_contract == 710025
    assert fee.fee_total_trx == Decimal("0")
    assert fee.bandwidth_used == 1979


def test_energy_total_is_rebuilt_when_the_response_has_none() -> None:
    cost = {"energy_usage": 1000, "origin_energy_usage": 500, "energy_fee": 20000}
    report = analyze_tron(changed("usdt_transfer", cost=cost))
    assert isinstance(report.fee, TronFee)
    assert report.fee.energy_used == 1000 + 500 + 200


def test_total_fee_adds_every_burned_part_once() -> None:
    cost = {"net_fee": 345000, "energy_fee": 516000, "account_create_fee": 1_000_000, "fee": 0}
    fee = analyze_tron(changed("usdt_transfer", cost=cost)).fee
    assert isinstance(fee, TronFee)
    assert fee.fee_total_trx == Decimal("1.861")


def test_total_fee_never_falls_below_the_provider_total() -> None:
    cost = {"net_fee": 345000, "energy_fee": 0, "fee": 1_345_000}
    fee = analyze_tron(changed("usdt_transfer", cost=cost)).fee
    assert isinstance(fee, TronFee)
    assert fee.fee_total_trx == Decimal("1.345")


def test_trx_sent_to_a_contract_warns() -> None:
    contract_map = {
        "TGFRRV96uabvdGRS1GDXmfHvRR1zfavo9g": False,
        "TBnyUEJwkLjy2LgMM5QXeMvMCi9DFFFFVS": True,
    }
    report = analyze_tron(changed("trx_transfer", contract_map=contract_map))
    assert report.to_is_contract is True
    assert codes(report) == [WarningCode.TO_IS_CONTRACT]


def test_payable_contract_call_with_trx_does_not_warn() -> None:
    info = copy.deepcopy(load("usdt_transfer"))
    info["trigger_info"]["call_value"] = 5_000_000
    report = analyze_tron(info)
    assert report.native_amount == Decimal("5")
    assert WarningCode.TO_IS_CONTRACT not in codes(report)


def test_zero_value_warning_only_for_plain_zero_transfers() -> None:
    zero = copy.deepcopy(load("trx_transfer"))
    zero["contractData"]["amount"] = 0
    assert codes(analyze_tron(zero)) == [WarningCode.ZERO_VALUE]

    zero["contract_map"]["TBnyUEJwkLjy2LgMM5QXeMvMCi9DFFFFVS"] = True
    assert analyze_tron(zero).warnings == []


def test_known_token_ignores_symbol_and_decimals_from_the_response() -> None:
    info = with_transfer("usdt_transfer", symbol="FAKE", decimals=18, name="Not Tether")
    report = analyze_tron(info)
    transfer = report.token_transfers[0]
    assert (transfer.token_symbol, transfer.decimals) == ("USDT", 6)
    assert transfer.amount == Decimal("1529.315")
    assert report.warnings == []


def test_unknown_token_keeps_the_raw_amount_and_warns() -> None:
    info = with_transfer(
        "usdt_transfer", contract_address=OTHER_CONTRACT, symbol="FOO", amount_str="1500000"
    )
    report = analyze_tron(info)
    transfer = report.token_transfers[0]
    assert transfer.token_symbol is None
    assert transfer.decimals is None
    assert transfer.amount == Decimal(1_500_000)
    assert transfer.token_contract == OTHER_CONTRACT
    assert codes(report) == [WarningCode.UNKNOWN_TOKEN]
    assert OTHER_CONTRACT in report.warnings[0].text
    assert "FOO" not in report.warnings[0].text


@pytest.mark.parametrize(
    ("claimed", "official"),
    [
        ("USDT", "USDT"),
        ("usdt", "USDT"),
        (" USDT. ", "USDT"),
        ("USDТ", "USDT"),  # Cyrillic Te
        ("ＵＳＤＴ", "USDT"),  # fullwidth letters
        ("USDC", "USDC"),
        ("Usdc", "USDC"),
    ],
)
def test_lookalike_token_is_flagged(claimed: str, official: str) -> None:
    info = with_transfer("usdt_transfer", contract_address=OTHER_CONTRACT, symbol=claimed)
    report = analyze_tron(info)
    assert codes(report) == [WarningCode.UNKNOWN_TOKEN, WarningCode.LOOKALIKE_TOKEN]
    text = report.warnings[1].text
    assert OTHER_CONTRACT in text
    assert f"uses the name {official} but is not the official {official}" in text


@pytest.mark.parametrize("claimed", ["TUSD", "USDT0", "", None, 7])
def test_other_symbols_are_not_lookalikes(claimed: object) -> None:
    info = with_transfer("usdt_transfer", contract_address=OTHER_CONTRACT, symbol=claimed)
    assert codes(analyze_tron(info)) == [WarningCode.UNKNOWN_TOKEN]


def test_lookalike_is_also_flagged_on_a_failed_attempt() -> None:
    info = copy.deepcopy(load("failed_out_of_energy"))
    info["trc20TransferInfo"][0].update(contract_address=OTHER_CONTRACT, symbol="USDT")
    report = analyze_tron(info)
    assert report.token_transfers == []
    assert WarningCode.LOOKALIKE_TOKEN in codes(report)


def test_legacy_usdc_is_shown_as_legacy_with_an_info_warning() -> None:
    legacy = "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"
    assert TRON_TOKENS[legacy] == Token(
        "USDC (legacy)", 6, legacy=True, explorer_symbol="USDCOLD", asset="USDC"
    )
    for claimed in ("USDC", "USDCOLD"):
        info = with_transfer(
            "usdt_transfer", contract_address=legacy, symbol=claimed, amount_str="13607896"
        )
        report = analyze_tron(info)
        transfer = report.token_transfers[0]
        assert (transfer.token_symbol, transfer.decimals) == ("USDC (legacy)", 6)
        assert transfer.amount == Decimal("13.607896")
        assert codes(report) == [WarningCode.LEGACY_TOKEN]
        text = report.warnings[0].text
        assert legacy in text
        assert "older version of USDC" in text
        assert "USDCOLD" in text
        assert "discontinued" not in text.lower()


def test_legacy_token_is_noted_once_even_with_several_transfers() -> None:
    legacy = "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"
    info = with_transfer("usdt_transfer", contract_address=legacy)
    info["trc20TransferInfo"] = [info["trc20TransferInfo"][0]] * 2
    assert codes(analyze_tron(info)) == [WarningCode.LEGACY_TOKEN]


def test_legacy_token_in_a_failed_attempt_is_noted_too() -> None:
    legacy = "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"
    info = with_transfer("failed_out_of_energy", contract_address=legacy)
    report = analyze_tron(info)
    assert report.token_transfers == []
    assert report.attempted_transfers[0].token_symbol == "USDC (legacy)"
    assert codes(report) == [WarningCode.LEGACY_TOKEN]


def test_a_pegged_token_calling_itself_usdc_is_a_lookalike() -> None:
    # a real "Tron-Peg USD Coin" contract that uses the symbol USDC but is not the registered one
    pegged = "TLZSucJRjnqBKwvQz6n5hd29gbS4P7u7w8"
    info = with_transfer("usdt_transfer", contract_address=pegged, symbol="USDC")
    assert WarningCode.LOOKALIKE_TOKEN in codes(analyze_tron(info))


def test_the_registered_contract_is_never_a_lookalike() -> None:
    assert TRON_TOKENS[USDT_CONTRACT].symbol == "USDT"
    assert WarningCode.LOOKALIKE_TOKEN not in codes(report_for("usdt_transfer"))


def test_same_unknown_token_is_warned_about_once() -> None:
    info = copy.deepcopy(load("usdt_transfer"))
    unknown = {**info["trc20TransferInfo"][0], "contract_address": OTHER_CONTRACT, "symbol": "FOO"}
    info["trc20TransferInfo"] = [unknown, dict(unknown)]
    report = analyze_tron(info)
    assert len(report.token_transfers) == 2
    assert codes(report) == [WarningCode.UNKNOWN_TOKEN]


def test_non_transfer_and_broken_entries_are_skipped() -> None:
    info = copy.deepcopy(load("usdt_transfer"))
    good = info["trc20TransferInfo"][0]
    info["trc20TransferInfo"] = [
        {**good, "type": "Approval"},
        {**good, "amount_str": "not a number"},
        {k: v for k, v in good.items() if k != "contract_address"},
        "junk",
        good,
    ]
    assert len(analyze_tron(info).token_transfers) == 1


def test_raw_data_is_attached_and_has_no_key() -> None:
    report = report_for("trx_transfer")
    assert report.raw is not None
    assert "TESTKEY" not in json.dumps(report.raw)
    assert report.raw["hash"] == report.hash


def test_to_dict_of_a_real_report_is_json_safe() -> None:
    data = report_for("failed_out_of_energy").to_dict()
    json.dumps(data)
    assert data["attempted_transfers"][0]["amount"] == "22"
    assert data["fee"]["fee_total_trx"] == "0.894"
    assert data["fee"]["energy_from_contract"] == 0


def test_fixtures_contain_no_key_material() -> None:
    for path in FIXTURES.glob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        assert "apikey" not in text and "api_key" not in text, path.name
        assert "tron-pro" not in text, path.name
