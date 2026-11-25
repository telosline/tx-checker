import copy
import json
import re
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tx_checker.analyze.evm import analyze_evm
from tx_checker.analyze.tron import analyze_tron
from tx_checker.config import get_chain
from tx_checker.detect import MSG_ADDRESS
from tx_checker.explain.explain import (
    CLIENT_WARNING_PRIORITY,
    LANGS,
    Scenario,
    explain,
    pick_scenario,
)
from tx_checker.explain.templates import TEMPLATES
from tx_checker.models import ReportWarning, Status, TronFee, TxReport, WarningCode
from tx_checker.providers.etherscan import EvmRawData

FIXTURES = Path(__file__).parent / "fixtures"
LEGACY_USDC = "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"
FAKE_USDT = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"
TRON_LINK = "https://tronscan.org/#/transaction/" + "ab" * 32
ETH_LINK = "https://etherscan.io/tx/0x" + "ab" * 32

# Words a client text must never contain. Substrings, compared in lower case.
FORBIDDEN = {
    "en": [
        "credited",
        "refund",
        "guarantee",
        "will arrive",
        "will be returned",
        "reimburs",
        "money back",
        "within",
        "stuck",
        "your fault",
    ],
    "uk": [
        "зарахован",
        "повернемо",
        "повернення",
        "гарантуємо",
        "гарантія",
        "надійдуть",
        "протягом",
        "ваша вина",
        "застряг",
    ],
    "ru": [
        "зачислен",
        "возврат",
        "вернём",
        "вернем",
        "гарантируем",
        "гарантия",
        "поступят",
        "в течение",
        "ваша вина",
        "застрял",
    ],
}


def tron_info(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / "tron" / f"{name}.json").read_text(encoding="utf-8"))


def tron(name: str, **changes: Any) -> TxReport:
    return analyze_tron({**tron_info(name), **changes})


def evm(name: str) -> TxReport:
    responses = json.loads((FIXTURES / "evm" / f"{name}.json").read_text(encoding="utf-8"))[
        "responses"
    ]

    def result(key: str) -> Any:
        return (responses.get(key) or {}).get("result")

    raw = EvmRawData(
        transaction=result("eth_getTransactionByHash"),
        receipt=result("eth_getTransactionReceipt"),
        latest_block=result("eth_blockNumber"),
        block=result("eth_getBlockByNumber"),
        to_code=result("eth_getCode"),
    )
    return analyze_evm(raw, get_chain(1))


def with_token(contract: str, symbol: str, amount: str = "1500000") -> TxReport:
    info = copy.deepcopy(tron_info("usdt_transfer"))
    info["trc20TransferInfo"][0].update(contract_address=contract, symbol=symbol, amount_str=amount)
    return analyze_tron(info)


def bare(status: Status, **fields: Any) -> TxReport:
    return TxReport(hash="ab" * 32, status=status, **fields)


CASES: dict[str, tuple[Callable[[], TxReport], Scenario]] = {
    "success_final_tron": (lambda: tron("usdt_transfer"), Scenario.SUCCESS_FINAL),
    "success_final_evm": (lambda: evm("eth_transfer"), Scenario.SUCCESS_FINAL),
    "success_waiting": (lambda: tron("unconfirmed_trx"), Scenario.SUCCESS_WAITING),
    "pending": (lambda: evm("pending"), Scenario.PENDING),
    "failed_out_of_gas": (lambda: evm("failed_out_of_gas"), Scenario.FAILED_OUT_OF_GAS),
    "failed_reverted_evm": (lambda: evm("failed_reverted"), Scenario.FAILED_REVERTED),
    "failed_reverted_tron": (lambda: tron("failed_reverted"), Scenario.FAILED_REVERTED),
    "failed_out_of_energy": (
        lambda: tron("failed_out_of_energy"),
        Scenario.FAILED_OUT_OF_ENERGY,
    ),
    "failed_other": (
        lambda: tron("failed_reverted", contractRet="BAD_JUMP_DESTINATION"),
        Scenario.FAILED_OTHER,
    ),
    "not_found_both": (
        lambda: bare(
            Status.NOT_FOUND,
            chain_name="TRON",
            explorer_url=TRON_LINK,
            networks_checked=["TRON", "Ethereum"],
        ),
        Scenario.NOT_FOUND,
    ),
    "not_found_one": (
        lambda: bare(
            Status.NOT_FOUND,
            chain_name="Ethereum",
            explorer_url=ETH_LINK,
            networks_checked=["Ethereum"],
        ),
        Scenario.NOT_FOUND,
    ),
    "invalid_input": (
        lambda: bare(Status.INVALID_INPUT, message="A transaction hash has 64 hex characters."),
        Scenario.INVALID_INPUT,
    ),
    "invalid_address": (
        lambda: bare(Status.INVALID_INPUT, message=MSG_ADDRESS),
        Scenario.INVALID_ADDRESS,
    ),
    "provider_error": (
        lambda: bare(
            Status.PROVIDER_ERROR,
            chain_name="TRON",
            explorer_url=TRON_LINK,
            message="Tronscan rate limit reached (HTTP 429).",
        ),
        Scenario.PROVIDER_ERROR,
    ),
}
NO_LINK = {Scenario.INVALID_INPUT, Scenario.INVALID_ADDRESS}
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def sentences_of(client: str) -> list[str]:
    body = client.split("\n")[0]
    return [s for s in re.split(r"(?<=[.!?])\s+", body) if s]


