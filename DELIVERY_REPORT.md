# V6 E2E Render QA Delivery Report

更新时间：2026-06-26

本模块负责 `feature/v6-e2e-render-qa` 的端到端验收脚本、Render 发布检查文档和小范围测试适配，不修改主业务实现。

## 改动范围

```text
scripts/smoke_product_flow.py
scripts/smoke_hunyuan_live.py
tests/test_product_flow.py
tests/test_v6_acceptance_docs.py
README.md
PRODUCT_ACCEPTANCE.md
DELIVERY_REPORT.md
```

## 已实现

- `scripts/smoke_product_flow.py` 增加 V6 gate：上传菜单、六张风格图、六张免费样图、正式生图 job、图片预览契约、单张保存、单张修改扣费、打包导出、积分扣费、library-status。
- 默认 `--no-live-generate` 不调用 `/api/jobs/<id>/run`，不会消耗真实混元额度。
- live 正式生图必须同时设置 `WAIMAI_ACCEPTANCE_LIVE=1` 并传 `--live-generate --limit 1`；缺任一条件直接退出。
- `scripts/smoke_hunyuan_live.py` 同步增加 `WAIMAI_ACCEPTANCE_LIVE=1` 保护。
- dry-run 会用专用 smoke 用户执行一次充值、正式出图扣费、自定义修改扣费，校验余额变化；这只触发本地账本 API，不触发真实支付。
- 导出检查新增 `scope=selected` 单张保存 gate，并继续检查 `all/single/combo` ZIP 中的 `delivery_report.xlsx` 和图片数量。
- live 报告继续检查正式结果是否出现 seed/mock/placeholder/local fallback；命中 red flag 阻塞发布。
- 文档测试覆盖 README、PRODUCT_ACCEPTANCE、DELIVERY_REPORT 是否包含启动、Render env、smoke 命令、live 安全开关和关键接口。

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
COS_LIBRARY_INDEX_URL=https://<bucket>.cos.<region>.myqcloud.com/waimai-gallery/index/library_index.jsonl
```

部署后先检查：

```bash
curl https://waimai-image-tool-1.onrender.com/api/tencent-status
curl https://waimai-image-tool-1.onrender.com/api/library-status
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

独立 ZIP 和混元检查：

```bash
python3 scripts/smoke_export.py
python3 scripts/smoke_hunyuan_live.py
WAIMAI_ACCEPTANCE_LIVE=1 python3 scripts/smoke_hunyuan_live.py --base-url https://waimai-image-tool-1.onrender.com --live --limit 1
```

测试：

```bash
python3 -m py_compile scripts/smoke_product_flow.py scripts/smoke_hunyuan_live.py scripts/smoke_export.py tests/test_product_flow.py tests/test_v6_acceptance_docs.py
python3 -m unittest tests.test_product_flow tests.test_v6_acceptance_docs
```

## 当前线上阻塞项

- live one-image gate 必须在 Render 已配置腾讯云密钥和 COS 后执行；仅 dry-run 通过不能证明混元正式出图真实性。
- `/api/library-status` 若未显示远程 `COS_LIBRARY_INDEX_URL` 成功读取，说明生产图库仍依赖内置 seed 或本地目录，不能作为最终商用发布通过条件。
- 真实支付、登录、PostgreSQL、后台异步队列仍是产品化阻塞项；本模块只验证现有 MVP 账本和同步 job 链路。
