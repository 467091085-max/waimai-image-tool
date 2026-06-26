# V7 E2E Render Acceptance Delivery Report

更新时间：2026-06-26

本模块负责 `feature/v7-e2e-render-acceptance` 的验收脚本、产品级验收文档和当前线上非破坏性检查。不修改 `app.py`、`static/`、`templates/`、`generation_engine.py`、`matching_engine.py`、`image_pipeline.py` 等业务模块。

## 本次改动范围

```text
scripts/smoke_product_flow.py
scripts/smoke_hunyuan_live.py
tests/test_v6_acceptance_docs.py
tests/test_v7_acceptance_scripts.py
PRODUCT_ACCEPTANCE.md
DELIVERY_REPORT.md
```

## 已实现

- `scripts/smoke_product_flow.py` 增加 V7 gate：真实图库、上传菜单、6 张背景、6 张免费单品样图、正式生图 job、图片预览、积分扣费、平台导出、打包 ZIP、Hunyuan live 证据。
- Render dry-run 现在会检查 `real_gallery_runtime`：`remoteIndex=true` 且 `remoteImages/indexImages > 0` 才算真实图库上线；`sources.internal` 不再被当成生产证据。
- 增加 `gallery_upload_env` gate，读取 `/api/admin/gallery-upload/status`，用于判断 `GALLERY_UPLOAD_TOKEN` 和 COS 上传代理是否启用。
- smoke 报告新增 `skips`，明确区分通过、失败、因 Render env 未配置而跳过。
- `scripts/smoke_hunyuan_live.py` 在 Tencent/COS env 不齐时直接返回 skipped，不创建付费 job。
- live 正式生图仍必须同时设置 `WAIMAI_ACCEPTANCE_LIVE=1` 并传 `--live-generate --limit 1`。
- red flag 现在会让产品 smoke 总体 `ok=false`，避免 mock/seed/local fallback 被误写成交付通过。

## 启动与部署

本地启动：

```bash
python3 -m pip install -r requirements.txt
PORT=8790 PYTHONPATH=. python3 app.py
```

Render Start Command：

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 180
```

Render 环境变量：

```text
TENCENT_HUNYUAN_ENABLED=true
TENCENTCLOUD_SECRET_ID=你的 SecretId
TENCENTCLOUD_SECRET_KEY=你的 SecretKey
TENCENTCLOUD_REGION=ap-guangzhou
PUBLIC_BASE_URL=https://waimai-image-tool-1.onrender.com
TENCENT_HUNYUAN_MODE=auto
TENCENT_HUNYUAN_SYNC_LIMIT=6
TENCENT_IMAGE3_ENABLED=true
TENCENT_IMAGE3_POLL_TIMEOUT=150
TENCENT_IMAGE3_POLL_INTERVAL=3
TENCENT_IMAGE3_FALLBACK_TO_LITE=true
TENCENT_COS_BUCKET=waimai-image-tool-inputs-1311836560
TENCENT_COS_REGION=ap-guangzhou
TENCENT_COS_PREFIX=waimai-model-inputs
COS_LIBRARY_INDEX_URL=https://waimai-image-tool-inputs-1311836560.cos.ap-guangzhou.myqcloud.com/waimai-gallery/index/library_index.jsonl
GALLERY_UPLOAD_TOKEN=一段只放在 Render 和本地上传命令里的私密 token
```

## 验收命令

本地 dry-run：

```bash
python3 scripts/smoke_product_flow.py --base-url local --style-first --limit 1 --no-live-generate
```

Render dry-run：

```bash
python3 scripts/smoke_product_flow.py --base-url render --style-first --limit 1 --no-live-generate
```

Render live one-image gate：

```bash
WAIMAI_ACCEPTANCE_LIVE=1 python3 scripts/smoke_product_flow.py --base-url render --style-first --live-generate --limit 1
```

混元单点 gate：

```bash
python3 scripts/smoke_hunyuan_live.py --base-url https://waimai-image-tool-1.onrender.com
WAIMAI_ACCEPTANCE_LIVE=1 python3 scripts/smoke_hunyuan_live.py --base-url https://waimai-image-tool-1.onrender.com --live --limit 1
```

相关测试：

```bash
python3 -m py_compile scripts/smoke_product_flow.py scripts/smoke_hunyuan_live.py
python3 -m pytest -q tests/test_v6_acceptance_docs.py tests/test_v7_acceptance_scripts.py
```

## 当前线上非破坏性检查

检查时间：2026-06-26，目标：`https://waimai-image-tool-1.onrender.com/`