def test_every_scenario_has_a_case() -> None:
    assert {scenario for _, scenario in CASES.values()} == set(Scenario)


@pytest.mark.parametrize("case", CASES)
def test_scenario_is_picked_from_the_report(case: str) -> None:
    build, scenario = CASES[case]
    assert pick_scenario(build()) is scenario


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("case", CASES)
def test_client_text_basics(case: str, lang: str) -> None:
    build, scenario = CASES[case]
    report = build()
    client = explain(report, lang).client

    assert "{" not in client and "}" not in client
    assert 2 <= len(sentences_of(client)) <= 5
    lowered = client.lower()
    for word in FORBIDDEN[lang]:
        assert word not in lowered, word
    if scenario in NO_LINK:
        assert "http" not in client
    else:
        assert client.splitlines()[-1].endswith(report.explorer_url or "missing")


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("case", ["success_final_tron", "failed_out_of_energy", "pending"])
def test_client_text_shows_short_addresses_only(case: str, lang: str) -> None:
    report = CASES[case][0]()
    client = explain(report, lang).client
    addresses = [report.to_address, report.from_address]
    addresses += [t.to_address for t in report.token_transfers + report.attempted_transfers]
    for address in addresses:
        assert address is not None
        assert address not in client


def test_final_success_says_fully_confirmed_without_the_count() -> None:
    report = tron("usdt_transfer")
    client = explain(report, "en").client
    assert "fully confirmed" in client
    assert str(report.confirmations) not in client
    assert "19" not in client


@pytest.mark.parametrize(
    ("lang", "expected"),
    [("en", "2 of 19"), ("uk", "2 з 19"), ("ru", "2 из 19")],
)
def test_waiting_success_shows_the_count_and_makes_no_promise(lang: str, expected: str) -> None:
    client = explain(tron("unconfirmed_trx"), lang).client
    assert expected in client


def test_success_text_has_recipient_time_and_the_next_step() -> None:
    client = explain(tron("usdt_transfer"), "en").client
    assert "1529.315 USDT" in client
    assert "THobV9…5iNb" in client
    assert "2026-11-22 10:41 UTC" in client
    assert (
        "Amount: 1529.315 USDT, recipient: THobV9\u20265iNb, time: 2026-11-22 10:41 UTC." in client
    )
    assert "send them this link" in client


def test_failed_text_states_the_attempt_and_the_fee() -> None:
    client = explain(tron("failed_out_of_energy"), "en").client
    assert "22 USDT" in client
    assert "not executed" in client
    assert "0.894 TRX" in client
    assert "TJVKZX…R5iK" in client
    assert "2026-11-22 10:41 UTC" in client


