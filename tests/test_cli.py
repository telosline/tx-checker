import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from tx_checker import cli, config
from tx_checker.cli import Lang, app
from tx_checker.deposit_check import DepositResult, Verdict, parse_expected
from tx_checker.explain.templates import TEMPLATES
from tx_checker.models import Status, TxReport
from tx_checker.providers.etherscan import BASE_URL as EVM_URL
from tx_checker.providers.tronscan import BASE_URL as TRON_API

FIXTURES = Path(__file__).parent / "fixtures"
TRON_URL = f"{TRON_API}/transaction-info"
KEY = "TESTKEY123SECRET"
ANSI = "\x1b["

EVM_FIXTURE = json.loads((FIXTURES / "evm" / "eth_transfer.json").read_text(encoding="utf-8"))
EVM_HASH: str = EVM_FIXTURE["_meta"]["tx_hash"]


def tron_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / "tron" / f"{name}.json").read_text(encoding="utf-8"))


USDT = tron_fixture("usdt_transfer")
MISSING = "ab" * 32


class Routes:
    def __init__(self, router: respx.MockRouter) -> None:
        self.tron = router.get(TRON_URL).mock(return_value=httpx.Response(200, json={}))
        self.evm = router.get(EVM_URL).mock(
            return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
        )

    def tron_returns(self, body: dict[str, Any]) -> None:
        self.tron.mock(return_value=httpx.Response(200, json=body))

    def evm_finds(self) -> None:
        responses = EVM_FIXTURE["responses"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=responses[request.url.params["action"]])

        self.evm.mock(side_effect=handler)


@pytest.fixture(autouse=True)
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_dotenv", lambda: False)
    monkeypatch.setenv("ETHERSCAN_API_KEY", KEY)
    monkeypatch.setenv("TRONSCAN_API_KEY", KEY)
    monkeypatch.delenv("NO_COLOR", raising=False)


@pytest.fixture
def routes() -> Any:
    with respx.mock(assert_all_called=False) as router:
        yield Routes(router)


runner = CliRunner()


def run(*args: str, color: bool = False) -> Any:
    return runner.invoke(app, ["check", *args], color=color)


def test_found_transaction_exits_zero_and_prints_both_blocks(routes: Routes) -> None:
    routes.tron_returns(USDT)
    result = run(USDT["hash"])
    assert result.exit_code == 0
    assert "Status: SUCCESS" in result.stdout
    assert "fully confirmed" in result.stdout
    assert "Internal summary" in result.stderr
    assert "Message for the client (en)" in result.stderr


def test_failed_transaction_still_counts_as_found(routes: Routes) -> None:
    routes.tron_returns(tron_fixture("failed_out_of_energy"))
    result = run(tron_fixture("failed_out_of_energy")["hash"])
    assert result.exit_code == 0
    assert "Status: FAILED (out of energy)" in result.stdout


def test_not_found_exits_one(routes: Routes) -> None:
    result = run(MISSING, "--network", "tron")
    assert result.exit_code == 1
    assert "could not find this transaction" in result.stdout


def test_invalid_input_exits_two_and_never_calls_a_provider(routes: Routes) -> None:
    result = run("hello")
    assert result.exit_code == 2
    assert "Status: INVALID INPUT" in result.stdout
    assert routes.tron.call_count == 0
    assert routes.evm.call_count == 0


def test_provider_error_exits_three(routes: Routes) -> None:
    routes.tron.mock(return_value=httpx.Response(400, json={"message": "bad"}))
    result = run(MISSING)
    assert result.exit_code == 3
    assert "Status: PROVIDER ERROR" in result.stdout
    assert "temporarily unavailable" in result.stdout


