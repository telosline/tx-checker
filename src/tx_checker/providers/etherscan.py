import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from tx_checker.providers import ProviderError, RateLimitedError, UnsupportedChainError
from tx_checker.redact import redact

BASE_URL = "https://api.etherscan.io/v2/api"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvmRawData:
    """Raw proxy responses for one transaction. Pending transactions only have `transaction`."""

    transaction: dict[str, Any]
    receipt: dict[str, Any] | None = None
    latest_block: str | None = None
    block: dict[str, Any] | None = None
    to_code: str | None = None


class EtherscanClient:
    def __init__(
        self,
        api_key: str,
        *,
        http: httpx.Client | None = None,
        retries: int = 2,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=10.0)
        self._owns_http = http is None
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "EtherscanClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

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
        for attempt in range(self._retries + 1):
            last_attempt = attempt == self._retries
            body, retry_error = self._request_once(query)
            if retry_error is None:
                return body
            if last_attempt:
                raise retry_error
            self._sleep(self._backoff * (attempt + 1))
        raise AssertionError("unreachable")

    def _request_once(self, query: dict[str, str]) -> tuple[Any, ProviderError | None]:
        """One HTTP call. Returns (result, None), or (None, error) when a retry makes sense.

        Errors that retrying cannot fix are raised straight away. The exception is built
        outside the `except` block so no httpx object with the URL stays attached to it.
        """
        failure = None
        try:
            response = self._http.get(BASE_URL, params=query)
        except httpx.HTTPError as exc:
            failure = f"Could not reach Etherscan: {type(exc).__name__}"
            log.debug("request failed: %s", redact(str(exc)))
        if failure:
            raise ProviderError(failure)

        log.debug("GET %s -> %s", redact(str(response.url)), response.status_code)
        if response.status_code == 429:
            return None, RateLimitedError("Etherscan rate limit reached (HTTP 429).")
        if response.status_code >= 500:
            return None, ProviderError(
                f"Etherscan is temporarily unavailable (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            raise ProviderError(f"Etherscan rejected the request (HTTP {response.status_code}).")

        try:
            body = response.json()
        except ValueError:
            raise ProviderError("Etherscan returned an unreadable response.") from None
        return self._unwrap(body)

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
