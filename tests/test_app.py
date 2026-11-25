import copy
import json
import tomllib
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import streamlit as st
from streamlit.testing.v1 import AppTest

from tx_checker import config, guard
from tx_checker.analyze.tron import analyze_tron
from tx_checker.explain.explain import explain
from tx_checker.providers.etherscan import BASE_URL as EVM_URL
from tx_checker.providers.tronscan import BASE_URL as TRON_API

ROOT = Path(__file__).parent.parent
APP = ROOT / "app" / "streamlit_app.py"
FIXTURES = Path(__file__).parent / "fixtures"
TRON_URL = f"{TRON_API}/transaction-info"
KEY = "TESTKEY123SECRET"
FAKE_CONTRACT = "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL"
LEGACY_USDC = "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8"

EVM_FIXTURE = json.loads((FIXTURES / "evm" / "eth_transfer.json").read_text(encoding="utf-8"))
EVM_HASH: str = EVM_FIXTURE["_meta"]["tx_hash"]


def tron_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / "tron" / f"{name}.json").read_text(encoding="utf-8"))


USDT = tron_fixture("usdt_transfer")
USDT_EXAMPLE = "example-6b614edb1f"
OOE_EXAMPLE = "example-02603687d2"
SUBMIT = "FormSubmitter:lookup-Check"


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
def environment(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "load_dotenv", lambda: False)
    monkeypatch.setenv("ETHERSCAN_API_KEY", KEY)
    monkeypatch.setenv("TRONSCAN_API_KEY", KEY)
    st.cache_resource.clear()  # the shared budget and cache must not leak between tests
    yield
    st.cache_resource.clear()


@pytest.fixture
def routes() -> Any:
    with respx.mock(assert_all_called=False) as router:
        yield Routes(router)


def new_app() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=30).run()


def submit(at: AppTest, text: str) -> AppTest:
    at.text_input(key="query").set_value(text)
    return at.button(key=SUBMIT).click().run()


def visible_text(at: AppTest) -> str:
    elements = [*at.title, *at.subheader, *at.markdown, *at.caption, *at.code]
    elements += [*at.info, *at.warning, *at.error, *at.success]
    return "\n".join(str(element.value) for element in elements)


def client_box(at: AppTest) -> str:
    return str(at.code[-1].value)


def internal_box(at: AppTest) -> str:
    return str(at.code[-2].value)


def test_the_page_renders_without_errors() -> None:
    at = new_app()
    assert not at.exception
    assert at.title[0].value == "Transaction checker"
    assert len(at.sidebar) == 0
    assert "Data from Etherscan and Tronscan APIs." in visible_text(at)


def test_only_free_tier_chains_are_offered() -> None:
    assert new_app().selectbox[0].options == [
        "Auto-detect",
        "TRON",
        "Ethereum",
        "Sepolia (testnet)",
        "Polygon",
        "Arbitrum One",
    ]


def test_language_choices_are_listed_in_the_ui() -> None:
    group = new_app().get("button_group")[0]
    assert group.options == ["English", "Українська", "Русский"]
    assert group.value == "en"


def test_an_example_runs_and_shows_the_two_text_boxes(routes: Routes) -> None:
    routes.tron_returns(USDT)
    at = new_app()
    at.button(key=USDT_EXAMPLE).click().run()
    assert not at.exception
    report = analyze_tron(USDT)
    expected = explain(report, "en")
    assert internal_box(at) == expected.internal
    assert client_box(at) == expected.client
    assert at.text_input(key="query").value == USDT["hash"]
    assert [s.value for s in at.subheader] == ["Internal summary", "Message for the client"]


def test_typing_a_hash_and_pressing_check(routes: Routes) -> None:
    routes.tron_returns(USDT)
    at = submit(new_app(), USDT["hash"])
    assert client_box(at).startswith("The transaction on the TRON network")


def test_the_client_text_follows_the_language_without_a_new_lookup(routes: Routes) -> None:
    routes.tron_returns(USDT)
    at = submit(new_app(), USDT["hash"])
    calls = routes.tron.call_count
    at.get("button_group")[0].set_value("uk").run()
    assert client_box(at).startswith("Транзакцію в мережі TRON")
    at.get("button_group")[0].set_value("ru").run()
    assert client_box(at).startswith("Транзакция в сети TRON")
    assert internal_box(at).startswith("Network: TRON")  # always English
    assert routes.tron.call_count == calls


def test_facts_fee_and_transfers_are_shown(routes: Routes) -> None:
    routes.tron_returns(USDT)
    at = submit(new_app(), USDT["hash"])
    text = visible_text(at)
    assert "Fully confirmed" in text
    assert "1529.315 USDT" in text
    assert "THobV9…5iNb" in text
    assert len(at.dataframe) == 1
    assert "TRX burned in total: 0.345" in text
    assert {e.label for e in at.expander} >= {"Fee breakdown", "Raw API data"}


def test_a_failed_transaction_lists_the_attempt_apart(routes: Routes) -> None:
    routes.tron_returns(tron_fixture("failed_out_of_energy"))
    at = new_app()
    at.button(key=OOE_EXAMPLE).click().run()
    text = visible_text(at)
    assert "Attempted transfers, not executed" in text
    assert "Token transfers" not in text
    assert len(at.dataframe) == 1


