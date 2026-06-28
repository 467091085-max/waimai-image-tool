# 外卖菜品图一键生成工具

这是一个“AI 生成 + 资产沉淀复用”的外卖菜品图 MVP。当前版本可以上传 Excel 菜单，展示风格和样图，选择风格后生成整店正式图预览，并按平台尺寸导出图片包。

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
- 积分计费：SQLite 本地账本，支持套餐充值、自定义充值、生成扣费、失败退款、幂等订单；fake 支付只在本地 demo 或显式配置时可用；支付宝电脑网站支付已有本地 MVP 下单和异步通知验签入账。
- 代理/邀请 MVP：支持一级代理档案、直接客户绑定、邀请注册积分、首充积分返利，并在 fake 支付回调成功后生成代理待结算佣金；退款后可重算/取消未结算佣金并追回首充返利积分；后台可释放 T+7 佣金并创建/标记结算批次。
- 品牌水印：支持文字水印和透明 PNG Logo，支持角标和平铺。
- 正式图预览：按单品图片、套餐图片、其他图片分组显示。
- 正式出图异步任务：前端通过 `/api/generation-jobs` 创建和轮询任务，展示排队、生成、超时、失败和完成状态；队列满返回 429，队列不可用返回 503；底层队列提供只读 `snapshot()`，可统计各状态数量、worker/limit、最老排队/运行时长、stale/timeout 和 closed 状态。
- 重做额度：每单提供免费换版额度，用完后按 10 积分/张。
- 导出：当前统一导出 JPG，支持勾选、全选、单品、套餐导出，并按平台上限压缩文件大小。
- 平台尺寸：支持美团、淘宝外卖/饿了么、京东外卖/京东秒送导出，不裁掉主体，按目标尺寸留边适配。
- 内部后台：`/admin` 可查看运营概览，并已接入生成任务、资产访问、佣金结算、订单等 lists 明细。
- AI 资产审核 API：后台可通过状态接口 approve/reject/disable AI 资产，保存审核备注并写后台审计；完整人工审核台仍未生产化。
- 存储底座：预留 SQLite 表结构和本地对象存储接口，并提供对象存储生产 readiness 评估；生产或关闭本地 demo 时会要求私有远程 provider 和签名 secret，真实 COS/OSS/R2 SDK 仍待接入。
- 资产访问审计：导出下载和对象读取的允许/拒绝会写入本地 `asset_access_logs`，后台可汇总并按 `status=denied/allowed` 等条件查看异常访问。
- 一次性下载保护：签名 token 可生成哈希化消费键，下载守卫可拒绝已消费 token；生产仍需接 Redis/DB 原子消费记录。
- 契约测试保护：测试已锁定前台不回退到“真实图库/免费样图预览”等旧口径，正式出图必须走异步任务，后台必须保留 lists 明细接入。

## 产品化口径

- 图库策略：新生成的品类背景图、免费样图、正式菜品图都沉淀到服务器目录或生产对象存储；AI asset manifest 打标签，未来按品类、菜名、关键词、风格和质量复用。
- 前台口径：不宣传“真实图库”，只表达 AI 生成、样图预览、历史生成资产复用；内部参考图只用于匹配、兜底、审核和资产沉淀。
- 代理/邀请：默认只做一级直推；代理统一按直接订单实付净额 20% 返佣；C 端注册邀请人 50 积分、被邀请人 50 积分，仅直接邀请首充返 10% 积分，不返现金、不提现。
- 当前代理/邀请只完成本地 MVP 闭环；提现、实名/主体认证、真实打款、已打款后的财务追索、月度自动结算和完整后台操作仍未生产化。
- 短信登录：本地 demo 可继续返回 `mockCode`；生产环境必须关闭本地 demo 并配置 `SMS_PROVIDER=webhook`、`SMS_WEBHOOK_URL`，否则不会静默生成验证码。
- 支付：本地 demo 可继续使用 fake pay；生产关闭 `ENABLE_LOCAL_DEMO_BILLING` 后，必须配置真实支付。当前支付宝电脑网站支付支持 RSA2 签名下单和异步通知验签入账；微信支付仍未接入 adapter，会 fail-closed。
- 对象存储：本地 demo 可继续使用 local/mock 存储；生产或设置 `ENABLE_LOCAL_DEMO_STORAGE=false` 时，local/mock 会被 readiness 标记为 not production-ready，必须配置私有远程 provider、bucket 和 `OBJECT_SIGNING_SECRET`。
- 合规边界：多级分销暂不启用，必须法务确认后另开方案。
- 产品模块：数据库、对象存储、积分/支付、账号/门店、生成队列、AI 图库沉淀、防盗图/签名 URL、防刷风控、代理/邀请、管理后台、审计、运营指标。
- 当前审计只完成本地落库和后台汇总；生产日志聚合、告警、长期留存和一次性 token 原子消费存储仍未完成。
- 当前后台只完成本地 dashboard、lists 明细和局部操作 API；完整 CRUD、权限分级、人工审核工作台仍未完成。
- 当前状态：仍是本地 MVP/产品化骨架，不要表述为已上线生产；详细状态见 `MODULE_STATUS.md`。

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

