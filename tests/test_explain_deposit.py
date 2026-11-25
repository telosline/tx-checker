import re
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest

from test_deposit_check import (
    ETH_RECIPIENT,
    HASH,
    TRON_RECIPIENT,
    evm_report,
    evm_with,
    expect,
    found_on_polygon,
    transfer,
    tron_report,
    tron_with,
)
from test_explain import FORBIDDEN, sentences_of
from tx_checker.deposit_check import DepositResult, Expected, check_deposit, parse_asset
from tx_checker.explain.explain import (
    LANGS,
    DepositScenario,
    Scenario,
    explain,
    explain_deposit,
    pick_deposit_scenario,
)
from tx_checker.explain.templates import DEPOSIT_INTERNAL, DEPOSIT_TEMPLATES, TEMPLATES
from tx_checker.models import Network, ReportWarning, Status, TxReport, WarningCode

OTHER_TRON = "TKgmbuXFYn58oCrFJFFDgneWc9aRh9eSkz"
FAKE_CONTRACT = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"
TRON_LINK = f"https://tronscan.org/#/transaction/{HASH}"
SIMILAR = TRON_RECIPIENT[:4] + "A" * 26 + TRON_RECIPIENT[-4:]
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def lookalike_report() -> TxReport:
    return tron_with(
        transfer(FAKE_CONTRACT, TRON_RECIPIENT, "30", symbol="USDT"),
        warnings=[ReportWarning(WarningCode.LOOKALIKE_TOKEN, "lookalike")],
        explorer_url=TRON_LINK,
        chain_name="TRON",
    )


def run(report: TxReport, expected: Expected) -> DepositResult:
    return check_deposit(report, expected)


Case = Callable[[], DepositResult]

CASES: dict[str, tuple[Case, DepositScenario | Scenario]] = {
    "match": (
        lambda: run(tron_report("usdt_transfer"), expect("USDT-TRON", TRON_RECIPIENT, "1529.315")),
        DepositScenario.MATCH,
    ),
    "waiting": (
        lambda: run(
            tron_report("unconfirmed_trx"),
            expect("TRX", tron_report("unconfirmed_trx").to_address or "", "0.000006"),
        ),
        DepositScenario.WAITING,
    ),
    "wrong_network": (
        lambda: run(
            tron_report("usdt_transfer"), expect("USDT-ETHEREUM", ETH_RECIPIENT, "1529.315")
        ),
        DepositScenario.WRONG_NETWORK,
    ),
    "wrong_network_evm": (
        lambda: run(
            replace(found_on_polygon(), explorer_url="https://polygonscan.com/tx/0x1"),
            expect("ETH", ETH_RECIPIENT, "1"),
        ),
        DepositScenario.WRONG_NETWORK,
    ),
    "wrong_token": (
        lambda: run(tron_report("usdt_transfer"), expect("USDC-TRON", TRON_RECIPIENT, "1529.315")),
        DepositScenario.WRONG_TOKEN,
    ),
    "wrong_token_lookalike": (
        lambda: run(lookalike_report(), expect("USDT-TRON", TRON_RECIPIENT, "30")),
        DepositScenario.WRONG_TOKEN,
    ),
    "wrong_token_nothing_moved": (
        lambda: run(
            evm_with(
                1,
                native_amount=Decimal(0),
                to_address=ETH_RECIPIENT,
                explorer_url="https://etherscan.io/tx/0x1",
            ),
            expect("USDT-ETHEREUM", ETH_RECIPIENT, "5"),
        ),
        DepositScenario.WRONG_TOKEN,
    ),
    "wrong_address": (
        lambda: run(tron_report("usdt_transfer"), expect("USDT-TRON", OTHER_TRON, "1529.315")),
        DepositScenario.WRONG_ADDRESS,
    ),
    "wrong_address_similar": (
        lambda: run(
            tron_report("usdt_transfer"),
            Expected(parse_asset("USDT-TRON"), SIMILAR, Decimal("1529.315")),
        ),
        DepositScenario.WRONG_ADDRESS,
    ),
    "amount_lower": (
        lambda: run(tron_report("usdt_transfer"), expect("USDT-TRON", TRON_RECIPIENT, "2000")),
        DepositScenario.AMOUNT_LOWER,
    ),
    "amount_higher": (
        lambda: run(tron_report("usdt_transfer"), expect("USDT-TRON", TRON_RECIPIENT, "1000")),
        DepositScenario.AMOUNT_HIGHER,
    ),
    "failed": (
        lambda: run(
            tron_report("failed_out_of_energy"),
            expect("USDT-TRON", "TJVKZXswjk77V3DTzTv1uvSYCyz3cVR5iK", "22"),
        ),
        Scenario.FAILED_OUT_OF_ENERGY,
    ),
    "failed_evm": (
        lambda: run(evm_report("failed_out_of_gas"), expect("ETH", ETH_RECIPIENT, "1")),
        Scenario.FAILED_OUT_OF_GAS,
    ),
    "pending": (
        lambda: run(evm_report("pending"), expect("ETH", ETH_RECIPIENT, "1")),
        Scenario.PENDING,
    ),
    "not_found": (
        lambda: run(
            TxReport(
                hash=HASH,
                status=Status.NOT_FOUND,
                network=Network.TRON,
                chain_name="TRON",
                explorer_url=TRON_LINK,
                networks_checked=["TRON", "Ethereum"],
            ),
            expect("USDT-TRON", TRON_RECIPIENT, "1"),
        ),
        Scenario.NOT_FOUND,
    ),
    "provider_error": (
        lambda: run(
            TxReport(
                hash=HASH,
                status=Status.PROVIDER_ERROR,
                network=Network.TRON,
                chain_name="TRON",
                explorer_url=TRON_LINK,
                message="limit reached",
            ),
            expect("USDT-TRON", TRON_RECIPIENT, "1"),
        ),
        Scenario.PROVIDER_ERROR,
    ),
    "invalid_input": (
        lambda: run(
            TxReport(hash="nonsense", status=Status.INVALID_INPUT, message="bad"),
            expect("USDT-TRON", TRON_RECIPIENT, "1"),
        ),
        Scenario.INVALID_INPUT,
    ),
}