def test_missing_key_is_a_short_message_without_a_traceback(
    routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRONSCAN_API_KEY")
    result = run(MISSING, "--network", "tron")
    assert result.exit_code == 4
    assert result.stdout == ""
    assert ".env.example" in result.stderr
    assert "TRONSCAN_API_KEY" in result.stderr
    assert "Traceback" not in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1


def test_unexpected_error_shows_no_traceback_and_no_secret(
    routes: Routes, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"boom https://x.test/api?apikey={KEY}")

    monkeypatch.setattr(cli, "check", boom)
    with caplog.at_level(logging.ERROR):
        result = run(MISSING)
    assert result.exit_code == 70
    assert "Traceback" not in result.output
    assert KEY not in result.output
    assert KEY not in caplog.text
    assert "RuntimeError" in caplog.text


def test_client_only_prints_just_the_text(routes: Routes) -> None:
    routes.tron_returns(USDT)
    result = run(USDT["hash"], "--client-only")
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].startswith("The transaction on the TRON network was executed successfully")
    assert lines[-1].startswith("You can check the transaction yourself: https://tronscan.org/")
    assert "Network:" not in result.stdout
    assert "Message for the client (en)" in result.stderr
    assert "Internal summary" not in result.stderr


def test_summary_only_prints_just_the_summary(routes: Routes) -> None:
    routes.tron_returns(USDT)
    result = run(USDT["hash"], "--summary-only")
    assert result.stdout.startswith("Network: TRON | Status: SUCCESS")
    assert "The transaction on" not in result.stdout
    assert "Message for the client" not in result.stderr


def test_client_text_is_plain_even_when_colors_are_on(routes: Routes) -> None:
    routes.tron_returns(USDT)
    colored = run(USDT["hash"], color=True)
    assert ANSI in colored.stdout  # the summary has colors
    client = run(USDT["hash"], "--client-only", color=True)
    assert ANSI not in client.stdout
    assert client.stdout in colored.stdout  # the same text, untouched, inside the full output


