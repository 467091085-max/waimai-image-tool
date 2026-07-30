Production Transaction Closure Review
Executive verdict

Pre-patch status: FAIL. The supplied snapshot contained multiple production P0/P1 transaction-boundary violations.

Post-patch status: conditional PASS for the code-level closure, with external verification still required. The patch closes the identified production-reachable bypasses without changing pricing, growth rules, provider prompts, or the customer-service entrypoint. The PostgreSQL/Redis/Render behavior has not been verified against real infrastructure.

The complete applicable patch is here:

Download the complete unified diff

Patch metadata:

Size:    167,988 bytes
Lines:   4,371
SHA-256: d78706bd231920af0e8e797daf3bc7bf4de5dce05c2852676b8ce05db208a282
git diff --check: passed
Findings
P0-1 — Image refinement bypassed the PostgreSQL transaction and outbox boundary
Evidence

The production refinement route still used the legacy flow:

HTTP request
  -> SQLite refinement record
  -> direct Redis enqueue
  -> worker

That meant refinement could:

publish work to Redis without a durable PostgreSQL outbox row;

create execution work without an atomic debit/settlement record;

lose work in the SQLite-to-Redis crash window;

consume free-rework allowance through a non-atomic read-before-write decision;

become inconsistent with the generation-job transaction model.

Relevant patched areas:

app.py: production refinement submission and PostgreSQL routing

shared/product_job_store.py: atomic revision job/debit/settlement/outbox creation

shared/product_job_store.py: transactional free-rework quota decision

shared/product_generation_settlement.py: revision completion validation

Fix

Production refinement now:

Authenticates the session principal.

Resolves the parent generation or revision from PostgreSQL.

Loads the private source and selected-background snapshots.

Verifies immutable object keys and SHA-256 values.

Freezes a server-authoritative revision contract.

Creates the revision job, debit, initial settlement, and outbox row in one PostgreSQL transaction.

Leaves Redis publication exclusively to the outbox dispatcher.

Uses the existing revision worker execution model without allowing the worker to mutate billing.

Legacy SQLite/direct-Redis refinement remains available only in explicit local/demo execution.

Free-rework race

Two concurrent requests could previously both observe an available free-rework slot before either request was recorded.

The patch adds a PostgreSQL transaction-scoped advisory lock keyed by owner and parent job, followed by a quota count within the same transaction. If the quota is exhausted, the request is refrozen using the paid server contract before job creation.

P0-2 — Payment order creation trusted browser-authoritative identity and value fields
Evidence

The payment endpoint accepted fields equivalent to:

userId

amountCents

points

provider/order-related values

This allowed the browser to influence the payer identity and the amount-to-points exchange rather than selecting a server-owned package.

The existing payment persistence was also SQLite-backed, which is outside the required PostgreSQL product transaction boundary for live runtime.

Relevant patched areas:

app.py: /api/payments/orders

app.py: payment callback and administrative reconciliation guards

transaction-boundary security tests

Fix

The payment-order API now:

derives the user exclusively from the authenticated session;

accepts only a server catalog packageId, plus an optional idempotency key;

derives cash amount, point quantity, and provider configuration server-side;

rejects browser-supplied identity, cash amount, points, provider, or order identifiers;

refuses live payment creation with:

503 payment_postgres_store_required

until a PostgreSQL payment order/event store is implemented.

The fake payment callback, Alipay callback, and administrative payment reconciliation paths are also disabled in live/PostgreSQL runtime where they would otherwise mutate SQLite.

Important functional boundary

This is intentionally fail-closed, not a complete PostgreSQL payment implementation. Production payment checkout remains unavailable until the payment order, callback event, and credit-ledger transaction are migrated to PostgreSQL.

That is safer than retaining a live SQLite or client-authoritative payment path.

P0-3 — Customer GET and cancel routes were performing reconciliation and settlement work
Evidence

Generation and refinement customer routes imported or invoked terminal-state reconciliation behavior, including functions equivalent to:

reading terminal state from Redis;

applying completion to PostgreSQL;

