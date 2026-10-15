import unittest
from chronos.batch import BatchTransferProcessor
from chronos.db import InMemoryAsyncLedgerDB
from chronos.engine import LedgerEngine
from chronos.models import (
    InsufficientFundsError,
    InvalidAmountError,
    TransferRequest,
    TransferSpec,
)
from chronos.outbox import OutboxRelayWorker


class DummyTransport:
    def __init__(self):
        self.sent = []

    async def send(self, event_id: str, payload: str) -> None:
        self.sent.append((event_id, payload))


class TestPublicLedger(unittest.IsolatedAsyncioTestCase):
    async def test_single_transfer_updates_balances_and_conservation(self):
        db = await InMemoryAsyncLedgerDB.create_with_accounts(
            {"alice": 5_000, "bob": 1_000}
        )
        engine = LedgerEngine(db, simulated_io_delay_sec=0.001)

        receipt = await engine.execute_transfer(
            TransferRequest(
                idempotency_key="tx_001",
                source_account="alice",
                target_account="bob",
                amount_cents=1_500,
            )
        )
        self.assertEqual(receipt.status, "COMMITTED")
        self.assertEqual(await db.get_balance("alice"), 3_500)
        self.assertEqual(await db.get_balance("bob"), 2_500)
        self.assertEqual(await db.verify_double_entry_conservation(), 0)

    async def test_sequential_idempotency_returns_same_receipt(self):
        db = await InMemoryAsyncLedgerDB.create_with_accounts({"alice": 5_000, "bob": 0})
        engine = LedgerEngine(db, simulated_io_delay_sec=0.001)
        req = TransferRequest("tx_seq_1", "alice", "bob", 2_000)

        r1 = await engine.execute_transfer(req)
        r2 = await engine.execute_transfer(req)

        self.assertEqual(r1.transfer_id, r2.transfer_id)
        self.assertEqual(await db.get_balance("alice"), 3_000)

    async def test_insufficient_funds_rejected(self):
        db = await InMemoryAsyncLedgerDB.create_with_accounts({"alice": 500, "bob": 0})
        engine = LedgerEngine(db, simulated_io_delay_sec=0.001)

        with self.assertRaises(InsufficientFundsError):
            await engine.execute_transfer(
                TransferRequest("tx_overdraft", "alice", "bob", 1_000)
            )
        self.assertEqual(await db.get_balance("alice"), 500)

    async def test_basic_outbox_dispatch_and_happy_path_batch(self):
        db = await InMemoryAsyncLedgerDB.create_with_accounts(
            {"a": 2_000, "b": 0, "c": 0}
        )
        engine = LedgerEngine(db, simulated_io_delay_sec=0.001)
        processor = BatchTransferProcessor(engine)

        res = await processor.execute_batch(
            "batch_happy_1",
            [
                TransferSpec("a", "b", 1_500),
                TransferSpec("b", "c", 1_000),  # Funded by Leg 1
            ],
        )
        self.assertEqual(len(res.receipts), 2)
        self.assertEqual(await db.get_balance("a"), 500)
        self.assertEqual(await db.get_balance("b"), 500)
        self.assertEqual(await db.get_balance("c"), 1_000)

        transport = DummyTransport()
        worker = OutboxRelayWorker(db, transport)
        count = await worker.run_tick()
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
