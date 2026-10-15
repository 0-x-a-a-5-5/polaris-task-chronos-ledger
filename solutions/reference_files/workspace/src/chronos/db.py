import asyncio
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional


class InMemoryAsyncLedgerDB:
    """
    Async-compatible SQLite storage engine for Chronos Ledger.
    Yields control to the event loop on every query to expose race conditions
    when callers fail to synchronize concurrent operations properly.
    """

    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE accounts (
                account_id TEXT PRIMARY KEY,
                balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0)
            );

            CREATE TABLE ledger_entries (
                entry_id TEXT PRIMARY KEY,
                transfer_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('DEBIT', 'CREDIT')),
                amount_cents INTEGER NOT NULL
            );

            CREATE TABLE idempotency_records (
                idempotency_key TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                transfer_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL
            );

            CREATE TABLE outbox_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                aggregate_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                claimed_until REAL
            );

            CREATE TABLE batch_idempotency (
                batch_id TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            """
        )

    @classmethod
    async def create_with_accounts(cls, accounts: Dict[str, int]) -> "InMemoryAsyncLedgerDB":
        db = cls()
        cur = db._conn.cursor()
        for acct_id, balance in accounts.items():
            cur.execute(
                "INSERT INTO accounts (account_id, balance_cents) VALUES (?, ?)",
                (acct_id, balance),
            )
        await asyncio.sleep(0)
        return db

    async def get_balance(self, account_id: str) -> Optional[int]:
        await asyncio.sleep(0)
        cur = self._conn.execute(
            "SELECT balance_cents FROM accounts WHERE account_id = ?", (account_id,)
        )
        row = cur.fetchone()
        return int(row["balance_cents"]) if row is not None else None

    async def update_balance(self, account_id: str, new_balance_cents: int) -> None:
        await asyncio.sleep(0)
        if new_balance_cents < 0:
            raise ValueError(f"Negative balance rejected by DB constraint for {account_id}")
        self._conn.execute(
            "UPDATE accounts SET balance_cents = ? WHERE account_id = ?",
            (new_balance_cents, account_id),
        )

    async def insert_ledger_entries(
        self, transfer_id: str, source: str, target: str, amount_cents: int
    ) -> None:
        await asyncio.sleep(0)
        debit_id = str(uuid.uuid4())
        credit_id = str(uuid.uuid4())
        self._conn.executemany(
            """
            INSERT INTO ledger_entries (entry_id, transfer_id, account_id, direction, amount_cents)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (debit_id, transfer_id, source, "DEBIT", amount_cents),
                (credit_id, transfer_id, target, "CREDIT", amount_cents),
            ],
        )

    async def fetch_idempotency_record(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        await asyncio.sleep(0)
        cur = self._conn.execute(
            """
            SELECT idempotency_key, payload_hash, transfer_id, amount_cents
            FROM idempotency_records
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    async def save_idempotency_record(
        self, idempotency_key: str, payload_hash: str, transfer_id: str, amount_cents: int
    ) -> None:
        await asyncio.sleep(0)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO idempotency_records
            (idempotency_key, payload_hash, transfer_id, amount_cents)
            VALUES (?, ?, ?, ?)
            """,
            (idempotency_key, payload_hash, transfer_id, amount_cents),
        )

    async def insert_outbox_event(
        self, event_id: str, aggregate_id: str, payload: str
    ) -> None:
        await asyncio.sleep(0)
        self._conn.execute(
            """
            INSERT INTO outbox_events (event_id, aggregate_id, payload, status, attempts)
            VALUES (?, ?, ?, 'PENDING', 0)
            """,
            (event_id, aggregate_id, payload),
        )

    async def fetch_pending_outbox_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        await asyncio.sleep(0)
        cur = self._conn.execute(
            """
            SELECT event_id, aggregate_id, payload, status, attempts, last_error
            FROM outbox_events
            WHERE status = 'PENDING'
            ORDER BY seq ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    async def claim_pending_outbox_events(
        self, limit: int = 20, lease_sec: float = 30.0
    ) -> List[Dict[str, Any]]:
        """
        Atomically claims up to `limit` events for `lease_sec` seconds.

        Eligible rows are PENDING, or IN_FLIGHT with an expired lease (a worker
        that claimed them hung or died). The SELECT and UPDATE run without
        yielding to the event loop, so two workers can never claim the same row.
        """
        await asyncio.sleep(0)
        now = time.monotonic()
        cur = self._conn.execute(
            """
            SELECT event_id, aggregate_id, payload, status, attempts, last_error
            FROM outbox_events
            WHERE status = 'PENDING'
               OR (status = 'IN_FLIGHT' AND claimed_until IS NOT NULL AND claimed_until < ?)
            ORDER BY seq ASC
            LIMIT ?
            """,
            (now, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        if rows:
            self._conn.executemany(
                "UPDATE outbox_events SET status = 'IN_FLIGHT', claimed_until = ? WHERE event_id = ?",
                [(now + lease_sec, r["event_id"]) for r in rows],
            )
        return rows

    async def fetch_batch_record(self, batch_id: str) -> Optional[Dict[str, Any]]:
        await asyncio.sleep(0)
        cur = self._conn.execute(
            "SELECT batch_id, payload_hash, result_json FROM batch_idempotency WHERE batch_id = ?",
            (batch_id,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    async def save_batch_record(self, batch_id: str, payload_hash: str, result_json: str) -> None:
        await asyncio.sleep(0)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO batch_idempotency (batch_id, payload_hash, result_json)
            VALUES (?, ?, ?)
            """,
            (batch_id, payload_hash, result_json),
        )

    async def mark_outbox_status(self, event_id: str, status: str) -> None:
        await asyncio.sleep(0)
        self._conn.execute(
            "UPDATE outbox_events SET status = ?, claimed_until = NULL WHERE event_id = ?",
            (status, event_id),
        )

    async def update_outbox_retry(
        self, event_id: str, status: str, attempts: int, last_error: Optional[str] = None
    ) -> None:
        await asyncio.sleep(0)
        self._conn.execute(
            """
            UPDATE outbox_events
            SET status = ?, attempts = ?, last_error = ?, claimed_until = NULL
            WHERE event_id = ?
            """,
            (status, attempts, last_error, event_id),
        )

    # Alias preserved for backward compatibility with unpatched OutboxRelayWorker
    update_outbox_Direct = update_outbox_retry

    async def verify_double_entry_conservation(self) -> int:
        """Returns SUM(CREDIT) - SUM(DEBIT) across all ledger entries (must equal 0)."""
        await asyncio.sleep(0)
        cur = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN direction = 'CREDIT' THEN amount_cents ELSE 0 END), 0) -
                COALESCE(SUM(CASE WHEN direction = 'DEBIT' THEN amount_cents ELSE 0 END), 0) AS net_diff
            FROM ledger_entries
            """
        )
        row = cur.fetchone()
        return int(row["net_diff"]) if row is not None else 0
