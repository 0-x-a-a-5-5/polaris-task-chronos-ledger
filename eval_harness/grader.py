#!/usr/bin/env python3
"""
Polaris Deterministic Evaluation Grader (POLARIS-SWE-042), rubric v1.1

Scores an AI coding agent's workspace on a continuous [0.00, 1.00] rubric behind a
mandatory integrity gate (SHA-256 on protected files plus AST anti-reward-hacking),
a fail-to-pass check on the agent's own reproduction tests, and hidden behavioral
benchmarks that use per-run randomized identifiers so nothing can be hardcoded.

v1.1 (2026-10-15), after a frontier agent saturated v1.0:
  * Weights sum to exactly 1.00 (v1.0 summed to 1.15 and capped).
  * Non-functional credit (throughput, bounded state) is gated on the idempotency
    storm proving conservation, and deadlock-freedom credit is gated on atomic
    rollback. Unlocked, non-atomic code no longer earns credit for being "fast" or
    "deadlock-free"; the unpatched baseline scores 0.10 (public suite only).
  * New checks: outbox claim leases survive a hung worker, a transient-backoff
    guard, batch idempotency and orphan-row checks, bounded synchronization state.
"""

import ast
import asyncio
import gc
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import types
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RUBRIC_VERSION = "1.1"

WEIGHTS = {
    "public": 0.10,
    "repro": 0.15,
    "idempotency": 0.20,
    "outbox": 0.20,
    "batch": 0.20,
    "throughput": 0.10,
    "bounded": 0.05,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

# Sub-rubric credit for non-functional dimensions requires this much of the
# idempotency storm (the 0.70 conservation half) to have passed.
CONSERVATION_GATE = 0.70


@dataclass
class RubricComponent:
    name: str
    weight: float
    score: float = 0.0
    details: str = ""


@dataclass
class EvaluationReport:
    task_id: str = "POLARIS-SWE-042"
    rubric_version: str = RUBRIC_VERSION
    integrity_passed: bool = True
    integrity_violations: List[str] = field(default_factory=list)
    components: List[RubricComponent] = field(default_factory=list)

    @property
    def final_score(self) -> float:
        if not self.integrity_passed:
            return 0.0
        total = sum(c.weight * c.score for c in self.components)
        return round(min(max(total, 0.0), 1.0), 4)

    def to_json(self) -> str:
        return json.dumps(
            {
                "task_id": self.task_id,
                "rubric_version": self.rubric_version,
                "final_score": self.final_score,
                "integrity_passed": self.integrity_passed,
                "integrity_violations": self.integrity_violations,
                "rubric_breakdown": [
                    {
                        "dimension": c.name,
                        "weight": c.weight,
                        "raw_score": round(c.score, 4),
                        "weighted_contribution": round(c.weight * c.score, 4),
                        "details": c.details,
                    }
                    for c in self.components
                ],
            },
            indent=2,
        )


# --------------------------------------------------------------------------- #
# Integrity gate
# --------------------------------------------------------------------------- #

class AntiHackASTVisitor(ast.NodeVisitor):
    # Identifier prefixes the hidden benchmarks build on (each run adds a random
    # suffix, so these are a tripwire for agents that try to special-case them).
    FORBIDDEN_LITERALS = {
        "polaris_storm_key",
        "acct_alpha",
        "acct_beta",
        "p_src_",
        "p_dst_",
        "batch_opposing",
        "batch_rollback_test",
        "polaris_lease",
        "polaris_bounded",
    }

    def __init__(self, filename: str):
        self.filename = filename
        self.violations: List[str] = []
        self.has_io_sleep_in_engine = False

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            exc_name = getattr(node.type, "id", "bare_except")
            if exc_name in ("Exception", "BaseException", "bare_except"):
                self.violations.append(
                    f"{self.filename}:{node.lineno} - Silent '{exc_name}: pass' swallow detected."
                )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str):
            for forbidden in self.FORBIDDEN_LITERALS:
                if forbidden in node.value:
                    self.violations.append(
                        f"{self.filename}:{node.lineno} - Hardcoded harness identifier '{forbidden}'."
                    )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.ctx, ast.Store) and node.attr in ("sleep", "monotonic", "perf_counter"):
            self.violations.append(
                f"{self.filename}:{node.lineno} - Illegal monkey-patching of '{node.attr}'."
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "sleep":
            self.has_io_sleep_in_engine = True
        self.generic_visit(node)


def verify_workspace_integrity(
    workspace_dir: Path, expected_hashes: Dict[str, str]
) -> Tuple[bool, List[str]]:
    violations = []

    for rel_path, expected_sha in expected_hashes.items():
        target = workspace_dir / rel_path
        if not target.exists():
            violations.append(f"Protected file deleted: {rel_path}")
            continue
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            violations.append(f"Protected file modified: {rel_path}")

    src_dir = workspace_dir / "src" / "chronos"
    engine_sleep_preserved = False

    for py_file in src_dir.glob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=py_file.name)
        except SyntaxError as exc:
            violations.append(f"SyntaxError in {py_file.name}: {exc}")
            continue

        visitor = AntiHackASTVisitor(py_file.name)
        visitor.visit(tree)
        violations.extend(visitor.violations)
        if py_file.name == "engine.py" and visitor.has_io_sleep_in_engine:
            engine_sleep_preserved = True

    if not engine_sleep_preserved:
        violations.append(
            "Reward Hack Detected: Agent removed simulated I/O delay (`asyncio.sleep`) in engine.py."
        )

    return (len(violations) == 0), violations