OWN_TEXT = [name for name, (_, scenario) in CASES.items() if isinstance(scenario, DepositScenario)]
FULL_ADDRESS_CASES = {"wrong_address", "wrong_address_similar"}

# What the last sentence of each deposit text says: one clear next step.
NEXT_STEP = {
    "en": {
        DepositScenario.MATCH: "to show the funds on the balance.",
        DepositScenario.WAITING: "current number of confirmations.",
        DepositScenario.WRONG_NETWORK: "sent through a different network depends on it.",
        DepositScenario.WRONG_TOKEN: "which asset the deposit was meant for.",
        DepositScenario.WRONG_ADDRESS: "compare both addresses character by character.",
        DepositScenario.AMOUNT_LOWER: "entered for the withdrawal.",
        DepositScenario.AMOUNT_HIGHER: "expected amount was entered correctly.",
    },
    "uk": {
        DepositScenario.MATCH: "залежить від платформи-отримувача.",
        DepositScenario.WAITING: "кількість підтверджень.",
        DepositScenario.WRONG_NETWORK: "надіслані через іншу мережу, залежить від неї.",
        DepositScenario.WRONG_TOKEN: "для якого активу призначався депозит.",
        DepositScenario.WRONG_ADDRESS: "порівняйте обидві адреси символ за символом.",
        DepositScenario.AMOUNT_LOWER: "вказано для виведення.",
        DepositScenario.AMOUNT_HIGHER: "чи правильно вказано очікувану суму.",
    },
    "ru": {
        DepositScenario.MATCH: "зависит от платформы-получателя.",
        DepositScenario.WAITING: "количество подтверждений.",
        DepositScenario.WRONG_NETWORK: "отправленные через другую сеть, зависит от неё.",
        DepositScenario.WRONG_TOKEN: "для какого актива предназначался депозит.",
        DepositScenario.WRONG_ADDRESS: "сравните оба адреса символ за символом.",
        DepositScenario.AMOUNT_LOWER: "указана для вывода.",
        DepositScenario.AMOUNT_HIGHER: "правильно ли указана ожидаемая сумма.",
    },
}


def test_every_deposit_scenario_has_a_case() -> None:
    covered = {s for _, s in CASES.values() if isinstance(s, DepositScenario)}
    assert covered == set(DepositScenario)


