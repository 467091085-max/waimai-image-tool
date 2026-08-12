# Current Task

## Goal
把外卖菜品图工具做成可上线的产品级系统，并解决 Codex 长任务失忆和上下文断裂。

## Current Step
Step 92 complete: the full 40-category x 6-style catalog is generated,
manually reviewed, hash-lock approved in private COS, frozen in commit
`b7b4395`, and pushed to the isolated staging branch. Render auto-deploy
`dep-d9n24ic9v7es73c3od8g` is live with the normal Gunicorn command. The
customer-ready checkpoint is 40 approved categories / 240 approved assets.

Step 93 complete: the final guarded Render acceptance passed with the exact
60-row workbook. The live test service classified it as `mixed_rice`, read and
SHA-verified all six approved backgrounds, generated and verified six free
samples, completed 60 formal images including 26 combo rows, validated the
server-owned point debit/refund arithmetic, and exported a ZIP containing all
60 Meituan images plus its manifest. The exact-instance public keep-alive made
26 successful probes with zero failures, and the redacted report is persisted
in private COS. Render has been restored to the normal Gunicorn command and all
nine temporary E2E variables have been removed.

Step 94 pending: this proves the protected staging flow for one real 60-row
menu, not production readiness or paid visual acceptance across all 40
categories. Production still requires Redis, an independent worker,
PostgreSQL deployment/migration, durable distributed single-flight/provider
recovery, and the separately authorized production integrations before main
can be promoted.

Step 95 in progress: the 2026-08-11 resumed audit is isolated on
`codex/gemini-dual-provider-staging`. The current Render test service is still
one free Web Service, not the declared Redis/PostgreSQL/Worker topology.
Gemini refinement code existed but the test service had no Gemini key and the
admin UI did not surface its readiness. A minimal candidate patch now exposes
sanitized Hunyuan/Gemini routing, disables unavailable Gemini refinement in
the customer UI, shows both providers in admin readiness, sends Gemini
Interactions requests with `store=false`, rejects non-Google endpoints, and
prevents permanent provider failures from being retried. The Render blueprint
also now injects `DATABASE_URL` into Product Worker and uses a dedicated public
`/healthz` liveness endpoint. Focused verification passed 132 tests; the
post-blueprint full run passed 1366 tests with 20 real-infrastructure tests
skipped.

Step 96 in progress: commit `dd84051` is live on the protected Render staging
service. Public `/healthz` returned 200, Hunyuan 3.0 and COS both reported
ready, and Gemini correctly reported unavailable without an API key. A fresh
remote run of the exact 60-row mixed-rice workbook passed upload, taxonomy,
six approved-background retrieval, SHA verification, and selected-background
binding. Its first free sample then failed after 75.961 seconds because the
provider result-image HTTPS download hit a TLS handshake timeout. The paid
generation request itself was not reported as rejected. A minimal candidate
patch now retries only that idempotent result-image GET, up to three bounded
attempts; it never resubmits the paid image-generation request. Focused
generation/security verification passes 83 tests.

Step 97 in progress: bounded result-image download retry commit `42a3739` is
live. The exact 60-row workbook then passed upload, `mixed_rice`
classification, all six approved COS backgrounds, selected `style-3`, and six
real Hunyuan free-sample requests in 330.054 seconds. Every output preserved
the selected background bytes, but visual review rejected two of six samples:
bracketed ingredients such as `【烤肉+烤排】` were malformed by combo parsing,
and the short prompt let Hunyuan interpret Chinese `烤排` as a western steak.
The formal job was canceled at 14/60; all 600 charged points were refunded and
the account returned to 1880 points. A minimal candidate now preserves
bracketed combo ingredients and adds mixed-rice, cutlet, choice, and
double/triple/quadruple-combo semantics without affecting non-rice categories.
Focused parser and selected-background verification passes 48 tests.
An independent security review then found that the result-image retry still
accepted arbitrary provider-controlled URLs and that the legacy replacement
path could start a second paid generation after a successful provider job but
failed result download. The candidate now accepts only credential-free HTTPS
under the Tencent result-domain allowlist, rejects non-public DNS answers,
revalidates every redirect, uses a shared retry deadline, and raises a
dedicated download error that cannot enter the paid text-generation fallback.
Security/generation focused verification passes 104 tests; full regression
initially passed 1374 tests with 20 real-infrastructure tests skipped. A second
review found uppercase/malformed result URLs could still escape the dedicated
error and an ambiguous TokenHub submit could trigger the legacy cloud fallback.
All provider-result processing failures now use the dedicated error, and a
configured TokenHub path fails closed without invoking a second paid provider.
The provider contract is `tokenhub-fail-closed-v2`; focused verification passes
115 tests and full regression initially passed 1375 tests with 20
infrastructure skips. The final P1 review extended the same error boundary over
directory creation and image/temp-file cleanup so cleanup errors cannot replace
the paid-result sentinel. Focused verification passes 116 tests and full
regression passes 1376 tests with 20 infrastructure skips.
Independent final P1 review passed for the default TokenHub staging route: no
reproducible automatic second paid generation remains. DNS connection binding,
strict DNS/body wall-clock enforcement, and cross-process result-download
recovery remain explicit P2 production work.

Step 98 in progress: the generation capacity layer now defaults to ten batch
workers, requests up to ten TokenHub slots, sets a 120-call formal-batch
budget, and exposes an explicit 100-images/3600-seconds performance contract.
Redis deployments share the active slots across Web, Product Worker, and
Prompt Worker through leased distributed slots; a missing Redis uses only a
process-local staging gate. Active provider concurrency is the lesser of the
requested and verified account limits, defaulting to one until a paid quota
test records the real value.
Both the working legacy TokenHub submit/query protocol and Tencent's current
official `hy-image-v3` synchronous protocol are supported without retrying a
paid POST. Gemini remains disabled without a key, but its official endpoint,
model, timeout, source-aspect output contract, frozen provider snapshot,
dedicated revision queue, and ten-consumer Revision Worker are now represented
in the candidate architecture. The formal API now rejects a batch above the
configured call limit before point debit. Capacity/Gemini/queue focused
verification passed `223 passed, 16 subtests passed`; the simulated 100-row
scheduler reached exactly ten concurrent row workers. Full regression,
staging deploy, real provider quota measurement, and visual acceptance remain
pending for this step.

Step 99 complete: background prompt-version binding now survives the complete
API snapshot, frozen batch contract, object storage, and Worker restore path.
Runtime v12 promotion is category-scoped to `mixed_rice` through
`MIXED_RICE_BACKGROUND_PROMPT_VERSION`; every other category remains on the
approved v11 contract. COS and PostgreSQL catalog validation now recompute
prompt hashes with the requested manifest version. Focused background,
contract, capacity, and acceptance verification passed `96 passed`. The
default remains v11 until six real v12 mixed-rice backgrounds pass paid visual
review.

Step 100 complete: the post-hardening full regression passed `1431 passed, 20
skipped` in 25.44 seconds. The skips remain live PostgreSQL/Redis integration
gates; the only warning is the pre-existing Python 3.9 LibreSSL warning. A
fresh deterministic run of the exact real menu plus static/security checks is
the next action before commit and staging deployment.

Step 101 complete: the exact 60-row mixed-rice workbook passed all eight
deterministic acceptance stages. The menu classified as `mixed_rice` with 96
confidence; six unique backgrounds, six representative free samples, 60
formal outputs including 26 combo rows, selected-background identity, point
arithmetic, 60 downloaded asset hashes, and a 61-entry Meituan ZIP all passed.
The report is `/tmp/waimai-mixed-rice-e2e-final.json`; it explicitly records
`deterministic-local`, `realProviderSmokePassed=false`, and no 100-image
capacity evidence. Generated repository test artifacts were removed.

Step 102 complete: the four capacity re-review findings are corrected. Real
acceptance now starts ten row workers, while the paid gate still clamps calls
to verified account concurrency. The 100-image/3600-second target is immutable
in code; ambiguous Redis release results retain the slot; and cloud Mask uses
an account-wide Redis gate in staging/production. Focused capacity, acceptance,
generation, selected-background, and Render checks passed `91 passed` plus
Python compilation and `git diff --check`.

Step 103 complete: the final post-capacity full regression passed `1435
passed, 20 skipped` in 24.89 seconds. The same live PostgreSQL/Redis skips and
pre-existing LibreSSL warning remain. Independent final capacity re-review is
running before the staging-branch commit.

Step 104 complete: the exact 60-row deterministic acceptance was repeated
after all capacity fixes and again passed all eight stages. The report remains
`/tmp/waimai-mixed-rice-e2e-final.json`, explicitly non-production and
non-paid. Generated repository artifacts were removed again.

Step 105 complete: the final cloud-Mask bypass is closed at the provider-call
boundary. Every staging/production cloud-Mask invocation, including the legacy
direct path when Chroma is disabled, now requires verified concurrency and a
Redis-distributed gate before any provider request. Focused verification
passed `92 passed`.

Step 106 complete: the final full regression after closing the direct cloud
Mask path passed `1436 passed, 20 skipped` in 25.53 seconds. Commit/push and
staging deployment remain pending the last independent review response.

Step 107 complete: independent final capacity review returned PASS for all
four negative boundaries and found no new P1. Background-version review and
Gemini/queue review also returned PASS. The candidate is ready for commit and
push to `codex/gemini-dual-provider-staging`; paid 10-way quota, paid 100-image
evidence, and v12 visual approval remain external gates, not claimed results.

Step 108 complete: candidate commit `2236784` was created and pushed only to
`origin/codex/gemini-dual-provider-staging`. `main`, production services,
databases, and production configuration were not changed. Render staging
deployment and live health/readiness verification are the active next action.

## Status
- 2026-08-11 live Render inventory: one free Python Web Service
  `waimai-image-tool-1`, branch `codex/gemini-dual-provider-staging`, commit
  `42a3739`;
  no deployed Redis, PostgreSQL, or independent Worker resources.
- 2026-08-11 live environment names include TokenHub/Tencent/COS staging
  configuration but no `GEMINI_API_KEY`; secret values were not copied into
  the repository or project memory.
- 2026-08-11 candidate regression: 1366 passed, 20 skipped. The skipped tests
  require live PostgreSQL/Redis and remain an external verification gate.
- Complete catalog freeze commit `b7b4395` is pushed; Render auto-deploy
  `dep-d9n24ic9v7es73c3od8g` reached live with the exact normal Gunicorn
  command and no catalog-review process.
- Real formal-generation staging root cause: the frontend correctly submits
  `/api/generation-jobs`, but the test service has no Redis and Render runtime
  policy therefore rejects the queue before debit with
  `redis_generation_queue_required`.
- Minimal staging queue correction: an in-process queue is permitted only when
  `APP_ENV=staging-demo`, staging Basic Auth is configured, and
  `ALLOW_STAGING_IN_PROCESS_GENERATION=true`. Production and every other
  Render environment continue to require Redis.
- Queue stale and terminal timeouts are environment-configurable while keeping
  the existing 300-second and 1800-second defaults. The staging test may opt
  into a two-hour timeout without weakening production defaults.
- Added a paid-call-gated Render HTTP acceptance runner. It validates the exact
  expected taxonomy, complete approved six-slot catalog, all six free samples,
  full formal manifest including combo rows, account debit/refund arithmetic,
  and downloadable Meituan ZIP, then persists its redacted report to private
  COS.
- Staging queue and real-acceptance focused verification passed `100 passed`.
  Full regression passed `1327 passed, 20 skipped`; scoped Python compilation
  and `git diff --check` passed.
- First real Render acceptance deploy `dep-d9n2dgjm8hqs73depqrg` passed
  preflight and uploaded all 60 rows, then failed closed at `plan`: expected
  `mixed_rice`, received `burger_hotdog`. No sample/formal provider call and no
  point debit occurred. The failed report is hash-verified in private COS at
  `generated/acceptance/render-staging/20260801T170318Z/report-fca2a922877bcdea.json`.
- Taxonomy failure root cause: immutable menu materialization renamed the Excel
  file to only its SHA-256. Re-parsing therefore lost the original store/file
  category signals and over-weighted repeated `热狗肠` add-on components.
- Minimal correction: keep the SHA-256 as the immutable parent directory while
  restoring a sanitized original filename; worker fallback recovers the same
  filename from the private object key. No taxonomy rule, catalog mapping, or
  category-specific exception was added.
- The same real workbook now materializes as
  `运营数据_美滋滋烤肉拌饭(成都店).xlsx`, parses all 60 rows, and resolves to
  `mixed_rice` with confidence 96. Snapshot/category and related focused
  regression passed `78 passed`; scoped compilation and `git diff --check`
  passed.
- Second real Render acceptance reached `plan` with the corrected taxonomy but
  failed closed because the test environment had not enabled approved-catalog
  reads. No paid provider call or point debit occurred. Its private-COS report
  is `generated/acceptance/render-staging/20260801T171145Z/report-5437934a2057da25.json`.
- The test environment now explicitly sets
  `BACKGROUND_CATALOG_APPROVED_ONLY=true` and
  `BACKGROUND_CATALOG_MANIFEST_BACKEND=object-storage`.
- Third real acceptance deploy `dep-d9n2ik3m8hqs73df3cu0` is live. It passed
  the 60-row upload, exact `mixed_rice` plan, all six approved-background COS
  reads and SHA checks, then selected `style-3` with SHA prefix
  `b432ea0bcb2a`. Paid six-sample generation is the active stage.
- The first paid sample returned HTTP 200 in about 113 seconds, proving the
  configured TokenHub generation path works. Its selected item was the
  non-food row `祝顾客:马年行大运,万事皆顺意`, exposing a deterministic
  preview-selection defect rather than a provider failure.
- Root cause: `preview_sample_entries()` discarded all combos and selected the
  first six single rows. In this workbook those rows are an announcement and
  low-price promotional add-ons, not representative mixed-rice products.
- Stopped the paid run before formal generation and restored normal Gunicorn
  with deploy `dep-d9n2l2bl550s7396t2o0`, which is live.
- Minimal correction selects two primary-category combos first, then
  primary-category singles and remaining real combos, deduplicates normalized
  names, and excludes explicit announcement rows. Formal generation and Excel
  parsing are unchanged.
- The corrected selector returned six unique non-announcement products for all
  24 local Excel menus. The exact mixed-rice workbook now selects five main
  combos and one mixed-rice dish. Complete regression passed `1330 passed, 20
  skipped`; scoped compilation and `git diff --check` passed.
- A fourth run proved the corrected first sample is a main mixed-rice combo,
  but it remained in the request for more than four minutes because the exact
  background pipeline was still using two serial cloud operations: foreground
  generation and cloud mask extraction. The run was stopped before a formal
  job.
- Enabled the existing tested staging fast path with
  `EXACT_BACKGROUND_CHROMA_FAST_PATH=true`. Normal success now uses one
  TokenHub foreground call plus local chroma mask extraction; invalid chroma
  output still falls back to the cloud mask and all outside-mask background
  pixel checks remain required.
- Fast-path real acceptance deploy `dep-d9n2s9flk1mc73dl0gqg` completed the
  six-sample stage and stopped at the formal-job status-poll transport failure.
- Fast-path acceptance generated six distinct, representative mixed-rice
  dish/combo samples in 68-94 seconds each. All six returned HTTP 200, used the
  selected `style-3` background SHA `b432ea0bcb2a`, and passed downloaded-output
  SHA verification plus exact-background identity checks.
- The formal job `generation-a31ace122ac40eab84e60a00` was accepted, but the
  acceptance client received one `ConnectionResetError(104)` on its third
  idempotent status poll. The server remained reachable; the failure report is
  private-COS object
  `generated/acceptance/render-staging/20260801T173449Z/report-9e113340a8ad19a9.json`.
- Stopped the unmonitored in-process task by restoring normal Gunicorn. Restore
  deploy `dep-d9n329bl550s7397if5g` is live.
- The first local patch retried all GET requests after connection resets,
  timeouts, or bounded transient HTTP statuses while leaving POST single-attempt.
  Its tests passed, but the next real run proved that boundary was too broad.
- Rejected that first retry boundary before completing a rerun: the legacy
  `/api/style-preview-sample` GET performs a paid generation side effect. If a
  request crossed its five-minute timeout, a generic GET retry could duplicate
  a provider call before the first server thread finished.
- Stopped the rerun after two successful samples and before the long third
  sample reached the retry boundary. Restored normal Gunicorn in live deploy
  `dep-d9n39fflk1mc73dllnt0`.
- The corrected retry allowlist contains only
  `/api/generation-jobs/...` status/manifest reads. Sample generation,
  background generation, exports, and every POST remain single-attempt.
  Focused verification passed `12 passed`; complete regression passed `1334
  passed, 20 skipped`; scoped compilation and `git diff --check` passed.
- Implemented a local formal-generation speed path: `standard` rows reuse the
  exact same-run free-preview PNG only after selected-background identity,
  pipeline version, persisted output SHA-256, and pixel-preservation metadata
  all verify. `premium` rows still call the provider.
- Formal cached, approved-asset, and preview-reuse outputs now count as both
  cached and succeeded, so a fully reused manifest can complete correctly.
  Private preview cache unavailability is treated as a cache miss and falls
  back to normal exact-background generation; the paid preview GET is never
  retried. Focused selected-background verification passed `16 passed`; full
  regression passed `1336 passed, 20 skipped` in 26.81 seconds. Scoped Python
  compilation and `git diff --check` passed. Commit `15d89e9` is pushed to the
  isolated staging branch, and normal-Gunicorn Render deploy
  `dep-d9n3dogae00c73f1li10` is live.
- Raised only the staging acceptance client's single-request timeout from 300
  to 600 seconds so a slow paid sample cannot be mistaken for a failed request
  or retried. Environment deploy `dep-d9n3f1rncjis739coju0` is live; provider
  concurrency and production defaults are unchanged.
- Real Excel acceptance deploy `dep-d9n3fujm8hqs73dgts50` is live. It passed
  preflight, uploaded all 60 rows, resolved the menu to `mixed_rice`, retrieved
  and SHA-verified all six approved backgrounds, and selected `style-3` with
  SHA prefix `b432ea0bcb2a`.
- All six paid representative samples completed once each with HTTP 200 in
  62-87 seconds. The runner downloaded and verified them, then accepted formal
  job `generation-76caa391a69b9c59f015174f`; the 60-row formal stage is now
  complete at the queue level.
- Manifest acceptance failed closed: `succeeded=7`, `failed=0`, `pending=53`.
  This exact shape proves the runtime formal-generation call budget was 1:
  six verified preview reuses plus one provider result, followed by 53
  `TENCENT_HUNYUAN_SYNC_LIMIT` pending rows. The private-COS report is
  `generated/acceptance/render-staging/20260801T181646Z/report-ceec3e01ae266e38.json`
  with SHA-256 `ceec3e01ae266e38497d1106f401635686954f59254d6efd6b5ae4c3efc7aa95`.
- Restoring the normal Gunicorn command before changing any environment value,
  so a configuration deploy cannot accidentally rerun the paid acceptance
  entrypoint.
