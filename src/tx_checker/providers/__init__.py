from tx_checker.redact import redact


class ProviderError(Exception):
    """A data provider could not give a usable answer. The message is always redacted."""

    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


class RateLimitedError(ProviderError):
    """The provider's request limit was reached."""


class UnsupportedChainError(ProviderError):
    """The provider does not serve this chain on the current plan."""