@pytest.mark.parametrize("case", CASES)
def test_scenario_is_picked_from_the_result(case: str) -> None:
    build, scenario = CASES[case]
    assert pick_deposit_scenario(build()) is scenario


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("case", CASES)
def test_client_text_basics(case: str, lang: str) -> None:
    result = CASES[case][0]()
    client = explain_deposit(result, lang).client

    assert "{" not in client
    assert "}" not in client
    assert 2 <= len(sentences_of(client)) <= 5
    lowered = client.lower()
    for word in FORBIDDEN[lang]:
        assert word not in lowered, word
    if case == "invalid_input":
        assert "http" not in client
    else:
        assert client.splitlines()[-1].endswith(result.report.explorer_url or "missing")


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("case", OWN_TEXT)
def test_each_text_ends_with_its_next_step(case: str, lang: str) -> None:
    scenario = CASES[case][1]
    sentences = sentences_of(explain_deposit(CASES[case][0](), lang).client)
    assert sentences[-1].endswith(NEXT_STEP[lang][scenario])


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("case", ["failed", "failed_evm", "pending", "not_found", "provider_error"])
def test_failed_and_inconclusive_results_use_the_transaction_texts(case: str, lang: str) -> None:
    result = CASES[case][0]()
    assert explain_deposit(result, lang).client == explain(result.report, lang).client


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("case", [c for c in OWN_TEXT if c not in FULL_ADDRESS_CASES])
def test_addresses_are_short_except_where_they_are_compared(case: str, lang: str) -> None:
    result = CASES[case][0]()
    client = explain_deposit(result, lang).client
    assert result.expected.address not in client
    assert (result.report.to_address or "-") not in client


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("case", sorted(FULL_ADDRESS_CASES))
def test_the_wrong_address_text_shows_both_addresses_in_full(case: str, lang: str) -> None:
    result = CASES[case][0]()
    client = explain_deposit(result, lang).client
    assert result.expected.address in client
    assert TRON_RECIPIENT in client


def test_similar_addresses_are_pointed_out_without_claiming_fraud() -> None:
    expected_words = {
        "en": "start and end with the same characters",
        "uk": "Початок і кінець цих адрес збігаються",
        "ru": "Начало и конец этих адресов совпадают",
    }
    fraud = ["fraud", "scam", "poison", "fake", "шахра", "отруєн", "мошенн", "отравл", "підроб"]
    for lang in LANGS:
        plain = explain_deposit(CASES["wrong_address"][0](), lang).client
        similar = explain_deposit(CASES["wrong_address_similar"][0](), lang).client
        assert expected_words[lang] not in plain
        assert expected_words[lang] in similar
        for word in fraud:
            assert word not in similar.lower(), (lang, word)


def test_the_internal_summary_names_address_poisoning_only_when_addresses_look_alike() -> None:
    similar = explain_deposit(CASES["wrong_address_similar"][0](), "en").internal
    plain = explain_deposit(CASES["wrong_address"][0](), "en").internal
    assert "possible address poisoning" in similar
    assert "poisoning" not in plain


def test_a_lower_amount_mentions_a_withdrawal_fee_gently() -> None:
    needles = {
        "en": ("This can happen if", "withdrawal fee"),
        "uk": ("Так може бути, якщо", "комісію за виведення"),
        "ru": ("Так может быть, если", "комиссию за вывод"),
    }
    for lang, parts in needles.items():
        client = explain_deposit(CASES["amount_lower"][0](), lang).client
        for part in parts:
            assert part in client
        higher = explain_deposit(CASES["amount_higher"][0](), lang).client
        assert parts[1] not in higher


def test_wrong_network_names_both_networks() -> None:
    client = explain_deposit(CASES["wrong_network_evm"][0](), "en").client
    assert "found on the Polygon network" in client
    assert "expected on the Ethereum network" in client
    client = explain_deposit(CASES["wrong_network"][0](), "en").client
    assert "found on the TRON network" in client
    assert "expected on the Ethereum network" in client


def test_wrong_token_says_what_moved_instead() -> None:
    assert (
        "Instead, 1529.315 USDT was sent."
        in explain_deposit(CASES["wrong_token"][0](), "en").client
    )
    nothing = explain_deposit(CASES["wrong_token_nothing_moved"][0](), "en").client
    assert "No transfer of this asset was found in it." in nothing