- 2026-08-01 Render TokenHub readiness: confirmed ready
- 2026-08-01 real Excel upload: 56 rows / 0 parse errors
- 2026-08-01 single background transport probe: passed, HTTP 200
- 2026-08-01 frontend-equivalent two-at-a-time six-background probe: passed, 6/6 HTTP 200
- 2026-08-01 downloaded six-background visual review: failed; multiple outputs contain raised rectangular plinths/color-block-like slabs
- Root cause split: provider transport currently works; prompt design, content QA, and reusable catalog architecture remain blocking
- Real `light_food` catalog pilot: generated and SHA-verified in private COS as
  six `pending` assets; no asset was auto-approved
- v9 pilot visual result: `style-3` and `style-6` usable; `style-1`, `style-2`,
  and `style-4` contain raised plinths, while `style-5` contains a white
  rectangular mat; category remains unavailable to customers
- Prompt v10 root cause correction: removed product-display cues such as
  `承载安全区`, requires continuous material through the center, puts the
  tabletop front edge outside the crop, and explicitly forbids a second plane
- Prompt/catalog focused verification after v10: `61 passed, 1 skipped`
- Full local regression after v10: `1304 passed, 20 skipped`
- Paid v10 pilot: 6/6 generated and SHA-verified in private COS, but all remain
  `pending`; style 2 contains overlapping paper sheets and styles 3, 4, and 6
  expose table fronts/slabs, so the group was not approved
- Provider-contract root cause: TokenHub `hy-image-v3.0` was treated like the
  Lite endpoint; its unsupported negative-prompt field could be ignored while
  prompt rewriting remained on by default and changed the requested geometry
- Prompt v11 fix: v3 catalog requests explicitly send `Revise=0` and a stable
  1..4294967295 seed per category/style; Lite-only negative prompts stay on the
  Lite path, and solid slots now use one category color without paper wording
- Prompt v11 focused verification: `91 passed, 1 skipped`
- Full local regression after v11: `1306 passed, 20 skipped`; scoped Python
  compilation and `git diff --check` passed
- Paid v11 pilot: 6/6 generated, uploaded, and SHA-verified in private COS;
  exact hashes are recorded in the execution log and all six remain `pending`
  until the hash-locked approval operation is implemented and run
- v11 visual result: styles 1 and 2 are empty single-color seamless spaces;
  styles 3 through 6 are ordinary full-size dining tables. No food, prop,
  paper roll, rectangular mat, small product plinth, inset image, or frame was
  found. The light-food six-slot set passes the revised product visual gate
- Render was restored after the pilot to the normal Gunicorn command; restore
  deploy `dep-d9mu195aeets73aq62b0` is live
- Remote paid-work resume: complete current-version manifests now re-read and
  verify all six prompt hashes, immutable keys, file sizes, object bytes, and
  SHA-256 values before skipping provider calls
- Batch checkpointing: each category manifest is persisted immediately after
  its sixth style, instead of waiting until the entire 240-image run finishes
- Review operator: approval requires all six exact reviewed SHA-256 values and
  writes reviewer/time/note metadata only after read-back verification
- Review contact sheet: every completed category gets a SHA-addressed 3-by-2
  sheet in private COS for efficient six-image visual review
- Catalog/resume/review focused verification: `98 passed, 1 skipped`
- Full local regression after resumable review flow: `1313 passed, 20 skipped`
- `light_food` approval: complete. The exact six recorded v11 hashes are now
  `approved` in the private COS manifest, reviewed by
  `catalog-visual-review` at `2026-08-01T12:19:51Z`
- Light-food review contact sheet:
  `contact-sheet-45170ad94f6665d1c5750c86ce60f5284443bdfc63b5fddb3bb3de5947c6e768.jpg`
- Approval operation deploy `dep-d9mu8he1egvs73f0980g` succeeded; the service
  was restored to normal Gunicorn start command and deploy
  `dep-d9mu95daeets73aqmh8g` is live
- First remaining-category batch deploy `dep-d9muan2jnfac739vofpg` is live and
  completed the five-category catalog builder in the background. The final
  report is `complete=true`, `completedAssetCount=30`, and `failureCount=0`;
  all new assets remain `pending` until visual review.
- Tencent console confirms `hy-image-v3.0` is running with per-image billing
  enabled. Its free allowance is exhausted, but postpaid service is active;
  the batch therefore did not depend on a free quota.
- First-batch visual gate: `topped_rice` and `mixed_rice` passed and are now
  hash-lock approved, bringing the approved catalog to 3 categories / 18
  assets including `light_food`. `rice_noodles` failed because four table
  slots became segmented color-block surfaces and style 5 added a second
  table plane. `porridge_soup_rice` and `wheat_noodles` remain pending because
  their second solid slot became a large decorative wood arch.
- Prompt-profile v10 compatibility patch is locally implemented: the 18
  approved v11 prompt hashes are frozen, while all unapproved flat-table
  prompts require one material and one color and forbid patchwork/inlay/color
  blocking. The unapproved second solid slot now uses the second palette
  color. Focused verification passed `28 passed`; complete regression passed
  `1315 passed, 20 skipped`, and scoped compilation plus `git diff --check`
  passed.
- Corrected three-category regeneration deploy `dep-d9muns3m8hqs73d6uh8g`
  completed `18/18` assets with zero failures. Exact COS contact sheets for
  `porridge_soup_rice`, `rice_noodles`, and `wheat_noodles` passed visual
  review: no food, props, plinths, inset frames, patchwork, mixed-material
  tabletops, or second planes remained.
- Hash-lock approval deploy `dep-d9musljl550s7390l4jg` approved all three
  manifests after exact six-hash object read-back. Review times were
  `2026-08-01T13:02:42Z`, `2026-08-01T13:03:11Z`, and
  `2026-08-01T13:03:39Z`. The approved catalog is now 6 categories / 36 assets.
- Second five-category batch deploy `dep-d9muuj2jnfac73a13dhg` completed
  `29/30` assets. `dumpling_wonton`, `buns_dim_sum`, `chinese_wraps`, and
  `malatang_maocai` each have a complete pending manifest. Only
  `hotpot_skewers/style-2` failed with TokenHub
  `FailedOperation.ImageIllegalDetected`; the other five slots succeeded.
- Fixed ineffective paid retries: attempt 1 keeps the existing exact seed,
  while later attempts use distinct deterministic retry seeds. Legacy Tencent
  fallback now removes TokenHub-v3-only `Revise` and `Seed` fields. Focused
  generation tests passed `36 passed`; complete regression passed
  `1317 passed, 20 skipped`; Python compilation and `git diff --check` passed.
- Retry patch commit `312d578` is pushed to the isolated staging branch and
  Render deploy `dep-d9mv5f3m8hqs73cjh8u0` is live.
- Hash-lock approved `dumpling_wonton`, `buns_dim_sum`, `chinese_wraps`, and
  `malatang_maocai` after exact COS contact-sheet review. The approved catalog
  checkpoint is now 10 categories / 60 assets.
- Current batch deploy `dep-d9mv7qfqj5pc73dsecig` completed 30/30 with zero
  provider failures. `hotpot_skewers` and `barbecue` passed visual review.
  `fried_chicken`, `burger_hotdog`, and `pizza` failed the visual gate and stay
  pending. Selective replacement of failed slots is the active implementation
  step; no failed category is marked approved.
- Selective pending-slot replacement is implemented. It requires one existing
  complete pending remote manifest, re-verifies all six COS objects, uses an
  explicit deterministic seed revision only for named rejected slots, preserves
  the other exact hashes, writes a fresh pending manifest/contact sheet, and
  refuses to modify an approved manifest.
- Selective-regeneration focused tests passed `16 passed`; full regression
  passed `1320 passed, 20 skipped`; Python compilation and `git diff --check`
  passed.
- Hash-lock approved `hotpot_skewers` at `2026-08-01T13:47:17Z` and
  `barbecue` at `2026-08-01T13:47:47Z`; the catalog checkpoint is 12 categories
  / 72 approved assets.
- Real selective regeneration completed 5/5 provider calls and preserved all
  unselected remote hashes. New table slots improved, but alternate seeds did
  not cure the repeated two-color solid scenes in `fried_chicken` and
  `burger_hotdog`.
- Prompt profile v11 removes the ambiguous `辅色` instruction for all unapproved
  style-2 slots and requires one identical hue across wall, architectural
  curve, and floor. Exact prompt bytes for all 12 approved categories are
  frozen. Selective replacement may accept a stale prompt hash only for the
  exact slot being replaced; every preserved slot still requires its current
  prompt hash and COS byte hash.
- Profile-v11 and selective stale-prompt focused verification passed `29
  passed`; full regression passed `1322 passed, 20 skipped`; Python compilation,
  maximum prompt-length check, and `git diff --check` passed.
- Corrected `fried_chicken`, `burger_hotdog`, and `pizza` contact sheets passed
  exact COS visual review. Hash-lock approval deploy
  `dep-d9mvpm2jnfac73a32hvg` reported `reviewStatus=approved` at
  `2026-08-01T14:04:59Z`, `2026-08-01T14:05:38Z`, and
  `2026-08-01T14:06:11Z`. The catalog checkpoint is 15 categories / 90 assets.
- Five-category deploy `dep-d9mvthnlk1mc73dgkn3g` completed 30/30 real paid
  images with `failureCount=0`. COS visual review passed `pasta_steak`,
  `korean`, and `southeast_asian`; `sandwich_bagel/style-2` and
  `japanese/style-2` remain rejected pending profile-v12 selective replacement.
- Five-category deploy `dep-d9n0k8942hec73elqfbg` completed 30/30 paid images
  for `sichuan_hunan`, `cantonese_roast`, `jiangzhe`, `northeast_chinese`, and
  `northwest_xinjiang` with `failureCount=0`.
- Exact COS visual review passed all five six-slot contact sheets. The review
  applies the frozen product rule that a broad full-size dining table may show
  a front edge or legs; a small central plinth, isolated slab, mat, prop, food,
  or inset image remains a failure. A local candidate that would have forbidden
  normal dining-table geometry was discarded before deployment.
- Approval deploy `dep-d9n0r3qjnfac73a5dnkg` hash-lock approved the five
  manifests at `2026-08-01T15:16:05Z`, `2026-08-01T15:16:39Z`,
  `2026-08-01T15:17:15Z`, `2026-08-01T15:17:49Z`, and
  `2026-08-01T15:18:26Z`. The catalog checkpoint is 25 categories / 150
  approved assets.
- Added exact prompt-hash regression coverage for the five newly approved
  categories. Focused verification passed `20 passed`; full regression passed
  `1324 passed, 20 skipped`; maximum prompt length is 588 and scoped Python
  compilation plus `git diff --check` passed.
- Restored the Render start command to normal Gunicorn; restore deploy
  `dep-d9n0tnrm8hqs73dbqq20` is live and its exact running command contains no
  paid generation or approval process.
- Prompt-freeze commit `c91660f` is pushed; its auto-deploy reached live with
  normal Gunicorn before the next paid operation started.
- Paid batch deploy `dep-d9n0vq942hec73emdrng` is running categories 26 through
  30 and completed 30/30 paid assets with zero failures. All five contact
  sheets passed visual review. Approval deploy `dep-d9n17djl550s7394dt70`
  completed exact object read-back and six-hash approval writes for all five.
- Catalog checkpoint: 30 approved categories / 180 approved assets.
- Added the five newly approved categories to the immutable single-hue and
  normalized-palette compatibility sets; exact prompt-hash test passes `15
  passed`.
- Full regression passed `1324 passed, 20 skipped`; maximum prompt length is
  588, scoped Python compilation passed, and `git diff --check` passed.
- Restored the Render start command to normal Gunicorn after approval. Restore
  deploy `dep-d9n1b90ae00c73alqhr0` is live; its running command contains no
  paid catalog-generation or approval process.
- Pushed prompt-freeze commit `2246870`; auto-deploy
  `dep-d9n1bvk9v7es73c359u0` is live with normal Gunicorn.
- Started paid generation deploy `dep-d9n1com417fc73cf9hog` for
  `braised_cooked_food`, `soup_stew`, `steamed_claypot`, `milk_fruit_tea`, and
  `coffee_cocoa`; all resulting assets must remain pending until exact COS
  visual review and six-hash approval.
- `braised_cooked_food` contact sheet
  `contact-sheet-bb28df8a67c88ff8837f2adafcf2700984c5a092cb98564d540dbcf0331e1699.jpg`
  passed manual review: two seamless empty scenes and four broad full-size
  tables, with no food, prop, small plinth, isolated slab, mat, or inset frame.
- `soup_stew` contact sheet
  `contact-sheet-6c1264c7fa33e3883dc2eac72f75b65b4c4a54efcc67658b7616f79cb8cab75b.jpg`
  passed the same manual product gate and remains pending.
- `steamed_claypot` contact sheet
  `contact-sheet-daccad121081500c5067cbedb57f7c71151a20542972f5360797c9b107dff30a.jpg`
  passed manual review and remains pending.
- `milk_fruit_tea` contact sheet
  `contact-sheet-ec01e9d5e1d1c98b7cd916c29b53b5b780efe51e770c96603820c7f784768eff.jpg`
  passed manual review and remains pending.
- `coffee_cocoa` contact sheet
  `contact-sheet-f5055aacdc40761b6598131bbcfbdc89d6cdd46a32fc58958bbca0f2995fb8ec.jpg`
  passed manual review and remains pending.
- Generation deploy `dep-d9n1com417fc73cf9hog` finished with
  `complete=true`, `completedAssetCount=30`, `failureCount=0`, and sentinel
  `CATALOG_BATCH_31_35_COMPLETE`.
- Started approval deploy `dep-d9n1j9m417fc73cfmii0` with all 30 exact
  manually reviewed image hashes.
- Approval deploy `dep-d9n1j9m417fc73cfmii0` approved
  `braised_cooked_food` at `2026-08-01T16:07:39Z`, `soup_stew` at
  `2026-08-01T16:08:06Z`, `steamed_claypot` at `2026-08-01T16:08:41Z`,
  `milk_fruit_tea` at `2026-08-01T16:09:11Z`, and `coffee_cocoa` at
  `2026-08-01T16:09:39Z`; sentinel `CATALOG_BATCH_31_35_APPROVED` completed.
- Catalog checkpoint: 35 approved categories / 210 approved assets.
- Added exact prompt-hash compatibility coverage for the five new categories.
  Focused verification passed `15 passed`; full regression passed `1324
  passed, 20 skipped`; maximum prompt length is 588, scoped compilation and
  `git diff --check` passed.
- Restored normal Gunicorn startup; deploy `dep-d9n1lsp42hec73eno410` is live
  with no paid generation or approval process in the running command.
- Pushed the 35-category freeze as commit `418707d`; auto-deploy
  `dep-d9n1mn8ae00c73f08jmg` is live with normal Gunicorn.
- Started final paid generation deploy `dep-d9n1nfvlk1mc73dj7cp0` for
  `bottled_drinks`, `fresh_drinks`, `dessert_bakery`, `fried_snacks`, and
  `fruit`; all 30 outputs must remain pending until visual and hash review.
- `bottled_drinks` contact sheet
  `contact-sheet-e99156fc43311b64893d61064becf9b2fad7aeccf4bc1bb9a033989955437128.jpg`
  passed manual review and remains pending.
- `fresh_drinks` contact sheet
  `contact-sheet-fb8144f54d4edd22cf21e132d5f8dc3d5796d4421b48217753ea8703ffe9810b.jpg`
  passed manual review and remains pending.
- `dessert_bakery` contact sheet
  `contact-sheet-b70dfa0951010e90672170693f64b95461b055f94c322a1bf6c69a64f34c805f.jpg`
  passed manual review and remains pending.
- `fried_snacks` contact sheet
  `contact-sheet-c6fd6bed82e3adaff503cc864f62a330afca25a67d366da4f321c75ac1ea477e.jpg`
  passed manual review and remains pending.
- `fruit` contact sheet
  `contact-sheet-a43ae5e8c513c0d8f63f1f3e202d9121654e75ddf988054e09a768c442986439.jpg`
  passed manual review and remains pending.
- Final generation deploy `dep-d9n1nfvlk1mc73dj7cp0` finished with
  `complete=true`, `completedAssetCount=30`, `failureCount=0`, and sentinel
  `CATALOG_BATCH_36_40_COMPLETE`.
- Started final approval deploy `dep-d9n1toflk1mc73djgftg` with all 30 exact
  manually reviewed image hashes.
- Final approval deploy approved `bottled_drinks` at
  `2026-08-01T16:30:15Z` and `fresh_drinks` at `2026-08-01T16:30:41Z`, then a
  COS SSL/read timeout stopped the command chain before the remaining three
  categories. No image regeneration is required; retry only the unapproved
  categories with their exact reviewed hashes.
- Remaining-three approval retry deploy `dep-d9n20h0ae00c73amvtj0` is running
  for `dessert_bakery`, `fried_snacks`, and `fruit` only.
- Staging browser false provider-failure root cause: fixed locally; private media required a Bearer token even though protected staging legitimately uses same-origin Basic Auth
- Private-media failures now remain distinct from Hunyuan provider failures: done locally
- Focused staging/customer UI contract verification: done, 27 passed
- Full regression after private-media staging fix: done, 1284 passed / 20 skipped
- Private-media staging fix deployed: done, commit `d6cbf15`
- Real browser upload/background display after deploy: done; 6/6 background cards and 6/6 blob images, no Hunyuan/media failure text, no console errors
- Current light-food background content acceptance: failed; raised plinths plus forbidden cup/flowers/cloth remain
- Sanitized Pro package: `/tmp/waimai-background-architecture-8f9afd6db1ea.zip`, SHA-256 `5394b3f88d938412ea37575112e4e40e5882c27283ca32a51b181a0a819825a6`
- ChatGPT Pro architecture review: delivered and triaged, `https://chatgpt.com/c/6a6dc7fa-9140-83e8-84ff-3f038953ce8b`
- ChatGPT Pro provider/UI failure review: delivered and triaged, `https://chatgpt.com/c/6a6dc81d-0b38-83e8-bfa2-5fb67fde2e94`
- ChatGPT Pro visual prompt review: delivered and triaged, `https://chatgpt.com/c/6a6dc847-c370-83e8-bb28-93b3f3191a66`
- Three ChatGPT Pro background reviews: delivered and independently triaged;
  accepted/rejected findings are saved in
  `AI-Project/handoffs/2026-08-01/CHATGPT_PRO_BACKGROUND_REVIEWS.md`.
