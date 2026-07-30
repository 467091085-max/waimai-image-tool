# Security High-Finding Acceptance

## Scope And Evidence Boundary

- Source baseline before this product closure: `4d3214bbd251914fa314265d5ac98d12c1a302fa`.
- The original CSV is no longer available in this worktree. Exact finding IDs and original titles cannot be recovered reliably and are not invented here.
- Eight code-proven risk groups were reconstructed from the archived session and current source.
- This is local code and disposable-infrastructure evidence. It does not claim a production deployment, production database migration, real COS IAM validation, or a paid-provider run.

## Reconstructed High-Risk Groups

| # | Reconstructed risk group | Code disposition |
|---|---|---|
| 1 | Download path traversal and unsafe file resolution | Closed by canonical server-owned object keys, safe relative-path validation, private signed object routes, and traversal regression tests. |
| 2 | Style/background path traversal and client-controlled local paths | Closed by immutable selected-background identities, owner/menu binding, object SHA verification, and removal of client path authority. |
| 3 | Anonymous preview/plan requests reaching paid generation | Closed by authenticated owner-scoped menu preview routes and provider checks after authorization. |
| 4 | Unauthenticated or client-controlled billing writes | Closed by authenticated server-catalog orders, server-owned prices/debits/refunds, PostgreSQL wallet transactions, and live SQLite denial. |
| 5 | Unauthenticated formal generation and queue writes | Closed by authenticated frozen batch contracts, owner checks, server-owned debit, and authenticated prompt API token enforcement. |
| 6 | External library/local-path/private-media disclosure | Closed by opaque private object references, owner-scoped signed delivery, restricted public media prefixes, and hidden local-path removal. |
| 7 | Unbounded export/provider/image decode resource use | Closed in code by bounded streaming object I/O, provider response limits, base64 limits, image byte/side/pixel/format gates, and pre-serialization JSON budgets. |
| 8 | Unbounded logo/base64/pixel handling and public path leakage | Closed by bounded logo/object reads, Pillow decode gates, opaque object URLs, and public payload stripping. |

## Independent Audit Rounds

### Resource-Boundary Audit

The first fresh audit reproduced four residual classes:

- Object downloads checked size after filling disk.
- JSON documents were budgeted after serialization.
- Revision manifests could be rejected after image persistence.
- Prompt/provider input could allocate before a request budget.

The patch added bounded local/COS streaming downloads, constant-memory JSON size/depth/node validation, manifest validation before persistence, and shared prompt character/UTF-8/request-body limits.

### Ordering And TOCTOU Audit

The next review reproduced:

- Prompt values reaching Redis before strict API validation.
- Serial export replay downloading before nonce denial.
- AI upload and legacy metadata read boundaries.
- Revision manifest orphan ordering.

Those were addressed with API-side prompt validation, bounded uploads and sidecar reads, download-before-consume integrity verification, and pre-upload revision-manifest validation.

The resumed reviewer then correctly found two remaining issues:

1. A read-only export nonce preflight did not reserve the nonce, so concurrent first-use requests could all download the same object.
2. AI asset persistence fingerprinted one source state but could upload a later state under the old deterministic key.

The final patch added:

- Append-only migration `014_product_export_nonce_reservations.sql`.
- Atomic download-time nonce reservation, caller-owned release on storage/integrity failure, and exact-reservation consumption after verified bytes.
- A distinct random reservation ID per HTTP request, so equal client `X-Request-Id` values cannot share a reservation.
- A PostgreSQL session advisory lock held across object verification. Concurrent use fails before storage, while process death releases the lock automatically and allows safe takeover of the abandoned row reservation.
- A deterministic access-audit lookup before storage, so an exact request previously denied by a nonce-consumer failure cannot trigger another object read.
- Reservation release and denial-audit insertion now share one PostgreSQL transaction while the session advisory lock remains held; commit or rollback completes before unlock.
- PostgreSQL readiness checks for all three migration 014 reservation columns, preventing a table-only false positive.
- A bounded private AI source snapshot whose exact bytes drive the image fingerprint, deterministic key, upload, and read-back verification.
- A bounded local-file reader that probes only `limit + 1` bytes even if a file grows after metadata inspection.

