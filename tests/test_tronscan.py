import httpx
import pytest
import respx

from tx_checker import config
from tx_checker.config import MissingApiKeyError, load_tronscan_key
from tx_checker.providers import ProviderError, RateLimitedError
from tx_checker.providers.tronscan import BASE_URL, KEY_HEADER, TronscanClient

KEY = "TESTKEY123SECRET"
HASH = "ab12cd34" * 8
URL = f"{BASE_URL}/transaction-info"
INFO = {"hash": HASH, "block": 1, "contractRet": "SUCCESS"}


def make_client(**kwargs: object) -> TronscanClient:
    return TronscanClient(KEY, http=httpx.Client(), sleep=lambda _: None, **kwargs)  # type: ignore[arg-type]


def assert_no_key(error: BaseException) -> None:
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        assert KEY not in str(current)
        current = current.__cause__ or current.__context__


@respx.mock
def test_fetch_returns_the_transaction_info() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=INFO))
    assert make_client().fetch(HASH) == INFO


@respx.mock
def test_empty_object_means_not_found() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={}))
    assert make_client().fetch(HASH) is None


@respx.mock
def test_key_goes_in_the_header_never_in_the_url() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=INFO))
    make_client().fetch(HASH)
    request = route.calls.last.request
    assert request.headers[KEY_HEADER] == KEY
    assert request.url.params["hash"] == HASH
    assert KEY not in str(request.url)


@respx.mock
def test_retries_429_then_succeeds() -> None:
    route = respx.get(URL).mock(side_effect=[httpx.Response(429), httpx.Response(200, json=INFO)])
    assert make_client().fetch(HASH) == INFO
    assert route.call_count == 2


@respx.mock
def test_gives_up_after_repeated_429() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitedError):
        make_client(retries=2).fetch(HASH)
    assert route.call_count == 3


@respx.mock
def test_retries_5xx() -> None:
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=INFO)])
    assert make_client().fetch(HASH) == INFO
    assert route.call_count == 2


@respx.mock
def test_400_is_not_retried() -> None:
    body = {"message": "hash must be a 64-character hex transaction hash"}
    route = respx.get(URL).mock(return_value=httpx.Response(400, json=body))
    with pytest.raises(ProviderError, match="HTTP 400"):
        make_client().fetch(HASH)
    assert route.call_count == 1


@respx.mock
def test_limit_message_in_body_is_rate_limited() -> None:
    body = {"message": "Daily request limit reached"}
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RateLimitedError, match="limit reached"):
        make_client(retries=0).fetch(HASH)


@respx.mock
def test_other_message_without_a_hash_is_an_error() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"message": "Invalid api key"}))
    with pytest.raises(ProviderError, match="Invalid api key"):
        make_client().fetch(HASH)


@respx.mock
def test_unexpected_body_shape() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=["oops"]))
    with pytest.raises(ProviderError, match="unexpected"):
        make_client().fetch(HASH)


@respx.mock
def test_unreadable_body() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ProviderError, match="unreadable"):
        make_client().fetch(HASH)


@respx.mock
def test_connection_error_never_leaks_the_key() -> None:
    message = f"failed for {URL}?hash={HASH} with {KEY_HEADER}: {KEY}"
    respx.get(URL).mock(side_effect=httpx.ConnectError(message))
    with pytest.raises(ProviderError) as info:
        make_client().fetch(HASH)
    assert_no_key(info.value)


def test_missing_key_points_to_env_example(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_dotenv", lambda: False)
    monkeypatch.delenv("TRONSCAN_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match=r"TRONSCAN_API_KEY.*\.env\.example"):
        load_tronscan_key()


def test_key_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "load_dotenv", lambda: False)
    monkeypatch.setenv("TRONSCAN_API_KEY", f"  {KEY} ")
    assert load_tronscan_key() == KEY
