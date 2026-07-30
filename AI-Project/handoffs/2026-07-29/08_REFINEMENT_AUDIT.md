# Single-Image Refinement Audit

## Current Status

The current customer “自定义修改” and “换一版” flows are placeholders:

- the modal collects an edit request;
- the browser calls `/api/debit`;
- success is displayed without an image-edit task;
- no source asset, output asset, provider call, version history, semantic verification, or reliable automatic refund exists.

Gemini/OpenAI image-edit SDKs and adapters are not installed. Pillow can crop, mask, composite, resize, and run deterministic checks, but cannot understand semantic edits such as removing ingredients.

## P0 Findings

1. Production user identity is not derived consistently from the authenticated session.
2. Client-controlled debit/refund requests are not a safe production billing workflow.
3. Refund does not verify the original debit, user, refundable balance, or aggregate refund ceiling.
4. Refinement overwrites fixed output paths instead of creating immutable parent/child asset versions.
5. Existing `generated_images` storage is not used by the customer generation/refinement flow.
6. Export is based on the newest menu/plan, not an explicit job and asset version list.
7. Selected-background composition now has a real Mask/identity foundation, but persisted exactness requires a lossless master. Platform JPEGs are derivatives and must not claim pixel identity.

## Minimum API Contract

```text
POST /api/image-refinements
Authorization: Bearer <session>
Idempotency-Key: <stable key>
Body:
  sourceAssetId
  sourceSha256
  editPrompt
  selectedBackground: { assetId, sha256 }

GET  /api/image-refinements/<refinementId>
POST /api/image-refinements/<refinementId>/cancel
GET  /api/image-assets/<rootAssetId>/versions
```

## Invariants

- User ID comes only from the server session.
- Source and background are authorized immutable private objects and are rehashed after retrieval.
- Server pricing creates deterministic `refine:<jobId>:debit` and `refine:<jobId>:refund` orders.
- Same idempotency key/same request returns one job; conflicting request returns 409.
- Provider readiness is checked before debit.
- Provider, timeout, cancellation, quality, or semantic-verification failure produces one idempotent full refund.
- Output objects are content addressed and record `rootAssetId`, `parentAssetId`, `versionNumber`, and SHA-256.
- Source assets are never overwritten.
- Background-locked edits may modify only the foreground subject. Background edits use a separate workflow.
- Canonical masters use lossless PNG and are reread to verify outside-mask pixels. Platform JPEGs are labeled derivatives.
- A configured semantic vision verifier must check dish identity and edit satisfaction. Without it, semantic edits fail closed and refund.
- Export receives explicit authorized asset/version IDs and binds the package to the job.

## Suggested Modules

- `refinement_service.py`: ownership, idempotency, billing, state machine, finalization
- `image_edit_provider.py`: provider interface and fail-closed readiness
- Worker typed task handler for refinement
- database helpers for immutable asset versions and refunds tied to original debits

The fixed public SaaS `/generate` prompt contract does not need to change; refinement can be an internal typed task carried by the shared queue.

## Acceptance Tests

- authenticated ownership and SHA conflict
- idempotency replay and conflict
- client user/points/provider/path fields ignored or rejected
- provider readiness before debit
- timeout/cancel/provider/quality/semantic failure refunds exactly once
- refund is bounded by the original debit and same user
- immutable parent-child version chain under concurrent creation
- Worker crash recovery does not double charge, double version, or over-refund
- persisted PNG outside-mask equality
- JPEG derivative never marked pixel-identical
- export remains bound to selected asset versions after another menu upload
- frontend stops calling generic `/api/debit` and `/api/refund` for refinement

This is a read-only audit, not an implemented refinement backend. No deployment, secret access, commit, push, or migration occurred.