@pytest.mark.parametrize(
    ("case", "phrase"),
    [
        ("failed_out_of_gas", "higher gas limit"),
        ("failed_out_of_energy", "raise the fee limit"),
        ("failed_reverted_tron", "contact the service or the wallet"),
        ("failed_other", "contact the service or the wallet"),
        ("success_final_evm", "send them this link"),
    ],
)
def test_every_scenario_ends_with_a_next_step(case: str, phrase: str) -> None:
    sentences = sentences_of(explain(CASES[case][0](), "en").client)
    assert phrase in sentences[-1]


def test_out_of_gas_says_likely() -> None:
    assert "most likely" in explain(evm("failed_out_of_gas"), "en").client


def test_not_found_names_the_networks_and_a_possible_late_block() -> None:
    both = explain(CASES["not_found_both"][0](), "en").client
    assert "on TRON or on Ethereum." in both
    one = explain(CASES["not_found_one"][0](), "en").client
    assert "on Ethereum." in one
    assert "not been included in a block yet" in one
    assert "in a minute" in one


def test_provider_error_text_never_repeats_the_technical_message() -> None:
    report = CASES["provider_error"][0]()
    for lang in LANGS:
        assert "429" not in explain(report, lang).client
        assert "Tronscan" not in explain(report, lang).client


def test_unknown_token_amount_is_marked_as_raw() -> None:
    client = explain(with_token(FAKE_USDT, "FOO"), "en").client
    assert "1500000 (smallest token units)" in client
    assert "not in our list of known tokens" in client


def test_lookalike_beats_unknown_and_only_one_warning_is_shown() -> None:
    report = with_token(FAKE_USDT, "USDT")
    assert {WarningCode.UNKNOWN_TOKEN, WarningCode.LOOKALIKE_TOKEN} <= {
        w.code for w in report.warnings
    }
    client = explain(report, "en").client
    assert "does not match the official one" in client
    assert "not in our list" not in client


@pytest.mark.parametrize("lang", LANGS)
def test_legacy_usdc_is_called_an_older_version_without_claims_about_the_issuer(
    lang: str,
) -> None:
    report = with_token(LEGACY_USDC, "USDC", "13607896")
    client = explain(report, lang).client
    assert "1529.315" not in client
    assert "13.607896 USDC (legacy)" in client
    lowered = client.lower()
    for word in ("discontinu", "circle", "припин", "снят", "USDCOLD".lower()):
        assert word not in lowered
    expected = {
        "en": "older version of USDC",
        "uk": "старіша версія USDC",
        "ru": "более старая версия USDC",
    }[lang]
    assert expected in client


def test_to_is_contract_text_is_neutral() -> None:
    info = copy.deepcopy(tron_info("trx_transfer"))
    info["contract_map"]["TBnyUEJwkLjy2LgMM5QXeMvMCi9DFFFFVS"] = True
    report = analyze_tron(info)
    assert [w.code for w in report.warnings] == [WarningCode.TO_IS_CONTRACT]
    client = explain(report, "en").client
    assert "often normal" in client
    for word in ("lost", "stuck", "cannot be recovered", "burn"):
        assert word not in client.lower()


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("code", CLIENT_WARNING_PRIORITY)
def test_every_client_warning_renders(code: WarningCode, lang: str) -> None:
    report = tron("usdt_transfer")
    report.warnings = [ReportWarning(code, "internal text")]
    client = explain(report, lang).client
    assert "{" not in client and "}" not in client
    assert TEMPLATES[lang]["warnings"][code.value].split("{")[0][:20] in client
    assert len(sentences_of(client)) <= 5


@pytest.mark.parametrize("code", [WarningCode.LOW_CONFIRMATIONS, WarningCode.STALE_PENDING])
def test_internal_only_warnings_add_nothing_to_the_client_text(code: WarningCode) -> None:
    plain = explain(tron("usdt_transfer"), "en").client
    report = tron("usdt_transfer")
    report.warnings = [ReportWarning(code, "internal text")]
    assert explain(report, "en").client == plain


def test_unsupported_language_is_an_error() -> None:
    with pytest.raises(ValueError, match="en, uk, ru"):
        explain(tron("usdt_transfer"), "de")