def test_a_lookalike_token_keeps_its_warning_in_the_deposit_text() -> None:
    for lang in LANGS:
        client = explain_deposit(CASES["wrong_token_lookalike"][0](), lang).client
        assert TEMPLATES[lang]["warnings"]["lookalike_token"] in client
        assert "TNPeea…YqeL" in client
        assert FAKE_CONTRACT not in client


def test_a_waiting_deposit_shows_the_count_and_makes_no_promise() -> None:
    assert "2 of 19" in explain_deposit(CASES["waiting"][0](), "en").client
    assert "2 з 19" in explain_deposit(CASES["waiting"][0](), "uk").client
    assert "2 из 19" in explain_deposit(CASES["waiting"][0](), "ru").client


def test_a_legacy_usdc_match_keeps_the_older_version_warning() -> None:
    legacy = "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"
    report = tron_with(
        transfer(legacy, TRON_RECIPIENT, "30"),
        warnings=[ReportWarning(WarningCode.LEGACY_TOKEN, "older")],
        explorer_url=TRON_LINK,
        chain_name="TRON",
    )
    result = run(report, expect("USDC-TRON", TRON_RECIPIENT, "30"))
    client = explain_deposit(result, "en").client
    assert "an older version of USDC" in client
    assert "30 USDC (legacy)" in client


def test_polite_form_is_used_in_uk_and_ru() -> None:
    forbidden = {"uk": [" твоя ", " ти ", " тебе "], "ru": [" твоя ", " ты ", " тебе "]}
    for lang, words in forbidden.items():
        for build, _ in CASES.values():
            client = " " + explain_deposit(build(), lang).client.lower() + " "
            for word in words:
                assert word not in client


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("case", CASES)
def test_texts_never_call_the_transaction_the_readers_own(case: str, lang: str) -> None:
    client = explain_deposit(CASES[case][0](), lang).client.split("\n")[0].lower()
    assert "your transaction" not in client
    assert not re.search(r"ваш\w* транзакц", client)


def test_every_language_has_the_same_scenarios_snippets_and_placeholders() -> None:
    reference = DEPOSIT_TEMPLATES["en"]
    assert set(reference["scenarios"]) == {s.value for s in DepositScenario}

    def fields(text: str) -> set[str]:
        return set(PLACEHOLDER.findall(text))

    for lang in LANGS:
        texts = DEPOSIT_TEMPLATES[lang]
        assert set(texts["scenarios"]) == set(reference["scenarios"]), lang
        assert set(texts["snippets"]) == set(reference["snippets"]), lang
        for name, parts in reference["scenarios"].items():
            assert fields(" ".join(parts)) == fields(" ".join(texts["scenarios"][name])), (
                lang,
                name,
            )
        for name, text in reference["snippets"].items():
            assert fields(text) == fields(texts["snippets"][name]), (lang, name)


def test_unsupported_language_is_an_error() -> None:
    with pytest.raises(ValueError, match="en, uk, ru"):
        explain_deposit(CASES["match"][0](), "de")


