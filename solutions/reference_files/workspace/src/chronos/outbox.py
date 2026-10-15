import asyncio
from typing import Any, Dict, List
from .models import PermanentWebhookError, TransientWebhookError


class OutboxRelayWorker:
    def __init__(
        self, db_pool, transport, max_attempts: int = 3, batch_size: int = 20
    ) -> None:
        self.db = db_pool
        self.transport = transport
        self.max_attempts = max_attempts
        self.batch_size = batch_size

        if not hasattr(self.db, "_chronos_outbox_claim_lock"):
            setattr(self.db, "_chronos_outbox_claim_lock", asyncio.Lock())

    async def _claim_pending_batch(self) -> List[Dict[str, Any]]:
        claim_lock: asyncio.Lock = getattr(self.db, "_chronos_outbox_claim_lock")
        async with claim_lock:
            events = await self.db.fetch_pending_outbox_events(limit=self.batch_size)
            for ev in events:
                await self.db.mark_outbox_status(ev["event_id"], "IN_FLIGHT")
            return events

    async def run_tick(self) -> int:
        claimed_events = await self._claim_pending_batch()
        dispatched = 0

        for ev in claimed_events:
            event_id = ev["event_id"]
            attempts = int(ev["attempts"]) + 1

            try:
                await self.transport.send(event_id, ev["payload"])
                await self.db.mark_outbox_status(event_id, "DISPATCHED")
                dispatched += 1
            except PermanentWebhookError as exc:
                await self.db.update_outbox_retry(
                    event_id,
                    status="DEAD_LETTER",
                    attempts=attempts,
                    last_error=str(exc),
                )
            except TransientWebhookError as exc:
                new_status = (
                    "DEAD_LETTER" if attempts >= self.max_attempts else "PENDING"
                )
                await self.db.update_outbox_retry(
                    event_id,
                    status=new_status,
                    attempts=attempts,
                    last_error=str(exc),
                )

        return dispatched
