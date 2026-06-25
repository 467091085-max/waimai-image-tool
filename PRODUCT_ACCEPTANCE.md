# V6 Product Acceptance

这是外卖菜品图项目 V6 的端到端验收和 Render 发布检查手册。默认命令只跑 dry-run，不会调用 `/api/jobs/<id>/run`，因此不消耗真实混元额度；live 检查必须同时设置 `WAIMAI_ACCEPTANCE_LIVE=1` 并传 `--live-generate --limit 1`。

## 本地启动

```bash
python3 -m pip install -r requirements.txt
PORT=8790 PYTHONPATH=. python3 app.py
```

访问地址：

```text
http://127.0.0.1:8790
http://127.0.0.1:8790/admin
```

Render Start Command 必须保持：

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 180
```

## Render 环境变量

最小可发布变量：

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
COS_LIBRARY_INDEX_URL=https://<bucket>.cos.<region>.myqcloud.com/waimai-gallery/index/library_index.jsonl
```

发布前检查：

```bash
curl https://waimai-image-tool-1.onrender.com/api/tencent-status
curl https://waimai-image-tool-1.onrender.com/api/library-status
```

`/api/tencent-status` 需要看到 `configured=true`，`cosReady=true`。`/api/library-status` 需要看到可复用图库数量大于 0；如果使用 COS 远程图库，还应看到 `remoteIndex=true`、`remoteImages/indexImages` 大于 0。`COS_LIBRARY_INDEX_URL` 来自 `scripts/sync_gallery_to_cos.py --no-dry-run` 输出的 `renderEnv.COS_LIBRARY_INDEX_URL`。

## Smoke Commands

本地 dry-run。覆盖上传菜单、六张风格图、六张免费样图槽、正式生图 job 创建、图片预览数据、单张修改扣费、单张导出、打包导出、积分扣费、library-status；不会跑正式混元生图：

```bash
python3 scripts/smoke_product_flow.py \
  --base-url local \
  --style-first \
  --limit 1 \
  --no-live-generate
```

Render dry-run：

```bash
python3 scripts/smoke_product_flow.py \
  --base-url render \
  --style-first \
  --limit 1 \
  --no-live-generate
```

Render live one-image gate。只有这一条会消耗真实混元额度：

```bash
WAIMAI_ACCEPTANCE_LIVE=1 python3 scripts/smoke_product_flow.py \
  --base-url render \
  --style-first \
  --live-generate \
  --limit 1
```

可选：真实生成六张免费样图。该模式会调用样图接口，运行前确认混元和 COS 配置：

```bash
python3 scripts/smoke_product_flow.py \
  --base-url render \
  --style-first \
  --generate-free-samples \
  --no-live-generate
```

导出 ZIP 独立 smoke：

```bash
python3 scripts/smoke_export.py
```

混元单点 smoke 也有同样的 live 保护：

```bash
python3 scripts/smoke_hunyuan_live.py
WAIMAI_ACCEPTANCE_LIVE=1 python3 scripts/smoke_hunyuan_live.py --base-url https://waimai-image-tool-1.onrender.com --live --limit 1
```

`scripts/smoke_product_flow.py` 会写 JSON 和 Markdown 到 `data/exports/acceptance/`。stdout 默认只输出摘要；需要完整报告时加 `--stdout full`。

## Required Gates

1. 首页返回非空 HTML。
2. `/api/tencent-status` 返回 provider 配置、COS 就绪状态和缺失项。
3. `/api/library-status` 返回图库总量、可复用图、风格数、远程索引状态。
4. `.xls` 和 `.xlsx` 上传菜单均成功，主菜单上传后 `/api/plan` 使用该菜单。
5. `/api/plan` 返回总数、单品数、套餐数、积分、pricing、quote、results。
6. 六张风格图存在，固定 ID `style-1` 到 `style-6` 会作为证据写入报告。
7. 六张免费样图槽通过 `/api/style-preview` 暴露；默认不物化样图。
8. 图片预览数据按单品/套餐/其他分组，并为每行暴露 `publicStatus` 和 `backgroundAction`。
9. 正式生图 job 可创建并返回可轮询 payload；dry-run 跳过 `/api/jobs/<id>/run`。
10. live 模式只允许 `WAIMAI_ACCEPTANCE_LIVE=1 --live-generate --limit 1`，并要求至少一张正式图返回真实 URL/path。
11. live 结果不能是 seed/mock/placeholder/local fallback；命中会作为 red flag 阻塞发布。
12. 积分扣费使用专用 smoke 用户完成一次充值、正式出图扣费、自定义修改扣费，校验余额递减。
13. 单张修改检查 `customEditPoints/customEditCash`，并验证自定义修改扣费链路。
14. 单张保存使用 `scope=selected` 导出；打包导出检查 `selected/all/single/combo`，成功 ZIP 必须包含 `delivery_report.xlsx` 和至少一张图片。

## Failure Policy

任意 `fail` 都阻塞发布，除非明确记录为外部 provider 临时故障并有重试/退款状态证据。provider 已配置但返回 seed/mock/local fallback 一律视为线上阻塞项。dry-run 通过只证明产品链路和账本链路可用，正式生图真实性必须以 live one-image gate 为准。
