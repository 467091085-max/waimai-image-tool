# Redis Reliability Review

## Independent Findings

The independent review rejected the first claim/lease implementation for three P0 defects:

1. Public `POST /generate` still used separate `HSET` and `RPUSH` commands.
2. `RPOPLPUSH` and lease assignment were separate operations, allowing recovery to interleave and leave a task permanently `running` with no queue or processing receipt.
3. Provider execution did not heartbeat continuously, and a timed-out provider thread could be followed by another paid attempt.

It also identified wrong-type partial writes, cross-slot keys, LIFO starvation, duplicate receipts, Worker-loop exit on transient Redis errors, local-clock leases, missing retention, and non-scalable recovery scanning.

## Applied Corrections

- Public and idempotent enqueue now use Redis Lua.
- Queue-to-processing move, attempt increment, lease token, and `running` transition now occur in one Lua operation.
- Queue keys share one Redis Cluster hash tag.
- Lua scripts validate Redis key types before writes.
- Queue order is FIFO through `LPUSH` plus right-pop claim.
- Every queue receipt has a unique `receipt_id`.
- Invalid or inconsistent receipts go to a dead-letter list.
- Lease timestamps use Redis `TIME`.
- Worker heartbeats every lease/3 during provider execution.
- A provider timeout is terminal for that task attempt; the Worker does not start a second provider call while the timed-out thread may still be running.
- Worker main-loop Redis failures use bounded exponential backoff instead of terminating the process.
- Recovery scans all current processing receipts until it reaches the configured recovery limit.

## Verification

- Queue/Worker focused tests: `27 passed`.
- Generation/product focused regression: `102 passed`.
- Full project regression: `413 passed`.
- Python compilation, customer JavaScript syntax, and `git diff --check`: passed.
- Lua-capable `fakeredis` executed the production scripts for 32 concurrent idempotent requests, concurrent claim ownership, conflict handling, success acknowledgement, wrong-type failure without partial task creation, and orphan recovery.
- Redis Cluster slot calculation confirmed queue, processing, dead-letter, task, and idempotency keys share one slot.

## Remaining Risks

- No real Redis server restart/failover test has run.
- Terminal task and idempotency retention/TTL is not yet implemented; production Redis must not use unsafe eviction.
- Processing-list recovery is still list scanning. Redis Streams consumer groups remain the preferred scale-out design.
- External image providers still need deterministic provider request IDs and output object IDs because infrastructure can only guarantee at-least-once execution.
- The timed-out provider thread cannot be forcibly terminated in Python. The current guard prevents an automatic second provider call, but a process boundary or provider hard timeout is still required for strict termination.
