from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any


class Network(StrEnum):
    EVM = "evm"
    TRON = "tron"


class Status(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    PROVIDER_ERROR = "provider_error"


class FailureReason(StrEnum):
    OUT_OF_GAS = "out_of_gas"
    REVERTED = "reverted"
    OUT_OF_ENERGY = "out_of_energy"
    FEE_LIMIT_EXCEEDED = "fee_limit_exceeded"
    OTHER = "other"
    UNKNOWN = "unknown"


class WarningCode(StrEnum):
    TO_IS_CONTRACT = "to_is_contract"
    ZERO_VALUE = "zero_value"
    UNKNOWN_TOKEN = "unknown_token"
    LOW_CONFIRMATIONS = "low_confirmations"
    NETWORK_GUESSED = "network_guessed"
    NETWORK_UNCONFIRMED = "network_unconfirmed"
    STALE_PENDING = "stale_pending"


@dataclass(frozen=True)
class ReportWarning:
    code: WarningCode
    text: str


@dataclass(frozen=True)
class TokenTransfer:
    """One token transfer. Unknown token: `amount` is the raw integer, `decimals` is None."""

    token_contract: str
    from_address: str
    to_address: str
    amount: Decimal
    token_symbol: str | None = None
    decimals: int | None = None


@dataclass(frozen=True)
class EvmFee:
    gas_used: int
    gas_limit: int
    effective_gas_price_gwei: Decimal
    fee_native: Decimal


@dataclass(frozen=True)
class TronFee:
    energy_used: int
    energy_from_stake: int
    energy_burned_trx: Decimal
    bandwidth_used: int
    bandwidth_burned_trx: Decimal
    fee_total_trx: Decimal


@dataclass(kw_only=True)
class TxReport:
    """Unified result for both networks. Only `hash` and `status` are always set."""

    hash: str
    status: Status
    network: Network | None = None
    chain_id: int | None = None
    chain_name: str | None = None
    explorer_url: str | None = None
    failure_reason: FailureReason | None = None
    message: str | None = None
    block_number: int | None = None
    timestamp: datetime | None = None
    confirmations: int | None = None
    required_confirmations: int | None = None
    from_address: str | None = None
    to_address: str | None = None
    to_is_contract: bool | None = None
    native_amount: Decimal | None = None
    native_symbol: str | None = None
    token_transfers: list[TokenTransfer] = field(default_factory=list)
    fee: EvmFee | TronFee | None = None
    warnings: list[ReportWarning] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.timestamp is not None and self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")

    @property
    def is_final(self) -> bool:
        return (
            self.status is Status.SUCCESS
            and self.confirmations is not None
            and self.required_confirmations is not None
            and self.confirmations >= self.required_confirmations
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict: Decimals as plain strings, datetimes as ISO 8601, enums as values."""
        data = _jsonable(self)
        data["is_final"] = self.is_final
        return data


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Decimal):
        # "f" avoids scientific notation such as 1E-7
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
