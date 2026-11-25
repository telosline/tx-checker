import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from tx_checker import config
from tx_checker.check import check
from tx_checker.config import MissingApiKeyError
from tx_checker.models import Network, Status, TxReport, WarningCode
from tx_checker.providers.etherscan import BASE_URL as EVM_URL
from tx_checker.providers.etherscan import EtherscanClient
from tx_checker.providers.tronscan import BASE_URL as TRON_API
from tx_checker.providers.tronscan import TronscanClient

FIXTURES = Path(__file__).parent / "fixtures"
TRON_URL = f"{TRON_API}/transaction-info"
KEY = "TESTKEY123SECRET"

EVM_FIXTURE = json.loads((FIXTURES / "evm" / "eth_transfer.json").read_text(encoding="utf-8"))
EVM_HASH: str = EVM_FIXTURE["_meta"]["tx_hash"]
BARE_EVM_HASH = EVM_HASH[2:]
TRON_FIXTURE = json.loads((FIXTURES / "tron" / "trx_transfer.json").read_text(encoding="utf-8"))
TRON_HASH: str = TRON_FIXTURE["hash"]
MISSING_HASH = "ab" * 32


class Routes:
    """Both providers mocked. Each starts as 'unknown hash'."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.tron = router.get(TRON_URL).mock(return_value=httpx.Response(200, json={}))
        self.evm = router.get(EVM_URL).mock(
            return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
        )

    def tron_finds(self) -> None:
        self.tron.mock(return_value=httpx.Response(200, json=TRON_FIXTURE))

    def evm_finds(self) -> None:
        responses = EVM_FIXTURE["responses"]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=responses[request.url.params["action"]])

        self.evm.mock(side_effect=handler)


@pytest.fixture
def routes() -> Any:
    with respx.mock(assert_all_called=False) as router:
        yield Routes(router)


def run(raw: str, **kwargs: Any) -> TxReport:
    return check(
        raw,
        etherscan=EtherscanClient(KEY, http=httpx.Client(), sleep=lambda _: None),
        tronscan=TronscanClient(KEY, http=httpx.Client(), sleep=lambda _: None),
        **kwargs,
    )


def codes(report: TxReport) -> list[WarningCode]:
    return [w.code for w in report.warnings]


@pytest.mark.parametrize("raw", ["", "   ", "hello", "0x1234", "T" + "a" * 33, "ab" * 31])
def test_invalid_input_never_reaches_a_provider(routes: Routes, raw: str) -> None:
    report = run(raw)
    assert report.status is Status.INVALID_INPUT
    assert report.message
    assert routes.tron.call_count == 0
    assert routes.evm.call_count == 0


def test_invalid_input_echo_is_redacted_and_short() -> None:
    report = check("https://api.etherscan.io/v2/api?apikey=SECRETVALUE&x=" + "a" * 300)
    assert report.status is Status.INVALID_INPUT
    assert "SECRETVALUE" not in report.hash
    assert len(report.hash) <= 100


def test_bare_hash_found_on_tron_is_the_normal_case(routes: Routes) -> None:
    routes.tron_finds()
    report = run(TRON_HASH)
    assert report.status is Status.SUCCESS
    assert report.network is Network.TRON
    assert WarningCode.NETWORK_GUESSED not in codes(report)
    assert routes.evm.call_count == 0


def test_bare_hash_falls_back_to_evm_and_says_so(routes: Routes) -> None:
    routes.evm_finds()
    report = run(BARE_EVM_HASH)
    assert report.status is Status.SUCCESS
    assert report.network is Network.EVM
    assert report.chain_id == 1
    assert report.hash == EVM_HASH
    assert codes(report)[0] is WarningCode.NETWORK_GUESSED
    assert routes.tron.call_count == 1


def test_not_found_on_both_networks_says_both_were_checked(routes: Routes) -> None:
    report = run(MISSING_HASH)
    assert report.status is Status.NOT_FOUND
    assert report.network is Network.TRON
    assert report.explorer_url
    assert report.message is not None
    assert "TRON" in report.message
    assert "Ethereum" in report.message
    assert "both" in report.message.lower()
    assert WarningCode.NETWORK_GUESSED not in codes(report)
    assert routes.tron.call_count == 1
    assert routes.evm.call_count == 1


def test_evm_error_during_the_fallback_is_not_reported_as_not_found(routes: Routes) -> None:
    routes.evm.mock(return_value=httpx.Response(503))
    report = run(MISSING_HASH)
    assert report.status is Status.PROVIDER_ERROR
    assert report.network is Network.EVM
    assert report.message is not None
    assert "Not found on TRON" in report.message


def test_prefixed_hash_goes_straight_to_evm(routes: Routes) -> None:
    routes.evm_finds()
    report = run(EVM_HASH)
    assert report.status is Status.SUCCESS
    assert codes(report) == []
    assert routes.tron.call_count == 0


def test_prefixed_hash_not_found_on_evm(routes: Routes) -> None:
    report = run("0x" + MISSING_HASH)
    assert report.status is Status.NOT_FOUND
    assert report.network is Network.EVM
    assert report.chain_id == 1
    assert report.explorer_url == f"https://etherscan.io/tx/0x{MISSING_HASH}"
    assert report.message is None
    assert routes.tron.call_count == 0


def test_forced_evm_takes_a_bare_hash_without_a_guess_warning(routes: Routes) -> None:
    routes.evm_finds()
    report = run(BARE_EVM_HASH, network=Network.EVM)
    assert report.network is Network.EVM
    assert report.hash == EVM_HASH
    assert WarningCode.NETWORK_GUESSED not in codes(report)
    assert routes.tron.call_count == 0


def test_forced_tron_does_not_fall_back(routes: Routes) -> None:
    report = run(MISSING_HASH, network=Network.TRON)
    assert report.status is Status.NOT_FOUND
    assert report.network is Network.TRON
    assert report.message is None
    assert routes.evm.call_count == 0


def test_forced_tron_rejects_a_prefixed_hash(routes: Routes) -> None:
    report = run(EVM_HASH, network=Network.TRON)
    assert report.status is Status.INVALID_INPUT
    assert routes.tron.call_count == 0


def test_chain_id_alone_means_evm(routes: Routes) -> None:
    routes.evm_finds()
    report = run(BARE_EVM_HASH, chain_id=137)
    assert report.network is Network.EVM
    assert report.chain_id == 137
    assert routes.evm.calls.last.request.url.params["chainid"] == "137"
    assert routes.tron.call_count == 0


def test_chain_id_with_tron_is_invalid(routes: Routes) -> None:
    report = run(MISSING_HASH, network=Network.TRON, chain_id=1)
    assert report.status is Status.INVALID_INPUT
    assert routes.tron.call_count == 0


@pytest.mark.parametrize("chain_id", [0, 999999])
def test_unknown_chain_id_is_invalid_input(routes: Routes, chain_id: int) -> None:
    report = run(EVM_HASH, chain_id=chain_id)
    assert report.status is Status.INVALID_INPUT
    assert report.message is not None
    assert "not supported" in report.message
    assert routes.evm.call_count == 0


def test_known_explorer_link_sets_the_chain(routes: Routes) -> None:
    routes.evm_finds()
    report = run(f"https://polygonscan.com/tx/{EVM_HASH}")
    assert report.chain_id == 137
    assert codes(report) == []


def test_unknown_explorer_link_uses_the_default_chain_with_a_warning(routes: Routes) -> None:
    routes.evm_finds()
    report = run(f"https://scan.example-chain.io/tx/{EVM_HASH}")
    assert report.chain_id == 1
    assert codes(report) == [WarningCode.NETWORK_UNCONFIRMED]
    assert "Ethereum was used" in report.warnings[0].text


def test_unknown_explorer_link_warning_also_rides_on_not_found(routes: Routes) -> None:
    report = run(f"https://scan.example-chain.io/tx/0x{MISSING_HASH}")
    assert report.status is Status.NOT_FOUND
    assert codes(report) == [WarningCode.NETWORK_UNCONFIRMED]


def test_explicit_chain_id_clears_the_unconfirmed_warning(routes: Routes) -> None:
    routes.evm_finds()
    report = run(f"https://scan.example-chain.io/tx/{EVM_HASH}", chain_id=42161)
    assert report.chain_id == 42161
    assert report.warnings == []


def test_bare_hash_uses_the_default_chain_without_a_warning(routes: Routes) -> None:
    routes.evm_finds()
    report = run(EVM_HASH)
    assert WarningCode.NETWORK_UNCONFIRMED not in codes(report)


def test_tronscan_link_goes_to_tron(routes: Routes) -> None:
    routes.tron_finds()
    report = run(f"https://tronscan.org/#/transaction/{TRON_HASH}")
    assert report.network is Network.TRON
    assert routes.evm.call_count == 0


def test_tron_provider_error_becomes_a_report_without_a_fallback(routes: Routes) -> None:
    routes.tron.mock(return_value=httpx.Response(503))
    report = run(MISSING_HASH)
    assert report.status is Status.PROVIDER_ERROR
    assert report.network is Network.TRON
    assert report.message
    assert report.explorer_url
    assert routes.evm.call_count == 0


def test_tron_rate_limit_becomes_a_report(routes: Routes) -> None:
    routes.tron.mock(return_value=httpx.Response(429))
    report = run(MISSING_HASH)
    assert report.status is Status.PROVIDER_ERROR
    assert report.message is not None
    assert "rate limit" in report.message.lower()


def test_evm_provider_error_becomes_a_report(routes: Routes) -> None:
    body = {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}
    routes.evm.mock(return_value=httpx.Response(200, json=body))
    report = run(EVM_HASH)
    assert report.status is Status.PROVIDER_ERROR
    assert report.network is Network.EVM
    assert report.chain_id == 1
    assert report.message is not None
    assert "rate limit" in report.message.lower()
    assert report.explorer_url


def test_unsupported_chain_is_a_provider_error_report(routes: Routes) -> None:
    body = json.loads((FIXTURES / "evm" / "provider_error_unsupported_chain.json").read_text())
    routes.evm.mock(
        return_value=httpx.Response(200, json=body["responses"]["eth_getTransactionByHash"])
    )
    report = run(EVM_HASH, chain_id=56)
    assert report.status is Status.PROVIDER_ERROR
    assert report.chain_id == 56
    assert report.message is not None
    assert "not supported" in report.message


def test_provider_error_report_never_contains_the_key(routes: Routes) -> None:
    message = f"failed for {EVM_URL}?apikey={KEY}"
    routes.evm.mock(side_effect=httpx.ConnectError(message))
    report = run(EVM_HASH)
    assert report.status is Status.PROVIDER_ERROR
    assert KEY not in json.dumps(report.to_dict())


def test_provider_errors_are_logged_without_the_key(
    routes: Routes, caplog: pytest.LogCaptureFixture
) -> None:
    routes.evm.mock(side_effect=httpx.ConnectError(f"{EVM_URL}?apikey={KEY}"))
    with caplog.at_level("DEBUG"):
        run(EVM_HASH)
    assert KEY not in caplog.text


def test_required_confirmations_override_reaches_both_analyzers(routes: Routes) -> None:
    routes.tron_finds()
    assert run(TRON_HASH, required_confirmations=10**9).is_final is False
    routes.evm_finds()
    assert run(EVM_HASH, required_confirmations=10**9).is_final is False
    assert run(EVM_HASH, required_confirmations=1).is_final is True


def test_missing_key_is_raised_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_dotenv", lambda: False)
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.delenv("TRONSCAN_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match=r"\.env\.example"):
        check(EVM_HASH)
    with pytest.raises(MissingApiKeyError, match=r"TRONSCAN_API_KEY"):
        check(MISSING_HASH, network=Network.TRON)


def test_tron_lookup_does_not_need_the_etherscan_key(
    monkeypatch: pytest.MonkeyPatch, routes: Routes
) -> None:
    routes.tron_finds()
    monkeypatch.setattr(config, "load_dotenv", lambda: False)
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    monkeypatch.setenv("TRONSCAN_API_KEY", KEY)
    assert check(TRON_HASH).status is Status.SUCCESS


@pytest.fixture
def no_etherscan_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_dotenv", lambda: False)
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)


def tron_only(raw: str, **kwargs: Any) -> TxReport:
    tronscan = TronscanClient(KEY, http=httpx.Client(), sleep=lambda _: None)
    return check(raw, tronscan=tronscan, **kwargs)


def test_missing_etherscan_key_softens_only_the_fallback(
    routes: Routes, no_etherscan_key: None
) -> None:
    report = tron_only(MISSING_HASH)
    assert report.status is Status.NOT_FOUND
    assert report.network is Network.TRON
    assert report.message == (
        "Not found on TRON. Ethereum was not checked because no Etherscan key is configured."
    )
    assert routes.tron.call_count == 1
    assert routes.evm.call_count == 0


def test_fallback_without_the_key_still_finds_a_tron_transaction(
    routes: Routes, no_etherscan_key: None
) -> None:
    routes.tron_finds()
    assert tron_only(TRON_HASH).status is Status.SUCCESS


@pytest.mark.parametrize(
    ("raw", "kwargs"),
    [
        (EVM_HASH, {}),
        (f"https://etherscan.io/tx/{EVM_HASH}", {}),
        (BARE_EVM_HASH, {"network": Network.EVM}),
        (BARE_EVM_HASH, {"chain_id": 137}),
    ],
)
def test_missing_etherscan_key_still_raises_when_evm_was_requested(
    routes: Routes, no_etherscan_key: None, raw: str, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(MissingApiKeyError, match="ETHERSCAN_API_KEY"):
        tron_only(raw, **kwargs)
    assert routes.evm.call_count == 0