settling or refunding jobs.

This made customer status or manifest reads capable of mutating durable settlement state. It violated the specified boundary:

Worker -> Redis terminal state
Independent reconciler -> PostgreSQL settlement/refund
Customer GET -> PostgreSQL read only

It also created several crash and concurrency problems:

a customer polling request could become the effective reconciler;

a status request could race the independent reconciler;

Redis availability could determine whether a durable state transition occurred;

cancellation could trigger settlement outside the reconciler claim protocol.

Relevant patched areas:

app.py: generation status, manifest, asset, refinement status, and cancellation routes

shared/product_generation_settlement.py: reconciler-only completion application

Fix

Production customer routes now behave as follows:

Route class	Production behavior
Status	Reads PostgreSQL only
Manifest	Reads validated PostgreSQL/private-manifest state only
Asset	Requires authenticated ownership and validated manifest membership
Cancel	Records durable cancel_requested; sends only a best-effort execution signal
Refund/settlement	Independent reconciler only

A missing Redis task during cancellation no longer causes settlement behavior or a false terminal result. The durable cancel request remains PostgreSQL truth.

P0-4 — Worker terminal success was insufficiently bound to the frozen contract and actual object bytes
Evidence

Before settlement, success needed stronger proof that the terminal result belonged to the exact:

job;

owner;

request digest;

execution fence;

job type;

manifest;

source object;

selected-background object;

output assets.

A terminal task or manifest with partially matching metadata could otherwise be accepted without independently validating every immutable byte reference.

Relevant patched file:

shared/product_generation_settlement.py

Fix

Settlement now validates both menu-generation and revision manifests before a successful PostgreSQL terminal transition.

The validator checks:

Redis terminal task

expected task type;

exact job ID;

exact owner;

exact request digest;

current execution fence.

PostgreSQL frozen contract

exact job type;

owner;

canonical recomputed request digest;

private manifest key.

Manifest bytes

exact deterministic object key;

canonical JSON representation;

transaction-envelope schema;

task type and job type;

job and owner IDs;

frozen request digest.

Source snapshots

exact menu upload or parent revision reference;

immutable object key;

expected SHA-256;

actual downloaded bytes matching that SHA-256.

Selected background

frozen asset ID and object key;

expected SHA-256;

actual object bytes matching the frozen digest.

Delivery assets

exact expected count;

unique asset IDs;

deterministic paths;

asset-to-result binding;

MIME and byte length;

actual object bytes matching each advertised SHA-256.

A mismatch prevents successful settlement rather than weakening owner, digest, fence, type, or byte checks.

The generation executor now writes a private _transactionEnvelope. That envelope is removed from public customer responses.

P1-1 — Durable outbox replay could not recreate a missing deterministic Redis task
Evidence

The queue’s idempotency mapping could survive while the actual Redis task hash disappeared or expired.

In that state, a replay could return the original deterministic task ID without recreating the task hash or queue membership. The durable outbox would appear published, but no executable Redis task would exist.

This is a realistic timeout-after-publish or partial Redis-loss recovery case.

Relevant patched file:

shared/redis_queue.py

Fix

The Lua enqueue operation now distinguishes:

Ordinary duplicate submission with a different candidate task ID

returns the already mapped task;

does not overwrite it.

The mapped deterministic task ID exists in the idempotency mapping, but its task hash is missing

atomically recreates the task hash;

restores queue membership;

retains the original deterministic task identity.

This preserves fence and idempotency stability while allowing the durable outbox to recover a lost Redis execution record.

P1-2 — Concurrent free refinement requests could exceed the server quota
Evidence

The previous free-rework decision was effectively:

count current free requests
if count < quota:
    create free request

Without serialization, two transactions could both observe the same remaining slot.

Relevant patched file:

shared/product_job_store.py

Fix

The patch adds:

transaction-scoped advisory locking by owner and parent job;

quota counting inside the job-creation transaction;

idempotency lookup before quota consumption;

an explicit FreeReworkQuotaExhausted path;

refreezing to the paid server contract before retrying job creation.