## Changed Security Files

- `migrations/014_product_export_nonce_reservations.sql`
- `shared/product_export_store.py`
- `shared/json_limits.py`
- `shared/prompt_limits.py`
- `api-server/app.py`
- `worker/prompt_provider.py`
- `worker/product_revision_handler.py`
- `object_storage_service.py`
- `app.py`
- `tests/test_product_export_store.py`
- `tests/test_postgres_export_api_protocol.py`
- `tests/test_object_storage_service.py`
- `tests/test_product_asset_runtime_integration.py`

## Verification

- Focused final P2 closure, default dependencies: `120 passed, 4 skipped`.
- Focused final P2 closure, disposable PostgreSQL: `124 passed`.
- Complete default repository suite: `1266 passed, 20 skipped in 20.24s`.
- Complete disposable PostgreSQL 16 plus Redis 7 suite: `1286 passed in 25.00s`, zero skips and zero failures.
- The real PostgreSQL HTTP test blocks the first export object stream, sends a concurrent request with the same signed token, and proves the second request returns `token_in_use` while the storage downloader is called only once.
- The concurrent requests deliberately use the same `X-Request-Id`; a pre-existing abandoned reservation is inserted first and safely replaced only after the new request acquires the advisory lock.
- A nonce-consumer failure is retried with the exact same request ID and the second attempt performs zero storage reads.
- The test also pauses the first request immediately before denial finalization, sends the same request ID concurrently, and proves the unlock-to-audit window cannot be entered.
- Tampered-object and transient-storage failures release the reservation without consuming the token; exact retry succeeds and later serial replay is rejected before storage access.
- The AI source mutation regression replaces the original file with different bytes of exactly the same size after fingerprinting and proves the persisted object and SHA remain bound to the private snapshot.
- The local-file growth regression reports a stale smaller `stat()` size while the stream contains `limit + 1` bytes and proves the bounded reader rejects it.
- The real PostgreSQL readiness protocol drops one migration 014 column, observes `schemaReady=false` with the exact missing column, reapplies the migration, and recovers readiness.
- Python AST parse: 165 files.
- Render YAML parse: 8 services.
- JavaScript syntax: `static/app.js` and `static/admin.js` passed.
- `git diff --check`: passed.
- Credential-pattern scan: only the payment PEM wrapper and explicit test dummy values/assertions matched.

## ChatGPT Pro Reconciliation

- Task 4: https://chatgpt.com/c/6a6b207b-bf8c-83e8-93df-46d41e49fc78
- Task 5: https://chatgpt.com/c/6a6b4d37-5198-83e8-b047-be3107d9987b
- Task 5 v2 withdrew its earlier false production-SQLite P0 after Codex supplied the live route graph and real PostgreSQL sentinel test.
- Final Task 5 v2 disposition: no confirmed P0/P1/P2 and `PASS candidate`.
- No ChatGPT Pro patch was applied without independent source and test verification.

## Residual External Verification

These are not closed by local code tests and must not be reported as production proof:

- Real Render service-token routing, network boundary, and auto-deploy behavior.
- Production PostgreSQL migration execution, scale, failover, and long-running concurrency.
- Long-running Redis Worker/dispatcher/reconciler stability.
- Real COS IAM, bucket policy, private-network behavior, and restart recovery.
- Paid Hunyuan/TokenHub quality, quota, latency, and every-category visual acceptance.
- Real Alipay credentials, callback networking, settlement, and withdrawal operations.

## Current Disposition

- Confirmed code P0: none.
- Confirmed code P1: none.
- Confirmed code P2: none.
- Final independent read-only disposition: `CLOSED`, with `P0=0 / P1=0 / P2=0`.
- Deployment performed by this closure: no.
- External database migration performed: no.
- Commit or push performed at this checkpoint: no.
