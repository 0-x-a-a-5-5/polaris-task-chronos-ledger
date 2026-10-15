# POLARIS-SWE-042: Chronos High-Concurrency Ledger & Transactional Outbox Benchmark

> [!IMPORTANT]
> **Live Eval Results (2026-10-15):** Gemini 3.8 Flash via Google's Antigravity CLI 1.3.6, running autonomously on the verbatim `prompt.md`. Rubric v1.0: **1.00 / 1.00** in 13 min 47 s. Rubric v1.1, hardened the same day: **1.00** at high effort in 10 min 22 s and **1.00** at low effort in 4 min 4 s, while the v1.0 patch re-grades to **0.85** under v1.1.
> All three patches are genuine, not reward hacks: sorted per-account locks, atomic outbox claims, and pre-validated batch legs, with the v1.1 patches adding lease-expiring claims and reference-counted lock registries once the prompt asked for them.
> Takeaway: at the current frontier this task measures specification-following under concurrency, not discovery. Diffs, grader JSON, and run metadata are in [`trajectories/`](trajectories/); section 6 logs the rubric iteration and what v1.2 has to change.

[![Polaris Grader Verification](https://github.com/0-x-a-a-5-5/polaris-task-chronos-ledger/actions/workflows/polaris-grader.yml/badge.svg)](https://github.com/0-x-a-a-5-5/polaris-task-chronos-ledger/actions/workflows/polaris-grader.yml)
[![Polaris Task Spec](https://img.shields.io/badge/Polaris_Eval_Spec-v1.1-blue.svg)](#1-benchmark-overview--design-philosophy)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/External_Dependencies-Zero-success.svg)](#2-architecture--trust-boundary)
[![Baseline Score](https://img.shields.io/badge/Unpatched_Baseline-0.10%20%2F%201.00-orange.svg)](#4-quickstart--docker-execution)
[![Reference Patch](https://img.shields.io/badge/Reference_Solution-1.00%20%2F%201.00-brightgreen.svg)](#4-quickstart--docker-execution)

**`POLARIS-SWE-042` (`chronos-ledger`)** is a hand-crafted, long-horizon software engineering evaluation task designed to measure an AI coding agent's ability to diagnose distributed systems race conditions, preserve double-entry financial invariants, and implement deadlock-free multi-hop batch transactions without degrading parallel throughput.

---

## 1. Benchmark Overview & Design Philosophy

Standard coding benchmarks rely heavily on single-file algorithmic puzzles or historical open-source commits that suffer from pre-training contamination and weak binary grading (`pass/fail` unit tests). When evaluated on naive harnesses, frontier coding agents frequently achieve inflated scores via **symptom patching** (e.g., serializing an entire async service behind a single global lock) or **reward hacking** (e.g., modifying test assertions, swallowing exceptions, or removing simulated I/O latency).

`POLARIS-SWE-042` is engineered around four core **Polaris Evaluation Principles**:

1. **Deceptive Baseline Health:** All public sequential tests (`tests/test_public_ledger.py`) pass out-of-the-box, and the unpatched baseline scores `0.10 / 1.00`, the public-suite credit alone. Rubric v1.0 let it reach `0.425`, because unlocked code trivially clears the throughput SLA and never deadlocks; v1.1 gates every non-functional credit on the correctness checks that make it meaningful. The agent cannot rely on an immediate stack trace; it must reason about interleaved coroutine execution paths and read-modify-write hazards across `engine.py`, `outbox.py`, `batch.py`, and `db.py`.
2. **Competing Functional & Non-Functional Constraints:** Fixing the idempotency and balance-drift race conditions by wrapping `LedgerEngine.execute_transfer()` in a coarse `asyncio.Lock()` satisfies basic correctness checks but violates the **Disjoint Account Throughput SLA** (100 concurrent transfers across disjoint account pairs with a 15ms I/O delay must complete in under `0.35s` for full credit).
3. **Authentic Fail-to-Pass Diagnostic Verification:** The agent is required to author deterministic reproduction tests in `tests/test_reproduction.py`. During grading, the harness executes the agent's tests against an isolated snapshot of the **unpatched baseline codebase** first (requiring failure) and then against the **patched workspace** (requiring passage). Trivial `assert True` tests score `0.00`.
4. **AST-Level Anti-Reward-Hacking Enforcement:** Before executing any behavioral test, `eval_harness/grader.py` verifies SHA-256 hashes of immutable files and parses the Python Abstract Syntax Tree (AST) of every module in `src/chronos/` to reject patches that delete `asyncio.sleep(self.io_delay)`, monkey-patch clock timers, hardcode harness tokens, or use silent `except Exception: pass` blocks.

---

## 2. Architecture & Trust Boundary

The repository enforces a strict separation between the **Agent Workspace** (mounted Read-Write during the agent trajectory) and the **Polaris Evaluation Harness** (isolated from the agent and executed post-trajectory).

```text
polaris-task-chronos-ledger/
├── Dockerfile                        # Air-gapped container; snapshots baseline & SHA-256 manifest
├── prompt.md                         # Production incident & feature specification given to the agent
├── OUTREACH_TEMPLATES.md             # High-signal templates for Polaris hiring leads & recruiters
├── workspace/                        # [AGENT READ-WRITE BOUNDARY]
│   ├── pyproject.toml                # Zero third-party runtime dependencies (stdlib asyncio + sqlite3)
│   ├── docs/
│   │   └── architecture.md           # System architecture notes
│   ├── src/chronos/
│   │   ├── __init__.py
│   │   ├── config.py                 # Default SLA and timeout constants
│   │   ├── models.py                 # [SHA-256 PROTECTED] Domain dataclasses & exception hierarchy
│   │   ├── db.py                     # Async-yielding SQLite WAL storage engine
│   │   ├── engine.py                 # [TARGET 1] TOCTOU idempotency race & stale balance overwrites
│   │   ├── outbox.py                 # [TARGET 2] Poison-pill head-of-line blocking & duplicate claims
│   │   ├── batch.py                  # [TARGET 3] Non-atomic multi-hop legs & AB-BA deadlock hazard
│   │   └── telemetry.py              # Structured audit event helper
│   └── tests/
│       ├── test_public_ledger.py     # [SHA-256 PROTECTED] Passing sequential unit test suite
│       └── test_reproduction.py      # [TARGET 4] Fail-to-pass diagnostic reproduction suite
├── eval_harness/                     # [ISOLATED GRADING BOUNDARY]
│   └── grader.py                     # AST inspector + fail-to-pass runner + 50-way concurrency stress
└── solutions/
    ├── reference_patch.diff          # Unified diff of the gold-standard solution (1.00 / 1.00)
    ├── apply_reference.py            # Pure-Python helper to apply reference solution in container
    └── reference_files/              # Clean reference source files
```

---

## 3. Continuous Numerical Rubric (`0.00` – `1.00`)

Rather than a brittle binary outcome, `eval_harness/grader.py` evaluates candidate patches across seven weighted dimensions that sum to `1.00`, behind a mandatory pre-flight integrity check. Every hidden identifier carries a per-run random suffix, so nothing can be hardcoded, and the two non-functional dimensions are credited only when the idempotency storm has proved conservation:

| Gate / Dimension | Weight | Verification Mechanism | Target Failure Mode Caught |
| :--- | :---: | :--- | :--- |
| **Pre-Flight Gate:** Workspace & AST Integrity | *Gate* (`0.00` on fail) | SHA-256 check on `models.py` and `test_public_ledger.py`; `AntiHackASTVisitor` inspects `src/chronos/*.py`. | Deleting `asyncio.sleep`, monkey-patching `time.perf_counter`, modifying public tests, or swallowing exceptions. |
| **1. Public Regression Suite** | `0.10` | Executes `pytest tests/test_public_ledger.py` on the patched workspace. | Breaking sequential transfers, overdraft validation, or public API signatures. |
| **2. Diagnostic Fail-to-Pass Reproduction** | `0.15` | Runs the agent's `tests/test_reproduction.py` on `/eval/eval_harness/unpatched_baseline` (must **FAIL**) and `/eval/workspace` (must **PASS**). | Tautological tests (`assert True`) or tests that never trigger concurrent coroutine interleaving. |
| **3. 50-Way Idempotency Storm & Mutation Conflict** | `0.20` | Fires 50 concurrent identical `TransferRequest` coroutines (`0.70` sub-weight) followed by a mutated payload with the same key (`0.30`). | TOCTOU duplicate debits, lost balance updates, or failing to raise `IdempotencyConflictError` on payload mismatch. |
| **4. Outbox: Poison-Pill Isolation, Atomic Claim & Lease Recovery** | `0.20` | 3 concurrent `OutboxRelayWorker` instances against a 422 poison pill: exactly-once dispatch of the 5 healthy events (`0.40`) and dead-letter with the error recorded (`0.10`). A worker hung inside `send()` must keep its claims until `claim_lease_sec` expires (`0.20`), after which they are recovered exactly once (`0.30`); lease credit requires the atomic-claim check. A transient-backoff regression halves the dimension. | Head-of-line stalls, duplicate dispatch across workers, events stranded `IN_FLIGHT` by a dead worker, broken retry accounting. |
| **5. Batch Processor: Atomic Rollback, Idempotency & AB-BA Deadlock Freedom** | `0.20` | A mid-batch overdraft must leave balances untouched (`0.25`) with zero ledger or outbox rows (`0.15`); sequential and concurrent replays must return identical receipts and a mutated replay must raise (`0.20`); opposing batches (`X->Y->Z` vs. `Y->X->Z`) must finish within `3.0s` with correct balances (`0.40`, credited only if rollback passed). | Partial leg commits, orphan rows, re-executed batches, `AB-BA` lock-ordering deadlocks, self-deadlocks on reentrant leg execution. |
| **6. Disjoint Account Parallel Throughput SLA** | `0.10` | 100 concurrent transfers across 100 disjoint account pairs with `io_delay = 15ms` must complete in $\le 0.35\text{s}$. Credited only if dimension 3 proved conservation. | **The Global Lock Trap:** one `asyncio.Lock()` around the engine forces $100 \times 15\text{ms} \ge 1.50\text{s}$. Unlocked, broken code is fast but earns nothing. |
| **7. Bounded Synchronization State** | `0.05` | 5,000 transfers across 10,000 distinct accounts, then at most 512 live asyncio primitives and 5,000 retained objects. Credited only if dimension 3 proved conservation. | One `asyncio.Lock` per key kept forever: memory that grows with every account and idempotency key ever seen. |

---

## 4. Quickstart & Docker Execution

The evaluation environment requires only Docker and runs completely offline in under 5 seconds.

### Build the Evaluation Container
```bash
docker build -t polaris-chronos-eval .
```

### 1. Run the Grader on the Unpatched Baseline (Expected Score: `0.10`)
```bash
docker run --rm polaris-chronos-eval
```

### 2. Verify the Gold-Standard Reference Patch (Expected Score: `1.00`)
```bash
docker run --rm -v "$(pwd)/solutions:/eval/solutions:ro" polaris-chronos-eval \
  sh -c "python3 /eval/solutions/apply_reference.py && python3 /eval/eval_harness/grader.py"
```

### 3. Evaluate an AI Coding Agent Patch
Mount your agent's modified `workspace/` directory into the container and invoke the grader:
```bash
docker run --rm -v "/path/to/agent/workspace:/eval/workspace" polaris-chronos-eval
```

---

## 5. Technical Post-Mortem: How Frontier Coding Agents Fail on `POLARIS-SWE-042`

The rubric is built to catch four failure archetypes that recur in agent trajectories on concurrency tasks. The one live trajectory recorded so far (Gemini 3.8 Flash, see the result at the top and [`trajectories/`](trajectories/)) avoided all four, so read these as what the grader checks for rather than as observed behaviour of current frontier models:

1. **Failure Mode 1 — The Coarse Global Lock Trap:** Agents wrap `execute_transfer()` in a single `self._lock = asyncio.Lock()`. This passes single-key race tests but fails Sub-Rubric 6 because 100 disjoint transfers with a 15ms I/O wait serialize to $\ge 1.50\text{s}$ instead of completing concurrently in $\sim 0.03\text{s}$.
2. **Failure Mode 2 — `AB-BA` Lock-Ordering & Reentrant Self-Deadlocks:** Agents that lock `source_account` then `target_account` deadlock when opposing batches (`X -> Y` and `Y -> X`) run concurrently. Agents that pre-lock all batch accounts using standard `asyncio.Lock` and then call `engine.execute_transfer()` self-deadlock unless they track coroutine-held locks via `contextvars.ContextVar`.
3. **Failure Mode 3 — Instance-Scoped Worker Locks:** Placing `asyncio.Lock()` on `self` inside `OutboxRelayWorker` fails when multiple worker instances poll the shared `db` concurrently in Sub-Rubric 4.
4. **Failure Mode 4 — Partial Multi-Hop Commits:** Executing batch legs directly against the DB before verifying sequential solvency leaves orphan ledger entries and outbox events when a later leg raises `InsufficientFundsError`.

---

## 6. Rubric Iteration Log

| Version | Date | Change | Why |
| :--- | :--- | :--- | :--- |
| v1.0 | 2026-10-14 | Six dimensions; weights summed to `1.15` and were capped at `1.00`. | Initial release. |
| v1.1 | 2026-10-15 | Claim-lease recovery and bounded synchronization state added to `prompt.md` and the grader; throughput, bounded-state and deadlock credit gated on the correctness checks they depend on; batch idempotency, orphan-row and dead-letter checks; transient-backoff guard; per-run randomized identifiers; weights sum to `1.00`. | Attempt 01 saturated v1.0 in 14 minutes, and a no-op patch scored `0.425` because unlocked code is trivially fast and deadlock-free. The attempt-01 patch re-grades to `0.85` under v1.1. |
| v1.1 result | 2026-10-15 | Two fresh attempts against the v1.1 prompt (high and low effort) both scored `1.00`, in 10 and 4 minutes. | Specified requirements are implemented reliably; the new checks only caught the patch written before they were specified. |
| v1.1.1 | 2026-10-15 | The retry storm also requires exactly one transfer's rows (2 ledger entries, 1 outbox event). | External review suggested adding a credit/debit sum to the storm; that sum is always zero when rows are inserted in pairs, so it cannot see duplicated rows, while a row count can. The unpatched baseline is the proof: its storm balances read a correct-looking 7500 and 2500 (every retry overwrote the same stale read) while it recorded 50 transfers and 50 outbox events. Recorded attempts re-grade identically (0.85, 1.00, 1.00). |
| v1.2 (proposed) | | Multi-engine contention: two `LedgerEngine` instances over one database must still be exactly-once (attempt 03 would fail; attempt 02 passes). Crash consistency: kill the process mid-batch against a file-backed SQLite database and require a clean ledger on restart. Hot-account contention: throughput SLA with 100 transfers over 5 accounts. One requirement deliberately left implicit in the prompt, with the grader checking whether the agent discovers it from the incident data. | Move the measured skill from specification-following to discovery and failure-mode reasoning. |
