# Task 1 Live Render Evidence

Use this evidence together with `00_MASTER_BRIEF.md` and
`01_GENERATION_PIPELINE.md`. It was collected against the user-authorized
Render test service on 2026-07-29. Do not infer access to the service or its
credentials from this report.

## Test Baseline

- Service: `https://waimai-image-tool.onrender.com`
- Deployed repository revision shown by Render: `1dbcbb4`
- Source archive baseline supplied for review:
  `4d3214bbd251914fa314265d5ac98d12c1a302fa`
- Provider: Tencent TokenHub HY-Image 3.0
- Real menu: `运营数据_蔬适圈·中式轻食健康餐（万达店）.xlsx`
- Parsed menu rows: 56
- Detected shop category: `轻食健康餐`
- Category confidence: 96

## Confirmed Provider State

- `/api/tencent-status` returned `configured=true`,
  `tokenhubReady=true`, and `provider=tencent-hunyuan`.
- `/api/ops/readiness.generationProvider` returned `ready=true`,
  `mode=tokenhub`, with no generation-provider blockers.
- The provider credential is intentionally absent from the source archive.

## Six Background Results

- The first TokenHub background request received a transient HTTP 504.
- A retry succeeded.
- Six demo background/style images then succeeded through
  `TokenHubImageV3`.
- All six were valid 1024x768 JPEGs with distinct SHA-256 values.
- Six real-menu background/style images also succeeded and visually showed
  distinct light-food photography rather than color blocks or SVG
  placeholders.

## Free Sample Failure And Recovery

- With `FINAL_GENERATION_WORKERS=3`, the six-sample bulk route produced only
  one successful image.
- Five transient provider failures were swallowed by
  `materialize_preview_candidate()` and returned as pending rows with the
  false customer-facing message `混元未配置`.
- Retrying one affected sample sequentially succeeded immediately.
- This proves that the observed error was not missing configuration or an
  exhausted Tencent balance.
- Test-only Render tuning used:
  - `FINAL_GENERATION_WORKERS=1`
  - `TENCENT_TOKENHUB_POLL_TIMEOUT=150`
  - one Gunicorn process, eight gthread threads, 180-second request timeout
- After tuning and a fresh menu upload, all six background requests succeeded
  in 16-21 seconds each.
- One six-sample request then returned all six samples successfully in 88.42
  seconds, with zero pending rows.

## Visual Quality Result

- The final six samples showed the requested dish identities.
- Food and background filled the canvas as complete commercial images.
- No small inner framed image or blurred filler canvas was visible.
- The six samples were visually coherent with the selected light-wood style.

## Remaining Correctness Gaps

### False Configuration Error

`materialize_preview_candidate()` catches broad exceptions, discards the
provider error, and falls through to `WaitingForModelConfig`. Transient 504,
timeout, throttling, and provider failures therefore become the false message
`混元未配置`.

The patch must preserve a safe, structured provider error category and must
not expose secrets or raw credentials.

### Bulk Request Latency And Concurrency

The frontend still requests all six samples through one long
`/api/style-preview?generate=1` call. A per-sample endpoint already exists.
Prefer a minimal progressive browser flow with bounded concurrency, retry, and
independent terminal state so one slow or failed image does not suppress five
successful images.

Do not claim that increasing Gunicorn timeouts alone is a production fix.

### Selected Background Is Not Image-Conditioned

The current generation code passes a textual style description derived from
the selected background option. It does not submit the selected background
asset as an image condition/reference to TokenHub.

Visual similarity is not proof of exact selected-background reuse. Research
the official provider capabilities and propose or implement the smallest
testable reference-image mechanism supported by the existing provider
boundary. If HY-Image 3.0 cannot perform the required image-conditioned edit,
fail the acceptance criterion explicitly instead of claiming prompt
similarity as identity.

### Local Storage Is Not Durable

Restarting the Render service removed the uploaded menu and generated local
images. This confirms that `objectStorage.provider=local` is a real durability
defect.

Do not bundle a cloud migration into the generation-error patch. Keep the
storage requirement explicit for Task 2.

## Requested Patch Scope

For the first revision, prioritize the smallest complete patch that:

1. requests six free samples progressively through the existing per-sample
   route;
2. uses bounded concurrency appropriate for the provider;
3. retries only transient provider errors with a finite policy;
4. preserves real provider failure categories instead of reporting
   `混元未配置`;
5. keeps already successful samples visible when another sample fails;
6. adds focused backend and frontend contract tests;
7. does not refactor unrelated billing, storage, agent, or admin modules.

Return a unified diff, changed-file manifest, exact test commands and results,
and a separate recommendation for genuine selected-background image
conditioning if it cannot fit safely in this minimal patch.
