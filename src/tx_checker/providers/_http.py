import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Self

import httpx

from tx_checker.providers import ProviderError, RateLimitedError
from tx_checker.redact import redact

log = logging.getLogger(__name__)

# Turns a decoded JSON body into (result, None), or (None, error) when a retry makes sense.
# Errors that retrying cannot fix are raised.
Unwrap = Callable[[Any], tuple[Any, ProviderError | None]]


class JsonApiClient:
    """Shared plumbing for the provider clients: timeouts, retries, redacted errors."""

    provider = "The provider"

    def __init__(
        self,
        *,
        http: httpx.Client | None = None,
        retries: int = 2,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or httpx.Client(timeout=10.0)
        self._owns_http = http is None
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get_json(
        self,
        url: str,
        params: Mapping[str, str],
        unwrap: Unwrap,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """GET with retries on 429 and 5xx. Never logs headers."""
        for attempt in range(self._retries + 1):
            result, retry_error = self._request_once(url, params, unwrap, headers)
            if retry_error is None:
                return result
            if attempt == self._retries:
                raise retry_error
            self._sleep(self._backoff * (attempt + 1))
        raise AssertionError("unreachable")

    def _request_once(
        self,
        url: str,
        params: Mapping[str, str],
        unwrap: Unwrap,
        headers: Mapping[str, str] | None,
    ) -> tuple[Any, ProviderError | None]:
        # The exception is built outside the `except` block so no httpx object with the
        # URL stays attached to it.
        failure = None
        try:
            response = self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            failure = f"Could not reach {self.provider}: {type(exc).__name__}"
            log.debug("request failed: %s", redact(str(exc)))
        if failure:
            raise ProviderError(failure)

        log.debug("GET %s -> %s", redact(str(response.url)), response.status_code)
        if response.status_code == 429:
            return None, RateLimitedError(f"{self.provider} rate limit reached (HTTP 429).")
        if response.status_code >= 500:
            return None, ProviderError(
                f"{self.provider} is temporarily unavailable (HTTP {response.status_code})."
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"{self.provider} rejected the request (HTTP {response.status_code})."
            )

        try:
            body = response.json()
        except ValueError:
            raise ProviderError(f"{self.provider} returned an unreadable response.") from None
        return unwrap(body)