GOLDEN = {
    "match": {
        "en": "The transaction on the TRON network was executed successfully and is fully "
        "confirmed. It matches the expected deposit: 1529.315 USDT sent to the address "
        "THobV9\u20265iNb, time: 2026-11-22 10:41 UTC. From here, it is up to the receiving "
        "platform to show the funds on the balance.",
        "uk": "Транзакцію в мережі TRON успішно виконано та повністю підтверджено. Вона "
        "відповідає очікуваному депозиту: 1529.315 USDT надіслано на адресу THobV9\u20265iNb, "
        "час: 2026-11-22 10:41 UTC. Подальше відображення коштів на балансі залежить від "
        "платформи-отримувача.",
        "ru": "Транзакция в сети TRON успешно выполнена и полностью подтверждена. Она "
        "соответствует ожидаемому депозиту: 1529.315 USDT отправлено на адрес THobV9\u20265iNb, "
        "время: 2026-11-22 10:41 UTC. Дальнейшее отображение средств на балансе зависит от "
        "платформы-получателя.",
    },
    "amount_lower": {
        "en": "The transaction on the TRON network was successful: 1529.315 USDT was sent to the "
        "expected address, which is less than the expected 2000 USDT. This can happen if the "
        "platform that sent the funds deducted a withdrawal fee from the amount. If the "
        "difference is not explained by a fee, please check the amount that was entered for "
        "the withdrawal.",
        "uk": "Транзакцію в мережі TRON успішно виконано: на очікувану адресу надіслано 1529.315 "
        "USDT, що менше за очікувані 2000 USDT. Так може бути, якщо платформа, з якої надсилали "
        "кошти, утримала комісію за виведення із суми. Якщо різницю не пояснює комісія, будь "
        "ласка, перевірте суму, яку було вказано для виведення.",
        "ru": "Транзакция в сети TRON успешно выполнена: на ожидаемый адрес отправлено 1529.315 "
        "USDT, что меньше ожидаемых 2000 USDT. Так может быть, если платформа, с которой "
        "отправляли средства, удержала комиссию за вывод из суммы. Если разницу не объясняет "
        "комиссия, пожалуйста, проверьте сумму, которая была указана для вывода.",
    },
    "wrong_address": {
        "en": "The transaction on the TRON network was successful, but the asset USDT was not "
        f"sent to the expected address. Expected address: {OTHER_TRON}, actual recipient: "
        f"{TRON_RECIPIENT}. Please compare both addresses character by character.",
        "uk": "Транзакцію в мережі TRON успішно виконано, але актив USDT надіслано не на "
        f"очікувану адресу. Очікувана адреса: {OTHER_TRON}, фактичний отримувач: "
        f"{TRON_RECIPIENT}. Будь ласка, порівняйте обидві адреси символ за символом.",
        "ru": "Транзакция в сети TRON успешно выполнена, но актив USDT отправлен не на "
        f"ожидаемый адрес. Ожидаемый адрес: {OTHER_TRON}, фактический получатель: "
        f"{TRON_RECIPIENT}. Пожалуйста, сравните оба адреса символ за символом.",
    },
}


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("case", GOLDEN)
def test_golden_client_texts(case: str, lang: str) -> None:
    result = CASES[case][0]()
    link = TEMPLATES[lang]["snippets"]["link_line"].format(link=result.report.explorer_url)
    assert explain_deposit(result, lang).client == GOLDEN[case][lang] + "\n" + link


# internal summary


def lines_of(case: str) -> list[str]:
    return explain_deposit(CASES[case][0](), "en").internal.splitlines()


@pytest.mark.parametrize("case", CASES)
def test_internal_summary_is_short_and_has_the_link(case: str) -> None:
    lines = lines_of(case)
    assert 3 <= len(lines) <= 7
    assert lines[0].startswith("Deposit check: ")
    if case != "invalid_input":
        assert lines[-1].startswith("Link: ")


def test_internal_verdict_lines() -> None:
    assert lines_of("match")[0] == "Deposit check: MATCH"
    assert lines_of("waiting")[0] == "Deposit check: WAITING (matches, not final yet: 2/19)"
    assert lines_of("failed")[0] == "Deposit check: MISMATCH (transaction failed)"
    assert lines_of("wrong_network_evm")[0] == (
        "Deposit check: MISMATCH (wrong network, found on Polygon)"
    )
    assert lines_of("wrong_token")[0] == "Deposit check: MISMATCH (wrong token)"
    assert lines_of("wrong_address")[0] == "Deposit check: MISMATCH (wrong receiving address)"
    assert lines_of("amount_lower")[0] == ("Deposit check: MISMATCH (amount lower than expected)")
    assert lines_of("pending")[0] == (
        "Deposit check: INCONCLUSIVE (not in a block yet, nothing to compare)"
    )
    assert lines_of("not_found")[0] == "Deposit check: INCONCLUSIVE (not found)"
    assert lines_of("provider_error")[0] == "Deposit check: INCONCLUSIVE (provider error)"
    assert lines_of("invalid_input")[0] == "Deposit check: INCONCLUSIVE (invalid input)"


def test_internal_expected_and_found_lines() -> None:
    match = lines_of("match")
    assert match[1] == f"Expected: 1529.315 USDT-TRON to {TRON_RECIPIENT}"
    assert match[2] == f"Found: 1529.315 USDT to {TRON_RECIPIENT}"
    lower = lines_of("amount_lower")
    assert lower[2] == f"Found: 1529.315 USDT to {TRON_RECIPIENT}, lower than expected by 470.685"
    higher = lines_of("amount_higher")
    assert higher[2].endswith("higher than expected by 529.315")
    assert lines_of("wrong_address")[2] == f"Found: USDT sent to {TRON_RECIPIENT}"
    assert lines_of("wrong_network")[2] == "Found on: TRON (expected Ethereum)"
    assert lines_of("failed")[2] == "Found: the transaction failed, nothing was transferred"


