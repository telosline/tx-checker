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

    def tx_url(self, tx_hash: str) -> str:
        return f"https://{self.explorer_host}/tx/{tx_hash}"


CHAINS: dict[int, Chain] = {
    chain.chain_id: chain
    for chain in (
        Chain(1, "Ethereum", "ETH", "etherscan.io", 12),
        Chain(11155111, "Sepolia (testnet)", "ETH", "sepolia.etherscan.io", 12),
        Chain(56, "BNB Smart Chain", "BNB", "bscscan.com", 15),
        Chain(137, "Polygon", "POL", "polygonscan.com", 128),
        Chain(42161, "Arbitrum One", "ETH", "arbiscan.io", 12),
        Chain(10, "OP Mainnet", "ETH", "optimistic.etherscan.io", 12),
        Chain(8453, "Base", "ETH", "basescan.org", 12),
    )
}

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


# Only entries checked against the issuer docs and the explorer token page are listed.
# Contracts are keyed by lowercase address. Bridged variants (for example USDC.e) are left
# out on purpose, they show up as unknown tokens with a raw amount.
_TOKEN_ROWS: tuple[tuple[int, str, str, int], ...] = (
    (1, "0xdAC17F958D2ee523a2206206994597C13D831ec7", "USDT", 6),
    (1, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "USDC", 6),
    # Polygon: addresses from the Circle and USDT0 docs, decimals and symbol read on-chain.
    (137, "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", "USDT0", 6),
    (137, "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "USDC", 6),
    (42161, "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "USDC", 6),
    (42161, "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "USD₮0", 6),
    (10, "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "USDC", 6),
    (8453, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC", 6),
)

TOKENS: dict[tuple[int, str], Token] = {
    (chain_id, address.lower()): Token(symbol, decimals)
    for chain_id, address, symbol, decimals in _TOKEN_ROWS
}


def lookup_token(chain_id: int, contract: str) -> Token | None:
    return TOKENS.get((chain_id, contract.lower()))


class MissingApiKeyError(RuntimeError):
    """A required API key is not configured."""


def load_etherscan_key() -> str:
    """Return ETHERSCAN_API_KEY from the environment or a local .env file."""
    load_dotenv()
    key = os.environ.get("ETHERSCAN_API_KEY", "").strip()
    if not key:
        raise MissingApiKeyError(
            "ETHERSCAN_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return key