The debit and free/paid decision are therefore part of the same transaction that inserts the job, settlement, and outbox rows.

P1-3 — Production-reachable legacy debit and in-process generation endpoints violated the transaction boundary
Evidence

Two adjacent endpoints remained dangerous in live runtime:

/api/debit

/api/generate-final

/api/debit allowed a customer-triggered point mutation outside atomic product-job creation.

/api/generate-final performed generation in the web process, bypassing:

PostgreSQL job creation;

outbox publication;

worker lease/fence handling;

independent settlement.

Relevant patched areas:

app.py

security regression tests

Fix

Both endpoints now fail closed in live/PostgreSQL runtime.

Their legacy behavior remains limited to explicit local/demo mode.

The customer website remains:

gunicorn app:app

The patch does not replace it with the queue-only API service.

P1-4 — Production identity and download routes still had browser-controlled or anonymous paths
Evidence

The snapshot included production-reachable risks around:

accepting a session token through the query string;

browser X-User-Id or query identity on download paths;

minting signed access for an arbitrary object key;

unauthenticated or incompletely protected administrative routes;

local filesystem download fallback;

model-input browsing.

Query-string credentials are especially problematic because they can be copied into logs, browser history, referrers, and monitoring systems.

Relevant patched areas:

app.py

admin_panel.py

security tests

Fix

The patch now enforces:

no ?sessionToken= authentication;

no browser-supplied user identity for production downloads;

active session authentication for object access;

signed object token owner matching;

internal/admin authorization for arbitrary object-signing in production;

capped signing TTL;

server-controlled object base URL;

blueprint-wide authorization on the administrative panel;

authorization on queue snapshot and upload-library administrative routes;

model-input browsing restricted to local mode;

no local /download export fallback in production.

A signed asset token remains reusable by the same authenticated owner until expiry. It is no longer an anonymous bearer token granting access without an active owner session.

P1-5 — Production export and read paths could fall back to local/SQLite state
Evidence

The required production rule is that PostgreSQL and private object storage are authoritative. Redis is only execution state, and SQLite/local files are only explicit demo behavior.

The snapshot retained fallback branches capable of serving:

job status;

manifests;

revision overrides;

export files;

local downloads.

Relevant patched area:

app.py

Fix

When live runtime is detected, PostgreSQL mode is treated as mandatory even if a feature flag or database declaration is malformed.

Production status, manifest, asset, cancellation, and export routes now:

require the PostgreSQL product record;

require matching authenticated ownership;

require the expected job type where applicable;

use validated private manifest/object-storage references;

fail closed instead of searching SQLite or local files.

Export also requires the object-signing/storage configuration necessary for private delivery.

P1-6 — Migration execution and readiness did not prove the required PostgreSQL boundary
Evidence

The Render customer service retained the correct entrypoint, but there was no complete executable PostgreSQL migration pre-deploy path.

Readiness also needed to distinguish “feature flag says PostgreSQL” from actual PostgreSQL reachability and expected schema state.

Relevant patched files:

shared/postgres_migrations.py

render.yaml

app.py

Fix

The patch adds a migration CLI:

Bash
python -m shared.postgres_migrations

It:

opens the configured PostgreSQL connection;

applies the product migrations;

emits redacted JSON status;

exits nonzero on migration failure;

does not print the database URL, credentials, or SQL parameter values.

The customer service retains:

YAML
startCommand: gunicorn app:app

and adds:

YAML
preDeployCommand: python -m shared.postgres_migrations

Readiness now probes the PostgreSQL product store rather than relying only on a configured flag. A missing/unreachable database or missing required schema blocks the production readiness claim.

No migration was run against a real database.

P2-1 — Live payment functionality is deliberately blocked pending PostgreSQL implementation

This is an external implementation blocker, not an insecure fallback.

The patch prevents production checkout from silently using SQLite, but it does not introduce a new payment schema or callback-event model because doing so would exceed the requested smallest-safe-fix scope.

A complete payment implementation still needs:

PostgreSQL payment order storage;