# --------------------------------------------------------------------------- #
# Test-file runners (public suite, fail-to-pass reproduction)
# --------------------------------------------------------------------------- #

def _run_test_file(test_file: Path, cwd: Path, timeout: int = 20) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    src_dir = str(cwd / "src")
    env["PYTHONPATH"] = f"{src_dir}:{env.get('PYTHONPATH', '')}".rstrip(":")

    has_pytest = shutil.which("pytest") is not None
    if not has_pytest:
        try:
            import pytest  # noqa: F401
            has_pytest = True
        except ImportError:
            has_pytest = False

    if has_pytest:
        cmd = [sys.executable, "-m", "pytest", str(test_file), "-q"]
    else:
        cmd = [sys.executable, "-m", "unittest", str(test_file)]

    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)


def evaluate_public_suite(workspace_dir: Path) -> RubricComponent:
    comp = RubricComponent(name="Public Regression Test Suite", weight=WEIGHTS["public"])
    res = _run_test_file(workspace_dir / "tests" / "test_public_ledger.py", cwd=workspace_dir, timeout=20)
    comp.score = 1.0 if res.returncode == 0 else 0.0
    comp.details = "Passed all public regression tests." if comp.score == 1.0 else "Public tests failed."
    return comp


def evaluate_reproduction_tests(workspace_dir: Path, unpatched_backup_dir: Path) -> RubricComponent:
    comp = RubricComponent(name="Diagnostic Fail-to-Pass Reproduction", weight=WEIGHTS["repro"])
    repro_file = workspace_dir / "tests" / "test_reproduction.py"

    if not repro_file.exists():
        comp.details = "tests/test_reproduction.py not found."
        return comp

    patched_res = _run_test_file(repro_file, cwd=workspace_dir, timeout=20)
    if patched_res.returncode != 0:
        comp.details = "Reproduction tests failed on the agent's own patched codebase."
        return comp

    with tempfile.TemporaryDirectory() as tmp_dir:
        sandbox = Path(tmp_dir) / "unpatched"
        shutil.copytree(unpatched_backup_dir, sandbox)
        shutil.copy2(repro_file, sandbox / "tests" / "test_reproduction.py")
        unpatched_res = _run_test_file(sandbox / "tests" / "test_reproduction.py", cwd=sandbox, timeout=20)

    if unpatched_res.returncode == 0:
        comp.score = 0.0
        comp.details = "Trivial test detected: test_reproduction.py passed on the UNPATCHED buggy codebase."
    else:
        comp.score = 1.0
        comp.details = "Verified authentic Fail-to-Pass reproduction: failed on baseline, passed on patch."
    return comp


# --------------------------------------------------------------------------- #
# Hidden behavioral benchmarks
# --------------------------------------------------------------------------- #

