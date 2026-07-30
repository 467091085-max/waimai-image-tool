# 外卖菜品图一键生成工具

这是一个“AI 生成 + 资产沉淀复用”的外卖菜品图产品代码基线。当前版本可以上传 Excel 菜单，生成并选择品类背景，批量生成整店菜品图，精细改单图，并按平台尺寸导出图片包。

## 已实现功能

- 菜单上传：支持 `.xls` 和 `.xlsx`，提供标准菜单模板下载。
- 菜单解析审计：支持多 Sheet、空行表头、运营数据导出、成本/活动噪声表过滤。
- 内部参考图读取：本地可扫描 `cleanpic` / `watermarkpic`，clean 图优先匹配，水印图只做参考或人工兜底。
- 线上种子参考图：仓库内已导入一批压缩后的内部参考图，Render 不依赖 Mac 本地目录也能跑通演示链路。
- 内部资产索引：可生成 JSONL 图片资产索引和缩略图，包含店铺、菜品名、尺寸、来源、标签、sha1。
- 菜品拆分：自动统计单品、套餐、小吃/其他图片数量。
- 风格预览：展示背景风格，免费样图生成和风格选择分离，避免选择风格时自动触发耗时生成。
- 出图质量：支持普通出图和精修出图两档，普通出图 10 积分/张，精修出图 20 积分/张。
- 账号登录：支持手机号 OTP、session、门店接口；短信发送已抽象为 local/mock 与 webhook provider，关闭本地 demo 且未配置 provider 时会返回明确 503。
- 积分计费：生产路径使用 PostgreSQL 钱包、流水、订单、结算和 outbox，支持服务端套餐、生成扣费、失败/部分退款、回调重放修复和人工对账；SQLite/fake 支付仅保留为显式本地 demo，live readiness 会拒绝使用。
- 代理/邀请：一级直接关系，代理首单 20%、复购 10%，邀请注册 100/20 积分，直接邀请首充返 10% 积分；佣金、退款冲销、结算、提现、财务角色和审计都已接入 PostgreSQL。
- 品牌水印：支持文字水印和透明 PNG Logo，支持角标和平铺。
- 正式图预览：按单品图片、套餐图片、其他图片分组显示。
- 正式出图异步任务：新的 SaaS 骨架已拆出 `api-server/`、`worker/`、`shared/`，标准接口固定为 `POST /generate`（只接受 `prompt` 并返回 `task_id`）和 `GET /status/<task_id>`（只返回 `status`、`image_url`），API Server 只写 Redis 队列和查询状态，AI 生成由独立 Worker 执行。
- 重做额度：每单提供免费换版额度，用完后按 10 积分/张。
- 导出：当前统一导出 JPG，支持勾选、全选、单品、套餐导出，并按平台上限压缩文件大小。
- 平台尺寸：支持美团、淘宝外卖/饿了么、京东外卖/京东秒送导出，不裁掉主体，按目标尺寸留边适配。
- 内部后台：`/admin` 可查看运营概览，并已接入生成任务、资产访问、佣金结算、订单等 lists 明细。
- AI 资产审核 API：后台可通过状态接口 approve/reject/disable AI 资产，保存审核备注并写后台审计；完整人工审核台仍未生产化。
- 存储底座：已实现本地/COS 对象存储 adapter、私有对象 key、限量流式读写、上传后 SHA-256 校验和生产 readiness；live 环境必须配置私有远程 provider、bucket 和签名 secret。
- 资产访问审计：导出下载和对象读取的允许/拒绝会写入本地 `asset_access_logs`，后台可汇总并按 `status=denied/allowed` 等条件查看异常访问。
- 一次性下载保护：PostgreSQL nonce 预留、session advisory lock、精确消费和拒绝审计保证并发首用只读取一次对象，重放在对象下载前被拒绝。
- 契约测试保护：测试已锁定前台不回退到“真实图库/免费样图预览”等旧口径，正式出图必须走异步任务，后台必须保留 lists 明细接入。

## 产品化口径

