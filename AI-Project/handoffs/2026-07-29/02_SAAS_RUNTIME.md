# Task 2: Web, API, Redis Worker, Storage, And End-To-End Runtime

Read `00_MASTER_BRIEF.md` first.

## Objective

Connect the existing customer Web workflow to a durable Redis-backed worker flow while preserving the fixed public SaaS generation API and preventing AI computation inside HTTP processes.

## Required Research

- Trace legacy `/api/plan`, style-background, preview, `/api/generation-jobs`, export, and frontend polling.
- Trace the separate `api-server`, Redis queue, worker, generator, storage, and database task records.
- Identify duplicate task/status sources, in-memory-only state, retry/refund race risks, and local-path assumptions.
- Determine the smallest architecture that supports the full Web product without changing the fixed public `/generate` and `/status/<task_id>` contract.

## Required Implementation

- Keep `api-server` lightweight and AI-free.
- Move actual background, preview, formal, and refinement model calls to independent worker execution.
- Use Redis as authoritative task queue/status for generation work. If product-specific orchestration requires richer internal payloads, keep it internal and do not alter the fixed public API response/request shape.
- Make retries, timeouts, stale-running recovery, cancellation, terminal failure, and idempotency consistent.
- Persist job/image records and object keys through existing storage/database abstractions.
- Ensure the Web frontend can:
  - upload menu;
  - receive six background tasks;
  - display progressive status;
  - choose a style;
  - generate six samples;
  - start all formal tasks;
  - show per-item progress/failure;
  - submit refinement;
  - export only authorized completed assets.
- Prevent the API or Web request thread from synchronously generating multiple images.
- Provide a local developer runtime path using documented commands. A test-only fake Redis/provider is allowed for automated tests but must not be presented as production verification.

## Required Tests

- Static/API contract test proving `api-server` imports no AI generator.
- Independent API and worker startup tests.
- Redis enqueue -> worker -> done/failed status flow.
- Background, sample, formal, and refine internal task-type flow.
- Retry, timeout, stale recovery, cancellation, duplicate idempotency, and partial batch failure.
- Points refund occurs exactly once on terminal failure.
- Object storage receives successful outputs; API responses do not leak local paths or private object keys.
- Frontend contract tests for polling and terminal error states.
- Existing full test suite.

## Acceptance Criteria

- No HTTP route performs external image-model work.
- No in-memory queue is the production source of truth for the migrated generation path.
- A batch with partial failures remains observable and exportable for successful rows.
- A worker restart does not lose or duplicate chargeable work.
- Fixed public `/generate` and `/status/<task_id>` fields remain unchanged.