- Chroma residual false-success root cause: done
- Fail-closed chroma-spill validator and cloud-Mask fallback: done locally
- Lossless RGB PNG chroma intermediate: done locally
- Test-service TokenHub in-process single-flight: done locally
- Chroma-spill and exact-background focused tests: done, 22 passed
- Provider concurrency plus image-path focused tests: done, 50 passed
- Real two-at-a-time six-background staging run: done, 6/6 success and exact SHA match
- First v3 real six-sample run: rejected, one remaining cyan halo on `煎蛋`
- Calibrated 0.4% residual-chroma threshold and pipeline v4: done locally
- Updated focused tests: done, 51 passed
- Full regression after calibrated threshold: done, 1283 passed / 20 skipped
- Render v4 deployment: done, commit `37429f9`
- Real pipeline-v4 six-sample acceptance: done, 6/6 visual and SHA pass
- Formal compositor probe: done, 1 succeeded / 55 limited by staging sync cap
- Full 56-row paid formal batch: not accepted
- Durable staging acceptance report: done
- Obsidian memory structure: done
- Render background generation failure reproduced: done
- Root cause located: done
- Async background generation patch: in progress
- Targeted async background tests: done
- Local tests after async patch: done
- Sync to deploy repository: done
- Deploy repository tests: done
- GitHub push for Render deploy: done
- Render deployment: done
- Render `/api/plan` verification: done
- Render Hunyuan background generation verification: blocked by Tencent Cloud ResourceInsufficient
- ResourceInsufficient UI copy: done
- ResourceInsufficient UI copy deployment: done
- Tencent Cloud console inspection: done
- TokenHub image API support patch: done
- Deploy repository TokenHub patch tests: done
- TokenHub patch pushed to GitHub and deployed on Render: done
- Render TokenHub readiness verification: blocked by missing `TENCENT_TOKENHUB_API_KEY`
- Ops readiness generation provider gate: done locally
- Admin ops readiness generation provider card: done locally
- Local productization readiness tests: done
- Deploy repository readiness patch tests: done
- Ops readiness generation provider gate deployed to Render: done
- Render `/api/ops/readiness` generation provider verification: done
- Tencent COS object storage backend: done locally
- Object route reads through storage facade instead of local path only: done locally
- Deploy repository COS backend tests: done
- COS backend pushed to GitHub main: done
- Render object storage readiness gate: done locally
- Render COS blueprint env vars: done locally
- Render object storage gate deploy repo tests: done
- Render object storage gate pushed to GitHub main: done
- Render object storage gate online verification: done
- Export ZIP object storage handoff: done locally
- Export package DB tracking: done locally
- Export object storage targeted tests: done locally
- Export object storage full tests: done
- Export object storage deploy repo sync: done
- Export object storage pushed to GitHub main: done
- Render export smoke test: done, blocked from object storage path by missing Render env
- Menu upload object storage handoff: done locally
- Menu upload DB tracking: done locally
- Menu upload targeted tests: done locally
- Menu upload full tests: done
- Menu upload deploy repo sync: done
- Menu upload pushed to GitHub main: done
- Render readiness smoke after menu upload patch: done
- Library upload object storage handoff: done locally
- Library upload DB tracking: done locally
- Library upload targeted tests: done locally
- Library upload full tests: done
- Library upload deploy repo sync: done
- Library upload pushed to GitHub main: done
- Render readiness smoke after library upload patch: done
- Payment provider live-runtime gate: done locally
- Payment readiness render/staging detection: done locally
- Payment provider targeted tests: done locally
- Payment provider full regression: done locally
- Payment provider deploy repo sync/tests: done
- Payment provider pushed to GitHub main: done
- Render payment provider live-runtime verification: done
- Tencent Cloud console reinspection after user login: blocked by Computer Use URL policy
- Tencent Cloud console reinspection after fresh login: blocked again by Computer Use URL policy
- Withdrawal admin status audit: done locally
- Withdrawal admin status audit tests: done locally
- Withdrawal admin status audit deploy repo sync/tests: done
- Withdrawal admin status audit pushed to GitHub main: done
- Render readiness smoke after withdrawal audit patch: done
- Withdrawal admin paid finance-role gate: done locally
- Withdrawal admin paid finance-role tests: done locally
- Withdrawal admin paid finance-role deploy repo sync/tests: done
- Withdrawal admin paid finance-role pushed to GitHub main: done
- Render readiness smoke after withdrawal role patch: done
- Commission settlement paid finance-role gate: done locally
- Commission settlement paid finance-role tests: done locally
- Commission settlement paid finance-role deploy repo sync/tests: done
- Commission settlement paid finance-role pushed to GitHub main: done
- Render readiness smoke after commission settlement role patch: done
- AI asset status role gate: done locally
- AI asset status role tests: done locally
- AI asset status role deploy repo sync/tests: done
- AI asset status role pushed to GitHub main: done
- Render readiness smoke after AI asset role patch: done
- Risk deny role gate: done locally
- Risk deny role tests: done locally
- Risk deny role deploy repo sync/tests: done
- Risk deny role pushed to GitHub main: done
- Render readiness smoke after risk role patch: done
- Deployment config report endpoint: done locally
- Deployment config report tests: done locally
- Deployment config report deploy repo sync/tests: done
- Deployment config report pushed to GitHub main: done
- Render deployment config endpoint verification: done
- Real payment checkout fail-closed guard: done locally
- Real payment checkout fail-closed tests: done locally
- Real payment checkout fail-closed deploy repo sync/tests: done
- Real payment checkout fail-closed pushed to GitHub main: done
- Render real payment fail-closed smoke: done
- Alipay page payment adapter: done locally
- Alipay notify signature verification and points credit: done locally
- Payment docs and Render blueprint env vars for Alipay: done locally
- Alipay adapter deploy repo sync/tests: done
- Alipay adapter pushed to GitHub main: done
- Render Alipay code deployment verification: done
- Render Alipay missing-config fail-closed smoke: done
- Payment manual reconciliation service: done locally
- Payment manual reconciliation finance-role admin API: done locally
- Payment manual reconciliation audit and idempotency tests: done locally
- Payment manual reconciliation deploy repo sync/tests: done
- Payment manual reconciliation pushed to GitHub main: done
- Render payment reconciliation route verification: done
- SaaS API Server directory: done locally
- SaaS Worker directory: done locally
- Shared Redis queue/status module: done locally
- SaaS runtime tests: done locally
- Render start command updated to `api-server`: done locally
- Deploy repo SaaS skeleton tests: done
- SaaS skeleton pushed to GitHub main: done
- Render SaaS `/healthz` verification: pending; current Render service still serves old monolith start command
- Worker task timeout support: done locally
- Worker stale running recovery: done locally
- Worker timeout/recovery deploy repo tests: done
- Worker timeout/recovery pushed to GitHub main: done
- Final SaaS API contract enforcement: done locally
- Final SaaS API contract deploy repo tests: done
- Final SaaS API contract pushed to GitHub main: done
- Final SaaS health check report: done locally
- Product output-generation specification export: done locally
- Dual-agent baseline audit: done
- Sanitized ChatGPT Pro source package: done
- ChatGPT Pro source handoff and corrected review: done
- Render six free sample generation: TokenHub configured and real-menu six-sample smoke passed
- Free-sample truthful error/progressive loading patch: done locally
- Free-sample focused and full regression tests: done locally
- Local real-menu browser flow: done with deterministic fallback; progressive `2/6` then `6/6` observed
- Task 2 sanitized current-source package and secret scan: done
- Task 2 ChatGPT Pro conversation: in progress
- Selected-background and Redis-boundary subagent audits: done
- Deterministic foreground-mask compositor: done locally
- Full-frame platform cover-crop conversion: done locally
- Deterministic compositor targeted tests: done, 6 passed
- Existing image-pipeline tests after cover-crop change: done, 7 passed
- Selected background asset identity generation and validation: done locally
- Frontend sample/formal/export background asset propagation: done locally
- Background digest cache invalidation paths: done locally
- Generation/UI/product API focused regression after identity patch: done, 82 passed
- Real foreground-mask provider composition: done locally
- Selected-background exact pipeline focused regression: done, 94 passed
- Pure-background prompt cache versioning: done locally
- Customer SVG/color-block generation fallback removal: done locally
- Immutable selected-background byte snapshot: done locally
- Foreground and Mask cache digest/version validation: done locally
- Selected-background HTTP and frontend propagation contracts: done, 107 focused tests passed
- Lossless exact-background PNG persistence and post-write identity verification: done locally
- Selected-background quality gate and tamper-resistant output cache validation: done locally
- Immutable server-owned generation batch contract and billing snapshot: done locally
- Redis, taxonomy/combo, and refinement gap audits: done and saved under `AI-Project/handoffs/2026-07-29/`
- Latest focused regression after exact-output and batch-contract hardening: done, 138 passed
- Latest full regression after exact-output and batch-contract hardening: done, 393 passed
- Redis atomic idempotent enqueue: done locally
- Redis claim/lease/heartbeat/ack lifecycle: done locally
- Redis Lua execution against a Lua-capable Redis test implementation: done locally
- Independent Redis reliability P0 review: done
- Public enqueue single-Lua atomicity and claim move-plus-lease atomicity: done locally
- Continuous provider heartbeat, FIFO receipts, dead-letter path, Redis-time leases, and Worker-loop backoff: done locally
- Redis terminal retention/TTL and restart/failover acceptance: done locally with production Lua executed through Lua-capable fakeredis
- Generation job server-session principal and owner checks: done locally
- Client job ID user namespace and spoofed `X-User-Id` rejection: done locally
- Frontend `menuUploadId` propagation through debit/generation/export: done locally
- Authenticated menu upload ownership and object-SHA resolution: done locally
- Cross-user menu-upload concealment and object-byte integrity gate: done locally
- Frozen batch contract binding to authenticated generation entry: done locally
- Server-owned formal-generation debit and bounded compensation: done locally
- Browser-side formal debit/refund removal: done locally
- Frozen menu/background object resolution in monolith compatibility worker: done locally
- Product Redis queue isolation from fixed prompt queue: done locally
- Product generation handoff from Web to Redis Worker: done locally
- Redis Worker provider execution separated from billing/product DB settlement: done locally
- Request-bound first-writer result manifest and authenticated manifest route: done locally
- Pending cancel and running cancel-request lease semantics: done locally
- Cumulative bounded refund target under concurrent finalizers: done locally
- Missing Redis task recovery from persisted frozen contract: done locally
- PostgreSQL product job/outbox/fence/settlement foundation: done locally, not integrated or migrated
- PostgreSQL result-slot and settlement completion APIs: done locally; focused fake-DB verification passed, real PostgreSQL execution pending
- Product-generation production readiness gate: done locally; staging/Render now blocks on durable PostgreSQL store, transactional outbox, independent product Worker, and Worker liveness integration
- Worker-generated delivery images persisted to shared object storage: done locally; private manifest references and owner-checked opaque asset reads verified
- Export ZIP from frozen generation manifest: done locally; owner, digest, purchased-platform, and frozen-watermark boundaries verified
- Gemini image-edit provider boundary: done locally against deterministic HTTP fakes; queue/billing/refinement integration pending
- Deterministic background-locked refinement compositor: done locally; outside-mask identity and fail-closed mask/size gates verified
- Immutable revision contract and server-owned pricing snapshot: done locally; focused contract/provider/compositor verification passed
- Authenticated refinement task creation: done locally; source/menu-row/background SHA binding, server-counted free quota, pre-debit provider/Redis gates, and enqueue compensation are implemented
- Refinement Redis Worker dispatch, settlement, opaque result asset route, cancellation, repeat-edit source binding, export override, and customer UI polling: done locally
- Image-refinement production readiness gate: done locally; live runtime blocks before launch when Gemini is missing
- Latest full regression after real refinement workflow integration: done, 579 passed
- Redis product Worker service heartbeat: done locally; Worker publishes an expiring heartbeat and readiness reads the real Redis TTL
- Worker liveness focused verification: done, 79 related tests passed
- Lazy PostgreSQL runtime connection/probe boundary: done locally; credentials are redacted, autocommit is forbidden, and all eight product tables are schema-probed
- PostgreSQL runtime focused verification: done, 6 tests passed
- PostgreSQL migration 008 finance readiness gate: done locally; all seven finance business tables are probed, with real PostgreSQL positive and fail-closed negative verification
- Transactional PostgreSQL job, wallet, settlement, and outbox store: done locally; real PostgreSQL execution pending
- Independent PostgreSQL outbox dispatcher: done locally; Redis timeout-after-publish ambiguity is resolved by exact task verification
- Shared PostgreSQL menu-upload metadata: done locally; production upload resolution is owner-scoped and object-SHA verified
- Formal-generation PostgreSQL submission: done locally; job, debit, settlement, and outbox are created atomically
- Formal-generation PostgreSQL status, manifest, asset, and cancel routes: done locally; production paths do not fall back to SQLite
- PostgreSQL product runtime focused verification: done, 63 related tests passed
- Independent PostgreSQL terminal reconciliation: done locally; completion/refund no longer depends on customer polling
- Missing Redis task recovery from published PostgreSQL outbox: done locally; exact running fence is required before republish
- Settlement claim crash recovery: done locally; stale claimed settlements can be fenced and reclaimed
- PostgreSQL migration runner: done locally; advisory lock, ordered checksums, idempotency, rollback, and history drift fail-closed are fake-DB verified
- Product generation readiness: done locally; PostgreSQL/outbox integration is reported truthfully and Worker/dispatcher/reconciler require independent TTL heartbeats
- Render customer-site-preserving multi-service blueprint: done locally; customer Web, API, dispatcher, product Worker, reconciler, Redis, and PostgreSQL are separate declarations
- Multi-Web PostgreSQL/outbox runtime integration: done in code for formal generation; real PostgreSQL/Redis execution remains pending
- Real TokenHub plus Mask provider smoke for exact composition: pending
- Render SaaS runtime activation: deferred until a separate web/API/worker service layout can preserve the customer website
- Real 24-menu parsing verification: done, 24 files / 3,036 rows / 0 failures
- Versioned 40-category taxonomy and conservative combo matching: done locally; focused parser/matcher verification passed
- Real 24-menu paid image generation verification: pending
- Idempotent payment callback wallet-repair retry: done locally
- PostgreSQL payment order/event/wallet atomic store and migration: done locally
- PostgreSQL finance order/settlement/withdrawal list contracts: done locally with disposable real PostgreSQL
- Owner/menu-upload scoped background, preview, foreground, and formal-generation caches: done locally
- Quantity/specification-aware combo asset fingerprints: done locally
- Generation batch provenance v2 and pre-provider Worker validation: done locally
- Paid provider timeout retains Redis lease until the active call exits: done locally
- Live real-provider payment order and callback routes use PostgreSQL: done locally
- Real Flask -> Alipay adapter -> PostgreSQL callback/replay smoke: done against disposable local PostgreSQL
- Payment growth events in the same PostgreSQL transaction/outbox: done locally
- Payment/refund growth outbox payload freezes `growth-direct-v2-2026-07-30`: done locally and real-PostgreSQL verified
- Live PostgreSQL payment order creation requires `Idempotency-Key`: done locally and real-PostgreSQL verified
- Caller-owned growth outbox success transition for atomic business processing: done locally and real-PostgreSQL verified
- Complete regression after PostgreSQL growth/finance integration: done, 1164 passed and 14 skipped
- Live legacy recharge/debit bypass closure: done locally
- Legacy wallet/payment/security focused regression: done, 109 passed
- Live admin point adjustment idempotency and atomic PostgreSQL audit: done locally
- Real PostgreSQL point-adjustment replay and audit-failure rollback: done
- Production SQLite fallback audit: done, 8 concrete findings recorded
- Live fake-payment callback bypass closure: done locally
- Live legacy library ZIP import authorization/fail-closed gate: done locally
- Live local-disk download removal: done locally
- Legacy/live endpoint regression: done, 98 passed
- PostgreSQL commission release, settlement, refund clawback/liability, and withdrawal lifecycle: independently accepted locally; default 56 passed/2 skipped and disposable real PostgreSQL 58 passed
- Concrete growth event handler plus independent worker process: done locally at unit/contract level; 41 combined tests passed, real PostgreSQL business-flow verification pending
- Growth worker fail-closed readiness and Render declaration: done locally; missing service declaration or real Redis TTL heartbeat blocks live readiness
- Growth event processor transaction/fence/retry/dead-letter executor: done locally; concrete growth handler integration remains in progress
- PostgreSQL growth event processor and durable growth business tables: in progress
- Production PostgreSQL auth/session plus Redis OTP persistence foundation: done locally and independently verified against disposable PostgreSQL 16 and Redis 7
- Production HTTP auth/session/store route integration: done locally
- Production auth Render secret declarations and live mock-OTP prohibition: done locally
- Protected Render test service on `codex/render-image-staging`: deployed
- Real 56-row light-food Excel upload on the test service: passed
- TokenHub `hy-image-v3.0` readiness on the test service: passed
- Pure-background prompt v4 first paid generation: passed; 23.27 seconds and new SHA-256
- Remaining five backgrounds: generated and SHA-256 verified
- Pure-background v4 visual acceptance: failed 2/6 because styles 2 and 3 contained edible produce
- Pure-background v5 edible-prop exclusion: implemented and locally verified
- Pure-background v5 focused regression: 83 passed
- Pure-background v5 full regression: 1269 passed, 20 skipped
- Pure-background v5 Render deployment: passed
- Pure-background v5 visual acceptance: failed 3/6 because styles 3, 5, and 6 contained glass/vase props
- Pure-background v6 no-props prompt: implemented and locally verified
- Pure-background v6 focused regression: 83 passed
- Pure-background v6 full regression: 1269 passed, 20 skipped
- Pure-background v6 Render deployment: passed
- Pure-background v6 visual acceptance: failed 1/6 because style 6 rendered a salad
- Pure-background v7 food-name-free category hint: implemented and locally verified
- Pure-background v7 focused regression: 83 passed
- Pure-background v7 full regression: 1269 passed, 20 skipped
- Pure-background v7 Render deployment: passed
- Pure-background v7 visual acceptance: passed 6/6 with exact downloaded SHA-256 verification
- Selected style-3 first real free sample: passed; exact background identity verified
- Free-sample latency: failed product target at 108.66 seconds per successful image
- Concurrent free-sample probe: one passed in 106.87 seconds; one failed provider quota in 8.25 seconds
- Single-provider-call fast foreground/mask path: implemented locally behind a default-off flag; ChatGPT Pro review remains in progress
- Chroma fast-path focused regression: 29 passed, 1 environment-gated skip
- Chroma fast-path full regression: 1277 passed, 20 environment-gated skips
- TokenHub HY-Image-V3.0 postpaid activation: done; service status is running and billed per image after the exhausted 50-image trial
- Latest real six-background run: 5/6 visual pass; style 3 contained a vase and plant despite the negative prompt
- Style-3 empty cyclorama prompt refinement: implemented locally; only style 3 prompt SHA changes
- Style-3 refinement focused/full regression: 34 passed; 1278 passed, 20 environment-gated skips
- Production auth readiness schema/Redis/SMS/secret probes: done locally
- Production registration anti-abuse context persistence and fail-closed reward gate: done locally
- PostgreSQL export package, nonce, and access-audit foundation: done locally
- Production export manifest SQLite fallback: removed locally
- Production export package persistence and PostgreSQL one-time download audit: done locally
- Real PostgreSQL export HTTP protocol: done locally
- ChatGPT Pro Task 4 report and exact patch artifact: archived and SHA-256 verified locally
- ChatGPT Pro Task 5 sanitized source package: done and SHA-256 verified locally
- ChatGPT Pro Task 5 production growth/finance review: in progress
- Final broad object/image resource-boundary audit: done; four residual write/download/input allocation classes reproduced
- Bounded streaming object downloads and pre-serialization JSON/prompt budgets: implemented locally
- Final resource-boundary focused regression: done, 110 passed and 2 PostgreSQL-gated skips
- Complete default regression after final resource-boundary patch: done, 1250 passed and 20 dependency-gated skips
- Complete disposable PostgreSQL 16 plus Redis 7 regression: done, 1270 passed with zero skips
- Fresh post-patch read-only security audit: done; two P1 and three P2 ordering/resource findings reproduced
- Prompt ingress, export replay preflight, bounded asset upload, legacy metadata read, and revision orphan patch: done locally
- Second security patch focused regression: done, 156 passed and 3 dependency-gated skips
- ChatGPT Pro Task 5 v2 sanitized source package: uploaded and review requested
- Second security patch real PostgreSQL/Redis focused regression: done, 159 passed with zero skips
- Complete default regression after second security patch: done, 1257 passed and 20 dependency-gated skips
- Complete disposable PostgreSQL 16 plus Redis 7 regression after second security patch: done, 1277 passed with zero skips
- Independent second-pass security review: done; it reopened concurrent export nonce download amplification as P1 and AI source-file TOCTOU as P2
- Atomic export nonce reservation/release/consume migration and route integration: done locally
- AI asset bounded private-snapshot fingerprint/upload binding: done locally
- Reservation/snapshot focused default verification: done, 85 passed and 3 PostgreSQL-gated skips
- Reservation/snapshot focused disposable PostgreSQL verification: done, 88 passed
- Complete default regression after reservation/snapshot fixes: done, 1263 passed and 20 dependency-gated skips
- Complete disposable PostgreSQL 16 plus Redis 7 regression after reservation/snapshot fixes: done, 1283 passed with zero skips
- Final static checks: done; 165 Python files parsed, Render YAML has 8 services, both JavaScript files and `git diff --check` passed
- Final credential-pattern scan: done; only the payment PEM wrapper and explicit test dummy values matched
- Final reviewer P2 follow-up: done; hard-crash reservation recovery, repeated denied-request reads, and migration-column readiness gaps reproduced
- PostgreSQL session advisory lock plus stale-reservation takeover: done locally
- Prior access-denial precheck before export object download: done locally
- PostgreSQL readiness checks for migration 014 reservation columns: done locally
- Final P2 focused default verification: done, 120 passed and 4 dependency-gated skips
- Final P2 focused disposable PostgreSQL verification: done, 124 passed
- Complete default regression after final P2 closure: done, 1266 passed and 20 dependency-gated skips
- Complete disposable PostgreSQL 16 plus Redis 7 regression after final P2 closure: done, 1286 passed with zero skips
- Final post-P2 static and credential scans: done and clean apart from the known PEM wrapper/test dummy matches
- Atomic reservation-release plus denial-audit finalization under the advisory lock: done locally
- Unlock-to-audit concurrent regression: done against disposable PostgreSQL
- Final independent incremental security disposition: CLOSED, P0 0 / P1 0 / P2 0
- Final complete default regression: done, 1266 passed and 20 dependency-gated skips
- Final complete disposable PostgreSQL 16 plus Redis 7 regression: done, 1286 passed with zero skips
- Final sanitized source archive: done, 1131030 bytes, 223 files, ZIP integrity passed
- Final sanitized source archive SHA-256: 4ee2d3366dc83be47a8f034b9704767c67c0323c13ed943966c85ce2d661b431
- Isolated remote-main integration default regression: done, 1266 passed and 20 dependency-gated skips
- Isolated remote-main integration PostgreSQL 16 plus Redis 7 regression: done, 1286 passed with zero skips
- Isolated remote-main integration static gates: done; 165 Python files, 8 Render services, both JavaScript syntax checks, credential scan, and diff check passed
- Render test-service auto-deploy inspection: done; `waimai-image-tool` is currently `On Commit`
- Remote-main push boundary: blocked by authorization; pushing `main` would automatically deploy the Render test service
- Final sanitized source archive refresh after the final documentation-only newline cleanup: done
- Final sanitized source archive v5: done, 1132010 bytes, 223 files, ZIP integrity passed
- Final sanitized source archive v5 SHA-256: 9544c4eacecf465337baa164469af816f61213ff8797419152baaadee3a08111
- Historical report whitespace normalization: done; content and business code unchanged
- Final isolated staged scope: done, 171 source/migration/test/report files
- Final isolated staged diff check: done; no whitespace errors and no unstaged tracked changes
- Final isolated forbidden-path gate: done; no env, data, report-output, cache, database, or archive path is staged
- Final isolated staged credential scan: done; only the runtime PEM wrapper and its negative test assertion matched
- Independent PostgreSQL export production review: done, six P1 findings reproduced
- Export object preflight SHA/size verification before nonce consumption: done locally
- Export transient storage failure retry without nonce loss: done locally
- Export semantic idempotency replay and orphan-object cleanup: done locally
- Export ZIP streaming upload/download and configurable size ceiling: done locally
- Export migration 010 production-readiness schema gate: done locally
- Real PostgreSQL export tamper/outage/replay protocol: done locally
- PostgreSQL asset-library foundation: independent review done; P1 hardening in progress
- Product-asset exact selected-background binding: done locally and real-PostgreSQL verified
- Asset reuse pipeline-version gate: done locally and real-PostgreSQL verified
- PostgreSQL asset-library generation/review/reuse runtime: done locally and real-PostgreSQL verified
- Paid-provider call prevention on asset-library outage: done locally
- Cross-thread asset owner/menu context propagation: done locally
- PostgreSQL AI-asset admin list/review/disable workflow: done locally and real-PostgreSQL verified
- Partial, repeated, out-of-order, and cumulative growth refund semantics: done locally
- PostgreSQL agent commission, settlement, withdrawal, finance ledger, and audit foundation: done locally and real-PostgreSQL verified
- Production finance/growth HTTP and processor integration: in progress
- Production PostgreSQL agent creation and customer binding HTTP adapters: done locally
- Production invite-code issuance and anti-abuse invite acceptance: done locally
- Invite registration reward enqueue in the same PostgreSQL transaction: done locally
- Production PostgreSQL commission release/settlement and withdrawal HTTP adapters: done locally
- Real PostgreSQL growth/finance HTTP protocol without SQLite fallback: done locally
- Growth-only future-reward debt recovery with immutable evidence: independently accepted locally
- Complete migration 007/008 PostgreSQL readiness table gate: done locally
- Live invite-code secret fail-closed readiness and Render declaration: done locally
- Legacy direct `/api/refund` live SQLite fallback: blocked locally; live runtime now requires durable settlement
- PostgreSQL customer status/manifest read purity: done locally
- PostgreSQL generation/refinement cancellation intent boundary: done locally
- Real-menu E2E acceptance route/test contract review: done
- Deterministic versus paid-provider E2E acceptance entry: done locally
- Real 56-row menu deterministic E2E smoke: done, all nine stages passed
- Real-provider E2E gate without explicit authorization: done, blocked before app import with zero provider calls
- E2E acceptance entry tests and README: done locally
- E2E acceptance focused tests: done, 2 passed
- E2E acceptance static/adjacent regression and report scan: done
- Final real-menu deterministic acceptance report: done, 56/56 formal images and 56/56 Meituan exports verified
- Final real-provider blocked report: done, zero provider calls
- Six customer background/free-preview object persistence: done locally
- Owner/menu-scoped preview object recovery after local cache loss: done locally
- Live preview object-storage fail-closed behavior: done locally
- Product approval requires its exact associated background to be approved with matching tenant, owner, and SHA-256: done locally and real-PostgreSQL verified
- Approved product reuse stops when its associated background is later disabled or otherwise ceases to be approved: done locally and real-PostgreSQL verified
- Growth rule `growth-direct-v2-2026-07-30`: done locally; one direct level only, agent first order 20%, repeat order 10%, inviter registration 100 points, invitee 20 points
- Growth rule display, SQLite compatibility behavior, tests, roadmap, and legal-review boundary alignment: done locally
- Production admin audit actor ignores client-supplied `X-Admin-User-Id`: done locally; verified session and trusted-service identities while preserving explicit local-demo compatibility
- Admin AI-asset mutations authenticate before input validation: done locally
- Archived original security-remediation scope recovery: done; the deleted CSV's exact IDs/titles remain unavailable, but the original session establishes eight risk groups covering download/style traversal, paid-call abuse, write authorization, path/media disclosure, and unbounded image/logo decoding
- Current eight-group security acceptance audit: first pass found three later regressions; five groups remain closed
- Queue-only prompt API token authentication: done locally; `/generate` and `/status/<task_id>` fail closed before Redis when the server token is missing or invalid
- Remote default local-demo wallet bypass closure: done locally
- Provider, remote, fingerprint, and formal-delivery image byte/pixel bounds: done locally
- Repaired security regression focused verification: done, 100 passed and 1 dependency-gated skip
- Independent repaired-security re-review: first re-review closed prompt API and demo billing, but found unbounded formal-delivery/object-storage and refinement image paths
- Shared bounded object-storage reads for local and COS backends: done locally
- Formal delivery and selected-background bounded object reads: done locally
- Gemini response/base64/pixel bounds: done locally
- Product revision source/background/manifest/asset byte and pixel bounds: done locally
- Second image-boundary focused verification: done, 110 passed and 1 dependency-gated skip
- Final independent image-boundary re-review: done; it reopened one selected-background consumer plus Web/export/settlement revision consumers
- Selected-background refinement snapshot bounded object and pixel validation: done locally
- Formal and revision manifest settlement bounded object reads: done locally
- Web revision manifest, asset, export, delivery, and compatibility consumers bounded: done locally
- Watermark object digest, byte, and pixel validation: done locally
- TokenHub Web and prompt-Worker response-body limits: done locally
- Private preview, menu, library import, product Worker, and generic object-route bounded reads: done locally
- Latest bounded-read focused verification: done, 164 passed
- Complete default regression after final security repair: done, 1239 passed and 20 dependency-gated skips
- Complete disposable PostgreSQL 16 plus real Redis 7 regression after final security repair: done, 1259 passed with zero skips
- Final broad read-only object/image boundary audit: in progress
- Real COS plus Render-restart preview recovery smoke: pending
- Formal generation selected-background recovery after local cache loss: done locally
- Final isolated integration commit: done, `f680c24b0ed826a6ea999d614cf7deb172d7710e`
- Official Render skip-deploy phrase: verified; `[skip render]` prevents an auto-deploy and emits a skipped-commit event
- Render Blueprint inventory: verified empty; the workspace has no Blueprint instance or automatic Blueprint sync
- GitHub deployment workflow inventory: verified empty; no `.github` deployment workflow exists
- Remote main push: done by fast-forward from `1dbcbb48...` to `f680c24...`
- Render no-deploy verification: done; event says `Deploy skipped for commit f680c24`
- Render live commit after push: unchanged at `1dbcbb48aac38a118367e650cbd6d27af4faa17f`
- Deployment, Render configuration change, and external database migration after push: not performed
- Final documentation reconciliation: in progress; stale MVP/SQLite status is being replaced with the current code-level acceptance boundary
- Final goal completion audit: done; every explicit implementation, verification, push, no-deploy, and no-migration requirement has an evidence disposition
- Final sanitized source archive v6: done, 1134096 bytes, 224 files, ZIP integrity passed
- Final sanitized source archive v6 SHA-256: f5409d62d735f70f57d27a422897cd72c3ca09981bd901402b7a08b01d076be3
- Final background generation deploy `dep-d9n1nfvlk1mc73dj7cp0`: complete,
  30/30 assets, `failureCount=0`, sentinel `CATALOG_BATCH_36_40_COMPLETE`
