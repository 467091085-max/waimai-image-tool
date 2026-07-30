# ChatGPT Pro Task 3 Delivery

Source conversation:
`https://chatgpt.com/c/6a6b04c3-69a0-83e8-b0c2-71042ad95dcf`

External package SHA-256 reported by ChatGPT Pro:
`4d395ef100940e958ac4254436c9bb52d14ab087511db2cae4b85d847656eb8a`

Codex acceptance boundary:

- The report reviewed public commit `1dbcbb48aac38a118367e650cbd6d27af4faa17f`, not the current unpushed worktree.
- ChatGPT Pro did not obtain a repository checkout and did not run repository pytest, Redis, Worker, provider, or browser integration tests.
- Its patch was not applied. Current-source behavior and tests remain authoritative.
- The architecture and failure invariants below are retained as external review input.

## Engineering Conclusion

The public baseline cannot be made transaction-safe by merely adding session
checks. The defects are structurally connected:

- `current_user_id()` trusts `X-User-Id` or query `userId`, while the
  authenticated-user helper falls back to that untrusted identity when the
  session is absent or invalid.
- The server persists a `menuUploadId` and raw file SHA-256, but the old browser
  drops that ID. Downstream code then obtains the newest file from a global
  upload directory.
- Generation uses a process-local queue, accepts a browser-selected job ID,
  executes final image generation in the Web process, and performs no owner
  comparison on status or cancellation.
- The old browser calculates the charge, calls `/api/debit`, creates a job
  separately, and later supplies the refund source and amount.
- Existing public-baseline tables lack the owner/request/debit/outbox/manifest
  fields needed to establish a trustworthy chain.

The target chain is:

```text
active phone session
  -> owned immutable menu upload + raw/parsed SHA
  -> exact selected background ID + SHA
  -> frozen composition recipe
  -> canonical request SHA + server price
  -> one DB transaction: job + debit + ledger + outbox
  -> idempotent Redis task with the same SHA
  -> fenced Worker resolving private objects
  -> immutable output manifest
  -> owner-scoped status/cancel/export
  -> server-calculated bounded refund
```

Because PostgreSQL and Redis cannot share one ACID transaction, the
transactional outbox is mandatory. Calling Redis directly after debit preserves
an orphan-debit window.

The fixed generic API remains separate:

```text
POST /generate {prompt} -> {task_id}
GET /status/<task_id> -> {status, image_url}
```

## Immediate Patch Versus Full Milestone

The external immediate patch proposed:

- making demo modes opt-in;
- requiring loopback plus a non-live environment for local demo behavior;
- removing `X-User-Id` and query `userId` as customer identity;
- restricting query/header session-token fallbacks to local demo mode;
- rejecting disabled-user sessions;
- requiring an authenticated session for account access;
- disabling unsafe global-menu, browser-debit, browser-refund, in-memory-job,
  direct-AI, and ownerless-export routes in live environments;
- retaining `menuUploadId` in browser state;
- stopping the browser from selecting the authoritative job ID.

The full functional cutover requires:

1. Authenticated menu snapshot persistence.
2. Integrated product-database wallet tables.
3. Job, debit, ledger, and outbox creation in one database transaction.
4. Canonical immutable batch contracts.
5. Lua idempotent Redis enqueue.
6. Worker lease fencing for Redis state and terminal promotion.
7. Private object resolution and digest verification by Worker.
8. Immutable output manifests.
9. Owner-scoped status, cancellation, asset reads, and export.
10. Internal bounded-refund reconciliation.
11. Removal of browser debit/refund orchestration.

## Proposed Schema Boundary

The external migration proposal adds:

- menu owner, canonical parsed snapshot pointer/digest, parser version, item
  count, and freeze timestamp;
- generation owner, idempotency key, canonical request SHA/JSON, pricing,
  debit, queue, cancellation, manifest, refund, and lease-fence fields;
- export owner/request/manifest binding;
- integrated point accounts and point orders;
- generation outbox;
- generation outputs;
- generation refund events.

It was explicitly reported as a proposed one-shot migration and was not run
against a repository, staging, Render, or production database.

## Refund Rule

Refunds are derived from the sealed terminal manifest:

```text
entitlement =
  min(
    original_debit,
    failed_master_count * unit_points
      + failed_extra_platform_count * 100
      + watermark_never_delivered * 50
  )

apply_now = max(0, entitlement - already_refunded)
```

Consequences:

- no deliverables: full refund;
- one failed standard master: 10 points;
- one failed premium master: 20 points;
- an extra platform with zero successful outputs: 100 points;
- watermark with zero successful watermarked outputs: 50 points;
- repeated reconciliation: zero additional refund;
- the browser never supplies `sourceOrderId` or refund points.

## External Test Status

ChatGPT Pro reported:

- Python compilation passed for its reference package.
- Ten standalone reference tests passed for pricing, digest determinism, owner
  and background binding, forbidden browser authority fields, bounded refunds,
  migration columns/constraints, and one outbox row per job.
- Its unified diff parsed with `73/22` lines for `app.py` and `8/5` lines for
  `static/app.js`.

It also reported these unverified boundaries:

- Git checkout failed because its environment could not resolve GitHub.
- `git apply --check` against the actual commit was not run.
- No repository Flask/pytest, Redis, Worker, provider, or browser integration
  suite was run.
- No unpushed local source was accessed.
- No commit, push, deployment, migration, production-data, or secret operation
  occurred.

## Codex Disposition

The report supports the current worktree decisions around authenticated
ownership, immutable menu/background contracts, server pricing, Redis fencing,
private object manifests, and bounded cumulative refunds. Its immediate patch
is stale relative to the current source and is not applied.

The remaining material architecture blocker is the same one identified by the
current readiness gate: integrate the PostgreSQL product store and
transactional outbox into the live request/settlement path, then verify against
a real PostgreSQL and Redis service.