def test_no_color_variable_turns_colors_off(
    routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes.tron_returns(USDT)
    monkeypatch.setenv("NO_COLOR", "1")
    assert ANSI not in run(USDT["hash"], color=True).stdout


def test_output_in_a_pipe_has_no_escape_codes(routes: Routes) -> None:
    routes.tron_returns(USDT)
    assert ANSI not in run(USDT["hash"]).output


@pytest.mark.parametrize("lang", ["en", "uk", "ru"])
def test_client_text_follows_the_language(routes: Routes, lang: str) -> None:
    routes.tron_returns(USDT)
    result = run(USDT["hash"], "--client-only", "--lang", lang)
    expected = {"en": "The transaction on", "uk": "Транзакцію в мережі", "ru": "Транзакция в сети"}
    assert result.stdout.startswith(expected[lang])
    assert f"Message for the client ({lang})" in result.stderr


def test_internal_summary_stays_english(routes: Routes) -> None:
    routes.tron_returns(USDT)
    result = run(USDT["hash"], "--lang", "uk", "--summary-only")
    assert "Status: SUCCESS" in result.stdout


def test_json_has_the_report_and_the_explanation(routes: Routes) -> None:
    routes.tron_returns(USDT)
    result = run(USDT["hash"], "--json", "--lang", "uk")
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["status"] == "success"
    assert data["network"] == "tron"
    assert data["is_final"] is True
    assert data["token_transfers"][0]["amount"] == "1529.315"
    assert data["fee"]["fee_total_trx"] == "0.345"
    assert data["explanation"]["internal"].startswith("Network: TRON | Status: SUCCESS")
    assert data["explanation"]["client"].startswith("Транзакцію в мережі TRON")
    assert result.stderr == ""


def test_json_keeps_the_exit_code_of_the_result(routes: Routes) -> None:
    result = run(MISSING, "--network", "tron", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["status"] == "not_found"


def test_only_one_output_mode_at_a_time(routes: Routes) -> None:
    for flags in (["--json", "--client-only"], ["--client-only", "--summary-only"]):
        result = run(USDT["hash"], *flags)
        assert result.exit_code == 2
        assert "only one of" in result.stderr
    assert routes.tron.call_count == 0


@pytest.mark.parametrize(
    "options",
    [["--lang", "de"], ["--network", "btc"], ["--chain-id", "0"], ["--confirmations", "0"]],
)
def test_bad_options_exit_two(routes: Routes, options: list[str]) -> None:
    result = run(USDT["hash"], *options)
    assert result.exit_code == 2
    assert routes.tron.call_count == 0


def test_chain_id_with_tron_exits_two(routes: Routes) -> None:
    result = run(MISSING, "--network", "tron", "--chain-id", "1")
    assert result.exit_code == 2
    assert routes.tron.call_count == 0


def test_chain_id_reaches_the_provider(routes: Routes) -> None:
    routes.evm_finds()
    result = run(EVM_HASH, "--chain-id", "137", "--summary-only")
    assert result.exit_code == 0
    assert "Network: Polygon (chain 137)" in result.stdout
    assert routes.evm.calls.last.request.url.params["chainid"] == "137"


def test_confirmations_override_reaches_the_analyzer(routes: Routes) -> None:
    routes.tron_returns(tron_fixture("unconfirmed_trx"))
    waiting = run(tron_fixture("unconfirmed_trx")["hash"], "--client-only")
    assert "not fully confirmed" in waiting.stdout
    final = run(tron_fixture("unconfirmed_trx")["hash"], "--client-only", "--confirmations", "2")
    assert "fully confirmed" in final.stdout
    assert "not fully confirmed" not in final.stdout


def test_the_key_never_reaches_the_output(routes: Routes) -> None:
    routes.evm.mock(side_effect=httpx.ConnectError(f"failed {EVM_URL}?apikey={KEY}"))
    result = run(EVM_HASH)
    assert result.exit_code == 3
    assert KEY not in result.output


def test_help_documents_exit_codes_and_the_clipboard_examples() -> None:
    result = runner.invoke(app, ["check", "--help"])
    assert result.exit_code == 0
    for text in (
        "Exit codes:",
        "0  the transaction was found",
        "1  not found",
        "2  invalid input",
        "3  provider error",
        "4  API key is not configured",
        "wl-copy",
        "pbcopy",
        "--client-only",
    ):
        assert text in result.stdout


def test_running_without_a_command_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage:" in result.output
    assert "check" in result.output


def test_languages_match_the_templates() -> None:
    assert {lang.value for lang in Lang} == set(TEMPLATES)


# deposit command

RECIPIENT = "THobV9PPMgzxJtrHAUNLJ88CWWLeJF5iNb"
OTHER_ADDRESS = "TKgmbuXFYn58oCrFJFFDgneWc9aRh9eSkz"
ETH_RECIPIENT = "0x59302f15c6c604d3f43d2902337444388119a1d1"
UNCONFIRMED = tron_fixture("unconfirmed_trx")


def deposit(
    tx: str,
    *args: str,
    asset: str = "USDT-TRON",
    address: str = RECIPIENT,
    amount: str = "1529.315",
    color: bool = False,
) -> Any:
    options = ["--address", address, "--amount", amount, "--asset", asset]
    return runner.invoke(app, ["deposit", tx, *options, *args], color=color)


def evm_found_only_on(chain_id: str, routes: Routes) -> None:
    responses = EVM_FIXTURE["responses"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["chainid"] != chain_id:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
        return httpx.Response(200, json=responses[request.url.params["action"]])

    routes.evm.mock(side_effect=handler)


def evm_chains_asked(routes: Routes) -> list[str]:
    return [
        call.request.url.params["chainid"]
        for call in routes.evm.calls
        if call.request.url.params["action"] == "eth_getTransactionByHash"
    ]


def test_a_matching_deposit_exits_zero_and_prints_both_blocks(routes: Routes) -> None:
    routes.tron_returns(USDT)
    result = deposit(USDT["hash"])
    assert result.exit_code == 0
    assert result.stdout.startswith("Deposit check: MATCH")
    assert "It matches the expected deposit" in result.stdout
    assert "Internal summary" in result.stderr
    assert "Message for the client (en)" in result.stderr
    assert routes.tron.call_count == 1
    assert routes.evm.call_count == 0


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"amount": "2000"}, "amount lower than expected"),
        ({"amount": "1000"}, "amount higher than expected"),
        ({"address": OTHER_ADDRESS}, "wrong receiving address"),
        ({"asset": "USDC-TRON"}, "wrong token"),
    ],
)
def test_a_mismatch_exits_five_and_names_the_reason(
    routes: Routes, options: dict[str, str], reason: str
) -> None:
    routes.tron_returns(USDT)
    result = deposit(USDT["hash"], **options)
    assert result.exit_code == 5
    assert f"Deposit check: MISMATCH ({reason})" in result.stdout


