# V4 Product Acceptance Checklist

This checklist is for the final end-to-end product gate after the V4 module branches are merged.

## Smoke Commands

Dry run. This must not call `/api/jobs/<id>/run` and must not spend Hunyuan quota:

```bash
python3 scripts/smoke_product_flow.py \
  --base-url http://127.0.0.1:8790 \
  --menu-file /path/to/real-menu.xlsx \
  --style-first \
  --limit 1 \
  --no-live-generate
```

Live one-image run. This is the only smoke mode allowed to spend model quota:

```bash
python3 scripts/smoke_product_flow.py \
  --base-url https://your-production-host.example.com \
  --menu-file /path/to/real-menu.xlsx \
  --style-first \
  --live-generate \
  --limit 1
```

The command prints JSON. The release gate is `ok=true`, `summary.failed=0`, and an empty `failures` array.

## Required Gates

1. Homepage is reachable and returns a non-empty HTML response.
2. `/api/tencent-status` returns `provider=tencent-hunyuan`, `configured=true`, and object storage readiness for live runs.
3. `/api/library-status` shows real product images, reusable images, stores, style coverage, and the configured external/COS library source.
4. Real menu upload through `/api/upload-menu` succeeds and returns a non-empty parsed menu count.
5. `/api/plan` returns menu results, pricing/quote fields, match candidates, points, and a selected style.
6. The plan contains all six required style cards: `style-1` through `style-6`.
7. `/api/style-preview?style=<style-id>` works for all six styles and returns sample jobs without paid formal generation.
8. Dry run creates a generation job and returns poll/progress fields, but does not run the job.
9. Live run with `--live-generate --limit 1` runs exactly one formal image and reports job status, item status, provider/action, error if any, and an image URL/path.
10. Progress feedback exposes job totals, completed/failed/pending counts, percent, per-item status, retry/refund fields on failures.
11. Platform export after a successful live image returns at least one image, a download URL, and the requested platform list.
12. Pricing and account fields are visible in the plan/account responses. Before launch, the UI payment path must also verify recharge, debit, insufficient balance handling, refund on failed generation, and admin ledger visibility.

## Failure Policy

Any failed step in the smoke JSON blocks release until the reason is explained and fixed. A live model failure can pass only when it is an expected provider outage and the product shows retry/refund status correctly; otherwise it blocks release.

Dry-run smoke is the default for CI and staging. Live smoke should be run manually on a known small menu after Tencent/COS credentials and library indexing are confirmed.