def test_languages_have_the_same_scenarios_snippets_and_warnings() -> None:
    reference = TEMPLATES["en"]
    for lang in LANGS:
        texts = TEMPLATES[lang]
        assert set(texts["scenarios"]) == {s.value for s in Scenario}
        assert set(texts["snippets"]) == set(reference["snippets"]), lang
        assert set(texts["warnings"]) == set(reference["warnings"]), lang
        assert {code.value for code in CLIENT_WARNING_PRIORITY} == set(texts["warnings"])


def test_languages_use_the_same_placeholders() -> None:
    def fields(text: str) -> set[str]:
        return set(PLACEHOLDER.findall(text))

    reference = TEMPLATES["en"]
    for lang in LANGS:
        texts = TEMPLATES[lang]
        for name, parts in reference["scenarios"].items():
            assert fields(" ".join(parts)) == fields(" ".join(texts["scenarios"][name])), (
                lang,
                name,
            )
        for section in ("snippets", "warnings"):
            for name, text in reference[section].items():
                assert fields(text) == fields(texts[section][name]), (lang, section, name)


def test_polite_form_is_used_in_uk_and_ru() -> None:
    forbidden = {"uk": [" твоя ", " ти ", " тебе "], "ru": [" твоя ", " ты ", " тебе "]}
    for lang, words in forbidden.items():
        for case in CASES:
            client = " " + explain(CASES[case][0](), lang).client.lower() + " "
            for word in words:
                assert word not in client


GOLDEN_USDT = {
    "en": "The transaction on TRON was executed successfully and is fully confirmed. Amount: "
    "1529.315 USDT, recipient: THobV9\u20265iNb, time: 2026-11-22 10:41 UTC. If the recipient "
    "does not see the funds, send them this link.",
    "uk": "Транзакцію в мережі TRON успішно виконано та повністю підтверджено. Сума: 1529.315 "
    "USDT, отримувач: THobV9\u20265iNb, час: 2026-11-22 10:41 UTC. Якщо отримувач не бачить "
    "коштів, надішліть йому це посилання.",
    "ru": "Транзакция в сети TRON успешно выполнена и полностью подтверждена. Сумма: 1529.315 "
    "USDT, получатель: THobV9\u20265iNb, время: 2026-11-22 10:41 UTC. Если получатель не видит "
    "средств, отправьте ему эту ссылку.",
}


@pytest.mark.parametrize("lang", LANGS)
def test_golden_client_text(lang: str) -> None:
    report = tron("usdt_transfer")
    expected = (
        GOLDEN_USDT[lang]
        + "\n"
        + TEMPLATES[lang]["snippets"]["link_line"].format(link=report.explorer_url)
    )
    assert explain(report, lang).client == expected


def lines_of(report: TxReport) -> list[str]:
    return explain(report, "en").internal.splitlines()


@pytest.mark.parametrize("case", CASES)
def test_internal_summary_is_short(case: str) -> None:
    lines = lines_of(CASES[case][0]())
    assert 2 <= len(lines) <= 6


