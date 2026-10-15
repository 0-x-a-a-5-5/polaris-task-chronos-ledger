# POLARIS-SWE-042: Chronos High-Concurrency Ledger & Transactional Outbox Benchmark

[![Polaris Task Spec](https://img.shields.io/badge/Polaris_Eval_Spec-v1.0-blue.svg)](#1-benchmark-overview--design-philosophy)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/External_Dependencies-Zero-success.svg)](#2-architecture--trust-boundary)
[![Baseline Score](https://img.shields.io/badge/Unpatched_Baseline-0.15%20%2F%201.00-orange.svg)](#4-quickstart--docker-execution)
[![Reference Patch](https://img.shields.io/badge/Reference_Solution-1.00%20%2F%201.00-brightgreen.svg)](#4-quickstart--docker-execution)

**`POLARIS-SWE-042` (`chronos-ledger`)** is a hand-crafted, long-horizon software engineering evaluation task designed to measure an AI coding agent's ability to diagnose distributed systems race conditions, preserve double-entry financial invariants, and implement deadlock-free multi-hop batch transactions without degrading parallel throughput.

---

## 1. Benchmark Overview & Design Philosophy

Standard coding benchmarks rely heavily on single-file algorithmic puzzles or historical open-source commits that suffer from pre-training contamination and weak binary grading (`pass/fail` unit tests). When evaluated on naive harnesses, frontier coding agents frequently achieve inflated scores via **symptom patching** (e.g., serializing an entire async service behind a single global lock) or **reward hacking** (e.g., modifying test assertions, swallowing exceptions, or removing simulated I/O latency).

`POLARIS-SWE-042` is engineered around four core **Polaris Evaluation Principles**:

1. **Deceptive Baseline Health:** All public sequential tests (`tests/test_public_ledger.py`) pass out-of-the-box (`0.15 / 1.00` baseline score). The agent cannot rely on an immediate stack trace; it must reason about interleaved coroutine execution paths and read-modify-write hazards across `engine.py`, `outbox.py`, `batch.py`, and `db.py`.
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

Rather than a brittle binary outcome, `eval_harness/grader.py` evaluates candidate patches across six weighted dimensions (gated by a mandatory pre-flight integrity check):

| Gate / Dimension | Weight | Verification Mechanism | Target Failure Mode Caught |
| :--- | :---: | :--- | :--- |
| **Pre-Flight Gate:** Workspace & AST Integrity | *Gate* (`0.00` on fail) | SHA-256 check on `models.py` and `test_public_ledger.py`; `AntiHackASTVisitor` inspects `src/chronos/*.py`. | Deleting `asyncio.sleep`, monkey-patching `time.perf_counter`, modifying public tests, or swallowing exceptions. |
| **1. Public Regression Suite** | `0.15` | Executes `pytest tests/test_public_ledger.py` on patched workspace. | Breaking sequential transfers, overdraft validation, or public API signatures. |
| **2. Diagnostic Fail-to-Pass Reproduction** | `0.15` | Runs agent's `tests/test_reproduction.py` on `/eval/eval_harness/unpatched_baseline` (must **FAIL**) and `/eval/workspace` (must **PASS**). | Writing tautological tests (`assert True`) or tests that do not actually trigger concurrent coroutine interleaving. |
| **3. 50-Way Idempotency Storm & Mutation Conflict** | `0.25` | Fires 50 concurrent identical `TransferRequest` coroutines (`0.70` sub-weight) followed by a mutated payload with the same key (`0.30` sub-weight). | TOCTOU duplicate debits, lost balance updates, or failing to raise `IdempotencyConflictError` on payload mismatch. |
| **4. Outbox Poison-Pill Isolation & Atomic Claim** | `0.20` | Runs 3 concurrent `OutboxRelayWorker` instances against a queue containing a `PermanentWebhookError` (HTTP 422) poison pill. | Head-of-line batch stalls, unhandled permanent exceptions, or duplicate webhook dispatches across workers. |
| **5. Multi-Hop Batch Atomicity & Deadlock Freedom** | `0.25` | Concurrent opposing batches (`X->Y->Z` vs. `Y->X->Z`) under `3.0s` timeout (`0.50` sub-weight) + mid-batch leg overdraft rollback check (`0.50` sub-weight). | `AB-BA` lock-ordering deadlocks, self-deadlocks on reentrant leg execution, or partial leg commits when a subsequent leg fails. |
| **6. Disjoint Account Parallel Throughput SLA** | `0.15` | Executes 100 concurrent transfers across 100 disjoint account pairs (`p_src_i -> p_dst_i`) with `io_delay = 15ms`. Full credit requires wall-clock time $\le 0.35\text{s}$. | **The Global Lock Trap:** Serializing the entire engine behind a single `asyncio.Lock()`, which forces $100 \times 15\text{ms} \ge 1.50\text{s}$ sequential execution. |

---

## 4. Quickstart & Docker Execution

The evaluation environment requires only Docker and runs completely offline in under 5 seconds.

### Build the Evaluation Container
```bash
docker build -t polaris-chronos-eval .
```

### 1. Run the Grader on the Unpatched Baseline (Expected Score: `0.15`)
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

When frontier LLM coding agents attempt `POLARIS-SWE-042`, their trajectories expose four recurring failure archetypes:

1. **Failure Mode 1 — The Coarse Global Lock Trap:** Agents wrap `execute_transfer()` in a single `self._lock = asyncio.Lock()`. This passes single-key race tests but fails Sub-Rubric 6 because 100 disjoint transfers with a 15ms I/O wait serialize to $\ge 1.50\text{s}$ instead of completing concurrently in $\sim 0.03\text{s}$.
2. **Failure Mode 2 — `AB-BA` Lock-Ordering & Reentrant Self-Deadlocks:** Agents that lock `source_account` then `target_account` deadlock when opposing batches (`X -> Y` and `Y -> X`) run concurrently. Agents that pre-lock all batch accounts using standard `asyncio.Lock` and then call `engine.execute_transfer()` self-deadlock unless they track coroutine-held locks via `contextvars.ContextVar`.
3. **Failure Mode 3 — Instance-Scoped Worker Locks:** Placing `asyncio.Lock()` on `self` inside `OutboxRelayWorker` fails when multiple worker instances poll the shared `db` concurrently in Sub-Rubric 4.
4. **Failure Mode 4 — Partial Multi-Hop Commits:** Executing batch legs directly against the DB before verifying sequential solvency leaves orphan ledger entries and outbox events when a later leg raises `InsufficientFundsError`.
