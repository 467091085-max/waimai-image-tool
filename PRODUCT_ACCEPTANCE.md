# V7 Product Acceptance

这是“外卖菜品图一键生成工具”的产品级验收清单。验收目标不是证明页面能打开，而是证明真实图库接入、上传菜单、6 张背景、6 张免费单品样图、正式生图 job、Hunyuan live、积分扣费、平台导出和打包 ZIP 都有可复现证据。

默认命令只跑 dry-run，不会触发正式混元出图。真实调用必须同时设置 `WAIMAI_ACCEPTANCE_LIVE=1` 并传 `--live-generate --limit 1`，避免误消耗额度。若腾讯云或 COS 的 Render env 缺失，脚本会把相关 gate 标成“因 Render env 未配置而跳过”，而不是伪装成通过。

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

产品验收至少需要：

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

检查命令：

```bash
curl https://waimai-image-tool-1.onrender.com/api/tencent-status
curl https://waimai-image-tool-1.onrender.com/api/library-status
curl https://waimai-image-tool-1.onrender.com/api/admin/gallery-upload/status
```

`/api/tencent-status` 需要 `configured=true`、`cosReady=true`。`/api/library-status` 需要 `remoteIndex=true`，并且 `remoteImages` 或 `indexImages` 大于 0。只看到 `sources.internal` 不算真实图库上线。`/api/admin/gallery-upload/status` 需要 `enabled=true`，否则真实图库上传代理未启用。

## Smoke Commands

本地 dry-run：

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

Render live one-image gate：

```bash
WAIMAI_ACCEPTANCE_LIVE=1 python3 scripts/smoke_product_flow.py \
  --base-url render \
  --style-first \
  --live-generate \
  --limit 1
```

可选：物化 6 张免费单品样图：

```bash
python3 scripts/smoke_product_flow.py \
  --base-url render \
  --style-first \
  --generate-free-samples \
  --no-live-generate
```

混元单点 smoke：

```bash
python3 scripts/smoke_hunyuan_live.py --base-url https://waimai-image-tool-1.onrender.com
WAIMAI_ACCEPTANCE_LIVE=1 python3 scripts/smoke_hunyuan_live.py --base-url https://waimai-image-tool-1.onrender.com --live --limit 1
```

报告会写入 `data/exports/acceptance/`，包括 JSON 和 Markdown。stdout 默认输出摘要；完整报告加 `--stdout full`。

## Required Gates

1. 首页返回非空 HTML。
2. `/api/tencent-status` 返回 provider、`configured`、`cosReady`、缺失 env 和 provider 状态。
3. Render provider 环境齐全；若缺失，live gate 标为“因 Render env 未配置而跳过”。
4. `/api/library-status` 返回图库数量、风格数、远程索引状态。
5. Render 真实图库必须 `remoteIndex=true`，并且 `remoteImages/indexImages > 0`。
6. `/api/admin/gallery-upload/status` 可读，且生产上传代理应 `enabled=true`。
7. `.xls` 和 `.xlsx` 上传菜单均成功，主菜单上传后 `/api/plan` 使用该菜单。
8. `/api/plan` 返回总数、单品数、套餐数、正式出图所需积分、pricing、quote、results。
9. 返回 6 张背景风格卡，固定 ID 覆盖 `style-1` 到 `style-6`。
10. `/api/style-preview` 对选中风格返回 6 张免费单品样图槽。
11. 可选物化样图时，6 张免费单品样图要有候选图片、生成状态和 provider/action 证据。
12. 图片预览按单品、套餐、其他分组，并为每行暴露 `publicStatus` 和 `backgroundAction`。
13. 正式生图 job 可创建，返回 job id、进度、可轮询 payload 和积分。
14. Hunyuan live 模式只允许 `WAIMAI_ACCEPTANCE_LIVE=1 --live-generate --limit 1`，并要求至少一张正式图返回真实 URL/path。
15. live 结果不能是 seed/mock/placeholder/local fallback；命中 red flag 阻塞发布。
16. 积分扣费使用 smoke 用户完成一次充值、正式出图扣费、自定义修改扣费，校验余额递减。
17. 单张修改检查 `customEditPoints`，并验证扣费链路。
18. 平台导出覆盖美团、淘宝、京东尺寸设置。
19. 单张保存使用 `scope=selected` 导出。
20. 打包 ZIP 检查 `selected/all/single/combo`，成功 ZIP 必须包含 `delivery_report.xlsx` 和至少一张图片。

## Pass / Fail / Skip

- `pass`：当前 gate 有直接证据证明可用。
- `fail`：产品交付要求未满足，例如 Render 仍只读 internal seed 图。
- `skip`：外部环境没准备好或该 gate 被显式关闭，例如“因 Render env 未配置而跳过”。skip 不等于产品可交付，只表示本次 smoke 没有误判为业务失败。

发布口径：任意 `fail` 或 red flag 都阻塞上线。只有 dry-run 通过、真实图库上线、Hunyuan live 通过、ZIP 导出有真实文件证据，才能说当前版本可交付。