unique provider order and callback-event constraints;

a server-owned package snapshot;

callback signature verification;

idempotent callback/event processing;

atomic point-credit ledger insertion;

reconciliation state and claim fencing.

Until that exists, live payment routes return a clear 503 boundary error.

P2-2 — Asset tokens are bounded but intentionally reusable within their TTL

A signed object token can still be replayed by the same authenticated owner until expiry. It is bound to:

the exact object key;

owner identity;

expiration;

server signature.

There is no one-time nonce registry in this patch. Adding one would introduce durable token-consumption state and materially broaden the design.

P2-3 — Baseline commit and dirty-worktree hash cannot be independently verified

The archive does not contain .git.

Therefore, I could verify:

Archive size:    632713 bytes
Archive SHA-256: cbfefc2fea212f16b4df8a4a89a31d243e93ebfde19bbb6c09b662af3354b594
ZIP integrity:   passed

I could not independently recompute:

Git baseline:
4d3214bbd251914fa314265d5ac98d12c1a302fa

Dirty-worktree status SHA-256:
02ef2806cf4a3c273ab22827ad4ae2b415a0eba17fb57f7e5367a97cd617db38

The unified diff is against the exact extracted current snapshot.

Atomicity and replay assessment

The existing PostgreSQL store already provided the correct central primitive: job creation, debit, initial settlement, and outbox insertion occur in one database transaction.

The patch preserves that structure and uses it for refinement.

The reviewed replay behavior is now:

Case	Result
Same owner and idempotency key, same frozen request	Existing job returned
Same key but changed request digest	Rejected
Same key but different owner	Not exposed across owners
Same job replay after committed debit/outbox	No second debit
Transaction fails before commit	No durable job/debit/outbox subset
Outbox publish timeout but Redis task exists	Idempotent original task returned
Idempotency mapping exists but task hash is missing	Original deterministic task recreated
New candidate task ID for already-published key	Original mapped task retained

The focused tests exercise the transaction protocol with fakes. No real PostgreSQL process was used.

Settlement and refund assessment

The existing settlement model already included:

settlement claims;

claim tokens and versions;

lease expiry/recovery;

idempotent point-ledger operations;

cumulative refund accounting;

maximum refund bounded by the original debit or computed refund target.

The patch keeps settlement mutation in the independent reconciler and extends terminal validation to revision jobs and immutable object bytes.

Duplicate or concurrent reconciliation is protected by the settlement claim/version protocol and idempotent ledger keys. This was tested at the store/protocol level only; no multi-process test against a real PostgreSQL instance was performed.

Changed-file rationale
File	Reason
app.py	Enforce live PostgreSQL mode, authenticated identity, PostgreSQL refinement creation, read-only customer status routes, durable cancel intent, payment fail-closed behavior, export/object/download authorization, and disable legacy transaction bypasses.
admin_panel.py	Apply authorization to the complete administrative blueprint rather than selected routes only.
shared/product_job_store.py	Add atomic free-rework quota enforcement and integrate it with revision job/debit/settlement/outbox creation.
shared/redis_queue.py	Recover a missing deterministic Redis task while preserving stable idempotency mappings and task IDs.
shared/product_generation_settlement.py	Validate menu and revision terminal state against owner, job type, digest, fence, canonical manifest, immutable snapshots, and actual object bytes.
shared/postgres_migrations.py	Add a redacted, nonzero-exit migration command suitable for Render pre-deploy execution.
render.yaml	Preserve gunicorn app:app and add the PostgreSQL migration pre-deploy command.
tests/redis_test_double.py	Model the missing-task recovery behavior required by the updated Lua queue protocol.
tests/test_product_job_store.py	Test atomic revision creation, idempotency, quota locking, debit behavior, and quota exhaustion.
tests/test_product_generation_settlement.py	Test owner/type/digest/fence/manifest/object-byte validation for generation and refinement.
tests/test_product_settlement_reconciler.py	Test terminal settlement, retry, duplicate handling, and refund bounds.
tests/test_redis_queue_reliability.py	Test missing task-hash republish and ordinary replay stability.
tests/test_postgres_migrations.py	Test migration CLI success/failure behavior and redaction.
tests/test_render_blueprint.py	Verify customer and queue-only service commands and migration pre-deploy declaration.
tests/test_postgres_product_runtime_integration.py	Test production fail-closed behavior and PostgreSQL-only reads.
tests/test_product_api_integration.py	Test product-route ownership, cancellation, status, and manifest behavior.
tests/test_refinement_api_integration.py	Test PostgreSQL refinement creation and removal of direct Redis publication.
tests/test_admin_panel.py	Test administrative route authorization.
tests/test_transaction_boundary_security.py	New focused coverage for payment fields, arbitrary object signing, token leakage, legacy debit/generation routes, refinement routing, and export fallback.