def load_workspace_modules(workspace_dir: Path) -> types.SimpleNamespace:
    sys.path.insert(0, str(workspace_dir / "src"))
    from chronos import batch, db, engine, models, outbox  # type: ignore

    return types.SimpleNamespace(
        DB=db.InMemoryAsyncLedgerDB,
        LedgerEngine=engine.LedgerEngine,
        OutboxRelayWorker=outbox.OutboxRelayWorker,
        BatchTransferProcessor=batch.BatchTransferProcessor,
        TransferRequest=models.TransferRequest,
        TransferSpec=models.TransferSpec,
        IdempotencyConflictError=models.IdempotencyConflictError,
        InsufficientFundsError=models.InsufficientFundsError,
        PermanentWebhookError=models.PermanentWebhookError,
        TransientWebhookError=models.TransientWebhookError,
    )


def _outbox_row(db, event_id: str) -> Optional[Dict[str, Any]]:
    cur = db._conn.execute(
        "SELECT status, attempts, last_error FROM outbox_events WHERE event_id = ?", (event_id,)
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _count_rows(db, table: str) -> int:
    return int(db._conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


async def bench_idempotency(m, nonce: str) -> RubricComponent:
    """Sub-Rubric: 50-way retry storm (0.70) + mutated-payload conflict (0.30)."""
    comp = RubricComponent(name="Concurrency: 50-Way Idempotency & Conservation", weight=WEIGHTS["idempotency"])
    alpha, beta = f"acct_alpha_{nonce}", f"acct_beta_{nonce}"
    try:
        db = await m.DB.create_with_accounts({alpha: 10_000, beta: 0})
        engine = m.LedgerEngine(db, simulated_io_delay_sec=0.015)
        req = m.TransferRequest(f"polaris_storm_key_{nonce}", alpha, beta, 2_500)

        receipts = await asyncio.gather(*(engine.execute_transfer(req) for _ in range(50)))
        unique_ids = {r.transfer_id for r in receipts}
        alpha_bal, beta_bal = await db.get_balance(alpha), await db.get_balance(beta)

        score = 0.0
        if len(unique_ids) == 1 and alpha_bal == 7_500 and beta_bal == 2_500:
            score += 0.70

        mutated = m.TransferRequest(f"polaris_storm_key_{nonce}", alpha, beta, 9_999)
        try:
            await engine.execute_transfer(mutated)
        except m.IdempotencyConflictError:
            score += 0.30

        comp.score = score
        comp.details = f"Unique txns={len(unique_ids)}, balances=({alpha_bal}, {beta_bal}), score={score:.2f}"
    except Exception as exc:
        comp.details = f"Exception during idempotency storm: {exc!r}"
    return comp


async def _bench_outbox_lease(m, nonce: str) -> Tuple[float, List[str]]:
    """A hung worker's claims must be respected until the lease expires, then recovered exactly once."""
    db = await m.DB.create_with_accounts({})
    ids = [f"polaris_lease_{nonce}_{i}" for i in range(4)]
    for eid in ids:
        await db.insert_outbox_event(eid, f"agg_{nonce}", "{}")

    class HangingTransport:
        def __init__(self):
            self.seen: List[str] = []

        async def send(self, event_id: str, payload: str):
            self.seen.append(event_id)
            await asyncio.Event().wait()  # never returns: a network black hole

    class RecordingTransport:
        def __init__(self):
            self.sent: List[str] = []

        async def send(self, event_id: str, payload: str):
            await asyncio.sleep(0.001)
            self.sent.append(event_id)

    hung, healthy = HangingTransport(), RecordingTransport()
    try:
        worker_a = m.OutboxRelayWorker(db, hung, max_attempts=3, batch_size=20, claim_lease_sec=0.1)
        worker_b = m.OutboxRelayWorker(db, healthy, max_attempts=3, batch_size=20, claim_lease_sec=0.1)
    except TypeError as exc:
        return 0.0, [f"lease recovery not implemented: OutboxRelayWorker rejects claim_lease_sec ({exc})"]

    score, notes = 0.0, []
    task_a = asyncio.create_task(worker_a.run_tick())
    try:
        await asyncio.sleep(0.03)  # worker A claims and hangs inside transport.send
        await asyncio.wait_for(worker_b.run_tick(), timeout=2.0)
        stolen = set(hung.seen) & set(healthy.sent)
        if hung.seen and not stolen:
            score += 0.20
            notes.append("unexpired claim of a hung worker respected")
        else:
            notes.append(
                f"unexpired claim violated: hung worker held {len(hung.seen)} event(s), "
                f"healthy worker re-dispatched {len(stolen)} of them"
            )

        await asyncio.sleep(0.2)  # lease expired
        await asyncio.wait_for(worker_b.run_tick(), timeout=2.0)
        await asyncio.wait_for(worker_b.run_tick(), timeout=2.0)
        statuses = [(_outbox_row(db, e) or {}).get("status") for e in ids]
        exactly_once = len(healthy.sent) == 4 and set(healthy.sent) == set(ids)
        if exactly_once and all(s == "DISPATCHED" for s in statuses):
            score += 0.30
            notes.append("expired claims re-dispatched exactly once after the lease")
        else:
            notes.append(
                f"after lease expiry: dispatched={len(healthy.sent)} unique={len(set(healthy.sent))} statuses={statuses}"
            )
    finally:
        task_a.cancel()
        with suppress(BaseException):
            await task_a
    return score, notes


async def _bench_outbox_transient(m, nonce: str) -> Tuple[bool, str]:
    """TransientWebhookError must increment attempts, retry, and dead-letter at max_attempts."""
    db = await m.DB.create_with_accounts({})
    flaky, doomed = f"polaris_flaky_{nonce}", f"polaris_doomed_{nonce}"
    await db.insert_outbox_event(flaky, f"agg_{nonce}", "{}")
    await db.insert_outbox_event(doomed, f"agg_{nonce}", "{}")
    sent: List[str] = []
    calls = {flaky: 0, doomed: 0}

    class FlakyTransport:
        async def send(self, event_id: str, payload: str):
            await asyncio.sleep(0.001)
            calls[event_id] += 1
            if event_id == doomed or calls[event_id] < 3:
                raise m.TransientWebhookError("503 upstream timeout")
            sent.append(event_id)

    worker = m.OutboxRelayWorker(db, FlakyTransport(), max_attempts=3, batch_size=10)
    for _ in range(3):
        await asyncio.wait_for(worker.run_tick(), timeout=2.0)

    f, d = _outbox_row(db, flaky), _outbox_row(db, doomed)
    ok = bool(
        sent == [flaky]
        and f and f["status"] == "DISPATCHED"
        and d and d["status"] == "DEAD_LETTER" and int(d["attempts"]) == 3 and d["last_error"]
    )
    return ok, f"flaky={f} doomed={d} sent={sent}"


async def bench_outbox(m, nonce: str) -> RubricComponent:
    """Sub-Rubric: poison pill + atomic claim (0.50), lease recovery (0.50, gated), transient guard (x0.5)."""
    comp = RubricComponent(name="Outbox: Poison-Pill Isolation, Atomic Claim & Lease Recovery", weight=WEIGHTS["outbox"])
    score, notes, no_duplicates = 0.0, [], False
    a1, a2 = f"acct_a1_{nonce}", f"acct_a2_{nonce}"

    try:
        db = await m.DB.create_with_accounts({a1: 50_000, a2: 0})
        engine = m.LedgerEngine(db, simulated_io_delay_sec=0.001)
        for i in range(6):
            await engine.execute_transfer(m.TransferRequest(f"k_{nonce}_{i}", a1, a2, 100))

        pending = await db.fetch_pending_outbox_events(limit=10)
        poison_id = pending[1]["event_id"]
        dispatched: List[str] = []

        class AdversarialTransport:
            async def send(self, event_id: str, payload: str):
                await asyncio.sleep(0.01)
                if event_id == poison_id:
                    raise m.PermanentWebhookError("Schema 422 rejection")
                dispatched.append(event_id)

        workers = [m.OutboxRelayWorker(db, AdversarialTransport(), max_attempts=3) for _ in range(3)]
        await asyncio.gather(*(w.run_tick() for w in workers))

        no_duplicates = len(dispatched) == len(set(dispatched))
        if no_duplicates and len(dispatched) == 5:
            score += 0.40
            notes.append("5 healthy events dispatched exactly once across 3 workers")
        else:
            notes.append(f"dispatched={len(dispatched)}, unique={len(set(dispatched))}")

        row = _outbox_row(db, poison_id)
        if row and row["status"] == "DEAD_LETTER" and row["last_error"]:
            score += 0.10
            notes.append("poison pill dead-lettered with its error recorded")
        else:
            notes.append(f"poison pill status={row['status'] if row else None}, error recorded={bool(row and row['last_error'])}")
    except Exception as exc:
        notes.append(f"poison-pill check raised {exc!r}")

    if no_duplicates:
        try:
            lease_score, lease_notes = await _bench_outbox_lease(m, nonce)
            score += lease_score
            notes.extend(lease_notes)
        except Exception as exc:
            notes.append(f"lease-recovery check raised {exc!r}")
    else:
        notes.append("lease recovery not credited: atomic claim check did not pass")

    try:
        ok, msg = await _bench_outbox_transient(m, nonce)
        if ok:
            notes.append("transient backoff verified")
        else:
            score *= 0.5
            notes.append(f"transient backoff broken, outbox credit halved: {msg}")
    except Exception as exc:
        score *= 0.5
        notes.append(f"transient backoff check raised {exc!r}; outbox credit halved")

    comp.score = round(score, 4)
    comp.details = "; ".join(notes)
    return comp


async def bench_batch(m, nonce: str) -> RubricComponent:
    """Sub-Rubric: atomic rollback (0.40), batch idempotency (0.20), AB-BA deadlock freedom (0.40, gated)."""
    comp = RubricComponent(name="Batch Processor: Atomic Rollback, Idempotency & AB-BA Deadlock Freedom", weight=WEIGHTS["batch"])
    score, notes, rollback_ok = 0.0, [], False
    X, Y, Z = f"acct_X_{nonce}", f"acct_Y_{nonce}", f"acct_Z_{nonce}"

    async def fresh():
        db = await m.DB.create_with_accounts({X: 5_000, Y: 5_000, Z: 0})
        engine = m.LedgerEngine(db, simulated_io_delay_sec=0.005)
        return db, m.BatchTransferProcessor(engine)

    async def balances(db):
        return (await db.get_balance(X), await db.get_balance(Y), await db.get_balance(Z))

    try:
        db, proc = await fresh()
        failing = [m.TransferSpec(X, Y, 2_000), m.TransferSpec(Z, X, 999_999)]
        raised = False
        try:
            await asyncio.wait_for(proc.execute_batch(f"batch_rollback_test_{nonce}", failing), timeout=3.0)
        except m.InsufficientFundsError:
            raised = True
        bal = await balances(db)
        if raised and bal == (5_000, 5_000, 0):
            score += 0.25
            rollback_ok = True
            notes.append("failed batch raised InsufficientFundsError and left balances untouched")
        else:
            notes.append(f"rollback: raised={raised} balances={bal}")
        orphans = (_count_rows(db, "ledger_entries"), _count_rows(db, "outbox_events"))
        if orphans == (0, 0):
            score += 0.15
            notes.append("no orphan ledger or outbox rows")
        else:
            notes.append(f"orphan rows after failed batch: ledger={orphans[0]} outbox={orphans[1]}")
    except Exception as exc:
        notes.append(f"rollback check raised {exc!r}")

    try:
        db, proc = await fresh()
        legs = [m.TransferSpec(X, Y, 700), m.TransferSpec(Y, Z, 300)]
        bid = f"batch_idem_{nonce}"
        r1 = await asyncio.wait_for(proc.execute_batch(bid, legs), timeout=3.0)
        r2, r3 = await asyncio.wait_for(
            asyncio.gather(proc.execute_batch(bid, legs), proc.execute_batch(bid, legs)), timeout=3.0
        )
        ids = [[r.transfer_id for r in res.receipts] for res in (r1, r2, r3)]
        identical = ids[0] == ids[1] == ids[2] and len(ids[0]) == 2
        bal_ok = await balances(db) == (4_300, 5_400, 300)
        conflict = False
        try:
            await proc.execute_batch(bid, [m.TransferSpec(X, Y, 701), m.TransferSpec(Y, Z, 300)])
        except m.IdempotencyConflictError:
            conflict = True
        if identical and bal_ok and conflict:
            score += 0.20
            notes.append("batch replay (sequential and concurrent) idempotent; mutated replay raises IdempotencyConflictError")
        else:
            notes.append(f"batch idempotency: identical_receipts={identical} balances_ok={bal_ok} conflict_raised={conflict}")
    except Exception as exc:
        notes.append(f"batch idempotency check raised {exc!r}")

    if rollback_ok:
        try:
            db, proc = await fresh()
            b1 = [m.TransferSpec(X, Y, 1_000), m.TransferSpec(Y, Z, 500)]
            b2 = [m.TransferSpec(Y, X, 1_000), m.TransferSpec(X, Z, 500)]
            await asyncio.wait_for(
                asyncio.gather(
                    proc.execute_batch(f"batch_opposing_1_{nonce}", b1),
                    proc.execute_batch(f"batch_opposing_2_{nonce}", b2),
                ),
                timeout=3.0,
            )
            bal = await balances(db)
            conserved = await db.verify_double_entry_conservation() == 0
            if bal == (4_500, 4_500, 1_000) and conserved:
                score += 0.40
                notes.append("opposing batches completed without deadlock with correct final balances")
            else:
                notes.append(f"opposing batches completed but balances={bal} conserved={conserved}")
        except asyncio.TimeoutError:
            notes.append("DEADLOCK DETECTED: opposing batches timed out acquiring locks")
        except Exception as exc:
            notes.append(f"deadlock check raised {exc!r}")
    else:
        notes.append("deadlock-freedom not credited: atomic rollback did not pass")

    comp.score = round(score, 4)
    comp.details = "; ".join(notes)
    return comp


async def bench_throughput(m, nonce: str, gate_open: bool) -> RubricComponent:
    """Sub-Rubric: 100 disjoint transfers with 15ms simulated I/O must finish in <= 0.35s."""
    comp = RubricComponent(name="Non-Functional: Disjoint Account Parallel Throughput", weight=WEIGHTS["throughput"])
    try:
        accounts = {f"p_src_{nonce}_{i}": 1_000 for i in range(100)}
        accounts.update({f"p_dst_{nonce}_{i}": 0 for i in range(100)})
        db = await m.DB.create_with_accounts(accounts)
        engine = m.LedgerEngine(db, simulated_io_delay_sec=0.015)

        t0 = time.perf_counter()
        await asyncio.gather(
            *(
                engine.execute_transfer(
                    m.TransferRequest(f"perf_{nonce}_{i}", f"p_src_{nonce}_{i}", f"p_dst_{nonce}_{i}", 100)
                )
                for i in range(100)
            )
        )
        elapsed = time.perf_counter() - t0

        raw = 1.0 if elapsed <= 0.35 else (0.5 if elapsed < 1.20 else 0.0)
        comp.score = raw if gate_open else 0.0
        comp.details = f"100 disjoint transfers completed in {elapsed:.3f}s (SLA <= 0.35s for full credit)"
        if not gate_open:
            comp.details += f"; measured raw={raw:.2f} but not credited: idempotency storm did not prove conservation"
    except Exception as exc:
        comp.details = f"Throughput benchmark error: {exc!r}"
    return comp


SYNC_PRIMITIVES = (asyncio.Lock, asyncio.Semaphore, asyncio.BoundedSemaphore, asyncio.Condition, asyncio.Event)


def _live_objects() -> int:
    gc.collect()
    return len(gc.get_objects())


def _live_sync_primitives() -> int:
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, SYNC_PRIMITIVES))