- 图库策略：新生成的品类背景图、免费样图、正式菜品图都沉淀到服务器目录或生产对象存储；AI asset manifest 打标签，未来按品类、菜名、关键词、风格和质量复用。
- 前台口径：不宣传“真实图库”，只表达 AI 生成、样图预览、历史生成资产复用；内部参考图只用于匹配、兜底、审核和资产沉淀。
- 代理/邀请：只做一级直接关系；代理首单按实付净额 20% 返佣、复购按 10% 返佣；C 端注册邀请人 100 积分、被邀请人 20 积分，仅直接邀请首充返 10% 积分，不返现金、不提现。上线前仍需由中国执业律师审核具体页面、合同和运营流程。
- 代理佣金、退款追索、结算与提现状态机已经完成；实名/主体认证、税务和真实打款属于上线前的外部财务与合规验收。
- 短信登录：本地 demo 可继续返回 `mockCode`；生产环境必须关闭本地 demo，配置 `SMS_PROVIDER=webhook`、`SMS_WEBHOOK_URL`、`AUTH_SESSION_HASH_SECRET` 和 `AUTH_OTP_HASH_SECRET`，并共享 `DATABASE_URL`/`REDIS_URL`。生产验证码、用户、session 和门店归属不会回退到本地 SQLite。
- 支付：本地 demo 可继续使用 fake pay；生产关闭 `ENABLE_LOCAL_DEMO_BILLING` 后，必须配置真实支付。当前支付宝电脑网站支付支持 RSA2 签名下单和异步通知验签入账；后台财务人工支付对账已可推进订单 paid/refunded/closed/failed 并复用积分入账/退款；微信支付仍未接入 adapter，会 fail-closed。
- 对象存储：本地 demo 可继续使用 local/mock 存储；生产或设置 `ENABLE_LOCAL_DEMO_STORAGE=false` 时，local/mock 会被 readiness 标记为 not production-ready，必须配置私有远程 provider、bucket 和 `OBJECT_SIGNING_SECRET`。
- 合规边界：多级分销暂不启用，必须法务确认后另开方案。
- 产品模块：数据库、对象存储、积分/支付、账号/门店、生成队列、AI 图库沉淀、防盗图/签名 URL、防刷风控、代理/邀请、管理后台、审计、运营指标。
- 当前代码已完成所需产品路径和角色/审计门禁；生产日志聚合、告警、备份、人工审核 SOP 和真实财务流程仍属于部署运营配置。
- 当前状态：产品实现已通过本地及一次性 PostgreSQL/Redis 验收，但没有部署或迁移外部数据库；详细证据见 `MODULE_STATUS.md` 和 `AI-Project/handoffs/2026-07-30/final-goal-completion-audit.md`。

## 默认平台尺寸

当前先按常见上架规格做成配置：

| 平台 | 默认尺寸 | 当前导出上限 |
|---|---:|---:|
| 美团外卖 | 800 x 600 | 5 MB |
| 淘宝外卖/饿了么 | 800 x 800 | 20 MB |
| 京东外卖/京东秒送 | 800 x 800 | 5 MB |

当前制作规则：美团按 4:3 输出 800 x 600；淘宝外卖/饿了么、京东外卖/京东秒送按 1:1 输出 800 x 800。全部导出为 JPG + RGB 模式，避免客户下载后还要二次转换。任意选择 1 个平台免费，每多选 1 个平台加 100 积分。若平台规则变化，只需要改 `image_pipeline.py` 里的 `PLATFORMS` 常量。

## 本地启动

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

默认地址：

```text
http://127.0.0.1:8790
```

内部后台：

```text
http://127.0.0.1:8790/admin
```

如果端口被占用：

```bash
PORT=8795 python3 app.py
```

## Render 部署

Render 拓扑以仓库根目录的 `render.yaml` 为准。蓝图关闭了自动部署和预览环境；同步蓝图会涉及付费 Worker、持久化 Key Value 和 PostgreSQL，必须在人工确认费用、凭据与迁移后进行。

当前活动入口：

- 客户网站 `waimai-image-tool`：`gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 60`，健康检查 `/`。
- 独立 API `waimai-image-tool-api`：`gunicorn --chdir api-server app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 60`；`/healthz` 只有在 Redis 可读且 `prompt-worker` TTL 心跳有效时返回 200。
- Prompt Worker：`python -m worker.prompt_worker`，独占 `generate` 队列和 TokenHub provider 调用；启动时要求 `AI_IMAGE_PROVIDER=tencent-tokenhub` 与 `TENCENT_TOKENHUB_API_KEY`。
- Outbox Dispatcher：`python -m worker.outbox_dispatcher`。
- Product Worker：`python -m worker.worker`，并设置 `WORKER_TASK_MODE=product`。
- Settlement Reconciler：`python -m worker.product_settlement_reconciler`，独立验证 Redis 终态、PostgreSQL fence、manifest 摘要和退款结算；客户是否轮询不影响最终结算。
- Redis/Key Value：所有活动进程通过 `fromService` 共用内部 `REDIS_URL`；队列配置为 `noeviction`，PostgreSQL/outbox 才是持久任务事实源。
- PostgreSQL：客户网站和 Outbox Dispatcher 通过 `fromDatabase` 获取 `DATABASE_URL`；蓝图不会自动执行 `migrations/`。

