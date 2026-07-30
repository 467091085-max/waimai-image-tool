# ChatGPT Pro Task 4: Production Transaction Closure Review

- Conversation: `https://chatgpt.com/c/6a6b207b-bf8c-83e8-93df-46d41e49fc78`
- Status: submitted with the verified archive below; external response pending

## Source Package

- Archive: `/Users/guiguixiaxia/Documents/Codex-Handoffs/waimai-image-tool/2026-07-29/waimai-image-tool-current-task4-v3.zip`
- Git baseline: `4d3214bbd251914fa314265d5ac98d12c1a302fa`
- Dirty-worktree status SHA-256: `02ef2806cf4a3c273ab22827ad4ae2b415a0eba17fb57f7e5367a97cd617db38`
- Archive size: `632713 bytes`
- Archive SHA-256: `cbfefc2fea212f16b4df8a4a89a31d243e93ebfde19bbb6c09b662af3354b594`
- ZIP integrity: passed
- Secret scan: passed after excluding `.git`, `.env*`, credentials, runtime data, uploads, exports, databases, caches, dependencies, browser state, and logs
- Reviewed benign PEM-marker hits:
  - `payment_service.py`: PEM wrapper formatting code only
  - `tests/test_render_blueprint.py`: assertion that the blueprint contains no private key

## Role

Act as an external senior engineer. The package is the only source of truth. Do
not assume access to this Mac, GitHub private state, Render, Tencent Cloud,
PostgreSQL, Redis, credentials, production data, or previous ChatGPT context.
Codex remains the final reviewer and will independently test every claim.

## Goal

Find and close production P0/P1 defects in the authenticated product transaction
path without broad refactoring:

`menu upload -> PostgreSQL job/debit/settlement/outbox -> dispatcher -> Redis ->
product Worker -> private manifest/assets -> independent reconciler -> bounded
refund/status/export`

Also inspect the adjacent refinement and payment paths for any production
fallback to SQLite, direct Redis publish, client-authoritative amount/points,
cross-user access, or crash window that violates the same transaction boundary.

## Current Architecture And Boundaries

- The customer website must remain `gunicorn app:app`.
- `api-server` is queue-only and must not run AI inference.
- Product Worker calls providers and object storage but never mutates billing or
  the product database.
- PostgreSQL is the durable product job, outbox, settlement, menu metadata, and
  point-ledger boundary in live runtime.
- Redis is an execution queue/status cache, not durable billing truth.
- Selected-background bytes and manifest pointers are immutable and SHA-256
  verified.
- SQLite/in-process behavior may remain only for explicit local demo mode.
- Do not change pricing, growth rules, provider prompts, or customer UI unless a
  security/correctness fix strictly requires it.

## Required Review

1. Verify atomic job/debit/settlement/outbox creation and replay behavior.
2. Verify outbox claim, fence stability, timeout-after-publish recovery, and
   missing-Redis-task republish.
3. Verify Worker terminal state cannot complete the wrong owner, request digest,
   fence, manifest, job type, or object bytes.
4. Verify settlement claim crash recovery, cumulative refund bounds, and
   duplicate/concurrent reconciler behavior.
5. Verify production status, manifest, asset, cancel, and export never fall back
   to SQLite or browser identity.
6. Identify whether image refinement still bypasses PostgreSQL/outbox and give
   the smallest complete patch if so.
7. Identify whether payment order creation trusts browser `userId`, amount, or
   points and give the smallest complete patch if so.
8. Identify any anonymous admin/asset/download route or replayable token that is
   still production-reachable.
9. Check migrations, Render service commands, required environment declarations,
   and readiness claims against actual entrypoints.

## Deliverables

- Findings first, ordered P0/P1/P2, with exact file and line evidence.
- A complete unified diff for the smallest safe fixes that fit this task.
- Changed-file rationale.
- Tests added for every fixed failure mode.
- Exact commands actually run and actual output. Do not claim a real
  PostgreSQL, Redis, provider, payment, or Render test unless it genuinely ran.
- PASS/FAIL checklist for the nine review items.
- Remaining external blockers and a clear implemented-vs-unverified boundary.

## Required Tests

- Focused new tests.
- Existing PostgreSQL store/outbox/reconciler/app tests.
- Payment/refinement/security tests touched by the patch.
- Full `python3 -m pytest -q`.
- `python3 -m py_compile` for changed Python.
- `node --check` only if JavaScript changes.
- `git diff --check`.

## Forbidden

- Do not commit, push, deploy, migrate a database, change live configuration,
  request secrets, access real users, or claim production verification.
- Do not replace the customer site with the API-only service.
- Do not weaken digest/fence/owner checks to make tests pass.
- Do not return a proposal without concrete findings and an applicable patch.
