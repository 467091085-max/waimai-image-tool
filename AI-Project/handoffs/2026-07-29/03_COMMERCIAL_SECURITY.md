# Task 3: Points, Billing, Agent/Invite, Anti-Abuse, And Anti-Theft

Read `00_MASTER_BRIEF.md` first.

## Objective

Complete and harden the commercial and security invariants needed by generation, without deploying or performing real payments.

## Product Rules

- Standard generation: 10 points per image.
- Premium generation: 20 points per image.
- Custom refinement: 10 points per image.
- Watermark: 50 points per order.
- One platform is included; each additional platform costs 100 points.
- Agent model: one direct level only, 20% cash commission on eligible direct-customer net paid amount.
- C-end invite: inviter 50 points, invitee 50 points; direct invitee first recharge returns 10% in non-withdrawable points.
- Multi-level distribution is disabled.

## Required Research

- Trace billing, ledger, payment callbacks, generation debit/refund, agent binding, commission release/withdrawal, invite rewards, OTP/risk controls, signed asset access, one-time downloads, and admin roles.
- Find any UI-only success, duplicate-credit, duplicate-refund, authorization bypass, predictable object path, or replay weakness.

## Required Implementation

- Make generation/refinement debit and refund atomic and idempotent with job identity.
- Ensure payment callback/reconciliation cannot double-credit points or duplicate commissions.
- Enforce direct-level-only agent ownership and commission.
- Enforce invite reward uniqueness and first-recharge-only 10% points.
- Prevent self-invite, same-phone/device/account loops, repeated OTP abuse, and obvious IP/device registration farms using existing risk/audit abstractions.
- Enforce private asset ownership for previews, originals, refinement sources, and exports.
- Use short-lived signed URLs and persisted/atomic one-time token consumption where Redis/storage support exists.
- Prevent local paths, private object keys, bucket credentials, and original asset URLs from entering customer JSON.
- Ensure low-resolution/watermarked preview rules are separate from paid original download rights.
- Preserve finance/admin role boundaries for reconciliation, paid settlement, withdrawals, and point adjustment.
- Add audit events for security-sensitive allows and denies.

## Required Tests

- Pricing matrix and insufficient-balance behavior.
- Duplicate request/callback/refund idempotency.
- Direct-agent-only commission and refund reversal.
- Invite reward, first recharge, self-invite, and abuse rejection.
- Cross-user/cross-store image and export denial.
- Signed URL expiry, tampering, replay, and purpose/variant mismatch.
- No secret/private path fields in public payloads.
- Admin/finance/risk role authorization.
- Existing full test suite.

## Acceptance Criteria

- No frontend-only point deduction is authoritative.
- No failed terminal generation/refinement remains permanently charged.
- No user can access another user's original or export through an object key or copied URL.
- No indirect agent level receives commission.
- Test-only fake payment remains impossible in live runtime.