部署前外部验收：

- 本次完成代码、确定性生图和一次性 PostgreSQL/Redis 验收；同步蓝图、创建付费 Worker、配置 TokenHub/Gemini/COS/支付密钥及真实供应商验收仍需单独授权。
- live 路径会强制 PostgreSQL、Redis、私有对象存储和服务心跳；SQLite、本地文件和 fake provider 只允许显式本地 demo。
- 首次启用 PostgreSQL 前必须人工执行并验证迁移；本次蓝图不会部署、创建资源或迁移数据库。

独立 Prompt Worker 本地启动需要与 API 共用 `REDIS_URL`，并配置：

```text
AI_IMAGE_PROVIDER=tencent-tokenhub
TENCENT_TOKENHUB_API_KEY=<Render secret>
TENCENT_TOKENHUB_IMAGE_MODEL=hy-image-v3.0
```

独立 API 还必须配置 `PROMPT_API_TOKEN`。`POST /generate` 和
`GET /status/<task_id>` 都要求 `Authorization: Bearer <token>` 或
`X-Prompt-API-Token: <token>`；缺少服务端 token 时接口会 fail closed，
不会把任务写入 Redis。该鉴权不改变固定 JSON 请求/响应字段。

Worker 会把 provider 返回的 HTTPS 图片地址写入 Redis 终态。当前独立
`/generate` 合同未承诺把该远程地址再次复制到产品对象存储；供应商 URL
的有效期仍需在真实联调中确认。

当前测试站地址（仍运行旧提交，本次推送已通过 `[skip render]` 跳过部署）：

```text
https://waimai-image-tool.onrender.com
```

## 测试命令

语法检查：

```bash
PYTHONPATH=.codex_deps:. python3 -m py_compile app.py admin_panel.py billing.py image_pipeline.py storage_db.py menu_parser.py matching_engine.py library_index.py
node --check static/app.js
node --check static/admin.js
git diff --check
```

单元测试：

```bash
PYTHONPATH=.codex_deps:. python3 -m unittest discover -s tests
PYTHONPATH=.codex_deps:. python3 test_matching_engine.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q
```

真实菜单解析审计：

```bash
PYTHONPATH=.codex_deps:. python3 -m menu_parser /Users/guiguixiaxia/Documents/menus
```

### 真实菜单端到端验收

默认模式不会调用腾讯或其他付费 provider。它使用电脑中的真实 Excel
菜单，替换的只有 provider 边界；上传、菜单快照、6 张背景、所选背景、
6 张免费样图、异步正式任务、manifest、私有图片读取和导出 ZIP 都走真实
应用代码：

```bash
python3 scripts/menu_e2e_acceptance.py \
  --mode deterministic \
  --menu "/Users/guiguixiaxia/Documents/menus/运营数据_蔬适圈·中式轻食健康餐（万达店）.xlsx" \
  --platform meituan \
  --report scripts/reports/menu-e2e-deterministic-real-menu.json
```

脚本逐阶段输出 `PASS` / `FAIL` / `SKIP` / `BLOCKED`，报告包含解析行数和
菜名、6 张背景 SHA-256、样图及正式图的所选背景一致性、生成数量、
manifest 资产校验和导出 ZIP 校验。`deterministic` 结果始终写入
`productionProviderVerified=false`，不能作为真实混元或生产部署通过证据。
省略 `--menu` 时，脚本会读取 `--menu-dir`（默认
`~/Documents/menus`）中按文件名排序的第一份 Excel。

真实 provider 模式会产生付费调用，必须同时满足精确确认、保守调用预算、
TokenHub、腾讯云 Mask 和 COS 预检。每次运行使用新的隔离目录，不把旧缓存
冒充本次 provider 证据；provider 操作预算在每个背景、前景和 Mask 调用
边界强制执行。预算按 `6 + 2 * 6 + 2 * 菜单行数` 计算，故意不扣除缓存
（TokenHub 异步状态轮询不计为新的图片生成操作）：

