# Task 1 Codex Acceptance

## Scope

This report covers only the free-sample reliability patch:

- truthful Tencent provider errors;
- progressive per-sample generation;
- bounded transient retry;
- partial-success retention;
- sample-index validation.

It does not claim that exact selected-background reuse, durable production
storage, or the complete product acceptance suite is finished.

## Baseline And Handoff

- Worktree baseline:
  `4d3214bbd251914fa314265d5ac98d12c1a302fa`
- Deployed/public main baseline inspected:
  `1dbcbb48aac38a118367e650cbd6d27af4faa17f`
- The Task 1 product files were byte-identical between those two baselines
  before this patch.
- Sanitized source ZIP:
  `/Users/guiguixiaxia/Documents/Codex-Handoffs/waimai-image-tool/2026-07-29/waimai-image-tool-source-4d3214b.zip`
- ZIP size: `372289` bytes
- ZIP SHA-256:
  `d29bfcaecd10c53e95949a7e870249e512853744934a7560775826f8b3c10136`
- ChatGPT Pro conversation:
  `https://chatgpt.com/c/6a6ae1aa-d71c-83e8-9f20-ffcf2aa49cb7`

## Root Cause

1. `materialize_preview_candidate()` caught every provider exception and
   discarded it. A configured TokenHub 504, timeout, throttling, quota, or
   authentication failure therefore fell through to
   `WaitingForModelConfig` and falsely displayed `混元未配置`.
2. The browser called `/api/style-preview?generate=1` once for all six
   samples. The customer saw no result until the complete long request
   returned.
3. An exception escaping one bulk future could fail the complete bulk
   response and erase already completed browser state.
4. The existing per-sample endpoint was unused and returned HTTP 500 for
   non-integer indices.

## Accepted Implementation

### Backend

- `sanitized_provider_error()` redacts Bearer credentials, API keys, secrets,
  Token values, TC3 credentials, and signatures before server logging.
- `preview_provider_failure()` returns one safe structured category:
  `provider_transient`, `provider_quota`, `provider_auth`, or
  `provider_error`.
- Raw provider text is not returned to the browser.
- `WaitingForModelConfig` is returned only when `tencent_ready()` is false.
- The bulk compatibility route isolates each future exception and preserves
  the other samples.
- `/api/style-preview-sample` returns:
  - 400 for missing, empty, non-integer, or decimal indices;
  - 404 for negative or out-of-range indices.
- A repeated request reuses the cached generated preview before calling the
  provider again.

### Frontend

- Fetches one non-generating preview manifest.
- Calls `/api/style-preview-sample` serially for indices `0..5`.
- Updates each fixed slot immediately.
- Retains completed samples if a later sample fails.
- Uses at most two attempts per sample.
- Retries only:
  - network failures;
  - HTTP 408, 425, 429, 500, 502, 503, or 504 when no structured override is
    present;
  - provider responses with `retryable=true`.
- A structured `retryable=false` overrides a transient-looking HTTP status.
- Style identity is checked before every state write, preventing a stale
  response from overwriting another style.

## External Review

ChatGPT Pro's first delivery was rejected because it contained non-applicable
pseudocode, omitted `ValueError` index handling, claimed unexecuted tests as
PASS, proposed duplicate backend and frontend retry, and incorrectly stated
that HY-Image 3.0 lacked reference-image input.

After receiving exact evidence, it explicitly withdrew those claims. Its
corrected review found no confirmed P0 or P1 issue in the implemented scope
and identified three conditional checks:

1. structured `retryable=false` must override HTTP status;
2. stale style responses must be rejected;
3. retries must reuse already generated samples.

All three are covered in the accepted implementation and tests.

## Independent Verification

- Focused suites:
  `62 passed in 0.61s`
- Full pytest:
  `360 passed in 5.00s`
- Python compile gate:
  passed for the monolith, admin, billing, image, storage, parser, matching,
  library, API server, worker, and shared queue/generator modules.
- JavaScript syntax:
  `static/app.js` and `static/admin.js` passed `node --check`.
- Patch whitespace:
  `git diff --check` passed.
- Local menu parsing:
  24 `.xls`/`.xlsx` files, 3,036 items, 0 failures.

## Browser Evidence

The latest-code browser check used:

- the real 56-row menu
  `运营数据_蔬适圈·中式轻食健康餐（万达店）.xlsx`;
- a dummy configured-provider marker;
- Tencent generation explicitly disabled;
- deterministic local background and preview fallback explicitly enabled.

Observed:

- six background cards rendered;
- the UI reached `2/6` while the request loop was still running;
- the UI then reached `6/6`;
- server logs showed one `/api/style-preview` request without `generate=1`,
  followed by six ordered `/api/style-preview-sample?index=0..5` requests.

This proves browser control flow and progressive state handling only. It is
not a real TokenHub quality or production-stability claim.

## Selected Background Boundary

Tencent's official documentation says HY-Image-V3.0 accepts reference images
through `images`/`Images.N`, up to three images:

- `https://cloud.tencent.com/document/product/1823/130080`
- `https://cloud.tencent.com/document/api/1668/124632`

The current application still sends only a textual style description for
free and formal dish generation. It does not send the selected background
asset as an image condition.

Even after adding `images`, the documented API does not expose a background
lock, reference weight, mask, or pixel-preservation guarantee. Exact
background identity therefore requires deterministic composition with an
immutable background and dish foreground/mask, plus outside-mask pixel
verification.

## Release Status

- Local changes: present in the current detached worktree.
- Git commit: not created.
- Remote push: not performed.
- Render code deployment: not performed.
- Render test configuration from the earlier authorized recovery remains
  separate from this local code patch.

The user authorized push to remote `main` only after the complete product
acceptance criteria pass. They do not yet pass, so this Task 1 patch was not
committed or pushed.

## Remaining Production Blockers

- exact selected-background identity is not implemented;
- Render still needs durable remote object storage configured in the actual
  service;
- the complete 24-menu paid generation and visual correctness matrix has not
  been run;
- the planned production web/API/Redis/worker layout is not activated;
- the broader product requirements for payment, agent settlement, anti-abuse,
  anti-theft delivery, and detailed image refinement require their own final
  production acceptance.

## Local Conflict Risk

The patch changes only the current worktree's preview/error flow, focused
tests, and project-memory documents. It does not touch the user's separate
local checkout, does not commit, does not push, and does not deploy. Runtime
artifacts created by the local checks were moved to
`/tmp/codex-task1-runtime-2026-07-29`.
