# Redis Batch Production Audit

## Scope

Read-only audit of the current customer Web flow, SaaS API/Redis skeleton, Worker, billing, object storage, and export boundaries. No code was changed by the audit agent.

## P0 Findings

1. The customer website still runs the monolith, while `api-server/` exposes only the fixed single-prompt demo contract. The two execution paths are not connected.
2. `/api/upload-menu` returns `menuUploadId`, but the customer frontend does not retain it. Generation and export read the newest local Excel file, so concurrent users can cross-contaminate jobs.
3. Browser login uses a Bearer session, but generation/billing identity still falls back to client-controlled `X-User-Id` or `userId`.
4. Selected backgrounds now have immutable local SHA-256 snapshots, but the frozen task does not yet carry a Worker-readable private object key.
5. Formal generation still uses `InMemoryGenerationQueue`; restart loses jobs and status, and status/cancel ownership is not enforced.
6. `shared/redis_queue.py` lacks atomic idempotent enqueue, request hashes, claim leases, heartbeat, cancellation, acknowledgement, and durable batch manifests.
7. Generated images and export packages are not bound to the generation job. Export rebuilds the newest menu plan instead of reading an immutable job result manifest.

## Frozen Batch Contract

The production batch job must include:

- `schemaVersion` and `jobType=menu_batch_generation`
- server-generated `jobId`
- authenticated `userId`
- authorized `menuUploadId`, private menu object key, file SHA-256, and parser version
- immutable selected-background asset ID, style ID, SHA-256, private object key, width, and height
- quality ID and server-owned points per image
- frozen platform dimensions/limits
- frozen watermark settings and private logo object key
- server-calculated billing snapshot, deterministic debit/refund order IDs, and pricing version
- stable idempotency key plus canonical request SHA-256
- creation timestamp

Redis stores task state and the final manifest object key. Per-dish outputs belong in an object-storage manifest, not a large Redis hash.

## Required Failure Semantics

- Validation or ownership failure: no debit and no enqueue.
- Debit failure: no enqueue.
- Enqueue failure after debit: deterministic full refund.
- Worker success requires object storage write plus SHA-256 verification.
- Fatal or zero-success job: full refund.
- Partial job: refund failed/unprocessed image points; retain add-on charges only when at least one deliverable succeeded.
- Only the Web finalizer changes balances. Worker never credits/debits points.
- Retry, recovery, and repeated status requests must reuse the same debit/refund order IDs.

## Minimal Implementation Order

1. Add a validated canonical batch contract and request hash in `shared/`.
2. Add atomic idempotency, claim lease, heartbeat, cancel, ack, and stale recovery to the Redis queue while preserving the fixed public `/generate` contract.
3. Add database helpers for menu ownership, generation jobs, generated images, and job-bound exports using existing tables.
4. Bind upload, generation, status, cancel, and export to the authenticated session user and `menuUploadId`.
5. Move pricing/debit/refund into the server generation service; remove customer-controlled debit/refund from the frontend flow.
6. Persist the selected-background snapshot to private object storage before enqueue.
7. Add a Worker batch handler that validates hashes, processes every menu row, writes deterministic outputs/manifests, and checks cancellation between rows.
8. Finalize jobs idempotently into database result rows and billing refunds.
9. Export only from the authorized completed job manifest and write the actual `export_packages.job_id`.
10. Move free backgrounds/samples to the same Worker boundary before production readiness is declared.

## Acceptance Tests

- schema validation and canonical request hash
- same idempotency key/same hash returns one job
- same idempotency key/different hash returns 409
- authenticated user and menu ownership enforcement
- server-side pricing and duplicate-debit prevention
- Redis claim/heartbeat/ack/cancel/stale recovery
- selected-background private object SHA verification
- combo batch generation without single-dish reuse
- full and partial failure refund idempotency
- repeated finalization safety
- job-bound export authorization
- `upload -> debit -> Redis -> Worker -> manifest -> status -> export` integration

## Boundary

This audit is an implementation plan, not proof that Redis batch production is complete. No deployment, database migration, commit, push, or real billing action was performed.
