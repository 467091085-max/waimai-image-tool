# V6 Worktree And Subagent Plan

Base branch: `main`
Base commit: `df434cb`
Main project: `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy`

Goal: turn the current demo into a usable product flow with real gallery matching, Tencent Hunyuan image generation, visible progress states, platform-safe export, and repeatable acceptance checks.

## Running Workers

| Module | Worktree | Branch | Agent | Scope |
|---|---|---|---|---|
| Real gallery and COS | `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v6/real-gallery-cos` | `feature/v6-real-gallery-cos` | Euclid `019eff8f-ce02-7f83-9909-5e17260880ca` | `scripts/sync_gallery_to_cos.py`, `library_index.py`, COS/readme/tests |
| Matching and style candidates | `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v6/matching-style-preview` | `feature/v6-matching-style-preview` | Parfit `019eff8f-ce58-7062-ad99-8ae3a854b61a` | `matching_engine.py`, `menu_parser.py`, style candidate APIs/tests |
| Hunyuan production generation | `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v6/hunyuan-production` | `feature/v6-hunyuan-production` | Banach `019eff8f-ce92-73d3-a8b8-796eec09cb7f` | `generation_engine.py`, `image_pipeline.py`, `generation_jobs.py`, generation tests |
| Async jobs and progress API | `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v6/async-jobs-progress` | `feature/v6-async-jobs-progress` | Aquinas `019eff8f-ced4-7691-b50a-3d5356e1a973` | `generation_jobs.py`, job APIs, minimal polling JS/tests |
| Customer product UI | `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v6/ui-product-flow` | `feature/v6-ui-product-flow` | Sartre `019eff8f-cf29-7913-b59c-0d3a4ef23abf` | `templates/index.html`, `static/styles.css`, `static/app.js` UI states |
| Export, watermark, platform sizes | `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v6/export-watermark-platform` | `feature/v6-export-watermark-platform` | Avicenna `019eff8f-cf8e-7a50-97ab-2f376dace011` | `image_pipeline.py`, export APIs, watermark/platform tests |

## Queued Worker

The first six workers are running now. The current subagent concurrency limit blocked the seventh worker, so this task is queued and should be started as soon as one worker finishes.

| Module | Worktree | Branch | Scope |
|---|---|---|---|
| End-to-end acceptance and Render QA | `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v6/e2e-render-qa` | `feature/v6-e2e-render-qa` | `tests/`, `scripts/smoke_*.py`, `README.md`, `PRODUCT_ACCEPTANCE.md`, `DELIVERY_REPORT.md` |

## Integration Order

1. Merge backend foundations first: real gallery/COS, matching/style, Hunyuan generation.
2. Merge async jobs before front-end UI polling work.
3. Merge export/watermark/platform after generation outputs are stable.
4. Run the queued e2e worker after the first worker finishes, then use its checks as the final merge gate.
5. After each merge into `main`, run the focused tests from that worker, then run the full suite.
6. Push `main` to GitHub and check Render deployment.

## Current External Blockers

The code can be prepared without stopping, but full production validation still needs:

1. Tencent COS credentials in `.env.cos` or Render environment variables.
2. `COS_LIBRARY_INDEX_URL` pointing to the uploaded gallery index.
3. Tencent Hunyuan image generation credentials on Render.
4. A live smoke run with explicit opt-in, because real generation consumes paid quota.

