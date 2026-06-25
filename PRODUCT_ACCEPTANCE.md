# V5 Product Acceptance

This is the release gate for proving the product is not broken after the V5 worktrees merge.

## Smoke Commands

Dry run against local. This uploads menus, checks six style cards and six free sample slots, creates a generation job, and validates grouped ZIP export without calling `/api/jobs/<id>/run`:

```bash
python3 scripts/smoke_product_flow.py \
  --base-url local \
  --style-first \
  --limit 1 \
  --no-live-generate
```

Dry run against Render. `render` resolves to the current known Render URL:

```bash
python3 scripts/smoke_product_flow.py \
  --base-url render \
  --style-first \
  --limit 1 \
  --no-live-generate
```

Live one-image run. This is the only mode that spends formal model quota:

```bash
python3 scripts/smoke_product_flow.py \
  --base-url https://your-render-host.onrender.com \
  --style-first \
  --live-generate \
  --limit 1
```

Optional free sample materialization:

```bash
python3 scripts/smoke_product_flow.py \
  --base-url local \
  --generate-free-samples \
  --no-live-generate
```

The script writes a full JSON report and a Markdown report under `data/exports/acceptance/`. Stdout is a concise JSON summary with only the key failures, red flags, and artifact paths. Use `--stdout full` if a full JSON dump is needed.

## Required Gates

1. Homepage returns non-empty HTML.
2. `/api/tencent-status` reports provider configuration and object storage readiness.
3. `/api/library-status` reports reusable product image coverage.
4. `.xls` and `.xlsx` menu uploads both succeed. The main menu is uploaded last so `/api/plan` uses it.
5. `/api/plan` returns total count, single count, combo count, points, pricing, quote, and menu results.
6. The plan exposes at least six style cards. Fixed IDs `style-1` through `style-6` are recorded as evidence when present, but dynamic library style IDs are accepted as long as six cards are shown.
7. The selected style exposes six free sample slots through `/api/style-preview`.
8. Dry run creates a formal generation job but skips `/api/jobs/<id>/run`.
9. Live run requires at least one completed item with a non-empty image URL/path.
10. If Tencent is configured, live results must not be seed, mock, placeholder, or local fallback output. These are marked in the Markdown report with red text and block the smoke.
11. Exports are checked for `all`, `single`, and `combo` scopes. Each successful export must return a readable ZIP with `delivery_report.xlsx` and at least one image.
12. If Tencent is not configured, the report must state that formal generation authenticity is blocked before model execution.

## Latest Local Dry Run

Run on `2026-06-25` against `http://127.0.0.1:8797`:

```text
ok=true
passed=21
failed=0
skipped=3
```

Artifacts:

```text
data/exports/acceptance/product_acceptance_127.0.0.1_8797_20260625T153431Z.json
data/exports/acceptance/product_acceptance_127.0.0.1_8797_20260625T153431Z.md
```

Provider note: local Tencent credentials were not configured, so formal live generation was intentionally skipped in this dry run.

## Failure Policy

Any failed step in the JSON report blocks release until explained. A provider outage can pass only when the product surfaces retry/refund state correctly and the failure is documented. A configured provider returning seed/mock/local fallback output is a red-flag failure.
