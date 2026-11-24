from tx_checker.config import CHAINS, TOKENS, get_chain, lookup_token

ETH_USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"


def test_tx_url() -> None:
    assert get_chain(1).tx_url("0xabc") == "https://etherscan.io/tx/0xabc"


def test_token_lookup_ignores_address_case() -> None:
    upper = lookup_token(1, ETH_USDT)
    lower = lookup_token(1, ETH_USDT.lower())
    assert upper is not None
    assert upper == lower
    assert upper.symbol == "USDT"
    assert upper.decimals == 6


def test_same_contract_on_another_chain_is_unknown() -> None:
    assert lookup_token(137, ETH_USDT) is None


def test_every_token_row_points_at_a_configured_chain() -> None:
    assert {chain_id for chain_id, _ in TOKENS} <= set(CHAINS)
