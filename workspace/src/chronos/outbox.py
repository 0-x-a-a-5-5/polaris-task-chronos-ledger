from .models import PermanentWebhookError, TransientWebhookError


class OutboxRelayWorker:
    def __init__(self, db_pool, transport, max_attempts: int = 3, batch_size: int = 20):
        self.db = db_pool
        self.transport = transport
        self.max_attempts = max_attempts
        self.batch_size = batch_size

    async def run_tick(self) -> int:
        """
        Polls pending events and dispatches them to the webhook transport.
        Returns the number of successfully dispatched events.
        """
        events = await self.db.fetch_pending_outbox_events(limit=self.batch_size)
        dispatched = 0

        for ev in events:
            try:
                await self.transport.send(ev["event_id"], ev["payload"])
                await self.db.mark_outbox_status(ev["event_id"], "DISPATCHED")
                dispatched += 1
            except TransientWebhookError as exc:
                attempts = ev["attempts"] + 1
                new_status = "DEAD_LETTER" if attempts >= self.max_attempts else "PENDING"
                await self.db.update_outbox_Direct(
                    ev["event_id"], status=new_status, attempts=attempts, last_error=str(exc)
                )
        return dispatched