- Final hash-lock approval: `bottled_drinks` at `2026-08-01T16:30:15Z`,
  `fresh_drinks` at `2026-08-01T16:30:41Z`, `dessert_bakery` at
  `2026-08-01T16:36:06Z`, `fried_snacks` at `2026-08-01T16:36:31Z`, and
  `fruit` at `2026-08-01T16:37:06Z`
- Final approval retry sentinel: `CATALOG_BATCH_36_40_APPROVED`
- Background catalog checkpoint: 40 approved categories / 240 approved assets
- Complete-catalog prompt freeze verification: focused `15 passed`; full
  regression `1324 passed, 20 skipped`; maximum prompt length 588; scoped
  Python compilation and `git diff --check` passed
- Render normal-start restore deploy `dep-d9n23cijnfac73a7sqeg`: live; running
  command is only `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads
  8 --worker-class gthread --timeout 180`

## Constraints
- Codex 每次继续任务前必须先读 `AI-Project/state/current.md`。
- 和当前任务相关时必须读 `AI-Project/decisions/decisions.md`。
- 每个 step 控制在 15 分钟以内，完成后立刻更新 `current.md` 和当天 log。
- 不依赖模型记忆，不假设缺失上下文。
- 不回滚 worktree 中既有改动，不影响其他本地任务。
- Render 上 `/api/plan` 不能同步调用混元生成 6 张背景图。
- 禁止默认用色块、SVG 或图库假图冒充真实背景图。
- 用户要求背景图、样图、正式图优先使用混元生成以保证正确率。

## Current Context
- 当前 worktree: `/Users/guiguixiaxia/.codex/worktrees/de51/waimai-image-tool`
- Render 测试站: `https://waimai-image-tool.onrender.com`
- Local Task 1 测试站: stopped after verification; last used `http://127.0.0.1:8792`
- Deploy repo: `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy`
- GitHub remote: `git@github.com:467091085-max/waimai-image-tool.git`