`/api/tencent-status`：

```text
configured=true
cosReady=true
provider=tencent-hunyuan
providerStatus=succeeded
image3Enabled=true
styleBackgroundsLive=true
missing=[]
```

结论：混元和 COS 生成环境已具备 live gate 条件。

`/api/library-status`：

```text
total=480
styles=6
sources={"internal":480}
remoteIndex=false
remoteImages=0
indexImages=0
```

结论：真实图库尚未成为线上运行时图库。当前仍是 internal seed 图，不能作为商用交付通过证据。

`/api/admin/gallery-upload/status`：

```text
cosReady=true
enabled=false
bucket=waimai-image-tool-inputs-1311836560
indexUrl=https://waimai-image-tool-inputs-1311836560.cos.ap-guangzhou.myqcloud.com/waimai-gallery/index/library_index.jsonl
renderEnv.COS_LIBRARY_INDEX_URL=已配置
```

结论：COS 可用，`COS_LIBRARY_INDEX_URL` 已配置，但上传代理未启用。需要 `GALLERY_UPLOAD_TOKEN` 生效并部署后，才能跑小批量真实图库上传、publish、verify-library。

## Render dry-run 验收结果

命令：

```bash
python3 scripts/smoke_product_flow.py --base-url render --style-first --limit 1 --no-live-generate --skip-billing-check --timeout 60
```

结果：

```text
ok=false
passed=22
failed=1
skipped=10
redFlags=1
```

失败项：

```text
real_gallery_runtime: Render must read the COS real gallery index; internal seed images are not production evidence
```

跳过项里最关键的是：

```text
gallery_upload_env: Render gallery upload proxy/env is not enabled; upload-live checks are skipped
job_run_live: not run by default; pass --live-generate --limit 1 to run one formal image
platform_export:*: dry-run has no formal image for this export scope; run live smoke to validate this ZIP
```

red flag：

```text
style_preview:style-1 -> seed marker: seed_
```

本次报告文件：

```text
data/exports/acceptance/product_acceptance_waimai-image-tool-1.onrender.com_20260625T171557Z.json
data/exports/acceptance/product_acceptance_waimai-image-tool-1.onrender.com_20260625T171557Z.md
```

## 当前距离可交付还差哪些证据

- 真实图库：缺 `remoteIndex=true`、`remoteImages/indexImages > 0` 的线上证据。
- 图库上传代理：`enabled=false`，缺 `GALLERY_UPLOAD_TOKEN` 已生效的线上证据。
- 6 张背景：需要 Render dry-run 报告证明 6 张背景均可返回，并且不是重复/占位图。
- 6 张免费单品样图：需要 `--generate-free-samples` 或产品 smoke 报告证明 6 张样图槽/物化图可用。
- 正式图：需要 `WAIMAI_ACCEPTANCE_LIVE=1 --live-generate --limit 1` 的真实 Hunyuan live 证据。
- 平台导出和打包 ZIP：需要 smoke 报告证明 `selected/all/single/combo` ZIP 可下载，包含 `delivery_report.xlsx` 和图片文件。

## 建议下一步

1. 合并 gallery-cos-runtime 的发布后 runtime index 激活补丁。
2. 在 Render 配置 `GALLERY_UPLOAD_TOKEN` 并确认 `/api/admin/gallery-upload/status` 返回 `enabled=true`。
3. 先上传 3 张真实图库并 publish：

```bash
PYTHONPATH=.codex_deps:. python3 scripts/push_gallery_via_app.py \
  --base-url https://waimai-image-tool-1.onrender.com \
  --token <GALLERY_UPLOAD_TOKEN> \
  --limit 3 \
  --publish \
  --wait-ready 300 \
  --verify-library
```

4. 小批量通过后全量上传真实图库。
5. 运行 Render dry-run，再运行 one-image live gate。

只有以上证据齐全，才能把这个版本标记为可交付。当前状态属于“混元环境 ready，但真实图库运行时证据缺失”。 
