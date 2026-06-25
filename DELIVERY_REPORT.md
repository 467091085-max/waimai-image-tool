# V5 E2E Acceptance Delivery Report

更新时间：2026-06-25

## 本次交付

本 worker 负责 `feature/v5-e2e-acceptance` 的验收脚本和交付报告，不改核心业务逻辑。

改动范围：

```text
scripts/smoke_product_flow.py
scripts/smoke_export.py
tests/test_product_flow.py
PRODUCT_ACCEPTANCE.md
DELIVERY_REPORT.md
requirements.txt
```

## 已实现

- `smoke_product_flow.py` 支持 `--base-url local`、`--base-url render` 和完整 URL；`local` 会解析为 `http://127.0.0.1:8790`，不会再把错误字符串当真实 URL 打出去。
- 验收流程覆盖 `.xls/.xlsx` 菜单上传、单品/套餐/总数/积分、六个风格卡、选风格、六张免费样图槽、正式 job 创建、live 正式出图校验、单品/套餐/all 分组导出 ZIP。
- live 模式会检查正式结果是否为 seed/mock/local fallback；provider configured=true 但返回假图时，JSON 失败且 Markdown 用红色标出。
- provider 未配置时，报告会明确说明正式生图真实性卡在 provider 配置前。
- 默认输出简洁 stdout，同时写完整 JSON 和 Markdown 到 `data/exports/acceptance/`。
- `smoke_export.py` 增加 ZIP 内容摘要：`zipBytes`、`zipEntries`、`zipImages`、`hasDeliveryReport`。

## 本地验证结果

```bash
python3 -m py_compile scripts/smoke_product_flow.py scripts/smoke_export.py tests/test_product_flow.py
python3 -m pytest tests/test_product_flow.py -q
python3 -m pytest tests/test_image_pipeline.py tests/test_export_remote_media.py -q
python3 scripts/smoke_export.py
python3 scripts/smoke_product_flow.py --base-url http://127.0.0.1:8797 --style-first --limit 1 --no-live-generate --stdout summary
```

结果：

```text
py_compile: passed
tests/test_product_flow.py: 6 passed
tests/test_image_pipeline.py + tests/test_export_remote_media.py: 8 passed
smoke_export: rows=9, images=9, zipImages=9, hasDeliveryReport=true
no-live product acceptance: ok=true, passed=21, failed=0, skipped=3
```

本地 no-live 验收产物：

```text
data/exports/acceptance/product_acceptance_127.0.0.1_8797_20260625T153431Z.json
data/exports/acceptance/product_acceptance_127.0.0.1_8797_20260625T153431Z.md
```

本地可以读取这些目录，线上服务器不可以。当前分支已提供 `scripts/sync_gallery_to_cos.py`：默认 dry-run 扫描 `cleanpic` / `watermarkpic`，生成可上传 COS 的 JSONL 索引和 summary；`cleanpic` 标为可复用，`watermarkpic` 标为 `reference_only` 且不会进入直接复用候选。上线前还需要主线程拿生产 COS 环境变量执行真实全量上传。

说明：本地环境未配置腾讯正式生图凭证，no-live 模式跳过 `/api/jobs/<id>/run`，所以正式出图真实性需要主线程在 Render 或已配置腾讯/COS 的环境跑 live smoke。

## 主线程合并后建议命令

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

本地 dry-run：

```bash
PORT=8797 PYTHONPATH=. python3 app.py
python3 scripts/smoke_product_flow.py --base-url http://127.0.0.1:8797 --style-first --limit 1 --no-live-generate
```

Render dry-run：

```bash
python3 scripts/smoke_product_flow.py --base-url render --style-first --limit 1 --no-live-generate
```

Render live one-image gate：

```bash
python3 scripts/smoke_product_flow.py --base-url render --style-first --live-generate --limit 1
```

导出 ZIP smoke：

```bash
python3 scripts/smoke_export.py
```

测试：

```bash
python3 -m pytest tests/test_product_flow.py tests/test_image_pipeline.py tests/test_export_remote_media.py -q
```

## 上线前必须完成

1. 用 `scripts/sync_gallery_to_cos.py --no-dry-run --limit 3` 先做 COS 小批量 live check。
2. 确认 summary 和 COS 对象后执行真实图库全量上传。
3. 在线上读取 COS 图库索引，而不是只读仓库内置 seed 图。
4. 用 1 个真实菜单在线上生成 6 张样图，确认混元真实出图质量和成本。
5. 再做支付和登录。