## Current Findings
- Render `/api/tencent-status` 曾显示混元和 COS 已配置。
- Render `/api/plan?quality=standard` 会长时间挂起并返回 500，之后普通接口也会超时。
- 根因是 `/api/plan` 通过 `style_options -> style_sample_candidate -> tencent_style_background` 在单个请求内同步生成 6 张混元背景图，Render 单 worker 被阻塞或崩溃。
- 本地已开始最小补丁：`/api/plan` 只返回 pending 背景卡片，新增 `/api/style-background?style=...&generate=1` 按单张风格背景异步生成，前端并发 2 个请求逐张加载。
- Render 新代码已上线，`/api/style-background` 不再 404。
- Render `/api/plan?quality=standard` 已验证 200，约 1.76 秒返回，6 张背景均为 `PendingGeneration`，不再同步生成导致阻塞。
- Render `/api/style-background?style=style-1&generate=1` 已验证 200，约 2.45 秒返回失败状态；腾讯云返回 `ResourceInsufficient`，需要开通资源包或后付费后才能真实出图。
- 前端已补充 ResourceInsufficient 显示：腾讯云额度不足时显示 `混元资源不足`，不再只显示笼统失败或继续转圈。
- 第二次线上复测：`/api/plan?quality=standard` HTTP 200，约 4.43 秒，6 个 style action 均为 `PendingGeneration`。
- 第二次线上复测：`/api/style-background?style=style-1&generate=1` HTTP 200，约 4.02 秒，返回 `ProviderError / ResourceInsufficient`，无图片 URL。
- 腾讯云控制台检查结果：当前登录主账号可见 TokenHub `HY-Image-3.0`、`HY-Image-Lite`，但 TokenHub API Key 管理页显示还没有创建任何 API Key。
- TokenHub 用量统计中 `HY-Image-3.0` 今天请求数和积分消耗为空，说明当前 Render 服务此前没有打到 TokenHub `HY-Image-3.0`。
- 旧版混元 API Key 管理页显示暂无数据，旧版资源包页只看到免费包，未看到用户提到的 `混元生图3.0 200` 和 `商品背景 100` 资源包。
- 根因更新：用户购买/充值的额度在新 TokenHub/视觉模型体系，当前 Render 原先只用旧 TC3 `TextToImageLite`/`ReplaceBackground` 接口和 `TENCENTCLOUD_SECRET_ID/KEY`，无法消耗 TokenHub 额度。
- 已向 deploy repo 添加 TokenHub 支持：有 `TENCENT_TOKENHUB_API_KEY` 时优先调用 `https://tokenhub.tencentmaas.com/v1/api/image/submit` + `/query` 或 `HY-Image-Lite` `/lite`，失败时 fallback 到旧接口。
- Deploy repo commit `cadd38f Add TokenHub image generation support` 已推送 GitHub main，并已在 Render 上线。
- Render `/api/tencent-status` 新代码已验证：返回 `tokenhubModel=hy-image-v3.0`、`tokenhubReady=false`、`cloudApiReady=true`。代码已上线，但 Render 仍未配置 TokenHub API Key。
- Render `/api/style-background?style=style-1&generate=1` 仍返回旧接口 `ResourceInsufficient`，因为缺少 `TENCENT_TOKENHUB_API_KEY` 时只能 fallback 到旧接口。
- 本地已新增 `generation_provider_readiness()`：`APP_ENV=staging/production` 或检测到 Render 运行环境时默认要求 TokenHub 图像 provider；缺 `TENCENT_TOKENHUB_API_KEY` 时 `/api/ops/readiness` 会返回 `ready=false`，并在 `generationProvider.blockingIssues` 标出 `tokenhub_image_provider_required`。
- 本地后台运维状态面板已新增 `AI 生图 provider` readiness 卡片，展示 TokenHub、旧 Cloud API、模型、warnings/errors。
- 本地验证：readiness 定向测试、后台契约测试、`node --check static/admin.js`、`python3 -m py_compile app.py` 均通过；全量 pytest `322 passed in 3.25s`。
- Deploy repo 已推送 readiness 补丁：`297a3ec Add generation provider readiness checks` 和 `4c596ca Detect Render runtime in generation readiness`。
- Render `/api/ops/readiness` 已验证：`ready=false`、`generationProvider.appEnv=render`、`generationProvider.mode=legacy_cloud_api`、`generationProvider.tokenhubRequired=true`、`generationProvider.blockingIssues=["tokenhub_image_provider_required"]`、`generationProvider.missingConfig=["TENCENT_TOKENHUB_API_KEY"]`。
- 本地已新增 `TencentCOSObjectStorageService`，支持逻辑 object key 到 COS remote key 的可选 prefix 映射，支持 `put_bytes`、`put_file`、`read_bytes`、`exists`、`delete`、`stat`、`list_prefix`。
- `/objects/<object_key>` 已改为通过 object storage facade 的 `exists/read_bytes` 服务对象，不再硬依赖本地 `path_for_key()`；本地和 COS backend 共享同一套签名校验与审计路径。
- `assess_object_storage_readiness()` 已升级：`OBJECT_STORAGE_PROVIDER=cos` 配齐 bucket、region、SecretId、SecretKey、签名 secret 时可 ready；OSS/R2/S3 等尚无 runtime adapter 的 provider 不再误报可生产 ready。
- 本地验证：对象存储定向测试、对象下载签名测试、安全回归测试、`python3 -m py_compile object_storage_service.py app.py` 均通过；全量 pytest `324 passed in 3.16s`。
- Deploy repo 已推送 COS backend：`143eece Add COS object storage backend`；deploy repo 全量 pytest `324 passed in 2.78s`。
- Render 线上冒烟：`/api/ops/readiness` 仍返回服务可响应；当前 objectStorage 仍为 `local_demo`，因为 Render 尚未设置 `OBJECT_STORAGE_PROVIDER=cos`。
- 本地已补 Render 对象存储 gate：缺 `APP_ENV` 但 `PUBLIC_BASE_URL` 包含 `.onrender.com` 或存在 Render 环境变量时，object storage `appEnv=render`，本地存储会返回 `private_remote_object_storage_provider_required`。
- 本地 `render.yaml` 已声明 `OBJECT_STORAGE_PROVIDER=cos`、`OBJECT_STORAGE_PRIVATE=true`、`OBJECT_STORAGE_BUCKET=waimai-image-tool-inputs-1311836560`、`OBJECT_STORAGE_REGION=ap-guangzhou`、`OBJECT_STORAGE_PREFIX=app-objects`。
- 本地验证：`python3 -m pytest tests/test_object_storage_service.py -q` 通过，15 passed；`python3 -m py_compile object_storage_service.py app.py` 通过。
- Current worktree 全量验证通过：`python3 -m pytest -q` -> 325 passed。
- Deploy repo 全量验证通过：`python3 -m pytest -q` -> 325 passed。
- Deploy repo 已提交并推送 `9de3812 Require remote object storage on Render` 到 GitHub main。
- Render 线上已部署新代码：`/api/ops/readiness` 返回 `objectStorage.ready=false`、`provider=local`、`mode=local_demo`、`appEnv=render`、`blockingIssues=["private_remote_object_storage_provider_required","object_signing_secret_required"]`，说明 Render 上 local_demo 不再误报 ready。
- Render 当前仍未实际启用 COS object storage provider；`render.yaml` 已写入 COS env，但现有 Render 服务没有自动应用这些 blueprint env vars，且本地未发现可用 Render CLI/API token 可直接修改平台环境变量。
- 本地已补 `/api/export` 交付包 object storage handoff：`export_delivery_zip()` 仍负责打包，API 层将生成的 ZIP 写入 `exports/` object key，返回 `/objects/exports/...?...token=` 签名链接，并写入 `export_packages` 表。
- 本地保留旧 `/download` 路由和无签名密钥时的 demo 行为，避免破坏本地兼容；产品环境有 `OBJECT_SIGNING_SECRET` 后默认使用对象存储签名下载。
- 本地验证：`python3 -m pytest tests/test_download_route.py tests/test_object_storage_service.py tests/test_security_regressions.py -q` 通过，44 passed；`python3 -m py_compile app.py object_storage_service.py` 通过。
- Current worktree 全量验证通过：`python3 -m pytest -q` -> 325 passed。
- Deploy repo Step 7 定向验证通过：`python3 -m pytest tests/test_download_route.py tests/test_object_storage_service.py tests/test_security_regressions.py -q` -> 44 passed；`python3 -m py_compile app.py object_storage_service.py` 通过。
- Deploy repo Step 7 全量验证通过：`python3 -m pytest -q` -> 325 passed。
- Deploy repo 已提交并推送 `fa47b11 Store export packages in object storage` 到 GitHub main。
- Render 线上轻量导出冒烟：`POST /api/export` with `selectedRows=[999]` 返回 HTTP 200，但 download 仍是 `/download/export_.../result.zip`，因为线上 readiness 仍显示 `objectStorage.provider=local`、`objectStorage.blockingIssues=["private_remote_object_storage_provider_required","object_signing_secret_required"]`。
- Step 7 代码路径已在测试中验证：配置对象签名密钥和 object store 后 `/api/export` 返回 `/objects/exports/...?...token=`，`/objects` 下载成功，并写入 `export_packages`。
- 本地已补 `/api/upload-menu` 上传原始 Excel 的 object storage handoff：接口仍保存本地副本用于当前解析，但同时将原始文件写入 `menus/` object key，并插入 `menu_uploads` 表，状态为 `parsed`。
- 上传菜单响应新增 `menuUploadId`，不返回 raw object key；原始文件 object key 只进入数据库。
- 如果菜单解析成功但 object storage 或 DB 写入失败，接口返回 `503 menu_object_storage_failed`，避免出现“上传成功但资产只在本地”的假成功。
- 本地验证：`python3 -m pytest tests/test_product_api_integration.py::test_upload_menu_persists_original_file_to_object_storage_and_db tests/test_product_api_integration.py::test_product_api_routes_are_registered tests/test_object_storage_service.py -q` 通过，17 passed；`python3 -m py_compile app.py object_storage_service.py storage_db.py` 通过。
- Current worktree 全量验证通过：`python3 -m pytest -q` -> 326 passed。
- Deploy repo Step 8 定向验证通过：17 passed；`python3 -m py_compile app.py object_storage_service.py storage_db.py` 通过。
- Deploy repo Step 8 全量验证通过：`python3 -m pytest -q` -> 326 passed。
- Deploy repo 已提交并推送 `c68bbc3 Persist uploaded menus in object storage` 到 GitHub main。
- Render 线上 readiness 冒烟仍正常响应；当前仍因 `objectStorage.provider=local`、`object_signing_secret_required`、`private_remote_object_storage_provider_required` 和 `TENCENT_TOKENHUB_API_KEY` 缺失而非产品 ready。未在线上上传假 Excel，避免制造业务测试数据。
- 本地已补 `/api/upload-library` 图库 zip 持久化：每张图片仍保存本地副本供当前匹配/预览读取，同时写入 `originals/<upload-batch>/style-upload/<filename>` object key，并插入 `library_images` 表。
- `/api/upload-library` 响应新增 `uploadedImageCount` 和 `libraryImageIds`，不返回 raw object key；非图片文件继续跳过。
- 如果 zip 无效返回 `400 invalid_library_zip`；如果图片对象存储或 DB 写入失败，返回 `503 library_object_storage_failed`。
- 本地验证：`python3 -m pytest tests/test_product_api_integration.py::test_upload_library_persists_images_to_object_storage_and_db tests/test_product_api_integration.py::test_upload_menu_persists_original_file_to_object_storage_and_db tests/test_object_storage_service.py -q` 通过，17 passed；`python3 -m py_compile app.py object_storage_service.py storage_db.py` 通过。
- Current worktree 全量验证通过：`python3 -m pytest -q` -> 327 passed。
- Deploy repo Step 9 定向验证通过：17 passed；`python3 -m py_compile app.py object_storage_service.py storage_db.py` 通过。
- Deploy repo Step 9 全量验证通过：`python3 -m pytest -q` -> 327 passed。
- Deploy repo 已提交并推送 `6314019 Persist uploaded library images in object storage` 到 GitHub main。
- Render 线上 readiness 冒烟仍正常响应；当前仍因 `objectStorage.provider=local`、`object_signing_secret_required`、`private_remote_object_storage_provider_required` 和 `TENCENT_TOKENHUB_API_KEY` 缺失而非产品 ready。未在线上上传假图库 zip，避免制造业务测试数据。
- 本地已补支付 live runtime gate：`payment_service.fake_payment_provider_enabled()` 在 `APP_ENV=staging/production/prod` 或检测到 Render runtime 时直接禁用 fake provider，即使 `ENABLE_LOCAL_DEMO_BILLING=true`、`PAYMENT_PROVIDER=fake` 或 `ALLOW_FAKE_PAYMENT_PROVIDER=true`。
- 本地已补支付 readiness：`assess_payment_provider_readiness()` 返回 `appEnv`，识别 Render/staging 为 live environment；fake provider 会返回 blocking issues `real_payment_provider_required` 和 `fake_payment_provider_forbidden_in_live_environment`。
- 本地 `render.yaml` 已将 `ENABLE_LOCAL_DEMO_BILLING` 改为 `"false"`，避免新 Render 环境默认开启 fake 支付。
- 本地验证：`python3 -m pytest tests/test_payment_service.py tests/test_product_api_integration.py::test_fake_payment_order_blocked_on_render_runtime_even_if_demo_billing_enabled tests/test_product_api_integration.py::test_ops_readiness_accepts_tokenhub_generation_provider_in_staging tests/test_product_api_integration.py::test_ops_readiness_treats_render_runtime_as_live_generation_environment -q` 通过，20 passed；`python3 -m py_compile payment_service.py app.py` 通过。
- Step 10 已同步到 deploy repo 并推送 GitHub main：commit `0d57fa9 Block fake payments in live runtimes`。
- Step 10 deploy repo 和 current worktree 全量验证均通过：`python3 -m pytest -q` -> 330 passed。
- Render `/api/ops/readiness` 已验证 Step 10 生效：`payments.ready=false`、`payments.provider=fake`、`payments.appEnv=render`，blocking issues 包含 `real_payment_provider_required` 和 `fake_payment_provider_forbidden_in_live_environment`。
- Render 仍未 product-ready：`generationProvider` 仍缺 `TENCENT_TOKENHUB_API_KEY`；`objectStorage` 仍是 local provider 且缺 `OBJECT_SIGNING_SECRET`/COS env。
- 用户表示已登录腾讯云后，尝试用 Computer Use 复查控制台；工具返回 `Computer Use is not allowed on the current browser URL`，无法继续读取或点击腾讯云页面。
- 用户再次确认已登录后，重新调用 Computer Use 读取 Chrome；工具仍直接终止并返回 `Computer Use is not allowed on the current browser URL`。这说明当前阻塞不是登录态问题，而是该腾讯云控制台 URL 不允许由 Computer Use 操作。
- 本地已补代理提现后台审批审计：`POST /api/admin/actions/withdrawals/<withdrawal_id>/status` 在状态更新成功后写入 `admin_audit_logs`，action 为 `withdrawal_status_updated`，记录 actor、原因、提现 ID、代理 ID、金额、from/to 状态和状态原因。
- 本地验证：提现 API 定向测试和 `tests/test_admin_actions.py` 通过，`python3 -m py_compile app.py withdrawal_service.py admin_actions.py` 通过，全量 pytest `330 passed in 2.74s`。
- Deploy repo 已同步提现审批审计补丁并验证：定向测试 8 passed，py_compile 通过，全量 pytest `330 passed in 2.93s`。
- Deploy repo 已提交并推送 `b7377ba Audit withdrawal admin status changes` 到 GitHub main。
- Render readiness 冒烟仍可响应；当前仍非 product-ready，blocking issues 仍为真实支付 provider、COS/object signing secret、TokenHub API Key。
- `MODULE_STATUS.md` 和 `PRODUCTIZATION_PLAN.md` 已更新，避免后续窗口把提现后台审批审计误判为完全未做。
- 本地已补提现状态权限分级：`admin_withdrawal_status_authorized()` 允许 `operator/ops/admin/finance/owner` 做 approved/rejected/canceled，但 `paid` 只允许 `finance/admin/owner/super_admin` 等财务权限；`ADMIN_API_TOKEN` 和本地 demo admin 兼容保留。
- 未授权后台写请求带空 payload 时仍优先返回 403，满足安全回归要求；授权用户传无效 status 仍返回 `invalid_withdrawal_input`。
- 本地验证：提现 RBAC 定向测试、安全回归定向测试、py_compile 通过，全量 pytest `331 passed in 2.85s`。
- Deploy repo 已同步提现 paid 权限分级补丁并验证：定向测试 3 passed，py_compile 通过，全量 pytest `331 passed in 2.81s`。
- Deploy repo 已提交并推送 `86979f4 Restrict withdrawal payout admin role` 到 GitHub main。
- Render readiness 冒烟仍可响应；当前生产 blockers 未变化：真实支付 provider、COS/object signing secret、TokenHub API Key。
- 本地已抽出 `admin_finance_action_authorized()`，提现 paid 与佣金结算 paid 共用同一套财务角色判断。
- 本地已补佣金结算状态权限分级：`admin_commission_settlement_status_authorized()` 允许 operator/ops/admin 继续 release/create/非 paid 状态操作，但 `POST /api/admin/actions/commission-settlements/<id>/status` 的 `paid` 只允许 finance/admin/owner/super_admin 等财务角色；`ADMIN_API_TOKEN` 和本地 demo admin 兼容保留。
- 本地验证：佣金结算 API RBAC 定向测试、提现 RBAC、安全回归定向测试、py_compile 通过，全量 pytest `332 passed in 2.93s`。
- Deploy repo 已同步佣金结算 paid 权限分级补丁并验证：定向测试 4 passed，py_compile 通过，全量 pytest `332 passed in 3.03s`。
- Deploy repo 已提交并推送 `f6a5933 Restrict commission settlement payout role` 到 GitHub main。
- Render readiness 冒烟仍可响应；当前生产 blockers 未变化：真实支付 provider、COS/object signing secret、TokenHub API Key。
- 本地已补 AI 资产审核状态权限分级：`admin_ai_asset_status_authorized()` 允许 reviewer/operator/admin 等角色执行 approved/rejected/pending，`disabled` 只允许 admin/super_admin/owner；`ADMIN_API_TOKEN` 和本地 demo admin 兼容保留。
- 本地 `POST /api/admin/actions/ai-assets/<asset_id>/status` 已先做泛后台写权限，再做状态级角色权限；兼容 `approve/reject/disable` action alias。
- 本地验证：AI 资产 RBAC 定向测试、后台 blueprint 审计测试、安全回归定向测试、py_compile 通过，全量 pytest `333 passed in 3.03s`。
- Deploy repo 已同步 AI 资产 disable 角色限制并验证：定向测试 3 passed，py_compile 通过，全量 pytest `333 passed in 3.03s`。
- Deploy repo 已提交并推送 `2c01b3c Restrict AI asset disable role` 到 GitHub main。
- Render readiness 冒烟仍可响应；当前生产 blockers 未变化：真实支付 provider、COS/object signing secret、TokenHub API Key。
- 本地已补风控处置权限分级：`admin_risk_decision_authorized()` 允许 operator/risk/admin 等角色执行 allow/review，但 `deny` 只允许 risk/security/admin/owner 等角色；`ADMIN_API_TOKEN` 和本地 demo admin 兼容保留。
- 本地 `POST /api/admin/actions/risk` 已先规范化 decision，再做动作级 RBAC；无后台身份仍返回 `admin_write_forbidden`，有基础后台权限但越权 deny 返回 `admin_permission_forbidden`。
- 本地验证：风控 RBAC 定向测试、后台写安全回归定向测试、py_compile 通过，全量 pytest `334 passed in 3.03s`。
- Deploy repo 已同步风控 deny 角色限制并验证：定向测试 3 passed，py_compile 通过，全量 pytest `334 passed in 3.07s`。
- Deploy repo 已提交并推送 `df9937c Restrict risk deny admin role` 到 GitHub main。
- Render readiness 冒烟仍可响应；当前生产 blockers 未变化：真实支付 provider、COS/object signing secret、TokenHub API Key。
- 本地已新增 `/api/ops/deployment-config`：按 runtime、AI 生图、对象存储、支付、队列分组输出生产 env 清单、推荐值、缺失项、blocking issues 和 `secretsRedacted=true`，不会返回 secret 原文。
- 本地验证：部署配置清单接口、路由注册、TokenHub 缺失 readiness 定向测试和 py_compile 通过，全量 pytest `335 passed in 3.19s`。
- Deploy repo 已同步部署配置清单并验证：定向测试 3 passed，py_compile 通过，全量 pytest `335 passed in 3.26s`。
- Deploy repo 已提交并推送 `463c633 Add deployment config readiness report` 到 GitHub main。
- Render `/api/ops/deployment-config` 已上线：轮询第 7 次返回 200，`ready=false`，`appEnv=render`，sections 包含 runtime、generationProvider、objectStorage、payments、generationQueue；当前缺失 `ADMIN_API_TOKEN`、`APP_ENV`、`OBJECT_SIGNING_SECRET`、`OBJECT_STORAGE_BUCKET`、`OBJECT_STORAGE_PRIVATE`、`OBJECT_STORAGE_PROVIDER`、`PAYMENT_PROVIDER`、`PAYMENT_WEBHOOK_SECRET`、`TENCENT_TOKENHUB_API_KEY`。
- Render `/api/ops/readiness` 仍可响应；当前生产 blockers 未变化：真实支付 provider、COS/object signing secret、TokenHub API Key。
- 本地已新增真实支付 checkout fail-closed guard：`payment_service.ensure_payment_checkout_available()` 允许 fake local demo，但 `wechat/alipay` 在凭证缺失时返回 `payment_provider_unavailable`，在凭证齐全但 adapter 未接入时返回 `payment_adapter_not_implemented`。
- 本地 `_clean_provider()` 已识别 `wechat/alipay` 和别名，但 `/api/payments/orders` 会在写入 `payment_orders` 前调用 checkout guard；真实 provider 未可用时不会创建 pending 脏订单。
- 本地验证：真实支付 guard、API fail-closed、fake payment 兼容定向测试和 py_compile 通过，全量 pytest `338 passed in 2.89s`。
- Deploy repo 已同步真实支付 fail-closed 补丁并验证：定向测试 6 passed，py_compile 通过，全量 pytest `338 passed in 2.95s`。
- Deploy repo 已提交并推送 `fdec3e8 Fail closed for real payment adapters` 到 GitHub main。
- Render 线上支付 smoke 已验证：`POST /api/payments/orders` with `provider=wechat` 前 3 次仍返回旧版 `400 invalid_recharge_package`，第 4 次部署后返回 `503 payment_provider_unavailable`，并列出缺失微信支付 env，不会进入 fake 成功或空收银台状态。
- 本地已接入支付宝电脑网站支付 MVP：`PAYMENT_PROVIDER=alipay` 且配置 `ALIPAY_APP_ID`、`ALIPAY_PRIVATE_KEY`、`ALIPAY_PUBLIC_KEY`、`PAYMENT_NOTIFY_URL` 后，`/api/payments/orders` 会生成 `alipay.trade.page.pay` RSA2 签名支付链接并把 checkout payload 写入 `payment_orders.provider_payload_json`。
- 本地新增 `/api/payments/alipay/notify`：读取 form/json 通知，使用支付宝公钥验签，按 `TRADE_SUCCESS/TRADE_FINISHED` 转 paid，复用现有 billing/growth 回调效果，成功返回纯文本 `success`。
- 微信支付仍未接入 adapter，凭证完整时仍会 fail-closed，避免创建无可用收银台的 pending 订单。
- Current worktree 验证通过：`python3 -m pytest tests/test_payment_service.py -q` -> 22 passed；支付宝 API/部署配置定向测试 -> 4 passed；`python3 -m pytest -q` -> 345 passed。
- Deploy repo 已同步并验证：支付/部署配置定向测试 -> 26 passed；`python3 -m pytest -q` -> 345 passed；提交并推送 `8a8f3ac Add Alipay page payment adapter` 到 GitHub main。
- Render 轮询已确认新代码上线：`/api/ops/deployment-config` 的 `payment_provider.recommended` 从 `wechat` 变为 `alipay`。
- Render 线上支付宝 smoke：未配置支付宝密钥时 `POST /api/payments/orders` with `provider=alipay` 返回 HTTP 503 `payment_provider_unavailable`，缺失 `alipay_app_id`、`alipay_private_key`、`alipay_public_key`、`payment_notify_url`，不会进入 fake 支付或成功下单状态。
- 本地已新增财务人工支付对账：`payment_service.reconcile_payment_event()` 可在人工核验后跳过外部 provider 签名校验，但仍走支付状态机、事件幂等、订单状态更新时间和积分入账/退款计算。
- 本地已新增 `POST /api/admin/actions/payments/reconcile`，只有 finance/admin/owner/super_admin 或 `ADMIN_API_TOKEN` 可用；普通 operator 会返回 `admin_permission_forbidden`。
- 人工支付对账接口要求填写 reason，成功后复用 `apply_payment_callback_effects()` 给用户积分入账/退款，并写 `admin_audit_logs` action=`payment_reconciled`。
- Current worktree 验证通过：`python3 -m py_compile payment_service.py app.py`；支付 service 测试 `23 passed`；支付对账 API 定向测试 `2 passed`；全量 pytest `347 passed in 3.71s`。
- 财务人工支付对账已同步 deploy repo、全量验证通过并推送 GitHub main：commit `a204e74 Add finance payment reconciliation`。
- Render 已确认支付对账新路由上线：`POST /api/admin/actions/payments/reconcile` 未授权请求从旧版 404 变为 403 `admin_write_forbidden`，没有产生业务写入。
- 已按附件 `REFACTOR_TO_SAAS.md` 新增目标结构：`api-server/`、`worker/`、`shared/`、`Dockerfile`。
- 新 `api-server/app.py` 提供 `POST /generate` 和 `GET /status/<task_id>`；只写 Redis 队列和读取状态，不包含 AI 生成逻辑。
- 新 `worker/worker.py` 独立消费 Redis 任务，调用 Worker-only generator handler，成功写 `image_url`，失败最多 retry 2 次后标记 failed。
- 新 `shared/redis_queue.py` 负责 Redis task queue、task status hash、任务入队/出队/运行/成功/失败状态。
- 默认 `shared/generator.py` 在未配置真实 provider 时 fail-closed；只有显式 `AI_IMAGE_PROVIDER=mock` 且 `ALLOW_MOCK_GENERATION=true` 时允许本地 mock。
- `render.yaml` 和 `Procfile` 已把 web start command 改为 `gunicorn --chdir api-server app:app ...`，health check 改为 `/healthz`，并加入 `REDIS_URL` 配置项。
- Current worktree SaaS 验证通过：py_compile 新模块通过，`tests/test_saas_runtime.py` -> 3 passed，全量 pytest -> 350 passed。
- Deploy repo SaaS 验证通过：py_compile 新模块通过，`tests/test_saas_runtime.py` -> 3 passed，全量 pytest -> 350 passed；commit `6ca9d8f Add SaaS API worker Redis skeleton` 已推送 GitHub main。
- Render 线上轮询 `/healthz` 和 `/generate` 仍返回 404，说明现有 Render 服务没有自动应用 `render.yaml` 的新 start command，仍在运行旧 monolith。需要在 Render Dashboard 手动改 Start Command 并配置 `REDIS_URL`，或重新按 blueprint 创建服务。
- 本机 `python3 -m gunicorn` 对旧 monolith 和新 API Server 都返回 502，但 Flask test client 对新 API Server 验证正常；该 502 是本机 gunicorn 环境问题，不能作为新代码失败证据。
- 已按 `CODEX_REWRITE_PROMPT.md` 继续补 Worker 必须项：`worker/worker.py` 单次 handler 执行默认 `WORKER_TASK_TIMEOUT=60` 秒，超时按失败处理并进入 retry；`WORKER_MAX_RETRIES` 默认 2。
- `shared/redis_queue.py` 已支持 `recover_stale_running()`：Worker 每轮消费前扫描 stale `running` 任务，未超过最大尝试次数则恢复为 `pending` 并重新入队，超过则标记 `failed`。
- Worker 会从 Redis 已记录的 `attempts` 继续计数，避免进程崩溃后从 0 重新开始导致无限重试。
- Current worktree 验证通过：SaaS 定向测试 `5 passed`，全量 pytest `352 passed in 3.95s`。
- Deploy repo 验证通过：SaaS 定向测试 `5 passed`，全量 pytest `352 passed in 3.81s`。
- Deploy repo 已推送 `37622d7 Add worker timeout and recovery` 和 `5b754c5 Document worker timeout recovery` 到 GitHub main。
- 已按附件 `CODEX_FINAL_CONSTITUTION.md` 执行下一步结构收敛：API 合同固定为 `POST /generate` 和 `GET /status/<task_id>`，不新增兼容 API。
- `api-server/app.py` 已收紧：`POST /generate` 只接受非空 `prompt`，只返回 `task_id`；`GET /status/<task_id>` 只返回 `status` 和 `image_url`。
- `shared/redis_queue.py` 默认任务 ID 已从 `task_<hex>` 改为标准 UUID 字符串，保留显式 `task_id` 兼容测试/内部调用。
- `tests/test_saas_runtime.py` 已补覆盖：响应字段严格匹配固定合同，旧 `category/dishName` 字段不能绕过 `prompt` 要求，API Server 不导入 `shared.generator` 或 `generate_image`。
- Current worktree 验证通过：`python3 -m pytest tests/test_saas_runtime.py -q` -> 7 passed；`python3 -m pytest -q` -> 354 passed；SaaS py_compile 和 `git diff --check` 通过。
- Deploy repo 验证通过：`python3 -m pytest -q` -> 354 passed；SaaS py_compile 和 `git diff --check` 通过。
- Deploy repo 已提交并推送 `1dbcbb4 Enforce SaaS generation API contract` 到 GitHub main。
- Final SaaS health check 结果：项目结构包含 `api-server/`、`worker/`、`shared/`；SaaS py_compile 通过；`tests/test_saas_runtime.py` -> 7 passed；API Server import 成功且路由为 `/healthz`、`/generate`、`/status/<task_id>`；静态 grep 未发现 `api-server/app.py` 导入 `shared.generator` 或 `generate_image`。
- Worker 独立类实例化和空队列处理成功；FakeRedis 生成流 `enqueue -> worker.process_one -> done/image_url` 成功。
- 当前本地环境 `REDIS_URL` 缺失，真实 Redis ping 未通过。
- Render 公开站点 `https://waimai-image-tool-1.onrender.com/healthz` 和 `POST /generate` 均返回 HTTP 404，说明现有 Render 服务仍未运行新的 `api-server` start command。
- Product specification extraction covered `menu_parser.py`, `matching_engine.py`, `image_pipeline.py`, `app.py`, `static/app.js`, `billing.py`, `platform_rules.py`, and related tests.
- Current implementation has only 5 category detection rules and 3 category-specific prompt templates; no code-defined 40-category taxonomy exists in this worktree.
- Gemini refinement is not implemented; README marks Gemini/OpenAI refinement as future work. Current "自定义修改" only charges points and records metadata in UI flow, with no image-editing backend route.
- Chrome login is now verified for both Tencent Cloud and Render.
- Tencent TokenHub `API Key 管理` currently shows `你还没有创建任何 API Key`; a new long-lived TokenHub credential is required before `TENCENT_TOKENHUB_API_KEY` can be configured.
- The authorized Render test service is `https://waimai-image-tool.onrender.com` (`srv-d8qmtgsvikkc73a4d5g0`), Free instance, GitHub `467091085-max/waimai-image-tool` branch `main`.
- Render currently uses Build Command `pip install -r requirements.txt`, Start Command `gunicorn app:app`, and has no Health Check Path.
- Replacing the only Render Web Service with the API-only `api-server` now would remove the customer website at `/`; the immediate `混元未配置` recovery therefore keeps `gunicorn app:app`, configures TokenHub only on this authorized test service, restarts it, and verifies the existing website flow.
- User confirmed creation of a long-lived TokenHub test credential. Created `waimai-render-test` with access limited to `hy-image-v3.0` and `hy-image-lite`; no IP allowlist because the Render Free service does not provide a stable egress IP.
- The credential was copied directly from Tencent Cloud into Render secret env `TENCENT_TOKENHUB_API_KEY`; its plaintext was not written to source, project state, logs, or conversation, and the clipboard/temporary variable were cleared.
- Render deploy after the secret change became live. `/api/tencent-status` now returns `configured=true`, `tokenhubReady=true`, `provider=tencent-hunyuan`; `/api/ops/readiness.generationProvider` returns `ready=true`, `mode=tokenhub`, no generation blockers.
- First real TokenHub style call returned transient HTTP 504; retry succeeded. Six demo style images then all succeeded through `TokenHubImageV3`, were valid 1024x768 JPEG files, had six unique SHA-256 values, and were visually non-placeholder images.
- Uploaded `/Users/guiguixiaxia/Documents/menus/运营数据_蔬适圈·中式轻食健康餐（万达店）.xlsx` to the Render test service. It parsed 56 rows and detected category `轻食健康餐` with 96 confidence.
- Real-menu six category style images all succeeded through `TokenHubImageV3`; visual QA showed actual light-food compositions across six distinct styles, not color blocks.
- Initial six-free-sample request used `FINAL_GENERATION_WORKERS=3`: only one sample succeeded while five transient provider failures were swallowed and mislabeled as `混元未配置`. Sequential retry of an affected sample succeeded, proving the root cause was provider concurrency/error handling rather than missing credentials or balance.
- Render test tuning is live: `FINAL_GENERATION_WORKERS=1`, `TENCENT_TOKENHUB_POLL_TIMEOUT=150`, Start Command `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 180`.
- After the tuning deploy, the real menu was uploaded again; all six background styles succeeded in 16-21 seconds each, and all six free samples succeeded in one request in 88.42 seconds with zero pending results.
- Visual QA of the six free samples passed dish identity, full-frame composition, and a coherent light-wood style. It did not prove exact selected-background reuse: current preview generation passes a text style prompt to TokenHub but does not condition on the selected background image.
- Restarting Render removed the previously uploaded menu and generated local images. This confirms the existing `objectStorage.provider=local` blocker is a real durability defect, not only a readiness warning.
- Added `AI-Project/handoffs/2026-07-29/04_TASK1_LIVE_EVIDENCE.md` so ChatGPT Pro receives the exact real-provider failure/recovery evidence, the false `混元未配置` root cause, and the boundary between prompt-level style similarity and true selected-background image conditioning.
- Chrome reconnect succeeded and a fresh logged-in ChatGPT Pro page is ready. The sanitized ZIP was rechecked at 372,289 bytes with SHA-256 `d29bfcaecd10c53e95949a7e870249e512853744934a7560775826f8b3c10136`, but Chrome rejected `fileChooser.setFiles` because the ChatGPT Chrome Extension does not currently have file-URL access. No source was transmitted; user action to enable that extension permission is pending.
- User authorized direct Computer Use for the extension setting. Four Computer Use attempts remained blocked because macOS Accessibility and Screen Recording permissions for ChatGPT are still pending. Chrome's dedicated control interface also explicitly blocks navigation to `chrome://extensions` by security policy, so it cannot safely toggle its own extension permission. Manual user action remains the only available path; no workaround was attempted.
- After the user enabled file-URL access, Chrome successfully attached the sanitized ZIP, but ChatGPT kept the send button disabled for ZIP and nontrivial text attachments. A 44-byte probe proved the chooser and permission worked; the blocker is ChatGPT attachment parsing, not local file access.
- ChatGPT Pro Task 1 conversation created: `https://chatgpt.com/c/6a6ae1aa-d71c-83e8-9f20-ffcf2aa49cb7` (`任务1补丁分析`).
- The external engineer received the complete task requirements and live Render evidence. Source delivery was switched to the public repository at exact deployed commit `1dbcbb48aac38a118367e650cbd6d27af4faa17f`; unauthenticated `git ls-remote` verified it is publicly readable.
- A clean temporary clone at `/tmp/waimai-image-tool-1dbcbb4` confirmed `app.py`, `static/app.js`, and `tests/test_app_generation.py` are byte-identical to the current isolated worktree despite the commit-label difference, so a patch to the deployed commit's generation files can be independently applied here.
- Official Tencent documentation confirms `hy-image-v3.0` accepts up to three reference images through `images`, but does not guarantee background pixels remain unchanged and does not expose background-lock/input-fidelity/mask controls. The product must distinguish reference-conditioned similarity from exact background identity; exact identity requires deterministic foreground-mask composition.
- Independent acceptance review found two P1 defects beyond the original symptom: `preview_samples()` can abort the entire six-image batch if any future raises, and the frontend clears all preview state before one bulk request, so an escaped exception loses already completed samples. It also found `/api/style-preview-sample?index=abc` returns 500 because `ValueError` is not handled.
- Local Task 1 patch now classifies provider failures as transient/quota/auth/terminal, keeps raw redacted diagnostics in server logs only, and reserves `WaitingForModelConfig` for a genuinely unconfigured provider.
- The frontend now fetches the six-row manifest without generation, calls the existing per-sample endpoint serially, updates stable slots immediately, retries at most once only for network failures or HTTP 408/425/429/5xx/provider-marked transient failures, and retains partial successes.
- The bulk backend route also isolates per-sample exceptions so one failed future cannot erase five successful rows; invalid sample indices now return 400 for non-integers and 404 for negative/out-of-range values.
- Focused Task 1 tests pass: 60 passed across generation, customer UI contract, and security regression suites.
- Local browser verification used the real 56-row light-food menu with Tencent calls deliberately disabled and deterministic local fallback enabled. Six backgrounds rendered, the sample UI visibly progressed from `2/6` to `6/6`, and server logs proved one manifest request followed by six ordered `/api/style-preview-sample` requests. This is UI/control-flow evidence only, not a new real-provider claim.
- ChatGPT Pro's first response was rejected for non-applicable pseudocode, unexecuted PASS claims, incomplete index handling, duplicate retry, and an incorrect HY-Image reference-input conclusion. Its corrected review explicitly withdrew those claims and found no confirmed P0/P1 in the implemented Task 1 scope.
- Conditional review checks are now locked: structured `retryable=false` overrides HTTP status, stale style responses are rejected before state writes, and repeated sample requests reuse the cached generated preview.
- Final Task 1 verification after the review fixes: 62 focused tests and 360 full tests passed; Python compile, both JavaScript syntax checks, and `git diff --check` passed.
- Final latest-code browser verification again observed `2/6` then `6/6` for the real 56-row menu under explicitly labeled deterministic fallback.
- Durable acceptance report: `AI-Project/handoffs/2026-07-29/05_TASK1_CODEX_ACCEPTANCE.md`.

