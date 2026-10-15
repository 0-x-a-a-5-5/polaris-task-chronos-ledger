from .config import (
    DEFAULT_OUTBOX_BATCH_SIZE,
    DEFAULT_OUTBOX_CLAIM_LEASE_SEC,
    DEFAULT_OUTBOX_MAX_ATTEMPTS,
)
from .models import PermanentWebhookError, TransientWebhookError


class OutboxRelayWorker:
    """
    Relays outbox events to a webhook transport. Each tick atomically claims a
    batch under a lease, so concurrent workers never dispatch the same event and
    a worker that hangs or dies releases its claims when the lease expires.
    """

    def __init__(
        self,
        db_pool,
        transport,
        max_attempts: int = DEFAULT_OUTBOX_MAX_ATTEMPTS,
        batch_size: int = DEFAULT_OUTBOX_BATCH_SIZE,
        claim_lease_sec: float = DEFAULT_OUTBOX_CLAIM_LEASE_SEC,
    ) -> None:
        self.db = db_pool
        self.transport = transport
        self.max_attempts = max_attempts
        self.batch_size = batch_size
        self.claim_lease_sec = claim_lease_sec

    async def run_tick(self) -> int:
        """Claims pending (or lease-expired) events and dispatches them; returns the dispatched count."""
        claimed = await self.db.claim_pending_outbox_events(
            limit=self.batch_size, lease_sec=self.claim_lease_sec
        )
        dispatched = 0

        for ev in claimed:
            event_id = ev["event_id"]
            attempts = int(ev["attempts"]) + 1
            try:
                await self.transport.send(event_id, ev["payload"])
                await self.db.mark_outbox_status(event_id, "DISPATCHED")
                dispatched += 1
            except PermanentWebhookError as exc:
                await self.db.update_outbox_retry(
                    event_id, status="DEAD_LETTER", attempts=attempts, last_error=str(exc)
                )
            except TransientWebhookError as exc:
                new_status = "DEAD_LETTER" if attempts >= self.max_attempts else "PENDING"
                await self.db.update_outbox_retry(
                    event_id, status=new_status, attempts=attempts, last_error=str(exc)
                )

        return dispatched
