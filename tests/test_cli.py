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
from tx_checker.explain.templates import TEMPLATES
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