```bash
export WAIMAI_E2E_REAL_PROVIDER_CONFIRM=I_ACCEPT_REAL_PROVIDER_CHARGES
export WAIMAI_E2E_REAL_PROVIDER_MAX_CALLS=<不小于报告预估值的整数>

python3 scripts/menu_e2e_acceptance.py \
  --mode real \
  --menu "/absolute/path/to/menu.xlsx" \
  --report /absolute/path/to/real-provider-report.json
```

未满足任一条件时，`real` 模式在导入应用和调用 provider 前返回退出码 2，
`preflight=BLOCKED`、后续阶段为 `SKIP`，并记录 `providerCalls=0`。报告只
记录凭据是否配置，不记录 API Key、SecretId 或 SecretKey。

内部参考图索引扫描：

```bash
PYTHONPATH=.codex_deps:. python3 -m library_index --no-thumbs --output data/library_index/library_index.jsonl
```

从 Mac 的 `cleanpic` 生成线上种子参考图：

```bash
PYTHONPATH=.codex_deps:. python3 scripts/import_seed_library.py --limit 360 --max-side 900 --quality 82
```

接口和导出链路检查：

```bash
PYTHONPATH=.codex_deps python3 - <<'PY'
from app import app, EXPORT_DIR
from zipfile import ZipFile
from PIL import Image
import io

c = app.test_client()
plan = c.get("/api/plan").get_json()
style = plan["styles"][0]["id"]
out = c.post("/api/export", json={
    "style": style,
    "scope": "selected",
    "selectedRows": [1],
    "format": "jpg",
    "platforms": ["meituan", "taobao", "jd"],
    "watermark": {
        "enabled": True,
        "type": "text",
        "text": "测试品牌",
        "position": "bottom-right",
        "pattern": "corner"
    }
}).get_json()
zip_path = EXPORT_DIR / out["download"].split("/download/", 1)[1]
with ZipFile(zip_path) as zf:
    print(out)
    print(zf.namelist()[:8])
PY
```

## 数据目录

```text
data/uploads/   上传菜单
data/library/   内部参考图、演示资产与 AI 资产库
data/library/seed_*/   可部署到 Render 的内部种子参考图
data/library_index/   本地参考图 JSONL 索引和缩略图
data/exports/   导出图片包
data/object_store/   本地对象存储占位
data/app.db   本地积分账本
```

这些目录已加入 `.gitignore`，不要把客户菜单、客户原图、未授权图片资产提交到 GitHub。

默认本地资料目录：

```text
/Users/guiguixiaxia/Documents/menus
/Users/guiguixiaxia/Documents/cleanpic
/Users/guiguixiaxia/Documents/watermarkpic
```

如需改成其他位置，可以设置：

```text
LIBRARY_SOURCE_DIRS=/path/to/cleanpic:/path/to/watermarkpic
```

## 当前验证结果

最近一次本地整合验证：

```text
node --check static/app.js 通过
node --check static/admin.js 通过
git diff --check 通过
定向产品化整合测试 97 passed
全量 pytest 回归 254 passed
HTTP 冒烟：前台、后台、dashboard、admin lists 和 AI 资产非法状态校验通过
```

线上检查：

```bash
curl https://waimai-image-tool.onrender.com/api/tencent-status
```

`configured=true` 代表 Render 已读到腾讯云密钥；`cosReady=true` 代表已能把临时商品图上传到腾讯 COS，商品背景生成会更稳定。

## 上线前外部验收

产品代码路径已经实现并通过本地及一次性 PostgreSQL/Redis 验收。正式对外提供服务前仍需完成这些依赖真实账号或运营流程的验证：

- 手机短信送达、设备/CAPTCHA 风控供应商和真实攻击样本验证。
- 支付宝真实商户、异步回调公网、退款/日账单、税务/KYC 和真实代理打款验证；微信支付未启用时保持 fail-closed。
- 腾讯 COS 私有桶 IAM、Bucket Policy、网络和 Render 重启恢复验证。
- 付费混元/Gemini 的额度、延迟和 40 品类人工视觉验收。
- PostgreSQL 迁移审批、备份/恢复、生产规模并发和 Redis Worker 长稳测试。
- 客服、财务、风控和 AI 资产审核的人工 SOP；代理合同与营销口径需中国执业律师审核。
- 上线前用美团、饿了么/淘宝外卖和京东商家后台最新规则复核导出规格。

缺少这些配置时 live readiness 会失败并阻止付费调用或本地数据回退。当前任务没有部署、创建 Render 资源或执行外部数据库迁移。

完整产品化路线图、代理规则、邀请返积分、防盗图机制和 worker/sub-agent 拆分保存在：