## Next Action
1. Commit and push the final five-category prompt freeze to
   `codex/render-image-staging`; confirm the resulting Render deploy is live
   with only the normal Gunicorn command.
2. Upload one real Excel menu from `/Users/guiguixiaxia/Documents/menus` and
   verify taxonomy routing plus retrieval of the correct approved six-slot
   category manifest.
3. Verify all six free samples, selected-background identity, every formal dish
   and combo output, platform export, points debit, and failure compensation.

## Latest Verified Checkpoint
- Render image staging acceptance: commit `37429f9`, real 56-row Excel parsed with zero errors, six pipeline-v4 paid samples passed visual/SHA/background checks, and the formal compositor probe passed one row. Evidence: `AI-Project/handoffs/2026-07-31/RENDER_IMAGE_STAGING_ACCEPTANCE.md`.
- 支付下单入口现在强制有效手机号会话，只接受服务端版本化套餐 `packageId`；用户、金额、积分、支付渠道和订单 ID 不再由浏览器指定，幂等键只从 `Idempotency-Key` 请求头读取。
- 支付回调按冻结订单金额校验后才允许入账；客户充值 UI 已改为固定套餐下单并移除直接加积分的自定义充值入口。
- 支付目录、服务、计费、API、增长联动和安全定向回归：`59 passed`。
- 40 叶品类 taxonomy、套餐指纹和主站图库门禁已独立验收；解析/匹配/上传相关回归：`130 passed`。
- 真实 `menu` 目录基线来自代理只读验证：24/24 Excel、3,038 行解析成功；约 31.24% 为 `unknown`，这些记录只允许规范化名称完全一致的图库复用，否则强制生图。该比例不是准确率证据。
- ChatGPT Pro Task 4: `https://chatgpt.com/c/6a6b207b-bf8c-83e8-93df-46d41e49fc78`，报告和补丁已归档并独立审查。
- ChatGPT Pro Task 5: `https://chatgpt.com/c/6a6b4d37-5198-83e8-b047-be3107d9987b`，生产 growth/finance review 正在进行；已独立确认 migration 007 缺失、生产 SQLite 路由和跨事务 outbox 风险。
- Task 5 sanitized archive: `/Users/guiguixiaxia/Documents/Codex-Handoffs/waimai-image-tool/2026-07-29/waimai-image-tool-current-task5-v1.zip`, size `821144`, SHA-256 `fcf962bab69dcad0abeec48cdf7644abd026ac7659e3326f27934ba0fd679b56`, baseline commit `4d3214bbd251914fa314265d5ac98d12c1a302fa`.
- 当前仍未提交、未推送、未部署、未执行数据库迁移。
- Prompt Worker closure: done locally; `POST /generate -> generate queue -> prompt Worker -> TokenHub adapter -> done/failed` is implemented, the API health check requires a live queue-scoped `prompt-worker` TTL heartbeat, the customer Web remains unchanged, and the product Worker remains isolated on `product-generate`.
- Prompt Worker focused verification: 48 passed across prompt flow, SaaS API, Render blueprint, and Redis reliability tests.
- Full local regression after Prompt Worker closure: 774 passed; scoped Python compilation, Render YAML parsing, credential scan, and `git diff --check` passed.
- Prompt Worker external verification: not performed; no real Redis, TokenHub request, Render resource creation, deployment, migration, commit, or push occurred.
- PostgreSQL revision settlement foundation: done locally. Terminal `product_revision` Redis tasks now recompute the frozen revision digest, verify owner/job/fence, re-read canonical manifest and image bytes, and produce the same fenced completion object used by the PostgreSQL job store.
- Independent settlement reconciler now dispatches both `product_batch` and `product_revision`; active tasks remain side-effect free and poison jobs remain isolated.
- Free revision settlement now finalizes a zero-point target without creating synthetic debit/refund point orders or ledger rows. Active free-rework quota counting has an owner/parent/job-type scoped PostgreSQL query.
- Revision settlement/store/reconciler focused verification: `57 passed`; scoped Python compilation passed.
- Security read-only audit confirmed four P0 groups: anonymous admin reads, global latest-menu preview leakage, replayable/cross-user object and export tokens, and anonymous private media/model-input reads. No production environment was touched.
- Customer preview/menu isolation: done locally. `/api/plan`, `/api/style-background`, `/api/style-preview`, and `/api/style-preview-sample` now authenticate before any generation boundary, require an owner-scoped `menuUploadId` outside explicit local demo mode, and run against its immutable materialized menu snapshot.
- Customer preview frontend binding: done locally. All four preview/background request families now propagate the current `menuUploadId`.
- Private media exposure closure: done locally. Anonymous `/media` access is limited to image files beneath `seed_public/` and `demo_store/`; generated, selected-background, uploaded-library, and metadata paths are denied. `/model-inputs` is unavailable in staging, production, and Render runtimes.
- Preview/media security contract verification: `27 passed`; related generation/product/background/security/UI regression: `136 passed`; Python and JavaScript syntax checks passed.
- Live PostgreSQL fail-closed boundary: done locally. Product runtime selection no longer chooses SQLite in staging, production, or Render when `DATABASE_URL` is absent or `PRODUCT_POSTGRES_ENABLED=false`; those configurations enter the PostgreSQL path and return a controlled 503 before provider, queue, or SQLite writes.
- PostgreSQL configuration parsing now rejects an explicitly disabled product runtime, and product-generation readiness separately reports database URL presence and the required enabled flag.
- The only live-label SQLite compatibility is a test-only switch that requires both Flask `TESTING` and `ALLOW_SQLITE_PRODUCT_RUNTIME_FOR_TESTS=true`; it is not honored by a real runtime.
- PostgreSQL fail-closed/runtime/readiness and adjacent API regression verification passed: `50`, `87`, and `26` test selections.
- PostgreSQL refinement status/cancel ordering: done locally. Owner-scoped revision type and immutable digest validation now happens before settlement, Redis reads, or cancellation mutation.
- Durable terminal revision status no longer requires Redis. Active cancellation checks the exact Redis revision first; a Redis terminal result is settled and cancellation is rejected, while a running cancellation is requested in Redis before PostgreSQL.
- A running PostgreSQL revision with a missing/unavailable Redis task fails without mutating PostgreSQL. Formal generation job IDs passed to revision status/cancel are rejected before side effects.
- Outbox claim-loss compensation: done locally and independently reviewed. After Redis acceptance, `OutboxClaimLost` triggers an atomic exact-task cancellation only when job, owner, request SHA-256, full payload, fence, and outbox identity match; mismatched tasks remain untouched.
- Refinement ordering regression: `35 passed`; outbox/Redis regression: `51 passed`; scoped Python compilation passed.
- Atomic PostgreSQL free-rework allocation: done locally. `create_or_get_revision_job_with_quota()` locks the owned succeeded parent batch, resolves idempotent replay before quota counting, chooses an immutable free/paid request candidate, and writes job, optional paid wallet debit, settlement, and outbox in one transaction.
- The refinement HTTP route now uses the atomic quota API for `rework`; `refine` remains on the existing atomic paid path. The in-process lock is no longer the production quota authority.
- Atomic quota/store/refinement/reconciler regression: `102 passed`. Real PostgreSQL multi-connection blocking and isolation behavior remain unverified.
- Private preview media delivery: done locally. Generated background/sample paths receive short-lived preview tokens bound to the authenticated user and exact path; `/api/private-media` requires the owner Bearer session and rejects anonymous, cross-user, and path-tampered access.
- The frontend recursively materializes signed private image URLs into authenticated Blob URLs at the API boundary, caches repeated fetches, validates `image/*`, and revokes Blob URLs when the auth session changes.
- Preview/media/UI/generation/security regression: `166 passed`; JavaScript syntax, Python compilation, and `git diff --check` passed.
- Full local regression after security, PostgreSQL refinement, outbox compensation, atomic quota, and private preview delivery: `860 passed in 10.40s`.
- Payment callback wallet-repair retry: done locally. If the payment event commits but the external wallet mutation fails, replaying the same provider event now retries the same idempotent credit/refund order instead of permanently returning a zero effect.
- Payment crash-window fault injection: credit and refund recovery both pass. The first wallet mutation can fail after the payment event commits, the duplicate provider event repairs it, and later replays remain idempotent; this does not make the SQLite payment order/event store durable.
- PostgreSQL customer reads are now side-effect free. Generation/refinement status and generation manifest no longer read Redis or invoke Web settlement; payload builders read the durable job, settlement snapshot, and existing wallet account only. The independent reconciler remains the sole terminal-state/settlement writer.
- Read-purity and adjacent settlement regression: `27 passed` in the PostgreSQL HTTP suite plus `43 passed` across refinement, generation/revision settlement, reconciler, and batch transaction suites.
- Generation/refinement cancellation no longer performs Web settlement. Active Redis cancellation is requested before PostgreSQL cancellation intent, queued unpublished jobs can be canceled durably, wrong job types fail before side effects, and Redis terminal results are left to the independent reconciler.
- Canceled/failed PostgreSQL responses now say `积分退款处理中` until the durable settlement records the full refund; only then do they claim points were returned.
- Cancellation/read-purity PostgreSQL HTTP regression: `29 passed`; adjacent outbox/settlement/refinement/batch suites: `61 passed`.
- Private preview persistence was independently reviewed after agent delivery. Backgrounds and free samples are stored under opaque owner/menu-scoped private object keys with image/metadata SHA-256 checks; live storage failures return 503 instead of serving local-only data.
- Review found and fixed one missing context edge: formal generation now resolves the selected background inside the same owner/menu preview context, so a Render restart can restore the chosen background before freezing the batch.
- Preview persistence plus PostgreSQL route verification: `64 passed`; selected-background/generation/product/security/UI/SaaS adjacent regression: `150 passed`.
- Two isolated security workers are in progress for the admin authorization dependency and one-time nonce consumer foundations; main-thread PostgreSQL refinement route integration remains the immediate critical path.
- PostgreSQL refinement HTTP integration: done locally. Production submission creates the revision job, paid debit or zero-point free job, settlement, and outbox through `ProductJobStore`; Web does not directly enqueue the revision and does not touch SQLite billing/job storage.
- Production refinement status verifies an available Redis terminal task through the revision settlement engine, then reads durable PostgreSQL state. A missing Redis task no longer triggers Web republish; the independent outbox/reconciler services own recovery.
- Production refinement asset and cancellation routes are owner-scoped through PostgreSQL. Queued cancellation settles from the durable debit target; a running job without its Redis task fails closed.
- Focused PostgreSQL refinement verification: `82 passed` across runtime integration, refinement API, revision settlement, reconciler, and job-store suites; `app.py` compilation passed.
- Admin authorization foundation returned from its isolated worker and awaits main-app authorizer wiring plus independent regression. One-time nonce foundation and an independent PostgreSQL refinement audit remain in progress.
- Customer background/free-preview durability now uses deterministic `generated/customer-previews/v1/` object keys scoped by hashed owner and menu-upload identities. Image bytes and integrity metadata are written to the configured private object store and rehydrated after local cache deletion.
- Existing private-media tokens remain bound to the authenticated owner and exact path. Anonymous, cross-user, and path-tampered requests remain rejected; valid signed URLs recover their exact object while unexpired.
- Staging, production, and Render require a ready remote-private store for these assets. Configuration, network, write, read-back, or integrity failures return HTTP 503 and never serve the generated local file as a live fallback; explicit local demo remains local.
- Six-background and six-parallel-free-preview recovery tests pass without a second provider call. Focused verification: `51 passed`; full regression: `873 passed in 10.86s`; scoped Python compilation and `git diff --check` passed.
- No real COS or Render restart was exercised for this checkpoint. No payment, ledger, Redis/PG job file, deployment, migration, commit, or push was performed.
- `ProductPaymentStore` now supports server-catalog-frozen order creation, one-time provider checkout binding, owner-safe lookup, strict event replay, partial/full refunds, and payment event plus point ledger mutation in one PostgreSQL transaction.
- The live real-provider HTTP path no longer writes `payment_orders` or `payment_events` to SQLite. Local fake payment and explicit SQLite test override remain unchanged.
- Disposable real PostgreSQL protocol smoke passed: order creation, signed Alipay checkout, paid callback, identical callback replay, one payment event, one 500-point credit, and zero SQLite payment orders. Direct store smoke also passed paid replay plus partial/full refund with final balance 0 and exactly three ledger rows.
- The same Flask/Alipay/PostgreSQL path is now a gated protocol test: it skips without `TEST_POSTGRES_DSN` and passed against the disposable PostgreSQL container when explicitly enabled.
- PostgreSQL readiness schema probing now requires both payment tables from migration `004`; focused runtime verification passed `7 passed, 1 skipped`.
- Payment success and each refund now enqueue a digest-bound PostgreSQL growth event through the same cursor as the payment event and wallet mutation. An outbox write failure rolls back the entire payment transaction; replay repairs a historically missing outbox row without re-crediting.
- Real Flask protocol verification proved a repeated signed Alipay notify leaves one paid event, one 500-point credit, and one pending first-payment growth outbox row. A test sentinel proved the live path does not call synchronous SQLite growth processing.
- Growth outbox fake/real protocol and payment integration verification passed `70 passed, 1 skipped`; explicit PostgreSQL API protocol passed `1 passed`.
- Payment service/store/API focused verification passed: `50 passed` for service/store and the complete payment/product API selection passed. Production growth, auth, withdrawal/settlement, admin audit, and library/export index persistence remain blocking and must not be reported complete.
- Live admin payment reconciliation now requires `Idempotency-Key`, rejects the fake provider, derives stable server-owned event/action IDs, and writes the payment event, wallet mutation, growth outbox, and finance audit in one PostgreSQL transaction. Identical replay is exact; an injected audit failure rolls back all four effects.
- Payment reconciliation verification passed the disposable real-PostgreSQL protocol (`1 passed`) and the adjacent payment/store/API/security selection (`140 passed`).
- Added append-only migration `012_fix_menu_object_ref_constraint.sql` instead of rewriting checksum-tracked migration 003. It replaces PostgreSQL's invalid `{0,1023}` repetition with an explicit length check plus an unbounded allowlist regex while retaining path-traversal guards.
- The menu constraint migration passed static verification (`1 passed, 1 skipped`) and disposable real PostgreSQL verification (`2 passed`), including two applications of migration 012 plus real menu-record create and exact replay.
- PostgreSQL admin read and immutable security stores have returned from isolated workers and are under independent route/readiness review; their worker-reported full-suite results are not yet accepted as integration evidence.
- Production admin dashboard and all eight list resources now select PostgreSQL providers dynamically; local demo keeps the existing SQLite provider. PostgreSQL failures return controlled 503 payloads instead of `ok=true` zero data.
- Live risk, asset-access, and generic audit actions now require `Idempotency-Key`, use trusted server actors and stable server IDs, and write migration 011 immutable PostgreSQL records. Client `actorUserId` and client request IDs are not trusted identities.
- Asset audit IDs now support guarded 512-character relative object references, including `/`, while rejecting traversal, schemes, duplicate separators, backslashes, and control characters.
- Live invite acceptance consults the latest PostgreSQL user/IP risk decisions before freezing a new relation's risk snapshot; replay continues to use the original immutable snapshot.
- Migration 011's three tables are now required by PostgreSQL readiness. The production admin HTTP protocol proved risk replay/conflict, path-bearing asset access, trusted audit actor, dashboard/list reads, zero SQLite creation, and fail-closed 503 behavior.
- Independent verification after integration: affected default suites `224 passed, 5 skipped`; disposable real PostgreSQL suites `78 passed`. No deployment, external migration, commit, or push occurred.
- Production `/api/upload-library` now requires admin authorization, `Idempotency-Key`, a known taxonomy category/style, and ready remote-private object storage before reading or writing the archive.
- ZIP imports enforce bounded archive/entry/uncompressed/image sizes, compression ratio, safe relative members, no symlinks/encryption, Pillow decode/pixel limits, metadata-stripping normalization, and read-back SHA-256 verification.
- Append-only migration 013 records immutable import batches/items. All asset registrations and the exact batch ledger commit in one PostgreSQL transaction; same-key changed content conflicts, and newly created objects are removed when upload verification or PostgreSQL commit fails.
- Imported assets enter `pending_review` with trusted provenance and `reuse_scope=tenant`. Approved shared backgrounds/products are queried only after the authenticated user's owner-scoped library misses; unreviewed assets remain ineligible.
- The live `product_db_conn()` now centrally raises `LiveSQLiteAccessForbidden` and returns controlled HTTP 503. Explicit local demo and the test-only SQLite override remain available.
- Library/import/asset/readiness verification passed `120 passed, 4 skipped`; disposable real PostgreSQL verification passed `51 passed`, including approval, cross-user shared lookup, exact replay/change conflict, zero SQLite creation, and failed-import object cleanup.
- ChatGPT Pro Task 5 remains visibly in progress and has not delivered final artifacts. No interruption or duplicate task was sent.
- Complete default regression after the library import and live SQLite guard passed `1213 passed, 20 skipped`.
- Complete disposable PostgreSQL regression passed `1231 passed, 2 skipped`; the two remaining skips were Redis-gated tests.
- Complete disposable PostgreSQL plus real Redis DB 15 regression passed `1233 passed in 28.95s` with zero skips. Redis `5.0.8` connected successfully to `redis://127.0.0.1:56379/15`.
- These results are local integration evidence. They do not prove real COS, paid Hunyuan/TokenHub generation, Render deployment, or production database migration.
- All 24 Excel files under `/Users/guiguixiaxia/Documents/menus` parse successfully: `3038` rows total, including `516` rows classified as `套餐/组合`.
- The first three-platform deterministic end-to-end menu run passed for `运营数据_蔬适圈·中式轻食健康餐（万达店）.xlsx`: 56 formal dish images, 168 exported platform derivatives, six unique backgrounds, six free samples, exact selected-background SHA consistency, and output-download SHA verification.
- The first E2E report is `scripts/reports/menu-e2e-deterministic-smoke-20260730.json`. It is local deterministic-provider evidence, not a paid Tencent provider claim.
- Full-menu batch acceptance found a real free-sample defect on `运营数据_朱小小螺蛳粉(春熙路店).xlsx`: a generated filename containing `#` was emitted as an unescaped `/media` URL, so URL parsing truncated it as a fragment and the private-media safety filter removed the URL.
- `media_url_for_path()` now percent-encodes the relative path while preserving `/`. The fix is limited to URL serialization; generation, filenames, storage, and authorization are unchanged.
- Reserved-character URL regression plus the deterministic acceptance contract passed `2 passed`. The same 98-row menu then passed all stages, including 98 formal images and 294 three-platform export derivatives.
- Real-menu taxonomy inspection also confirmed that the 40-leaf dish taxonomy exists, but menu-level background category detection still uses five coarse rules and only three category-specific six-style prompt sets. That is now the next correctness gap under audit.
- Final all-menu evidence is `scripts/reports/menu-e2e-deterministic-all-20260730.json`: `24/24` menus passed, `3038/3038` formal dish rows passed, and `9114` Meituan/Taobao-Eleme/JD derivatives were validated.
- Across the 24 final runs, the deterministic provider boundary recorded 144 style-background calls, 3182 foreground calls, and 3182 mask calls. Every menu also passed six free samples, selected-background identity, manifest row/name/output SHA checks, and delivery ZIP checks.
- The first aggregate process exited nonzero only because it retained the pre-fix 螺蛳粉 result in memory. The exact failed menu was rerun successfully and the aggregate was rebuilt from the 24 immutable final per-menu JSON reports; no individual result was rewritten by hand.
- The all-menu result is not production-provider evidence and does not validate Tencent account quota, model quality, COS networking, or Render deployment.
- Added `background_profiles.py` without changing the 40 stable taxonomy IDs. Every leaf category now has a category-specific scene profile combined with six distinct style axes; no leaf silently falls back to the old three-category prompt table.
- Menu background selection now prefers an explicit store taxonomy, otherwise combines item taxonomy, combo-component, and bounded keyword evidence. No evidence returns a truthful `mixed` context.
- Pure-background prohibitions are at the front of the prompt and have a dedicated negative prompt. Style background cache version is now 3 and cache identity binds taxonomy ID, profile version, and prompt SHA-256.
- Focused taxonomy/background/generation verification passed `61 passed`.
- `scripts/reports/menu-background-taxonomy-20260730.json` passed: 40 profiles, six unique prompts each, 24 real menus, 3038 rows, and 516 combo rows. This report validates prompt/category coverage only.
- Post-change complete deterministic E2E for the 84-row Southeast Asian menu passed: 84 formal images and 252 platform derivatives. Evidence: `scripts/reports/menu-e2e-background-v3-thai-20260730.json`.
- Core taxonomy classification now removes follower-marketing and flavor-only phrases before category evidence and gives the concrete `锅贴` shape precedence over the broader `牛肉锅` substring.
- Exact regressions prove `【粉丝福利】烤翅一对` and `火锅味微辣` stay unknown while `酥炸牛肉锅贴` is `dumpling_wonton`.
- Matching/taxonomy/parser/background focused verification passed `29 passed`. All 24 local Excel files still parse to 3038 rows and 516 combos; conservative unknowns continue to require generation instead of unsafe reuse.
- Complete default regression after the reserved-character, 40-category, and taxonomy-edge fixes passed `1221 passed, 20 skipped`.
- The first PostgreSQL+Redis run found one stale test expectation for `style-background.v2`; production registration correctly used v3. The protocol test now asks `product_asset_pipeline_version("category_background")` instead of pinning an obsolete version.
- Focused real PostgreSQL library-import protocol passed `1 passed`; complete disposable PostgreSQL plus real Redis regression then passed `1241 passed in 28.52s` with zero skips.
- The user superseded the proposed 25-category background layer. The existing
  40 leaf taxonomies are now the canonical background catalog keys as well as
  the dish/combo recognition categories; the target catalog is `40 x 6 = 240`
  reviewed assets.
