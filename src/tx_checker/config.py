import os
from dataclasses import dataclass

from dotenv import load_dotenv

from tx_checker.models import Network

DEFAULT_CHAIN_ID = 1


@dataclass(frozen=True)
class Chain:
    chain_id: int
    name: str
    native_symbol: str
    explorer_host: str
    # Example values only. Every exchange and payment provider sets its own.
    required_confirmations: int
    # Whether the free Etherscan plan serves this chain. The public demo offers only these.
    free_tier: bool = True
    testnet: bool = False

    def tx_url(self, tx_hash: str) -> str:
        return f"https://{self.explorer_host}/tx/{tx_hash}"


CHAINS: dict[int, Chain] = {
    chain.chain_id: chain
    for chain in (
        Chain(1, "Ethereum", "ETH", "etherscan.io", 12),
        Chain(11155111, "Sepolia (testnet)", "ETH", "sepolia.etherscan.io", 12, testnet=True),
        Chain(56, "BNB Smart Chain", "BNB", "bscscan.com", 15, free_tier=False),
        Chain(137, "Polygon", "POL", "polygonscan.com", 128),
        Chain(42161, "Arbitrum One", "ETH", "arbiscan.io", 12),
        Chain(10, "OP Mainnet", "ETH", "optimistic.etherscan.io", 12, free_tier=False),
        Chain(8453, "Base", "ETH", "basescan.org", 12, free_tier=False),
    )
}

DEMO_CHAIN_IDS: tuple[int, ...] = tuple(c.chain_id for c in CHAINS.values() if c.free_tier)

# Public demo protection. Example values, tuned for free API keys.
SESSION_LOOKUPS_PER_MINUTE = 10
# Provider calls, counted separately for Etherscan and Tronscan so one cannot starve the other.
PROVIDER_CALLS_PER_MINUTE = 60
PROVIDER_CALLS_PER_DAY = 5000
# Worst case per lookup. A mined EVM transaction needs up to five Etherscan calls.
EVM_CALLS_PER_LOOKUP = 5
TRON_CALLS_PER_LOOKUP = 1
CACHE_SECONDS_SETTLED = 600
CACHE_SECONDS_WAITING = 60
CACHE_SECONDS_NOT_FOUND = 30
CACHE_MAX_ENTRIES = 500

# Explorer host -> (network, chain id). A known host tells us the network and the chain.
EXPLORER_HOSTS: dict[str, tuple[Network, int | None]] = {
    **{chain.explorer_host: (Network.EVM, chain.chain_id) for chain in CHAINS.values()},
    "tronscan.org": (Network.TRON, None),
}


class UnknownChainError(LookupError):
    """The chain id is not in the CHAINS table."""


def get_chain(chain_id: int) -> Chain:
    try:
        return CHAINS[chain_id]
    except KeyError:
        supported = ", ".join(str(c) for c in CHAINS)
        raise UnknownChainError(
            f"Chain {chain_id} is not supported. Supported chain ids: {supported}."
        ) from None


@dataclass(frozen=True)
class Token:
    symbol: str
    decimals: int
    # An older version of a token that is still held and sent.
    legacy: bool = False
    # The symbol the explorer shows, when it differs from ours.
    explorer_symbol: str | None = None
    # The ticker this token counts as when it differs from the symbol (USDT0 is USDT).
    asset: str | None = None

    @property
    def ticker(self) -> str:
        return self.asset or self.symbol


# Only entries checked against the issuer docs and the explorer token page are listed.
# Contracts are keyed by lowercase address. Bridged variants (for example USDC.e) are left
# out on purpose, they show up as unknown tokens with a raw amount.
_TOKEN_ROWS: tuple[tuple[int, str, Token], ...] = (
    (1, "0xdAC17F958D2ee523a2206206994597C13D831ec7", Token("USDT", 6)),
    (1, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", Token("USDC", 6)),
    # Polygon: addresses from the Circle and USDT0 docs, decimals and symbol read on-chain.
    (137, "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", Token("USDT0", 6, asset="USDT")),
    (137, "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", Token("USDC", 6)),
    (42161, "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", Token("USDC", 6)),
    (42161, "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", Token("USD₮0", 6, asset="USDT")),
    (10, "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", Token("USDC", 6)),
    (8453, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", Token("USDC", 6)),
    # BNB Smart Chain: these Binance-Peg tokens use 18 decimals, not 6 like on Ethereum.
    (
        56,
        "0x55d398326f99059fF775485246999027B3197955",
        Token("USDT", 18, explorer_symbol="BSC-USD"),
    ),
    (56, "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", Token("USDC", 18)),
)

TOKENS: dict[tuple[int, str], Token] = {
    (chain_id, address.lower()): token for chain_id, address, token in _TOKEN_ROWS
}


def lookup_token(chain_id: int, contract: str) -> Token | None:
    return TOKENS.get((chain_id, contract.lower()))


TRON_NAME = "TRON"
TRON_SYMBOL = "TRX"
# Example value only. Every exchange and payment provider sets its own. 19 is roughly where
# TRON considers a block solidified.
TRON_REQUIRED_CONFIRMATIONS = 19


def tron_tx_url(tx_hash: str) -> str:
    return f"https://tronscan.org/#/transaction/{tx_hash}"


# TRON addresses are case-sensitive base58, so the keys keep their case.
# USDT: address from the Tether supported-protocols page, decimals from Tronscan token data.
# USDC: Circle's contract list has no TRON entry. The old contract is still in use. Tronscan
# names it "USD Coin Old" with the symbol USDCOLD and 6 decimals, so it is listed as legacy.
TRON_TOKENS: dict[str, Token] = {
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": Token("USDT", 6),
    "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8": Token(
        "USDC (legacy)", 6, legacy=True, explorer_symbol="USDCOLD", asset="USDC"
    ),
}

# Symbols scammers copy. A token that uses one of them without being the registered
# contract is flagged as a lookalike.
PROTECTED_SYMBOLS = frozenset({"USDT", "USDC"})


def lookup_tron_token(contract: str) -> Token | None:
    return TRON_TOKENS.get(contract)


class MissingApiKeyError(RuntimeError):
    """A required API key is not configured."""


def _load_key(name: str) -> str:
    load_dotenv()
    key = os.environ.get(name, "").strip()
    if not key:
        raise MissingApiKeyError(f"{name} is not set. Copy .env.example to .env and add your key.")
    return key


def load_etherscan_key() -> str:
    """Return ETHERSCAN_API_KEY from the environment or a local .env file."""
    return _load_key("ETHERSCAN_API_KEY")


def load_tronscan_key() -> str:
    """Return TRONSCAN_API_KEY from the environment or a local .env file."""
    return _load_key("TRONSCAN_API_KEY")