```text
PRODUCTIZATION_PLAN.md
MODULE_STATUS.md
```

## AI 资产库计划

后续高质量版本的主链路不再依赖内部参考图识别，而是把混元生成结果沉淀为可复用 AI 资产库：

1. 生成入口统一：不同品类的 6 张背景图、6 张免费样图、正式产品图默认走混元文生图；内部参考图只做参考或人工兜底，不作为默认生成来源。
2. 资产沉淀：混元生成成功后，把品类背景图、免费样图和正式产品图复制到 `data/library/_ai_asset_library/`；生产环境可迁移到 COS/OSS/R2 的 `ai-assets/` 对象前缀。
3. 标签入库：每张图写入 manifest，字段包括 `kind`、`category`、`productName`、`normalizedProductName`、`matchNames`、`keywords`、`styleId`、`quality`、`provider`、`modelAction`、`sourceMenuKey`、尺寸、sha256 和存储位置。
4. 存储策略：本地开发默认写入服务器目录；生产环境如果 COS 配置完整，会额外上传到 `ai-assets/` 对象前缀，manifest 中保留 COS key 和本地副本路径。
5. 未来复用：后续匹配优先查 AI 产品资产库，按 `category + normalizedProductName + matchNames + keywords` 命中可复用图；命中不足、低置信度或审核失败时再调用混元生成，并继续沉淀新资产。
6. 质量控制：只有 `provider=tencent-hunyuan` 且生成成功的图片进入资产库；本地兜底图、失败图、等待模型配置的占位状态不会入库。

当前代码已提供只读接口：

```bash
curl http://127.0.0.1:8790/api/ai-asset-plan
```

本地资产库路径：

```text
data/library/_ai_asset_library/
data/library/_ai_asset_library/manifest.jsonl
```

## 腾讯云生图配置

当前版本支持两套腾讯云图像接口：

- TokenHub `HY-Image-3.0` / `HY-Image-Lite`：用于背景风格图、无图库命中时的文生图，优先消耗 TokenHub 图像额度。
- 旧版 `TextToImageLite` / `ReplaceBackground`：作为 fallback，并继续用于 COS、商品背景旧接口等路径。

Render 环境变量：

```text
TENCENT_HUNYUAN_ENABLED=true
TENCENT_TOKENHUB_API_KEY=你的 TokenHub API Key
TENCENT_TOKENHUB_IMAGE_MODEL=hy-image-v3.0
TENCENT_TOKENHUB_POLL_TIMEOUT=120
TENCENTCLOUD_SECRET_ID=你的 SecretId
TENCENTCLOUD_SECRET_KEY=你的 SecretKey
TENCENTCLOUD_REGION=ap-guangzhou
PUBLIC_BASE_URL=https://waimai-image-tool.onrender.com
TENCENT_HUNYUAN_MODE=auto
TENCENT_HUNYUAN_SYNC_LIMIT=6
TENCENT_COS_BUCKET=waimai-image-tool-inputs-1311836560
TENCENT_COS_REGION=ap-guangzhou
TENCENT_COS_PREFIX=waimai-model-inputs
TENCENT_COS_AI_ASSET_PREFIX=ai-assets
AI_ASSET_UPLOAD_TO_COS=true
PAYMENT_PROVIDER=alipay
PAYMENT_NOTIFY_URL=https://waimai-image-tool.onrender.com/api/payments/alipay/notify
PAYMENT_RETURN_URL=https://waimai-image-tool.onrender.com/
ALIPAY_APP_ID=你的支付宝应用 APP ID
ALIPAY_PRIVATE_KEY=你的支付宝应用私钥
ALIPAY_PUBLIC_KEY=支付宝公钥
```

说明：

- `TENCENT_TOKENHUB_API_KEY` 是新 TokenHub 平台的 API Key，和腾讯云访问管理里的 `SecretId/SecretKey` 不是同一个东西。购买 `HY-Image-3.0` 额度后，必须创建并配置这个 Key，Render 才能消耗对应额度。
- `TENCENT_TOKENHUB_IMAGE_MODEL=hy-image-v3.0` 会使用异步 submit/query 生成，质量优先；如果要更快预览，可以改成 `hy-image-lite`，但需要确认该模型有可用额度或后付费。
- 商品背景生成要求腾讯云能下载 `ProductUrl`。Render 域名在腾讯云侧可能下载失败，所以正式联调建议配置腾讯 COS。
- 当前腾讯 COS 临时图桶是 `waimai-image-tool-inputs-1311836560`，地域是 `ap-guangzhou`。
- `TENCENT_COS_BUCKET` 需要使用完整 bucket 名，例如 `waimai-image-tool-125xxxxxxx`。
- 如果 COS bucket 是私有读，程序会上传临时 JPG 后生成 1 小时有效的签名 URL 给商品背景接口使用。