Render 使用本仓库里的 `render.yaml` / `Procfile` 部署。

推荐配置：

- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 180`
- Python: Render 自动识别 Python 3

部署流程：

1. 推送代码到 GitHub。
2. Render 绑定该 GitHub 仓库。
3. 创建 Web Service。
4. 选择 `main` 分支。
5. 等待自动部署完成。

当前线上地址：

```text
https://waimai-image-tool-1.onrender.com
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
curl https://waimai-image-tool-1.onrender.com/api/tencent-status
```

`configured=true` 代表 Render 已读到腾讯云密钥；`cosReady=true` 代表已能把临时商品图上传到腾讯 COS，商品背景生成会更稳定。

## 生产化遗留问题

当前版本还是 MVP，已经能跑通主流程，但要正式卖给客户，还需要继续补：

- 登录系统：目前使用默认 demo 用户，需要接手机号/微信登录。
- 支付系统：现在是账本记账，还没有真实微信/支付宝收款回调。
- 对象存储：图片资产不要放 Render 硬盘。当前商品背景生成已支持腾讯 COS 临时图；生产图片资产建议迁到腾讯 COS、阿里 OSS 或 Cloudflare R2。`object_storage_service.assess_object_storage_readiness()` 会检查 local/mock、future remote provider、bucket、私有读和签名 secret 的生产 readiness，但不初始化真实 SDK。
- 数据库：当前是 SQLite，商用后菜单、订单、积分流水、导出记录需要迁到 PostgreSQL。
- 图片资产清洗后台：当前已有 AI 资产状态 API 和质量字段，仍缺自动识别品牌水印、菜品名水印、可复用图、需抠图图的完整人工审核工作台。
- AI 接口：普通出图已接腾讯云混元；精修出图还需要后续接 Gemini/OpenAI 或其他高质量编辑模型。
- 异步任务队列：当前已接入本地内存队列和 `/api/generation-jobs`，并补了队列满 429/队列不可用 503 与底层只读监控快照；正式商用仍应替换为 Redis/RQ/Celery 等跨进程 Worker，并补监控接口和告警。
- 平台尺寸复核：上线前用美团/淘宝/京东商家后台最新规则再确认一次。

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
PUBLIC_BASE_URL=https://waimai-image-tool-1.onrender.com
TENCENT_HUNYUAN_MODE=auto
TENCENT_HUNYUAN_SYNC_LIMIT=6
TENCENT_COS_BUCKET=waimai-image-tool-inputs-1311836560
TENCENT_COS_REGION=ap-guangzhou
TENCENT_COS_PREFIX=waimai-model-inputs
TENCENT_COS_AI_ASSET_PREFIX=ai-assets
AI_ASSET_UPLOAD_TO_COS=true
PAYMENT_PROVIDER=alipay
PAYMENT_NOTIFY_URL=https://waimai-image-tool-1.onrender.com/api/payments/alipay/notify
PAYMENT_RETURN_URL=https://waimai-image-tool-1.onrender.com/
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
curl https://waimai-image-tool-1.onrender.com/api/tencent-status
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
PUBLIC_BASE_URL=https://waimai-image-tool-1.onrender.com
TENCENT_HUNYUAN_MODE=auto
TENCENT_HUNYUAN_SYNC_LIMIT=6
TENCENT_COS_BUCKET=waimai-image-tool-inputs-1311836560
TENCENT_COS_REGION=ap-guangzhou
TENCENT_COS_PREFIX=waimai-model-inputs
ALLOW_LOCAL_IMAGE_FALLBACK=false
```

线上自检：

```bash
curl https://waimai-image-tool-1.onrender.com/api/tencent-status
curl https://waimai-image-tool-1.onrender.com/api/library-status
```

当前仍然保留的限制：

- `TENCENT_HUNYUAN_SYNC_LIMIT=6` 表示一次网页请求最多同步真实生成 6 张，适合风格和样图的小批量验证。正式图已走本地异步任务入口，但正式卖给客户前仍要替换为 Redis/RQ/Celery 等跨进程队列。
- 现在还没有真实短信/微信登录、微信支付、支付宝真实商户联调/退款补单、生产对象存储全链路、跨进程队列和完整运营后台；这些是商业化版本下一阶段要补的。
- 如果腾讯云额度、权限或接口报错，前端会显示「模型生成失败」或「待正式生成」，不会再假装已经生成成功。