def test_a_lookalike_token_is_a_warning_and_a_legacy_token_an_info(routes: Routes) -> None:
    fake = copy.deepcopy(USDT)
    fake["trc20TransferInfo"][0].update(contract_address=FAKE_CONTRACT, symbol="USDT")
    routes.tron_returns(fake)
    at = submit(new_app(), fake["hash"])
    assert any("is not the official USDT" in w.value for w in at.warning)

    legacy = copy.deepcopy(USDT)
    legacy["hash"] = "11" * 32  # the first result is cached under the old hash
    legacy["trc20TransferInfo"][0]["contract_address"] = LEGACY_USDC
    routes.tron_returns(legacy)
    at = submit(new_app(), legacy["hash"])
    assert not at.warning
    assert any("older version of USDC" in i.value for i in at.info)


def test_a_waiting_transaction_honours_the_confirmation_option(routes: Routes) -> None:
    waiting = tron_fixture("unconfirmed_trx")
    routes.tron_returns(waiting)
    at = new_app()
    at.number_input[0].set_value(2)
    at = submit(at, waiting["hash"])
    assert "fully confirmed" in client_box(at)
    assert "not fully confirmed" not in client_box(at)


def test_the_network_option_reaches_the_provider(routes: Routes) -> None:
    routes.evm_finds()
    at = new_app()
    at.selectbox[0].select("Polygon")
    submit(at, EVM_HASH)
    assert routes.evm.calls.last.request.url.params["chainid"] == "137"
    assert routes.tron.call_count == 0


def test_not_found_is_explained_with_both_networks(routes: Routes) -> None:
    at = submit(new_app(), "ab" * 32)
    assert not at.exception
    assert "on TRON or on Ethereum" in client_box(at)
    assert "Not found" in visible_text(at) or "NOT FOUND" in internal_box(at)


def test_invalid_input_is_answered_without_any_request(routes: Routes) -> None:
    at = submit(new_app(), "hello")
    assert not at.exception
    assert client_box(at).startswith("We could not read this")
    assert routes.tron.call_count == 0
    assert routes.evm.call_count == 0


def test_the_session_limit_shows_a_friendly_message(
    routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "SESSION_LOOKUPS_PER_MINUTE", 2)
    at = new_app()
    for n in range(2):
        at = submit(at, f"{n + 1:064x}")
        assert not at.warning
    at = submit(at, f"{3:064x}")
    assert [w.value for w in at.warning] == [
        "You have reached the limit of 2 lookups per minute. Please wait a little and try again."
    ]
    assert not at.exception


def test_the_shared_budget_stops_every_session_with_a_busy_message(
    routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROVIDER_CALLS_PER_MINUTE", 5)
    st.cache_resource.clear()
    routes.evm_finds()
    first = submit(new_app(), EVM_HASH)
    assert not first.info
    other_session = submit(new_app(), "0x" + "cd" * 32)
    assert [i.value for i in other_session.info] == [
        "The demo is busy right now. Please try again in a minute."
    ]
    assert not other_session.exception


def test_a_repeated_lookup_is_served_from_the_shared_cache(routes: Routes) -> None:
    routes.tron_returns(USDT)
    submit(new_app(), USDT["hash"])
    submit(new_app(), USDT["hash"])
    assert routes.tron.call_count == 1


def test_a_missing_key_is_a_friendly_message(
    routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ETHERSCAN_API_KEY")
    at = submit(new_app(), EVM_HASH)
    assert [e.value for e in at.error] == ["The demo is not configured: an API key is missing."]
    assert not at.exception
    assert "Traceback" not in visible_text(at)


def test_keys_from_streamlit_secrets_are_used(
    routes: Routes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ETHERSCAN_API_KEY")
    routes.evm_finds()
    at = new_app()
    at.secrets["ETHERSCAN_API_KEY"] = "KEYFROMSECRETS"
    submit(at, EVM_HASH)
    assert routes.evm.calls.last.request.url.params["apikey"] == "KEYFROMSECRETS"


def test_an_unexpected_error_shows_a_plain_message_and_no_secret(
    routes: Routes, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"boom https://x.test/api?apikey={KEY}")

    monkeypatch.setattr(guard, "guarded_check", boom)
    with caplog.at_level("ERROR", logger="tx_checker.app"):
        at = submit(new_app(), USDT["hash"])
    assert [e.value for e in at.error] == ["Something went wrong. Please try again later."]
    assert not at.exception
    assert KEY not in caplog.text
    assert "RuntimeError" in caplog.text


def test_a_provider_failure_never_puts_the_key_on_the_page(routes: Routes) -> None:
    routes.evm.mock(side_effect=httpx.ConnectError(f"failed {EVM_URL}?apikey={KEY}"))
    at = submit(new_app(), EVM_HASH)
    assert not at.exception
    assert "temporarily unavailable" in client_box(at)
    assert KEY not in visible_text(at)
    assert KEY not in json.dumps([str(e.value) for e in at.get("json")])


def test_the_streamlit_config_hides_error_details() -> None:
    config_file = ROOT / ".streamlit" / "config.toml"
    settings = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert settings["client"]["showErrorDetails"] == "none"
    assert settings["browser"]["gatherUsageStats"] is False


def test_the_secrets_file_stays_out_of_git() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".streamlit/secrets.toml" in ignored