- Contradictory or insufficient menu evidence must return `mixed/review` rather
  than choose an unsafe catalog. `mixed` does not create a 41st background set.
- Added the versioned 40-by-6 catalog contract: 240 exact category/style pairs,
  two seamless-solid slots, four edge-to-edge flat-table slots, immutable COS
  object keys, and fail-closed complete-manifest validation.
- Reworked all 40 prompt palettes to describe only color, light, and flat
  materials. Raised plinths, platforms, boards, trays, cloth, cups, plants,
  frames, and blurred picture-in-picture layouts are explicitly prohibited.
- Catalog, prompt, and taxonomy focused verification passed `27 passed`.
- Added one-request approved catalog retrieval from the shared PostgreSQL asset
  tenant. Customer UI now loads the six-slot manifest as a group; incomplete,
  duplicate, unavailable, or low-confidence categories never fall back to live
  Hunyuan generation when approved-only mode is enabled.
- Added `scripts/build_background_catalog.py`: dry-run proves 40 categories,
  six slots, and 240 targets; execute mode supports prompt-bound resume,
  bounded retries, quality checks, immutable COS upload/read-back, pending-only
  PostgreSQL registration, and per-category manifest upload.
- Related runtime/catalog/customer UI verification passed `114 passed, 2 skipped`.
- Background-catalog builder contract verification passed `12 passed`; the
  default plan is exactly 40 categories, six slots, and 240 assets, and every
  generated asset enters review as `pending` rather than auto-approval.