- `PUBLIC_BASE_URL` 用于把内部参考图或生成图地址拼成腾讯云可下载的公网 URL。
- `TENCENT_HUNYUAN_MODE=auto` 会优先尝试商品背景生成，条件不满足时走文生图。
- `TENCENT_HUNYUAN_SYNC_LIMIT` 是同步请求内最多真实调用腾讯云的图片数，默认 6。正式商用时不要在 Web 请求里一次同步生成 100 多张；正式图应继续走异步任务，并替换为跨进程 worker。
- `PAYMENT_PROVIDER=alipay` 会启用支付宝电脑网站支付下单链接；`ALIPAY_PRIVATE_KEY` 用于服务端生成 RSA2 签名，`ALIPAY_PUBLIC_KEY` 用于验签支付宝异步通知。
- 本地未配置腾讯云密钥时，系统会自动使用本地演示图兜底，保证上传、预览、导出流程不断。

检查环境变量是否生效：

```bash
curl https://waimai-image-tool.onrender.com/api/tencent-status
```

返回里的 `configured` 为 `true` 才代表 Render 已读取到密钥。
返回里的 `tokenhubReady` 为 `true` 才代表 Render 可以调用 TokenHub `HY-Image-3.0`。

## 2026-06-20 交付说明

本轮把产品主流程改成更接近真实交付版本：

- 上传菜单、风格预览、正式出图、充值、单张保存、打包导出都会显示运行中提示，避免用户误以为页面卡死。
- 整店风格改为 6 张背景图，两行三列展示，并统一命名为「一号背景」到「六号背景」。
- 免费样图预览单独成区，不再和背景风格混在一起。
- 添加品牌水印预览改成真实图片比例画布，文字水印和 PNG Logo 直接叠在图上，不再额外套圆形底。
- 正式出图接入腾讯云混元：有内部参考原图时走换背景，没有内部参考原图时走文生图；腾讯云已配置时不会再用本地假图冒充成功。
- 风格背景和免费样图已改为真实生成链路：腾讯云已配置时，背景图和免费样图都会调用混元并缓存；菜品匹配增加严格语义过滤，不再用不相关参考图硬凑。
- 导出接口会过滤未真实生成完成的图片；未完成、模型失败、待正式生成的图片不会混进 ZIP。
- Render 可用种子参考图已重新导入，当前内置 6 套风格、约 419 张可复用图。

本地启动：

```bash
cd /Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy
PYTHONPATH=.codex_deps:. python3 app.py
```

本地访问：

```text
http://127.0.0.1:8765
```

Render 部署需要的核心环境变量：

```text
TENCENT_HUNYUAN_ENABLED=true
TENCENT_TOKENHUB_API_KEY=你的 TokenHub API Key
TENCENT_TOKENHUB_IMAGE_MODEL=hy-image-v3.0
TENCENTCLOUD_SECRET_ID=你的 SecretId
TENCENTCLOUD_SECRET_KEY=你的 SecretKey
TENCENTCLOUD_REGION=ap-guangzhou
PUBLIC_BASE_URL=https://waimai-image-tool.onrender.com
TENCENT_HUNYUAN_MODE=auto
TENCENT_HUNYUAN_SYNC_LIMIT=6
TENCENT_COS_BUCKET=waimai-image-tool-inputs-1311836560
TENCENT_COS_REGION=ap-guangzhou
TENCENT_COS_PREFIX=waimai-model-inputs
ALLOW_LOCAL_IMAGE_FALLBACK=false
```

线上自检：

```bash
curl https://waimai-image-tool.onrender.com/api/tencent-status
curl https://waimai-image-tool.onrender.com/api/library-status
```

当前仍然保留的限制：

- `TENCENT_HUNYUAN_SYNC_LIMIT=6` 只约束网页中的六张风格/样图小批量请求；整店正式图和精修改图走 PostgreSQL outbox、Redis Worker 和独立 reconciler。
- 真实短信、支付商户、COS IAM、付费图像供应商及生产迁移仍需外部授权和验收；缺失时 live readiness 会 fail closed。
- 如果腾讯云额度、权限或接口报错，前端会显示「模型生成失败」或「待正式生成」，不会用占位图假装成功。