def test_internal_wrong_token_lists_what_was_moved() -> None:
    assert lines_of("wrong_token")[2] == "Found: 1529.315 USDT"
    lookalike = lines_of("wrong_token_lookalike")[2]
    assert lookalike == f"Found: unknown token {FAKE_CONTRACT} (raw amount 30)"
    assert lines_of("wrong_token_nothing_moved")[2] == "Found: no transfer of USDT"


def test_internal_summary_says_usdt0_was_sent() -> None:
    report = evm_with(
        137,
        transfer("0xc2132d05d31c914a87c6611c10748aeb04b58e8f", ETH_RECIPIENT, "50"),
        explorer_url="https://polygonscan.com/tx/0x1",
    )
    result = run(report, expect("USDT-POLYGON", ETH_RECIPIENT, "50"))
    found = explain_deposit(result, "en").internal.splitlines()[2]
    assert found == f"Found: 50 USDT0 to {ETH_RECIPIENT} (sent as USDT0, counts as USDT)"
    assert "50 USDT0 sent to" in explain_deposit(result, "en").client


def test_internal_summary_keeps_report_warnings() -> None:
    lines = lines_of("wrong_token_lookalike")
    assert any(line.startswith("Notes: LOOKALIKE_TOKEN") for line in lines)


def test_internal_summary_is_english_whatever_the_client_language() -> None:
    internal = {explain_deposit(CASES["wrong_address"][0](), lang).internal for lang in LANGS}
    assert len(internal) == 1


def test_internal_labels_cover_every_reason() -> None:
    from tx_checker.deposit_check import Reason

    assert set(DEPOSIT_INTERNAL["reasons"]) == {reason.value for reason in Reason}


SIMILAR_ADVICE = {
    "en": "copied from the deposit page of the platform, not from the transaction history.",
    "uk": "Адресу варто копіювати зі сторінки депозиту платформи, а не з історії транзакцій.",
    "ru": "Адрес стоит копировать со страницы депозита платформы, а не из истории транзакций.",
}


@pytest.mark.parametrize("lang", LANGS)
def test_similar_addresses_come_with_advice_on_where_to_copy_the_address_from(lang: str) -> None:
    similar = explain_deposit(CASES["wrong_address_similar"][0](), lang).client
    plain = explain_deposit(CASES["wrong_address"][0](), lang).client
    assert SIMILAR_ADVICE[lang] in similar
    assert SIMILAR_ADVICE[lang] not in plain
    assert 2 <= len(sentences_of(similar)) <= 5


@pytest.mark.parametrize("lang", LANGS)
def test_similar_addresses_stay_within_five_sentences_even_with_a_warning(lang: str) -> None:
    legacy = ReportWarning(WarningCode.LEGACY_TOKEN, "older")
    report = tron_report("usdt_transfer")
    report.warnings.append(legacy)
    expected = Expected(parse_asset("USDT-TRON"), SIMILAR, Decimal("1529.315"))
    client = explain_deposit(run(report, expected), lang).client
    assert len(sentences_of(client)) <= 5
    assert TEMPLATES[lang]["warnings"]["legacy_token"] not in client
    other = explain_deposit(run(report, expect("USDT-TRON", OTHER_TRON, "1")), lang).client
    assert TEMPLATES[lang]["warnings"]["legacy_token"] in other
    assert len(sentences_of(other)) <= 5


def test_wrong_network_uses_the_deposit_wording_in_every_language() -> None:
    build = CASES["wrong_network"][0]
    assert "не відповідає очікуваному депозиту" in explain_deposit(build(), "uk").client
    assert "не соответствует ожидаемому депозиту" in explain_deposit(build(), "ru").client
    assert "so it does not match the expected deposit" in explain_deposit(build(), "en").client
    assert "не збігається" not in explain_deposit(build(), "uk").client


def test_the_match_text_no_longer_tells_the_reader_to_forward_the_link() -> None:
    for lang in LANGS:
        client = explain_deposit(CASES["match"][0](), lang).client.lower()
        assert "send them this link" not in client
        for word in FORBIDDEN[lang]:
            assert word not in client