def test_a_failed_transaction_is_a_mismatch(routes: Routes) -> None:
    failed = tron_fixture("failed_out_of_energy")
    routes.tron_returns(failed)
    result = deposit(failed["hash"], address="TJVKZXswjk77V3DTzTv1uvSYCyz3cVR5iK", amount="22")
    assert result.exit_code == 5
    assert "Deposit check: MISMATCH (transaction failed)" in result.stdout
    assert "ran out of energy" in result.stdout


def test_a_matching_but_not_final_deposit_exits_six(routes: Routes) -> None:
    routes.tron_returns(UNCONFIRMED)
    result = deposit(
        UNCONFIRMED["hash"], asset="TRX", address=UNCONFIRMED["toAddress"], amount="0.000006"
    )
    assert result.exit_code == 6
    assert "Deposit check: WAITING" in result.stdout


def test_confirmations_override_turns_waiting_into_a_match(routes: Routes) -> None:
    routes.tron_returns(UNCONFIRMED)
    result = deposit(
        UNCONFIRMED["hash"],
        "--confirmations",
        "2",
        asset="TRX",
        address=UNCONFIRMED["toAddress"],
        amount="0.000006",
    )
    assert result.exit_code == 0
    assert "Deposit check: MATCH" in result.stdout


def test_not_found_everywhere_exits_one_and_searched_the_other_chains(routes: Routes) -> None:
    result = deposit(MISSING)
    assert result.exit_code == 1
    assert "Deposit check: INCONCLUSIVE (not found)" in result.stdout
    assert routes.tron.call_count == 1
    assert evm_chains_asked(routes) == ["1", "137", "42161"]


def test_a_hash_found_on_another_chain_is_a_wrong_network(routes: Routes) -> None:
    evm_found_only_on("137", routes)
    result = deposit(EVM_HASH, asset="ETH", address=ETH_RECIPIENT, amount="0.08")
    assert result.exit_code == 5
    assert "MISMATCH (wrong network, found on Polygon)" in result.stdout
    assert "found on the Polygon network" in result.stdout
    assert evm_chains_asked(routes) == ["1", "137"]


def test_an_evm_deposit_that_matches(routes: Routes) -> None:
    routes.evm_finds()
    result = deposit(EVM_HASH, asset="ETH", address=ETH_RECIPIENT, amount="0.08")
    assert result.exit_code == 0
    assert evm_chains_asked(routes) == ["1"]


def test_a_tron_transfer_for_an_ethereum_asset_is_a_wrong_network(routes: Routes) -> None:
    routes.evm.mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
    )
    routes.tron_returns(USDT)
    result = deposit(USDT["hash"], asset="USDT-ERC20", address=ETH_RECIPIENT)
    assert result.exit_code == 5
    assert "wrong network, found on TRON" in result.stdout


