import re
from dataclasses import dataclass
from urllib.parse import urlparse

from tx_checker.models import Network

_HEX = "0-9a-fA-F"
_BASE58 = "1-9A-HJ-NP-Za-km-z"

# Optional 0x prefix, 64 hex chars, not part of a longer hex run.
_HASH_RE = re.compile(rf"(?<![{_HEX}])(0[xX])?([{_HEX}]{{64}})(?![{_HEX}])")
_EVM_ADDRESS_RE = re.compile(rf"(?<![{_HEX}])(?:0[xX])?[{_HEX}]{{40}}(?![{_HEX}])")
# Format check only: base58, starts with T, 34 chars. The checksum is not verified.
_TRON_ADDRESS_RE = re.compile(rf"(?<![{_BASE58}])T[{_BASE58}]{{33}}(?![{_BASE58}])")

# Hosts that tell us the network. Only mainnet Etherscan implies chain 1.
_EXPLORER_HOSTS: dict[str, tuple[Network, int | None]] = {
    "etherscan.io": (Network.EVM, 1),
    "tronscan.org": (Network.TRON, None),
}

MSG_EMPTY = "Please paste a transaction hash or an explorer link."
MSG_ADDRESS = (
    "This looks like a wallet address, not a transaction hash. "
    "Please paste the transaction hash instead."
)
MSG_NO_HASH_IN_URL = "No transaction hash was found in this link."
MSG_NOT_HEX = (
    "A transaction hash contains only the characters 0-9 and a-f, "
    "but this input has other characters."
)
MSG_TRON_PREFIX = (
    "This hash starts with 0x, which TRON hashes do not use. "
    "It looks like an EVM transaction, so check the selected network."
)


@dataclass(frozen=True)
class Detection:
    network: Network | None = None
    tx_hash: str | None = None
    guessed: bool = False
    chain_id: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def detect(raw: str, network: Network | None = None) -> Detection:
    """Extract a normalized hash from a bare hash or explorer URL and pick the network."""
    text = raw.strip()
    if not text:
        return _invalid(MSG_EMPTY)

    is_url = "/" in text
    match = _find(_HASH_RE, text, is_url)
    if match is None:
        return _invalid(_explain_invalid(text, is_url))

    hint, chain_id = _explorer_hint(text) if is_url else (None, None)
    effective = network if network is not None else hint
    return _resolve(match.group(1) is not None, match.group(2), effective, chain_id)


def _resolve(
    prefixed: bool, digits: str, network: Network | None, chain_id: int | None
) -> Detection:
    digits = digits.lower()
    if network is Network.TRON:
        if prefixed:
            return _invalid(MSG_TRON_PREFIX)
        return Detection(network=Network.TRON, tx_hash=digits)
    if network is Network.EVM or prefixed:
        return Detection(network=Network.EVM, tx_hash=f"0x{digits}", chain_id=chain_id)
    # Bare 64 hex with no hint: TRON first, the caller may retry as EVM.
    return Detection(network=Network.TRON, tx_hash=digits, guessed=True)


def _find(pattern: re.Pattern[str], text: str, is_url: bool) -> re.Match[str] | None:
    return pattern.search(text) if is_url else pattern.fullmatch(text)


def _explorer_hint(text: str) -> tuple[Network | None, int | None]:
    try:
        host = urlparse(text if "://" in text else f"https://{text}").hostname or ""
    except ValueError:
        return None, None
    host = host.removeprefix("www.")
    return _EXPLORER_HOSTS.get(host, (None, None))


def _explain_invalid(text: str, is_url: bool) -> str:
    if _find(_EVM_ADDRESS_RE, text, is_url) or _find(_TRON_ADDRESS_RE, text, is_url):
        return MSG_ADDRESS
    if is_url:
        return MSG_NO_HASH_IN_URL
    digits = text[2:] if text[:2].lower() == "0x" else text
    if re.fullmatch(rf"[{_HEX}]*", digits):
        return f"A transaction hash has 64 hex characters, but this input has {len(digits)}."
    return MSG_NOT_HEX


def _invalid(message: str) -> Detection:
    return Detection(error=message)