- Menu-level classification no longer lets an uncorroborated store-name hit
  override strongly conflicting item evidence; such menus fail closed to
  `mixed/review`, while store/file or store/item agreement remains usable.
- Full regression after the 40-by-6 catalog and classification gate passed
  `1300 passed, 20 skipped`; scoped Python compilation, JavaScript syntax, and
  `git diff --check` also passed.
- Real local menu classification audit passed for 24/24 Excel workbooks with
  zero `mixed` results. This proves deterministic taxonomy routing for those
  fixtures, not paid image quality or provider acceptance.
- Durable catalog contract and operator sequence saved at
  `AI-Project/handoffs/2026-08-01/BACKGROUND_CATALOG_40X6.md`.
- Render test environment audit: the Free service has no Shell and no
  PostgreSQL environment, but its private Tencent COS configuration is ready.
- COS manifest backend: done locally. Pending manifests remain unavailable;
  six approved, prompt-bound, immutable-key assets are required as a complete
  group, and tampered entries fail the category closed.
- COS upload-only builder mode: done locally; generation can upload pending
  objects and a pending six-slot manifest without pretending PostgreSQL review
  registration occurred.
- Full regression after the COS manifest backend passed `1304 passed,
  20 skipped`.
- Visual prompt hardening from independently accepted review findings: done in
  `style-background.v9`; adds a 25-degree camera angle, a 60%-by-50% safe area,
  one coherent primary light and explicit phantom-shadow prohibitions while
  preserving all 40 category-specific palettes.
- Full regression after v9 prompt hardening passed `1304 passed, 20 skipped`.
- Render staging now has `TENCENT_HUNYUAN_SYNC_LIMIT=60`. Environment deploy
  `dep-d9n3mugae00c73apvds0` reached `live`; its logs confirm the normal
  Gunicorn-only start command, so changing the limit did not launch another
  paid acceptance run.
- Next action: start one controlled real-Excel acceptance deploy. Require all
  six approved-background samples, all 60 formal rows including 26 combos,
  selected-background SHA consistency, billing arithmetic, manifest integrity,
  and Meituan ZIP export to pass before restoring normal staging.
- Controlled real-Excel acceptance deploy
  `dep-d9n3o2vlk1mc73dmdef0` has started with the corrected 60-image sync
  limit. Monitor without interrupting while provider progress continues.
- That deploy passed preflight, 60-row upload/plan, all six approved-background
  COS reads, selected style-3 SHA `b432ea0bcb2a`, and all six paid free
  samples without retries. Formal job
  `generation-9c72f9a8144a9c381e52c09a` is active; leave it running while it
  continues to make progress.
- The formal job again completed as `succeeded=7`, `pending=53`; report object
  `generated/acceptance/render-staging/20260801T183408Z/report-d7ac29a6780f6b8f.json`
  has SHA-256
  `d7ac29a6780f6b8fb29e7d8ddbda0368c33d1fc4398be9d0e44bbcc8a42c4df7`.
- Direct Render row inspection found the actual persisted
  `TENCENT_HUNYUAN_SYNC_LIMIT` was still `1`. The application reads this exact
  variable; there is no second hidden limit. The target row was visibly filled
  to `60` before saving this time, and the start command had already been
  restored to normal Gunicorn so the environment deploy cannot auto-run E2E.
- Corrected environment deploy `dep-d9n41lvlk1mc73dmrm50` is live with the
  normal Gunicorn-only command. Reopening the exact Render row after deployment
  confirmed its persisted value is now `60`.
- The staging E2E runner now reads `/api/tencent-status` before paid work and
  fails preflight unless the provider is configured and runtime `syncLimit` is
  at least the exact menu row count. Focused tests passed `7 passed`; complete
  regression passed `1339 passed, 20 skipped`.
- Next action: deploy this preflight guard to the isolated staging branch, then
  run the same 60-row acceptance. The first log proof must show runtime limit
  60 before any of the six paid samples starts.
- Preflight guard commit `ca6ab38` is pushed only to the isolated staging
  branch and Render deploy `dep-d9n43ntbedkc73er93sg` is live. Third controlled
  acceptance deploy `dep-d9n44rajnfac73abn6v0` has started.
- Third-run runtime-capacity preflight passed for all 60 rows. Upload, plan,
  approved-background SHA checks, and all six paid free samples passed without
  retries; selected style-3 SHA remains `b432ea0bcb2a`. Formal job
  `generation-e9e7f968f4b464477150a305` started, but did not finish.
- Render sent Gunicorn `SIGTERM` at 12:16:54 PM, about 15 minutes after the
  E2E deploy became live. The Render event timeline contains no later deploy or
  configuration change. The current root-cause hypothesis is Free-instance
  inactivity spin-down because the embedded E2E runner polls only
  `127.0.0.1`, so its traffic does not keep the public service awake.
- Next action: prove the inactivity hypothesis without paid generation, then
  add public keep-alive plus restart-safe E2E execution before rerunning the
  60-row paid acceptance. Do not restart another paid run until that guard is
  verified.
- Render's official Free-service documentation confirms the root cause: a Web
  Service spins down after 15 minutes without inbound HTTP/WebSocket traffic,
  and local filesystem state is lost on spin-down. The observed 15-minute
  termination therefore matches the documented behavior.
- Implemented a staging E2E public keep-alive guard. Paid and side-effecting
  flow calls remain pinned to localhost/new-instance; a separate HTTPS-origin
  probe targets only the configured public Render hostname, does not follow
  redirects, and must return HTTP 200 before any paid call can start.
- Implemented exact-product foreground reuse using full NFKC/casefold/space
  normalized name, exact kind, and ordered components. Cache metadata binds
  the identity SHA, and striped locks prevent concurrent duplicate provider
  calls. Similar combo names remain distinct.
- Added per-row in-process queue progress updates without changing the Redis
  worker path. Focused keep-alive, exact-background, and queue tests passed
  `46 passed`.
- The real 60-row Excel audit remains 42 strict product identities and 18 safe
  duplicate rows, with 26 combos. Next action: review the external engineer's
  response, run the full regression, then deploy this isolated patch with the
  normal start command before one final paid acceptance.
- Complete regression after the keep-alive, strict-identity reuse, and local
  per-row progress patch passed `1351 passed, 20 skipped` in 22.59 seconds.
  Scoped Python compilation, JavaScript syntax, and `git diff --check` also
  passed.
- Render deploy `dep-d9n4mvrm8hqs73dj8bb0` is live with commit `ca6ab38` and
  only the normal Gunicorn command. No paid E2E entrypoint ran while restoring
  this safe baseline.
- Next action: finish the two independent reviews, address only confirmed
  defects, push the isolated staging patch, wait for its normal deploy, and
  then start exactly one guarded real-Excel acceptance run.
- Independent review found that provisional row progress must not mutate the
  queue's terminal `completed/failed` counters before delivery assets and the
  manifest are durable. Local progress now lives under `rowProgress`; a later
  manifest failure can still atomically finish the queue as fully failed and
  remain consistent with refund settlement.
- Public keep-alive now proves instance identity, not only HTTP 200. The E2E
  process writes a per-instance 256-bit nonce; the Basic-Auth-protected staging
  endpoint must verify that nonce through the public Render hostname before
  paid work starts. All side-effecting flow calls are restricted to the exact
  current `127.0.0.1:$PORT` origin.
- Final exact-background metadata now binds the dish prompt version, and mask
  reuse binds a dedicated extraction cache version. Focused verification for
  these review corrections passed `60 passed`.
- The real-acceptance manifest now accounts for each formal row as a paid
  foreground call, strict-product foreground reuse, verified free-preview
  reuse, final-cache reuse, or approved-asset reuse. Missing evidence fails
  acceptance instead of making an unverified speed claim.
- Complete regression after all review corrections passed `1359 passed, 20
  skipped` in 24.58 seconds. Python compilation, JavaScript syntax, and
  `git diff --check` passed. The only warning is the pre-existing local
  urllib3/LibreSSL compatibility warning.
- ChatGPT Pro completed its architecture review, but reviewed an older source
  archive and therefore did not independently verify the final queue/E2E
  patch. Confirmed findings were applied locally: exact-instance keep-alive,
  restart duplicate-work guard, batch failure fan-out, atomic image/metadata
  writes, and evidence-scoped foreground-call accounting.
- The persistent COS run record is a controlled single-instance staging
  restart guard, not a production distributed lease. Process locks also do not
  coordinate multiple workers, and synchronous Tencent foreground generation
  does not provide durable provider-job recovery after process loss.
- Post-review focused verification passed `65 passed`; final complete
  regression passed `1362 passed, 20 skipped` in 22.09 seconds. Python
  compilation, JavaScript syntax, and `git diff --check` all passed. The only
  warning remains the pre-existing local urllib3/LibreSSL compatibility
  warning.
- Next action: commit and push only this isolated staging patch, verify its
  normal Render deploy, set one unique `WAIMAI_STAGING_E2E_RUN_ID`, and run the
  60-row real Excel acceptance once. Restore normal startup and remove all
  temporary E2E variables immediately after the result is persisted.
- Final guarded Render deploy `dep-d9n546e1egvs73fdpsg0` passed the complete
  paid-provider staging acceptance for the exact workbook SHA-256
  `ca4093931d503c08eff9fc9c959214c9b4ce0817d980cbbfe9240325af857df3`.
  Runtime classification was `mixed_rice`; all 6 approved backgrounds and all
  6 free samples passed downloaded-byte SHA and selected-background identity
  checks.
- Formal job `generation-da25d369dc900438051bed5c` completed all 60 menu rows,
  including 26 combos. The formal manifest passed 60/60 asset downloads and
  selected-background SHA consistency. The Meituan export ZIP was 5,424,386
  bytes and contained all 60 platform images plus the report manifest.
- The public exact-instance keep-alive completed 26 probes with zero
  consecutive failures, so this run continued for about 44 minutes instead of
  being terminated by the Free-service 15-minute inactivity boundary.
- The final redacted evidence report is private COS object
  `generated/acceptance/render-staging/20260801T200813Z/report-584da9946be4d4b0.json`,
  SHA-256
  `584da9946be4d4b08dd0fa7aec76f11ba3a4c0d7157b189a403e137ad760f025`,
  size 51,556 bytes.
- Restored the exact normal Gunicorn command in live deploy
  `dep-d9n5u8m1egvs73ff818g`, then removed all nine
  `WAIMAI_STAGING_E2E_*` variables. Cleanup deploy
  `dep-d9n5vn0ae00c73au73o0` is live, starts only Gunicorn, and retains the
  required catalog, provider, queue-timeout, worker-count, and sync-limit
  settings.
- Staging acceptance is complete. Do not merge to `main` or describe this as
  production verification: Redis/independent Worker, production PostgreSQL,
  distributed paid-call recovery, real payment/SMS/KYC, and production soak
  evidence remain outside this acceptance.

## 2026-08-11 Resume Checkpoint

- Step 98 in progress: Render commit `6786792` passed upload, taxonomy,
  retrieval of all six approved `mixed_rice` backgrounds, selected-background
  SHA binding, and six real Hunyuan sample generations in 399.733 seconds.
  Structural verification was 6/6, but visual QA was only 4/6: row 13 drew
  more than one `三选一` option and row 61 printed Chinese text on the meal
  box. Formal 60-row generation remains stopped; the earlier partial formal
  run was fully refunded and the staging balance remains 1880 points.
- Root cause is isolated to Hunyuan v3 prompt adherence. The foreground request
  allowed provider prompt revision, had no stable seed, retained every option
  from explicit `二/三选一` names, and placed the no-text rule too late.
- A minimal v3 candidate disables provider prompt revision (`Revise=0`), uses
  a product/category/quality-scoped deterministic seed, resolves explicit
  slash/or choices to one fixed option before generation, removes the rejected
  alternatives from combo components, and gives unprinted containers highest
  prompt priority. Prompt/cache versions are bumped so rejected foregrounds
  cannot be reused.
- The first independent review rejected the candidate because the default
  reference-conditioned path still used the old prompt, and the first regex
  could split `或者` or the `or` inside `Original`. Both generation paths now
  share the same resolver. It supports slash, `or`, `或者`, comma, Chinese
  comma, enumeration comma, and vertical-bar forms while using the declared
  option count to ignore category prefixes. Lite/cloud metadata no longer
  claims that an ignored requested seed was applied.
- Focused verification passed 154 tests. A static check over all 60 real menu
  rows confirmed that all 21 explicit-choice rows remove rejected options from
  both Chroma and reference-conditioned prompts. Full regression passed `1382
  passed, 20 skipped`; the skips still require real PostgreSQL/Redis. Next
  action is one staging deploy and a fresh six-image
  visual acceptance. Do not start formal 60-row generation unless all six
  samples pass visual review.
- Independent re-review passed after `seedApplied` was restricted to requests
  actually sent through TokenHub v3. Lite and legacy cloud may still report
  their provider-generated output seed, but no longer claim the requested seed
  was applied.
- Render deploy `dep-d9tq6i8ae00c73bjhnv0` made commit `50a6e1f` live. The
  exact 60-row workbook again passed upload, `mixed_rice`, six approved
  background hashes, and six real sample generations in about 313 seconds.
  Structural verification was 6/6. Visual QA confirmed the explicit-choice
  row now shows only the fixed egg and the self-select meal has no printed
  text, but rejected three samples because `烤排` still looked like pink
  western steak.
- Root cause of the remaining visual error is positive-prompt contamination:
  the Chinese prompt repeatedly named the unwanted steak while negating it,
  and Hunyuan rendered that noun. Prompt contract v4 removes the unwanted noun
  entirely and describes only the required visual: fully cooked light-brown
  Chinese black-pepper boneless pork cut into six slices, with fully cooked
  positive descriptions for chicken and pork cutlets. The version bump
  prevents reuse of the rejected v3 foregrounds. Formal generation remains
  blocked until a fresh six-image visual review passes.
- Step 98 in progress: the current six `mixed_rice` backgrounds were visually
  rejected 0/6. Their low and inconsistent camera geometry, visible table
  edges, missing placement anchors, and generic repeated styling make dishes
  appear upright or pasted even when the provider call succeeds.
- Added the pure `product-image-compiler.v1` foundation. It resolves menu
  choices and food semantics, binds them to one of six structured background
  camera/light/support contracts, writes a complete prompt without arbitrary
  truncation, performs pre-provider Prompt QA, and derives deterministic
  prompt/payload/scene digests plus the generation seed. Wiring it into the
  provider and compositor is the active next step; no paid generation was
  started in this step.
- Step 99 in progress: both reference-conditioned and Chroma foreground paths
  now consume the compiler output. Exact-background pipeline v5 caches are
  bound to the compiler digest and scene-contract digest, and batch-contract
  schema v3 freezes the SHA-bound scene contract for the Worker.
- The compositor now uses the compiled subject anchor and dimensions instead
  of fixed bottom alignment. It adds a light-direction-aware contact shadow
  inside the verified modification mask, preserving every selected-background
  pixel outside the union of dish and shadow.
- Focused compiler, scene-contract, batch-contract, cache, compositor, and
  selected-background verification passed `58 passed, 4 subtests passed`.
  Full regression and the isolated `mixed_rice` v12 catalog path remain next;
  no paid provider request was made in these steps.

- Step 100 local verification complete: the final focused capacity/Gemini
  suite passed `225 passed, 16 subtests passed`; the full repository regression
  passed `1409 passed, 20 skipped, 157 subtests passed`. Python compilation,
  both JavaScript syntax checks, and `git diff --check` also passed.
- A deterministic end-to-end run used the real 60-row workbook
  `运营数据_美滋滋烤肉拌饭（成都店）.xlsx`. It classified the menu as
  `mixed_rice`, generated and SHA-verified six unique backgrounds, bound one
  selected background to six free samples and all 60 formal images, and
  exported all 60 Meituan images plus the manifest. Formal generation took
  36.251 seconds in this local deterministic boundary.
- This run made no Hunyuan or Gemini paid request and is not evidence of real
  image quality, provider quota, or production throughput. The 100-row
  scheduler test reached exactly ten workers, but the live gate remains one
  provider slot until account concurrency is verified. Next action is staging
  branch review/deploy, followed by fail-closed Gemini readiness verification;
  the one-hour target requires a real 100-image run with verified provider
  concurrency and measured p95 latency.
- Step 101 in progress: production throughput readiness now requires three
  independent facts: ten provider slots verified on the real account, measured
  provider P95 within the active-wave budget, and one complete 100-image
  end-to-end batch observed within 3600 seconds. Configuration alone can no
  longer report the target as verified.
- The distributed provider lease now has a safety floor of request timeout plus
  legacy poll timeout plus 60 seconds. This keeps a paid call's slot reserved
  through the configured worst-case provider window even if Redis heartbeat
  renewal is interrupted. Focused capacity verification passed `8 passed`.
- Gemini transport and queue wiring review is complete. The default client now
  refuses HTTP redirects before the API key can be forwarded and malformed
  endpoint ports fail readiness cleanly. PostgreSQL revision outbox records are
  routed only to `product-revision`, and the settlement reconciler reads each
  job from the queue selected by its frozen database job type.
- The non-PostgreSQL compatibility path now compensates a fresh paid debit and
  marks the persisted job failed when Redis reports an idempotency conflict.
  Focused Gemini, refinement, outbox, settlement, and job-store verification
  passed `111 passed`. No Gemini request or debit occurred.
- Background Scene Contract v2 now binds the source background prompt version.
  Existing v11 assets compile dish geometry at their actual 25-degree camera;
  only v12 assets compile against the new 52-62 degree camera templates. The
  runtime still selects approved v11 assets until six real v12 mixed-rice
  backgrounds pass visual review, so no unreviewed catalog was promoted.
- Exact-background pipeline v6 invalidates old output/library records. Tencent
  cache reuse now requires the current row-specific compiler digest and scene
  digest; approved library reuse requires the exact current background scene
  digest. The current official `TokenHubHyImageV3` action also records that its
  deterministic seed was applied. Focused prompt/background/cache verification
  passed `130 passed, 1 skipped`; the skip requires external infrastructure.
- Step 101 code verification is complete. With the declared Redis dependency
  installed only in `/tmp`, the complete repository suite passed
  `1421 passed, 20 skipped` in 23.92 seconds. Python compilation, both frontend
  syntax checks, `git diff --check`, and tracked plus untracked Gitleaks scans
  passed. The skips still require live PostgreSQL/Redis services.
- A fresh deterministic run of the real 60-row mixed-rice Excel passed all
  eight stages. Formal generation completed in 38.866 seconds; all 60 outputs
  retained the selected background SHA and the Meituan ZIP contained 60 images
  plus its manifest. Report: `/tmp/waimai-mixed-rice-e2e-v6.json`. This remains
  local deterministic evidence, not paid-provider visual or throughput proof.
