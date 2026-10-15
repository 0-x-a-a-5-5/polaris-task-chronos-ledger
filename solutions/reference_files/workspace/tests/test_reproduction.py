import asyncio
import contextlib
import json
import unittest
from chronos.batch import BatchTransferProcessor
from chronos.db import InMemoryAsyncLedgerDB
from chronos.engine import LedgerEngine
from chronos.models import (
    IdempotencyConflictError,
    InsufficientFundsError,
    PermanentWebhookError,
    TransferRequest,
    TransferSpec,
    TransientWebhookError,
)
from chronos.outbox import OutboxRelayWorker


class ReproductionWebhookTransport:
    def __init__(self, poison_event_ids: set, transient_fail_once_ids: set):
        self.poison_event_ids = poison_event_ids
        self.transient_fail_once_ids = set(transient_fail_once_ids)
        self.dispatched_events = []

    async def send(self, event_id: str, payload: str) -> None:
        await asyncio.sleep(0.005)
        if event_id in self.poison_event_ids:
            raise PermanentWebhookError(f"Permanent 422 rejection for {event_id}")
        if event_id in self.transient_fail_once_ids:
            self.transient_fail_once_ids.remove(event_id)
            raise TransientWebhookError(f"Transient 503 timeout for {event_id}")
        self.dispatched_events.append((event_id, json.loads(payload)))


class TestIncidentReproduction(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_idempotency_retry_storm(self):
        db = await InMemoryAsyncLedgerDB.create_with_accounts(
            {"merchant_a": 10_000, "merchant_b": 0}
        )
        engine = LedgerEngine(db, simulated_io_delay_sec=0.01)

        req = TransferRequest(
            idempotency_key="retry_storm_key_01",
            source_account="merchant_a",
            target_account="merchant_b",
            amount_cents=3_000,
        )

        receipts = await asyncio.gather(
            *(engine.execute_transfer(req) for _ in range(25))
        )
        unique_transfer_ids = {r.transfer_id for r in receipts}

        self.assertEqual(len(unique_transfer_ids), 1)
        self.assertEqual(await db.get_balance("merchant_a"), 7_000)
        self.assertEqual(await db.get_balance("merchant_b"), 3_000)
        self.assertEqual(await db.verify_double_entry_conservation(), 0)

        with self.assertRaises(IdempotencyConflictError):
            await engine.execute_transfer(
                TransferRequest(
                    idempotency_key="retry_storm_key_01",
                    source_account="merchant_a",
                    target_account="merchant_b",
                    amount_cents=5_000,
                )
            )

    async def test_outbox_poison_pill_and_concurrent_workers(self):
        db = await InMemoryAsyncLedgerDB.create_with_accounts(
            {"treasury": 20_000, "settlement": 0}
        )
        engine = LedgerEngine(db, simulated_io_delay_sec=0.001)

        for i in range(4):
            await engine.execute_transfer(
                TransferRequest(f"outbox_tx_{i}", "treasury", "settlement", 500)
            )

        pending = await db.fetch_pending_outbox_events(limit=10)
        self.assertEqual(len(pending), 4)
        poison_id = pending[0]["event_id"]
        transient_id = pending[1]["event_id"]

        transport = ReproductionWebhookTransport(
            poison_event_ids={poison_id},
            transient_fail_once_ids={transient_id},
        )

        workers = [
            OutboxRelayWorker(db, transport, max_attempts=3, batch_size=10)
            for _ in range(3)
        ]
        await asyncio.gather(*(w.run_tick() for w in workers))
        await workers[0].run_tick()

        dispatched_ids = [ev_id for ev_id, _ in transport.dispatched_events]
        self.assertEqual(len(dispatched_ids), 3)
        self.assertEqual(len(set(dispatched_ids)), 3)
        self.assertNotIn(poison_id, dispatched_ids)

    async def test_concurrent_opposing_batches_atomic_rollback(self):
        db = await InMemoryAsyncLedgerDB.create_with_accounts(
            {"acct_a": 4_000, "acct_b": 4_000, "acct_c": 0}
        )
        engine = LedgerEngine(db, simulated_io_delay_sec=0.005)
        processor = BatchTransferProcessor(engine)

        with self.assertRaises(InsufficientFundsError):
            await processor.execute_batch(
                "batch_atomic_fail",
                [
                    TransferSpec("acct_a", "acct_b", 1_500),
                    TransferSpec("acct_c", "acct_a", 50_000),
                ],
            )

        self.assertEqual(await db.get_balance("acct_a"), 4_000)
        self.assertEqual(await db.get_balance("acct_b"), 4_000)
        self.assertEqual(await db.get_balance("acct_c"), 0)

        batch_1 = [
            TransferSpec("acct_a", "acct_b", 1_000),
            TransferSpec("acct_b", "acct_c", 500),
        ]
        batch_2 = [
            TransferSpec("acct_b", "acct_a", 1_000),
            TransferSpec("acct_a", "acct_c", 500),
        ]

        res1, res2, res1_retry = await asyncio.wait_for(
            asyncio.gather(
                processor.execute_batch("batch_opp_1", batch_1),
                processor.execute_batch("batch_opp_2", batch_2),
                processor.execute_batch("batch_opp_1", batch_1),
            ),
            timeout=2.0,
        )

        self.assertEqual(
            [r.transfer_id for r in res1.receipts],
            [r.transfer_id for r in res1_retry.receipts],
        )
        self.assertEqual(await db.get_balance("acct_a"), 3_500)
        self.assertEqual(await db.get_balance("acct_b"), 3_500)
        self.assertEqual(await db.get_balance("acct_c"), 1_000)
        self.assertEqual(await db.verify_double_entry_conservation(), 0)

    async def test_outbox_claim_lease_recovers_hung_worker(self):
        db = await InMemoryAsyncLedgerDB.create_with_accounts({})
        for i in range(3):
            await db.insert_outbox_event(f"lease_evt_{i}", "agg_lease", "{}")

        class HangingTransport:
            async def send(self, event_id: str, payload: str) -> None:
                await asyncio.Event().wait()

        recorder = ReproductionWebhookTransport(poison_event_ids=set(), transient_fail_once_ids=set())
        hung_worker = OutboxRelayWorker(db, HangingTransport(), claim_lease_sec=0.05)
        healthy_worker = OutboxRelayWorker(db, recorder, claim_lease_sec=0.05)

        hung_task = asyncio.create_task(hung_worker.run_tick())
        try:
            await asyncio.sleep(0.01)
            self.assertEqual(await healthy_worker.run_tick(), 0)  # claims still leased
            await asyncio.sleep(0.1)
            self.assertEqual(await healthy_worker.run_tick(), 3)  # leases expired
        finally:
            hung_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hung_task

        dispatched_ids = [ev_id for ev_id, _ in recorder.dispatched_events]
        self.assertEqual(sorted(dispatched_ids), [f"lease_evt_{i}" for i in range(3)])


if __name__ == "__main__":
    unittest.main()