def test_internal_summary_of_a_success() -> None:
    report = tron("usdt_transfer")
    lines = lines_of(report)
    assert lines[0] == "Network: TRON | Status: SUCCESS | Confirmations: 86685/19 (final)"
    assert (
        "From: TWsLQpfLw8BJmzdUh2JdEvQung2qjnRpMA -> To: TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    ) in lines[1]
    assert "2026-11-22 10:41:24 UTC" in lines[1]
    assert lines[2].startswith("Moved: 1529.315 USDT")
    assert "THobV9PPMgzxJtrHAUNLJ88CWWLeJF5iNb" in lines[2]
    assert lines[3].startswith("Fee: 0.345 TRX burned (energy 64285")
    assert lines[-1] == f"Link: {report.explorer_url}"


def test_internal_summary_of_a_failure_shows_the_attempt() -> None:
    lines = lines_of(tron("failed_out_of_energy"))
    assert lines[0] == "Network: TRON | Status: FAILED (out of energy) | Confirmations: 86690/19"
    attempt = next(line for line in lines if "ttempted" in line)
    assert attempt.startswith("Attempted transfer: 22 USDT")
    assert attempt.endswith("not executed")
    assert not any(line.startswith("Moved") for line in lines)
    assert "5160 burned = 0.516 TRX" in next(line for line in lines if line.startswith("Fee"))


def test_internal_summary_lists_every_warning_with_its_code() -> None:
    notes = next(
        line for line in lines_of(with_token(FAKE_USDT, "USDT")) if line.startswith("Notes")
    )
    assert "UNKNOWN_TOKEN" in notes
    assert "LOOKALIKE_TOKEN" in notes


def test_internal_summary_of_evm_fee_and_pending() -> None:
    fee_line = next(line for line in lines_of(evm("eth_transfer")) if line.startswith("Fee"))
    assert fee_line == "Fee: 0.011655 ETH (gas 21000/21000, 555 gwei)"
    pending = lines_of(evm("pending"))
    assert "Status: PENDING" in pending[0]
    assert any(line.startswith("Amount (not in a block yet): 0.08 ETH") for line in pending)
    assert not any(line.startswith("Moved") for line in pending)


def test_internal_summary_of_errors_keeps_the_technical_message() -> None:
    provider = lines_of(CASES["provider_error"][0]())
    assert "Status: PROVIDER ERROR" in provider[0]
    assert any("rate limit reached (HTTP 429)" in line for line in provider)
    not_found = lines_of(CASES["not_found_both"][0]())
    assert any("Checked: TRON, Ethereum" in line for line in not_found)
    invalid = lines_of(CASES["invalid_input"][0]())
    assert invalid[0].startswith("Status: INVALID INPUT")


def test_internal_summary_is_english_whatever_the_client_language() -> None:
    report = tron("usdt_transfer")
    assert (
        explain(report, "uk").internal
        == explain(report, "en").internal
        == (explain(report, "ru").internal)
    )


def test_out_of_gas_blames_the_gas_limit_not_the_balance() -> None:
    report = evm("failed_out_of_gas")
    assert "gas limit" in explain(report, "en").client
    assert "ліміту gas" in explain(report, "uk").client
    assert "лимита gas" in explain(report, "ru").client
    for lang in LANGS:
        assert "more ETH" not in explain(report, lang).client


def test_out_of_energy_text_has_no_staking_detail_and_points_at_the_fee_limit() -> None:
    report = tron("failed_out_of_energy")
    for lang, words in {
        "en": ("fee limit", "enough TRX"),
        "uk": ("fee limit", "достатньо TRX"),
        "ru": ("fee limit", "достаточно TRX"),
    }.items():
        client = explain(report, lang).client
        for word in words:
            assert word in client
        for staking in ("stak", "стейк", "burn", "спал", "сжиг"):
            assert staking not in client.lower()


def test_client_fee_is_rounded_to_six_decimals_with_an_approximation_sign() -> None:
    report = evm("failed_out_of_gas")
    assert "\u2248 0.001725 ETH" in explain(report, "en").client
    assert "0.00172521949728" not in explain(report, "en").client
    assert "0.00172521949728 ETH" in explain(report, "en").internal


@pytest.mark.parametrize(
    ("fee", "expected"),
    [
        ("0.894", "0.894 TRX"),
        ("0.0000004", "< 0.000001 TRX"),
        ("1.2345678", "\u2248 1.234568 TRX"),
        ("0.1000004", "\u2248 0.1 TRX"),
    ],
)
def test_fee_rounding_cases(fee: str, expected: str) -> None:
    report = tron("failed_out_of_energy")
    assert isinstance(report.fee, TronFee)
    report.fee = replace(report.fee, fee_total_trx=Decimal(fee))
    assert f"A network fee of {expected} was still charged." in explain(report, "en").client


def test_details_use_labels() -> None:
    report = tron("usdt_transfer")
    expected = {
        "en": "Amount: 1529.315 USDT, recipient: THobV9\u20265iNb, time: ",
        "uk": "Сума: 1529.315 USDT, отримувач: THobV9\u20265iNb, час: ",
        "ru": "Сумма: 1529.315 USDT, получатель: THobV9\u20265iNb, время: ",
    }
    for lang, text in expected.items():
        assert text in explain(report, lang).client


def test_not_found_in_two_networks_is_phrased_per_language() -> None:
    report = CASES["not_found_both"][0]()
    assert "ні в мережі TRON, ні в мережі Ethereum" in explain(report, "uk").client
    assert "ни в сети TRON, ни в сети Ethereum" in explain(report, "ru").client
