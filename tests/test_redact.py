import pytest

from tx_checker.redact import redact

BASE = "https://api.etherscan.io/v2/api"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{BASE}?apikey=SECRET123&chainid=1", f"{BASE}?apikey=***&chainid=1"),
        (
            f"{BASE}?chainid=1&apikey=SECRET123&module=proxy",
            f"{BASE}?chainid=1&apikey=***&module=proxy",
        ),
        (f"{BASE}?chainid=1&apikey=SECRET123", f"{BASE}?chainid=1&apikey=***"),
        (f"{BASE}?chainid=1&APIKEY=SECRET123", f"{BASE}?chainid=1&APIKEY=***"),
        (f"{BASE}?chainid=1&ApiKey=SECRET123", f"{BASE}?chainid=1&ApiKey=***"),
        (f"{BASE}?apikey=A%2Fb%3D&chainid=1", f"{BASE}?apikey=***&chainid=1"),
        (f"{BASE}?apikey=SECRET123#frag", f"{BASE}?apikey=***#frag"),
    ],
)
def test_redacts_key_in_url(url: str, expected: str) -> None:
    assert redact(url) == expected


def test_redacts_multiple_occurrences() -> None:
    text = f"{BASE}?apikey=ONE and again {BASE}?apikey=TWO&x=1"
    result = redact(text)
    assert "ONE" not in result
    assert "TWO" not in result
    assert result.count("apikey=***") == 2


def test_redacts_url_inside_exception_message() -> None:
    err = RuntimeError(f"Client error for url '{BASE}?chainid=1&apikey=SECRET123'")
    result = redact(str(err))
    assert "SECRET123" not in result
    assert "apikey=***'" in result


def test_leaves_other_params_untouched() -> None:
    url = f"{BASE}?chainid=1&module=proxy&action=eth_blockNumber"
    assert redact(url) == url


def test_text_without_key_is_unchanged() -> None:
    assert redact("nothing to hide here") == "nothing to hide here"


def test_empty_key_value() -> None:
    assert redact(f"{BASE}?apikey=&chainid=1") == f"{BASE}?apikey=***&chainid=1"


def test_is_idempotent() -> None:
    once = redact(f"{BASE}?apikey=SECRET123")
    assert redact(once) == once