def test_invalid_hash_exits_two_and_never_calls_a_provider(routes: Routes) -> None:
    result = deposit("hello")
    assert result.exit_code == 2
    assert "Deposit check: INCONCLUSIVE (invalid input)" in result.stdout
    assert routes.tron.call_count == 0
    assert routes.evm.call_count == 0


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"asset": "BTC"}, "Unknown asset"),
        ({"asset": "USDT"}, "Unknown asset"),
        ({"address": "T123"}, "TRON address"),
        ({"address": ETH_RECIPIENT}, "TRON address"),
        ({"amount": "0"}, "positive number"),
        ({"amount": "1,5"}, "positive number"),
        ({"amount": "abc"}, "positive number"),
        ({"asset": "ETH", "address": RECIPIENT}, "EVM address"),
    ],
)
def test_bad_expectations_exit_two_before_any_call(
    routes: Routes, options: dict[str, str], message: str
) -> None:
    result = deposit(USDT["hash"], **options)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("Error: ")
    assert message in result.stderr
    assert routes.tron.call_count == 0
    assert routes.evm.call_count == 0


def test_missing_required_options_exit_two(routes: Routes) -> None:
    for args in (["deposit", USDT["hash"]], ["deposit", USDT["hash"], "--asset", "ETH"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 2
        assert "Missing option" in result.stderr
    assert routes.tron.call_count == 0


def test_deposit_provider_error_exits_three(routes: Routes) -> None:
    routes.tron.mock(return_value=httpx.Response(400, json={"message": "bad"}))
    result = deposit(MISSING)
    assert result.exit_code == 3
    assert "Deposit check: INCONCLUSIVE (provider error)" in result.stdout
    assert routes.evm.call_count == 0


def test_missing_key_for_the_expected_network_exits_four(
    routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRONSCAN_API_KEY")
    result = deposit(MISSING)
    assert result.exit_code == 4
    assert result.stdout == ""
    assert ".env.example" in result.stderr
    assert "Traceback" not in result.stderr


def test_missing_etherscan_key_only_skips_the_extra_search(
    routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ETHERSCAN_API_KEY")
    result = deposit(MISSING)
    assert result.exit_code == 1
    assert "no Etherscan key" in result.stdout
    assert routes.evm.call_count == 0


def test_unexpected_deposit_error_shows_no_traceback_and_no_secret(
    routes: Routes, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"boom https://x.test/api?apikey={KEY}")

    monkeypatch.setattr(cli, "find_deposit", boom)
    with caplog.at_level(logging.ERROR):
        result = deposit(MISSING)
    assert result.exit_code == 70
    assert "Traceback" not in result.output
    assert KEY not in result.output
    assert KEY not in caplog.text


def test_deposit_client_only_and_summary_only(routes: Routes) -> None:
    routes.tron_returns(USDT)
    client = deposit(USDT["hash"], "--client-only")
    assert client.stdout.startswith("The transaction on the TRON network was executed")
    assert "Deposit check" not in client.stdout
    assert "Internal summary" not in client.stderr
    summary = deposit(USDT["hash"], "--summary-only")
    assert summary.stdout.startswith("Deposit check: MATCH")
    assert "The transaction on" not in summary.stdout
    assert "Message for the client" not in summary.stderr


def test_deposit_client_text_is_plain_even_when_colors_are_on(routes: Routes) -> None:
    routes.tron_returns(USDT)
    assert ANSI in deposit(USDT["hash"], color=True).stdout
    client = deposit(USDT["hash"], "--client-only", color=True)
    assert ANSI not in client.stdout


def test_deposit_output_in_a_pipe_has_no_escape_codes(routes: Routes) -> None:
    routes.tron_returns(USDT)
    assert ANSI not in deposit(USDT["hash"]).output


@pytest.mark.parametrize("lang", ["en", "uk", "ru"])
def test_deposit_client_text_follows_the_language(routes: Routes, lang: str) -> None:
    routes.tron_returns(USDT)
    result = deposit(USDT["hash"], "--client-only", "--lang", lang)
    expected = {"en": "The transaction on", "uk": "Транзакцію в мережі", "ru": "Транзакция в сети"}
    assert result.stdout.startswith(expected[lang])
    assert f"Message for the client ({lang})" in result.stderr


def test_deposit_json_has_the_result_the_report_and_the_explanation(routes: Routes) -> None:
    routes.tron_returns(USDT)
    result = deposit(USDT["hash"], "--json", "--lang", "uk", amount="2000")
    assert result.exit_code == 5
    assert result.stderr == ""
    data = json.loads(result.stdout)
    assert data["verdict"] == "mismatch"
    assert data["reasons"] == ["amount_lower"]
    assert data["expected"]["asset"]["name"] == "USDT-TRON"
    assert data["expected"]["asset"]["network"] == "tron"
    assert data["expected"]["amount"] == "2000"
    assert data["expected"]["address"] == RECIPIENT
    assert data["actual_amount"] == "1529.315"
    assert data["report"]["status"] == "success"
    assert data["report"]["is_final"] is True
    assert data["explanation"]["internal"].startswith("Deposit check: MISMATCH")
    assert data["explanation"]["client"].startswith("Транзакцію в мережі TRON")


def test_deposit_json_of_a_search_result_names_where_it_was_found(routes: Routes) -> None:
    evm_found_only_on("137", routes)
    result = deposit(EVM_HASH, "--json", asset="ETH", address=ETH_RECIPIENT, amount="0.08")
    data = json.loads(result.stdout)
    assert data["verdict"] == "mismatch"
    assert data["reasons"] == ["wrong_network"]
    assert data["found_on"] == "Polygon"
    assert data["report"]["chain_id"] == 137


def test_deposit_only_one_output_mode_at_a_time(routes: Routes) -> None:
    for flags in (["--json", "--client-only"], ["--client-only", "--summary-only"]):
        result = deposit(USDT["hash"], *flags)
        assert result.exit_code == 2
        assert "only one of" in result.stderr
    assert routes.tron.call_count == 0


def test_deposit_has_no_network_options(routes: Routes) -> None:
    for option in (["--network", "tron"], ["--chain-id", "1"]):
        assert deposit(USDT["hash"], *option).exit_code == 2
    assert routes.tron.call_count == 0


def test_the_key_never_reaches_the_deposit_output(routes: Routes) -> None:
    routes.evm.mock(side_effect=httpx.ConnectError(f"failed {EVM_URL}?apikey={KEY}"))
    result = deposit(EVM_HASH, asset="ETH", address=ETH_RECIPIENT, amount="1")
    assert result.exit_code == 3
    assert KEY not in result.output


def test_deposit_help_documents_codes_assets_and_the_clipboard_example() -> None:
    result = runner.invoke(app, ["deposit", "--help"])
    assert result.exit_code == 0
    for text in (
        "Exit codes:",
        "0  match",
        "5  mismatch",
        "6  waiting",
        "1  not found",
        "2  invalid input",
        "3  provider error",
        "4  API key is not configured",
        "USDT-TRON",
        "USDC-ETHEREUM",
        "USDT-TRC20 and USDT-ERC20",
        "wl-copy",
        "--client-only",
    ):
        assert text in result.stdout


def test_the_main_help_points_to_the_deposit_codes() -> None:
    result = runner.invoke(app, ["--help"])
    assert "deposit" in result.stdout
    assert "exit codes 5 and 6" in result.stdout


def test_deposit_exit_codes_for_every_verdict_and_inconclusive_status() -> None:
    expected = parse_expected(RECIPIENT, "1", "USDT-TRON")

    def result(verdict: Verdict, status: Status = Status.SUCCESS) -> DepositResult:
        return DepositResult(
            verdict=verdict, expected=expected, report=TxReport(hash=MISSING, status=status)
        )

    assert cli._deposit_exit_code(result(Verdict.MATCH)) == 0
    assert cli._deposit_exit_code(result(Verdict.MISMATCH)) == 5
    assert cli._deposit_exit_code(result(Verdict.WAITING)) == 6
    inconclusive = {
        Status.NOT_FOUND: 1,
        Status.INVALID_INPUT: 2,
        Status.PROVIDER_ERROR: 3,
        Status.PENDING: 6,
    }
    for status, code in inconclusive.items():
        assert cli._deposit_exit_code(result(Verdict.INCONCLUSIVE, status)) == code