No JavaScript files were changed.

Test results
Source-package verification

Commands run:

Bash
stat -c '%s' /mnt/data/waimai-image-tool-current-task4-v3.zip
sha256sum /mnt/data/waimai-image-tool-current-task4-v3.zip
unzip -t /mnt/data/waimai-image-tool-current-task4-v3.zip

Actual result:

632713
cbfefc2fea212f16b4df8a4a89a31d243e93ebfde19bbb6c09b662af3354b594
ZIP integrity test passed
Focused non-Flask transaction tests

Command run:

Bash
python3 -m pytest -q \
  tests/test_redis_queue_reliability.py \
  tests/test_product_job_store.py \
  tests/test_product_generation_settlement.py \
  tests/test_product_settlement_reconciler.py \
  tests/test_product_outbox_dispatcher.py \
  tests/test_product_revision_handler.py \
  tests/test_postgres_migrations.py \
  tests/test_render_blueprint.py

Actual output:

........................................................................ [ 54%]
...........................................................              [100%]
131 passed in 0.29s

Focused non-Flask test log

Migration and Render tests

Command run:

Bash
python3 -m pytest -q \
  tests/test_postgres_migrations.py \
  tests/test_render_blueprint.py

Actual output:

..................                                                       [100%]
18 passed in 0.11s

Migration and Render test log

Focused Flask/application tests

Command attempted:

Bash
python3 -m pytest -q \
  tests/test_admin_panel.py \
  tests/test_postgres_product_runtime_integration.py \
  tests/test_product_api_integration.py \
  tests/test_refinement_api_integration.py \
  tests/test_security_regressions.py \
  tests/test_transaction_boundary_security.py

Actual result:

5 collection errors
ModuleNotFoundError: No module named 'flask'
pytest exit code: 2

These tests did not execute their assertions.

Focused Flask test log

Full test suite

Command attempted:

Bash
python3 -m pytest -q

Actual result:

14 collection errors
All reported collection failures were rooted in:
ModuleNotFoundError: No module named 'flask'

pytest exit code: 2

Affected collections included:

tests/test_admin_panel.py
tests/test_app_generation.py
tests/test_download_route.py
tests/test_generation_batch_transaction.py
tests/test_generation_queue_integration.py
tests/test_postgres_menu_app_integration.py
tests/test_postgres_product_runtime_integration.py
tests/test_product_redis_integration.py
tests/test_refinement_api_integration.py
tests/test_refinement_queue_integration.py
tests/test_security_regressions.py
tests/test_selected_background_pipeline.py
tests/test_strict_matching.py
tests/test_transaction_boundary_security.py

This is not a passing full-suite result.

Full pytest log

Dependency installation attempts

I attempted to install the repository requirements.

The configured package index did not supply the required packages, and a public PyPI attempt failed because DNS/network access was unavailable in the execution environment. Flask, psycopg, and redis therefore remained unavailable.

No local substitute modules were created to manufacture passing results.

Python compilation

Command run:

