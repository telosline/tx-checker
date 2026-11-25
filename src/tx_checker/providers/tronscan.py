from typing import Any

from tx_checker.providers import ProviderError, RateLimitedError
from tx_checker.providers._http import JsonApiClient

BASE_URL = "https://apilist.tronscanapi.com/api"
KEY_HEADER = "TRON-PRO-API-KEY"


class TronscanClient(JsonApiClient):
    provider = "Tronscan"

    def __init__(self, api_key: str, **options: Any) -> None:
        super().__init__(**options)
        self._headers = {KEY_HEADER: api_key}

    def fetch(self, tx_hash: str) -> dict[str, Any] | None:
        """Return the transaction-info JSON, or None when Tronscan does not know the hash."""
        body = self._get_json(
            f"{BASE_URL}/transaction-info",
            {"hash": tx_hash},
            self._unwrap,
            headers=self._headers,
        )
        return body or None

    @staticmethod
    def _unwrap(body: Any) -> tuple[Any, ProviderError | None]:
        """An unknown hash is an empty object. A message without a hash is an error."""
        if not isinstance(body, dict):
            raise ProviderError("Tronscan returned an unexpected response.")
        if not body:
            return {}, None
        if "hash" not in body:
            text = str(body.get("message") or body.get("error") or "")
            if "limit" in text.lower():
                return None, RateLimitedError(text)
            raise ProviderError(text or "Tronscan returned an unexpected response.")
        return body, None
