from dataclasses import dataclass
from typing import Any

from tx_checker.providers import ProviderError, RateLimitedError, UnsupportedChainError
from tx_checker.providers._http import JsonApiClient

BASE_URL = "https://api.etherscan.io/v2/api"


@dataclass(frozen=True)
class EvmRawData:
    """Raw proxy responses for one transaction. Pending transactions only have `transaction`."""

    transaction: dict[str, Any]
    receipt: dict[str, Any] | None = None
    latest_block: str | None = None
    block: dict[str, Any] | None = None
    to_code: str | None = None


class EtherscanClient(JsonApiClient):
    provider = "Etherscan"

    def __init__(self, api_key: str, **options: Any) -> None:
        super().__init__(**options)
        self._api_key = api_key

    def fetch(self, tx_hash: str, chain_id: int) -> EvmRawData | None:
        """Collect everything the analyzer needs. Returns None when the hash is unknown."""
        tx = self._proxy(chain_id, "eth_getTransactionByHash", txhash=tx_hash)
        if tx is None:
            return None
        if not isinstance(tx, dict):
            raise ProviderError("Etherscan returned an unexpected transaction response.")
        if tx.get("blockNumber") is None:
            return EvmRawData(transaction=tx)

        receipt = self._proxy(chain_id, "eth_getTransactionReceipt", txhash=tx_hash)
        if not isinstance(receipt, dict):
            raise ProviderError("The transaction receipt is not available yet. Try again shortly.")
        latest = self._proxy(chain_id, "eth_blockNumber")
        block = self._proxy(
            chain_id, "eth_getBlockByNumber", tag=tx["blockNumber"], boolean="false"
        )
        to = tx.get("to")
        code = self._proxy(chain_id, "eth_getCode", address=to, tag="latest") if to else None
        return EvmRawData(
            transaction=tx,
            receipt=receipt,
            latest_block=latest if isinstance(latest, str) else None,
            block=block if isinstance(block, dict) else None,
            to_code=code if isinstance(code, str) else None,
        )

    def _proxy(self, chain_id: int, action: str, **params: str) -> Any:
        return self._call(chain_id, module="proxy", action=action, **params)

    def _call(self, chain_id: int, **params: str) -> Any:
        query = {"chainid": str(chain_id), **params, "apikey": self._api_key}
        return self._get_json(BASE_URL, query, self._unwrap)

    @staticmethod
    def _unwrap(body: Any) -> tuple[Any, ProviderError | None]:
        """Handle both the JSON-RPC shape and the status/message/result shape."""
        if not isinstance(body, dict):
            raise ProviderError("Etherscan returned an unexpected response.")
        if isinstance(body.get("error"), dict):
            raise ProviderError(str(body["error"].get("message", "Etherscan returned an error.")))
        if str(body.get("status")) == "0":
            text = (
                body["result"] if isinstance(body.get("result"), str) else body.get("message", "")
            )
            lowered = str(text).lower()
            if "rate limit" in lowered:
                return None, RateLimitedError(str(text))
            if "not supported" in lowered or "upgrade" in lowered or "api plan" in lowered:
                raise UnsupportedChainError(str(text))
            raise ProviderError(str(text) or "Etherscan returned an error.")
        if "result" not in body:
            raise ProviderError("Etherscan returned an unexpected response.")
        return body["result"], None