Bash
python3 -m py_compile \
  app.py \
  admin_panel.py \
  shared/postgres_migrations.py \
  shared/product_generation_settlement.py \
  shared/product_job_store.py \
  shared/redis_queue.py \
  tests/redis_test_double.py \
  tests/test_admin_panel.py \
  tests/test_postgres_migrations.py \
  tests/test_postgres_product_runtime_integration.py \
  tests/test_product_api_integration.py \
  tests/test_product_generation_settlement.py \
  tests/test_product_job_store.py \
  tests/test_product_settlement_reconciler.py \
  tests/test_redis_queue_reliability.py \
  tests/test_refinement_api_integration.py \
  tests/test_render_blueprint.py \
  tests/test_transaction_boundary_security.py

Actual result:

exit code 0
No Python syntax errors reported

Python compilation log

Patch whitespace validation

Command run in the synthetic patch repository:

Bash
git diff --check

Actual result:

exit code 0
No output

git diff --check log

JavaScript syntax check
node --check: not run
Reason: no JavaScript file changed
Nine-item review checklist
#	Review item	Result	Boundary
1	Atomic job/debit/settlement/outbox creation and replay	PASS — code/unit level	Existing atomic PostgreSQL primitive preserved and extended to refinement. No real PostgreSQL test.
2	Outbox claim, stable fence, timeout-after-publish, missing-task republish	PASS — code/unit level	Missing deterministic Redis task is recreated without changing mapped task identity. No real Redis/dispatcher process test.
3	Worker terminal state cannot complete wrong owner/digest/fence/manifest/type/bytes	PASS — code/unit level	Strict menu and revision validation added, including actual source/background/output bytes.
4	Settlement claim recovery, cumulative refund bounds, concurrent reconciler behavior	PASS — protocol/unit level	Claims, versions, lease recovery, idempotent ledger keys, and refund caps retained and tested with fakes. No real concurrent PostgreSQL test.
5	Production status, manifest, asset, cancel, export avoid SQLite/browser identity	PASS — implemented	Live routes are PostgreSQL/object-storage based and fail closed. Flask integration tests were not executable in this environment.
6	Refinement PostgreSQL/outbox closure	PASS — implemented	Production refinement now uses atomic PostgreSQL job/debit/settlement/outbox creation; direct Redis remains local/demo only.
7	Payment browser identity, amount, and points	PASS for security boundary; functional blocker remains	Fields are server-authoritative. Live order creation is deliberately 503 until a PostgreSQL payment store exists.
8	Anonymous admin/asset/download or replayable token	PASS with P2 residual	Admin/download/object routes require authorization and owner binding. Same-owner signed tokens remain reusable until expiry.
9	Migrations, Render commands, environment and readiness claims	PASS — static/code level	Customer remains gunicorn app:app; migration pre-deploy and PostgreSQL readiness checks added. No Render or live database execution.
Implemented versus unverified
Implemented in the patch

PostgreSQL transaction-backed production refinement.

Atomic free-rework quota handling.

No production direct Redis publication from the refinement HTTP route.

Customer status/manifest routes no longer reconcile or settle.

Durable cancellation intent with best-effort worker signaling.

Deterministic Redis missing-task recovery.

Strict menu and revision terminal validation.

Immutable source, selected-background, and output-byte verification.

Browser-authoritative payment values removed.

Live payment SQLite fallback disabled.

Live legacy debit and in-process generation disabled.

Production SQLite/local status, manifest, export, and download fallbacks removed.

Session query-string authentication removed.

Administrative and object/download authorization tightened.

PostgreSQL migration CLI and Render pre-deploy command.

PostgreSQL-backed readiness behavior.

Not verified or not implemented

Real PostgreSQL migrations.

Real PostgreSQL locking, claim contention, or concurrent reconciler processes.

Real Redis Lua execution, failover, eviction, or dispatcher recovery.

Provider calls or object-storage integration.

Payment-provider signature/callback behavior.

Render build, pre-deploy, health check, or service startup.

Full Flask application test suite.

Complete PostgreSQL payment order/event store.

Production credentials, data, environment declarations, or live user behavior.

Baseline Git commit or dirty-worktree status hash due the archive excluding .git.

No commit, push, deployment, live configuration change, database migration, provider call, or real-user access was performed.