import pytest

from tx_checker.detect import MSG_ADDRESS, MSG_NO_HASH_IN_URL, MSG_TRON_PREFIX, detect
from tx_checker.models import Network

DIGITS = "ab12cd34" * 8
EVM_HASH = f"0x{DIGITS}"
TRON_HASH = DIGITS

# Public token contract addresses, used here only as sample address-shaped input.
EVM_ADDRESS = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
TRON_ADDRESS = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def test_evm_hash() -> None:
    result = detect(EVM_HASH)
    assert result.ok
    assert result.network is Network.EVM
    assert result.tx_hash == EVM_HASH
    assert not result.guessed
    assert result.chain_id is None


def test_tron_hash_is_a_guess() -> None:
    result = detect(TRON_HASH)
    assert result.ok
    assert result.network is Network.TRON
    assert result.tx_hash == TRON_HASH
    assert result.guessed


def test_trims_whitespace() -> None:
    assert detect(f"  \n{EVM_HASH}\t ").tx_hash == EVM_HASH


@pytest.mark.parametrize("prefix", ["0x", "0X"])
def test_mixed_case_is_lowercased(prefix: str) -> None:
    result = detect(prefix + DIGITS.upper())
    assert result.network is Network.EVM
    assert result.tx_hash == EVM_HASH
    assert detect(DIGITS.upper()).tx_hash == TRON_HASH


@pytest.mark.parametrize(
    "url",
    [
        f"https://etherscan.io/tx/{EVM_HASH}",
        f"http://www.etherscan.io/tx/{EVM_HASH}?foo=bar",
        f"etherscan.io/tx/{EVM_HASH}",
    ],
)
def test_etherscan_url(url: str) -> None:
    result = detect(url)
    assert result.network is Network.EVM
    assert result.tx_hash == EVM_HASH
    assert result.chain_id == 1
    assert not result.guessed


@pytest.mark.parametrize(
    "url",
    [
        f"https://tronscan.org/#/transaction/{TRON_HASH}",
        f"https://www.tronscan.org/#/transaction/{TRON_HASH}",
    ],
)
def test_tronscan_url(url: str) -> None:
    result = detect(url)
    assert result.network is Network.TRON
    assert result.tx_hash == TRON_HASH
    assert not result.guessed


def test_etherscan_url_without_prefix_is_still_evm() -> None:
    result = detect(f"https://etherscan.io/tx/{DIGITS}")
    assert result.network is Network.EVM
    assert result.tx_hash == EVM_HASH


@pytest.mark.parametrize(
    ("host", "chain_id"),
    [
        ("etherscan.io", 1),
        ("sepolia.etherscan.io", 11155111),
        ("bscscan.com", 56),
        ("polygonscan.com", 137),
        ("arbiscan.io", 42161),
        ("optimistic.etherscan.io", 10),
        ("basescan.org", 8453),
    ],
)
def test_known_explorer_maps_to_chain(host: str, chain_id: int) -> None:
    result = detect(f"https://{host}/tx/{EVM_HASH}")
    assert result.network is Network.EVM
    assert result.chain_id == chain_id
    assert not result.chain_unconfirmed


def test_unknown_explorer_link_is_unconfirmed() -> None:
    result = detect(f"https://scan.example.org/tx/{EVM_HASH}")
    assert result.network is Network.EVM
    assert result.chain_id is None
    assert result.chain_unconfirmed


def test_bare_evm_hash_is_not_unconfirmed() -> None:
    result = detect(EVM_HASH)
    assert result.chain_id is None
    assert not result.chain_unconfirmed


def test_unknown_host_with_bare_hash_is_a_guess() -> None:
    result = detect(f"https://example.org/tx/{TRON_HASH}")
    assert result.network is Network.TRON
    assert result.guessed


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "hello",
        "0x1234",
        "ab" * 31,
        "ab" * 33,
        "0x" + "ab" * 33,
        "g" * 64,
        "0x" + "zz" * 32,
        "0x",
    ],
)
def test_invalid_input(text: str) -> None:
    result = detect(text)
    assert not result.ok
    assert result.error
    assert result.network is None
    assert result.tx_hash is None


def test_wrong_length_message_reports_length() -> None:
    assert "62" in (detect("ab" * 31).error or "")


@pytest.mark.parametrize(
    "text",
    [
        EVM_ADDRESS,
        EVM_ADDRESS.lower(),
        EVM_ADDRESS[2:],
        TRON_ADDRESS,
        f"  {TRON_ADDRESS}  ",
        f"https://etherscan.io/address/{EVM_ADDRESS}",
        f"https://tronscan.org/#/address/{TRON_ADDRESS}",
    ],
)
def test_wallet_address_is_called_out(text: str) -> None:
    result = detect(text)
    assert not result.ok
    assert result.error == MSG_ADDRESS


def test_near_miss_tron_address_is_not_called_an_address() -> None:
    result = detect(TRON_ADDRESS[:-1])
    assert not result.ok
    assert result.error != MSG_ADDRESS


def test_url_without_hash() -> None:
    result = detect("https://etherscan.io/tx/")
    assert result.error == MSG_NO_HASH_IN_URL


def test_forced_evm_adds_prefix() -> None:
    result = detect(TRON_HASH, network=Network.EVM)
    assert result.network is Network.EVM
    assert result.tx_hash == EVM_HASH
    assert not result.guessed


def test_forced_tron_accepts_bare_hash() -> None:
    result = detect(TRON_HASH, network=Network.TRON)
    assert result.network is Network.TRON
    assert result.tx_hash == TRON_HASH
    assert not result.guessed


def test_forced_tron_rejects_prefixed_hash() -> None:
    result = detect(EVM_HASH, network=Network.TRON)
    assert result.error == MSG_TRON_PREFIX


def test_forced_network_does_not_rescue_garbage() -> None:
    assert not detect("hello", network=Network.EVM).ok
    assert not detect(EVM_ADDRESS, network=Network.TRON).ok
