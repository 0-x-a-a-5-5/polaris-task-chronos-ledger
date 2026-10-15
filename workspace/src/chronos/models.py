from dataclasses import dataclass, field
from typing import List, Optional


class ChronosError(Exception):
    """Base exception for all Chronos ledger errors."""


class InsufficientFundsError(ChronosError):
    """Raised when a debit would bring an account balance below zero."""


class AccountNotFoundError(ChronosError):
    """Raised when either source_account or target_account does not exist."""


class IdempotencyConflictError(ChronosError):
    """Raised when an existing idempotency_key is reused with different parameters."""


class InvalidAmountError(ChronosError):
    """Raised when amount_cents <= 0 or source_account == target_account."""


class PermanentWebhookError(ChronosError):
    """Raised by WebhookTransport on non-retryable 4xx errors (poison pill)."""


class TransientWebhookError(ChronosError):
    """Raised by WebhookTransport on retryable 5xx or network timeout errors."""


@dataclass(frozen=True)
class TransferRequest:
    idempotency_key: str
    source_account: str
    target_account: str
    amount_cents: int


@dataclass(frozen=True)
class TransferReceipt:
    transfer_id: str
    idempotency_key: str
    source_account: str
    target_account: str
    amount_cents: int
    status: str = "COMMITTED"


@dataclass(frozen=True)
class TransferSpec:
    source_account: str
    target_account: str
    amount_cents: int


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    status: str
    receipts: List[TransferReceipt] = field(default_factory=list)


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    transfer_id: str
    account_id: str
    direction: str  # "DEBIT" or "CREDIT"
    amount_cents: int


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    aggregate_id: str
    payload: str
    status: str = "PENDING"
    attempts: int = 0
    last_error: Optional[str] = None