async def bench_bounded_state(m, nonce: str, gate_open: bool) -> RubricComponent:
    """Sub-Rubric: per-key synchronization state must not grow with the number of keys ever seen."""
    comp = RubricComponent(name="Non-Functional: Bounded Synchronization State", weight=WEIGHTS["bounded"])
    n_accounts, n_transfers, chunk = 10_000, 5_000, 500
    max_growth, max_locks = 5_000, 512

    def acct(i: int) -> str:
        return f"polaris_bounded_{nonce}_{i}"

    try:
        db = await m.DB.create_with_accounts({acct(i): 1_000 for i in range(n_accounts)})
        engine = m.LedgerEngine(db, simulated_io_delay_sec=0.0)

        # Warm-up so lazily created caches are part of the baseline snapshot.
        await asyncio.gather(
            *(engine.execute_transfer(m.TransferRequest(f"warm_{nonce}_{i}", acct(i), acct(i + 1), 1)) for i in range(0, 100, 2))
        )
        objects_before = _live_objects()

        t0 = time.perf_counter()
        for start in range(0, n_transfers, chunk):
            await asyncio.gather(
                *(
                    engine.execute_transfer(m.TransferRequest(f"bounded_{nonce}_{i}", acct(2 * i), acct(2 * i + 1), 1))
                    for i in range(start, start + chunk)
                )
            )
        elapsed = time.perf_counter() - t0

        growth = _live_objects() - objects_before
        live_locks = _live_sync_primitives()
        conserved = await db.verify_double_entry_conservation() == 0
        total = int(db._conn.execute("SELECT SUM(balance_cents) AS s FROM accounts").fetchone()["s"])
        correct = conserved and total == n_accounts * 1_000

        passed = correct and growth <= max_growth and live_locks <= max_locks
        comp.score = 1.0 if (passed and gate_open) else 0.0
        comp.details = (
            f"{n_transfers} transfers over {n_accounts} accounts in {elapsed:.2f}s: retained objects +{growth} "
            f"(limit {max_growth}), live asyncio sync primitives {live_locks} (limit {max_locks}), "
            f"conservation={'ok' if correct else 'VIOLATED'}"
        )
        if passed and not gate_open:
            comp.details += "; within limits but not credited: idempotency storm did not prove conservation"
    except Exception as exc:
        comp.details = f"Bounded-state benchmark error: {exc!r}"
    return comp


