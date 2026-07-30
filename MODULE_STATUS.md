# Product Module Status

This file is the authoritative implementation-status summary for the current
source tree. It replaces the earlier SQLite/MVP-only snapshot.

Status meanings:

- `Implemented`: the requested production code path exists and has executable
  contract or integration coverage.
- `Externally gated`: the adapter and fail-closed boundary exist, but proving
  the real vendor, account, IAM, network, or legal workflow requires an
  explicitly authorized external test.
- `Not deployed`: source is ready for a later controlled rollout, but the
  current task intentionally did not deploy or migrate an external database.

## Implementation Matrix

| Product area | Current code status | Primary evidence |
|---|---|---|
| Excel menu ingestion | Implemented | Multi-sheet `.xls`/`.xlsx` parsing, noisy-table filtering, immutable menu snapshots, owner binding, and 24-menu deterministic acceptance |
| Category and combo understanding | Implemented | Stable 40-leaf taxonomy, conservative unknown handling, complete combo fingerprints, staple/drink preservation, and forced generation instead of unsafe fuzzy reuse |
| Six category backgrounds | Implemented | Every taxonomy leaf has six distinct pure-background prompts; menu/store evidence selects the category and cache identity binds taxonomy/profile/prompt SHA-256 |
| Formal dish generation | Implemented | Authenticated frozen batch, selected-background asset identity, server pricing, PostgreSQL debit/outbox, Redis Worker execution, private manifest, reconciliation, refund, export, and cancellation |
| Exact selected background | Implemented | Immutable background byte snapshot, foreground/mask composition, outside-mask identity verification, lossless canonical PNG, and platform derivatives |
| Fine image editing | Implemented | Authenticated revision contract, free rework quota, paid refinement, Gemini adapter boundary, background-locked composition, Redis Worker, PostgreSQL settlement, repeat edit, export override, and opaque result access |
| Accounts and anti-abuse | Implemented | PostgreSQL users/sessions/stores, Redis OTP state and rate limits, SMS provider boundary, trusted-proxy context, registration risk evidence, and fail-closed live readiness |
| Points and payment | Implemented | Versioned server package catalog, authenticated orders, PostgreSQL wallet/ledger/payment transaction, idempotent Alipay callback and repair, refund/growth outbox, reconciliation, and live fake-provider denial |
| Direct agent and C-end invite | Implemented | One direct level only; agent first order 20%, repeat order 10%; inviter registration 100 points, invitee 20 points; direct first-recharge 10% points; abuse checks and immutable rule version |
| Commission and withdrawal | Implemented | PostgreSQL commission release, settlement, partial/cumulative refund reversal, clawback/liability, withdrawal reservation/status, finance-role gates, idempotency, and audit |
| Private assets and anti-theft | Implemented | Private object keys, owner/menu/order/job checks, short-lived signed access, low-resolution/watermarked previews, PostgreSQL one-time export nonce reservation, replay denial before download, bounded object/image reads, and access audit |
| PostgreSQL/Redis/object consistency | Implemented | Ordered migrations 001-014, migration checksums/readiness, transactional job/wallet/settlement/outbox stores, Redis Lua queue/lease/fence lifecycle, dispatcher, Worker heartbeats, reconciler, and COS/local object adapters with read-back SHA verification |
| AI asset reuse | Implemented | PostgreSQL asset library, category/name/component tags, pipeline-version and source-digest gates, review/disable workflow, selected-background association, and paid-provider avoidance on storage outage |
| Admin and audit | Implemented for required operations | Authenticated role-scoped reads and mutations for users/jobs/orders/wallets/assets/risk/commission/withdrawal, plus durable admin, finance, risk, and asset-access audit records |
| Render service topology | Implemented, not deployed | Customer Web, API, prompt Worker, outbox dispatcher, product Worker, growth Worker, reconciler, Redis, and PostgreSQL are declared separately with fail-closed readiness |

## Acceptance Evidence

- Complete default suite: `1266 passed, 20 skipped`.
- Complete disposable PostgreSQL 16 plus Redis 7 suite: `1286 passed`,
  zero skipped and zero failed.
- Local deterministic Excel acceptance: `24/24` menus, `3038/3038` rows,
  `9114` platform derivatives, `144` style-background calls, `3182`
  foreground calls, and `3182` mask calls.
- Category coverage audit: 40 taxonomy profiles, six unique prompts per
  profile, 24 menus, 3038 rows, and 516 combo rows.
- Static gates: 165 Python files parsed, 8 Render services parsed, both
  JavaScript files passed syntax checks, `git diff --check` passed, and the
  credential scan found no real credential.
- Independent security disposition: `P0=0`, `P1=0`, `P2=0`.
- ChatGPT Pro Task 5 v2 disposition: no confirmed P0/P1/P2 and `PASS
  candidate`; Codex independently reran all mandatory gates.

## External Activation Boundary

The following are external verification or operating-process work, not missing
code paths:

- Real SMS delivery, CAPTCHA/device-risk vendor behavior, and production phone
  abuse samples.
- Real Alipay merchant credentials, callback networking, refund settlement,
  tax/KYC, and actual cash payout.
- Real COS IAM/bucket policy/private-network behavior and restart recovery.
- Paid Hunyuan/Gemini quota, latency, and visual-quality acceptance across
  every category.
- Production-scale PostgreSQL failover and long-running Redis Worker stability.
- China-qualified legal review of agent contracts, marketing, withdrawal, and
  operating procedures.

Live readiness fails closed when required providers, secrets, schema,
heartbeats, or private storage are missing. The current closure intentionally
did not deploy, create Render resources, execute external database migrations,
or operate on real customer data.

## Intentional Product Boundaries

- Agent and consumer rewards have one direct relationship level only.
- C-end rewards are points and cannot be withdrawn as cash.
- Unknown or ambiguous dish names never force a fuzzy cross-category reuse;
  they are generated.
- Combo components can help composition but cannot masquerade as a complete
  combo product image.
- Local SQLite, fake payment, mock SMS, and local object storage remain
  explicit development compatibility paths and are forbidden in live
  readiness.
- A green deterministic acceptance run proves orchestration and invariants,
  not paid-provider visual quality or production deployment.
