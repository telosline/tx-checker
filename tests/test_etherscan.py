import httpx
import pytest
import respx

from tx_checker import config
from tx_checker.config import MissingApiKeyError, UnknownChainError, get_chain, load_etherscan_key
from tx_checker.providers import ProviderError, RateLimitedError, UnsupportedChainError
from tx_checker.providers.etherscan import BASE_URL, EtherscanClient

KEY = "TESTKEY123SECRET"
HASH = "0x" + "ab12cd34" * 8


def make_client(**kwargs: object) -> EtherscanClient:
    return EtherscanClient(KEY, http=httpx.Client(), sleep=lambda _: None, **kwargs)  # type: ignore[arg-type]


def rpc(result: object) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def assert_no_key(error: BaseException) -> None:
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        assert KEY not in str(current)
        current = current.__cause__ or current.__context__


@respx.mock
def test_fetch_not_found_returns_none() -> None:
    respx.get(BASE_URL).mock(return_value=rpc(None))
    assert make_client().fetch(HASH, 1) is None


@respx.mock
def test_fetch_pending_makes_a_single_call() -> None:
    route = respx.get(BASE_URL).mock(return_value=rpc({"hash": HASH, "blockNumber": None}))
    raw = make_client().fetch(HASH, 1)
    assert raw is not None
    assert raw.receipt is None
    assert route.call_count == 1


@respx.mock
def test_fetch_mined_collects_all_parts() -> None:
    responses = {
        "eth_getTransactionByHash": {"hash": HASH, "blockNumber": "0x10", "to": "0xabc"},
        "eth_getTransactionReceipt": {"status": "0x1"},
        "eth_blockNumber": "0x20",
        "eth_getBlockByNumber": {"timestamp": "0x1"},
        "eth_getCode": "0x",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return rpc(responses[request.url.params["action"]])

    respx.get(BASE_URL).mock(side_effect=handler)
    raw = make_client().fetch(HASH, 137)
    assert raw is not None
    assert raw.receipt == {"status": "0x1"}
    assert raw.latest_block == "0x20"
    assert raw.block == {"timestamp": "0x1"}
    assert raw.to_code == "0x"


@respx.mock
def test_contract_creation_skips_code_lookup() -> None:
    responses = {
        "eth_getTransactionByHash": {"hash": HASH, "blockNumber": "0x10", "to": None},
        "eth_getTransactionReceipt": {"status": "0x1"},
        "eth_blockNumber": "0x20",
        "eth_getBlockByNumber": {"timestamp": "0x1"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return rpc(responses[request.url.params["action"]])

    respx.get(BASE_URL).mock(side_effect=handler)
    raw = make_client().fetch(HASH, 1)
    assert raw is not None
    assert raw.to_code is None


@respx.mock
def test_request_carries_chain_id_and_key() -> None:
    route = respx.get(BASE_URL).mock(return_value=rpc(None))
    make_client().fetch(HASH, 42161)
    params = route.calls.last.request.url.params
    assert params["chainid"] == "42161"
    assert params["apikey"] == KEY
    assert params["module"] == "proxy"


@respx.mock
def test_retries_429_then_succeeds() -> None:
    route = respx.get(BASE_URL).mock(side_effect=[httpx.Response(429), rpc(None)])
    assert make_client().fetch(HASH, 1) is None
    assert route.call_count == 2


@respx.mock
def test_every_attempt_is_reported_including_retries() -> None:
    respx.get(BASE_URL).mock(side_effect=[httpx.Response(429), httpx.Response(500), rpc(None)])
    seen: list[str] = []
    assert make_client(on_request=seen.append).fetch(HASH, 1) is None
    assert seen == ["etherscan"] * 3


@respx.mock
def test_gives_up_after_repeated_429() -> None:
    route = respx.get(BASE_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitedError):
        make_client(retries=2).fetch(HASH, 1)
    assert route.call_count == 3


@respx.mock
def test_retries_5xx() -> None:
    route = respx.get(BASE_URL).mock(side_effect=[httpx.Response(503), rpc(None)])
    assert make_client().fetch(HASH, 1) is None
    assert route.call_count == 2


@respx.mock
def test_other_4xx_is_not_retried() -> None:
    route = respx.get(BASE_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(ProviderError):
        make_client().fetch(HASH, 1)
    assert route.call_count == 1


@respx.mock
def test_limit_message_in_body_is_rate_limited() -> None:
    body = {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RateLimitedError):
        make_client(retries=0).fetch(HASH, 1)


@respx.mock
def test_unsupported_chain_message() -> None:
    body = {
        "status": "0",
        "message": "NOTOK",
        "result": "Free API access is not supported for this chain. Please upgrade your api plan.",
    }
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(UnsupportedChainError):
        make_client().fetch(HASH, 56)


@respx.mock
def test_json_rpc_error_object() -> None:
    body = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid argument"}}
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(ProviderError, match="invalid argument"):
        make_client().fetch(HASH, 1)


@respx.mock
def test_unreadable_body() -> None:
    respx.get(BASE_URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ProviderError, match="unreadable"):
        make_client().fetch(HASH, 1)


@respx.mock
def test_connection_error_never_leaks_the_key() -> None:
    message = f"failed for {BASE_URL}?chainid=1&apikey={KEY}"
    respx.get(BASE_URL).mock(side_effect=httpx.ConnectError(message))
    with pytest.raises(ProviderError) as info:
        make_client().fetch(HASH, 1)
    assert_no_key(info.value)


def test_provider_error_redacts_its_message() -> None:
    error = ProviderError(f"bad url {BASE_URL}?apikey={KEY}&x=1")
    assert KEY not in str(error)
    assert "apikey=***" in str(error)


def test_missing_key_points_to_env_example(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_dotenv", lambda: False)
    monkeypatch.delenv("ETHERSCAN_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match=r"\.env\.example"):
        load_etherscan_key()


def test_key_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_dotenv", lambda: False)
    monkeypatch.setenv("ETHERSCAN_API_KEY", f"  {KEY} ")
    assert load_etherscan_key() == KEY


def test_unknown_chain_lists_supported_ids() -> None:
    with pytest.raises(UnknownChainError, match="42161"):
        get_chain(999999)