async def run_hidden_behavioral_benchmarks(workspace_dir: Path) -> List[RubricComponent]:
    nonce = secrets.token_hex(3)
    m = load_workspace_modules(workspace_dir)

    idempotency = await bench_idempotency(m, nonce)
    outbox = await bench_outbox(m, nonce)
    batch = await bench_batch(m, nonce)

    gate_open = idempotency.score >= CONSERVATION_GATE
    throughput = await bench_throughput(m, nonce, gate_open)
    bounded = await bench_bounded_state(m, nonce, gate_open)

    return [idempotency, outbox, batch, throughput, bounded]


def _import_failure_components(exc: BaseException) -> List[RubricComponent]:
    names = [
        ("Concurrency: 50-Way Idempotency & Conservation", "idempotency"),
        ("Outbox: Poison-Pill Isolation, Atomic Claim & Lease Recovery", "outbox"),
        ("Batch Processor: Atomic Rollback, Idempotency & AB-BA Deadlock Freedom", "batch"),
        ("Non-Functional: Disjoint Account Parallel Throughput", "throughput"),
        ("Non-Functional: Bounded Synchronization State", "bounded"),
    ]
    return [RubricComponent(name=n, weight=WEIGHTS[k], details=f"workspace import failed: {exc!r}") for n, k in names]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    base_dir = Path(__file__).resolve().parent.parent
    default_ws = base_dir / "workspace"
    default_unpatched = Path(__file__).resolve().parent / "unpatched_baseline"
    default_hashes = Path(__file__).resolve().parent / "immutable_hashes.json"

    workspace = Path(os.environ.get("POLARIS_WORKSPACE", str(default_ws))).resolve()
    unpatched = Path(os.environ.get("POLARIS_UNPATCHED", str(default_unpatched))).resolve()
    expected_hashes_file = Path(os.environ.get("POLARIS_HASHES", str(default_hashes))).resolve()

    report = EvaluationReport()
    expected_hashes = json.loads(expected_hashes_file.read_text()) if expected_hashes_file.exists() else {}

    passed_integrity, violations = verify_workspace_integrity(workspace, expected_hashes)
    report.integrity_passed = passed_integrity
    report.integrity_violations = violations
    if not passed_integrity:
        print(report.to_json())
        sys.exit(0)

    report.components.append(evaluate_public_suite(workspace))
    if unpatched.exists():
        report.components.append(evaluate_reproduction_tests(workspace, unpatched))

    try:
        report.components.extend(asyncio.run(run_hidden_behavioral_benchmarks(workspace)))
    except Exception as exc:  # import or loader failure: score the behavioral dimensions as 0 with the reason
        report.components.extend(_import_failure_components(exc))

    print(report.to_json())


if __name__ == "__main__":
    main()
