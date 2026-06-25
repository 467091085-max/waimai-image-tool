# 外卖菜品图一键生成工具

这是一个“历史图库检索 + 少量 AI 二次加工”的外卖菜品图 MVP。当前版本可以上传 Excel 菜单，展示图库风格，选择风格后生成整店正式图预览，并按平台尺寸导出图片包。

## 已实现功能

- 菜单上传：支持 `.xls` 和 `.xlsx`，提供标准菜单模板下载。
- 菜单解析审计：支持多 Sheet、空行表头、运营数据导出、成本/活动噪声表过滤。
- 真实图库读取：本地自动扫描 `cleanpic` / `watermarkpic`，clean 图优先匹配，水印图只做参考。
- 线上真实种子图库：仓库内已导入一批由 `cleanpic` 压缩生成的无水印真实菜品图，Render 不依赖 Mac 本地目录也能展示真实图片。
- 图库索引：可生成 JSONL 图片资产索引和缩略图，包含店铺、菜品名、尺寸、来源、标签、sha1。
- 菜品拆分：自动统计单品、套餐、小吃/其他图片数量。
- 风格预览：展示 6 套图库风格，真实图库优先；选择风格后展示 6 张免费单品样图。
- 出图质量：支持普通出图和精修出图两档，普通出图 10 积分/张，精修出图 20 积分/张。
- 积分计费：SQLite 本地账本，支持套餐充值、自定义充值、生成扣费、失败退款、幂等订单。
- 品牌水印：支持文字水印和透明 PNG Logo，支持角标和平铺。
- 正式图预览：按单品图片、套餐图片、其他图片分组显示。
- 重做额度：每单提供免费换版额度，用完后按当前出图质量价格扣积分。
- 自定义修改：15 积分/张，前端按钮和弹窗统一读取后端价格。
- 导出：当前统一导出 JPG，支持勾选、全选、单品、套餐导出，并按平台上限压缩文件大小。
- 平台尺寸：支持美团、淘宝外卖/饿了么、京东外卖/京东秒送导出，不裁掉主体，按目标尺寸留边适配。
- 内部后台：`/admin` 可查看图库来源统计、样例图片、菜单解析审计。
- 存储底座：预留 SQLite 表结构和本地对象存储接口，后续可迁到 PostgreSQL + COS/OSS/R2。

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
```

真实菜单解析审计：

```bash
PYTHONPATH=.codex_deps:. python3 -m menu_parser /Users/guiguixiaxia/Documents/menus
```

真实图库索引扫描：

```bash
PYTHONPATH=.codex_deps:. python3 -m library_index --no-thumbs --output data/library_index/library_index.jsonl
```

扫描输出会直接包含 `total`、`clean`、`watermark`、`reusable`、`referenceOnly`、`sha1Deduped` 和 `sha1Duplicates`。其中 `cleanpic` 默认可复用，`watermarkpic` 默认 `has_brand_watermark=true`、`reference_only=true`、`reusable=false`，不会进入直接复用候选；菜品名文字水印只记录 `has_dish_text_watermark` 并降权，饮料/小料/主食/泛图/低质图会写入 `tags`、`style_weight`、`match_weight`，避免成为风格卡或匹配首选。

真实图库同步到腾讯 COS（默认 dry-run，不会上传）：

先复制本地密钥模板，填入腾讯云 COS 密钥。`.env.cos` 已被 `.gitignore` 忽略，不会提交到 GitHub：

```bash
cp .env.cos.example .env.cos
open .env.cos
```

```bash
PYTHONPATH=.codex_deps:. python3 scripts/sync_gallery_to_cos.py \
  --clean-dir /Users/guiguixiaxia/Documents/cleanpic \
  --watermark-dir /Users/guiguixiaxia/Documents/watermarkpic \
  --bucket waimai-image-tool-inputs-1311836560 \
  --region ap-guangzhou \
  --prefix waimai-gallery \
  --output data/library_index/cos_library_index.jsonl
```

dry-run 会扫描 `cleanpic`/`watermarkpic`、转 RGB JPG、按 `--max-side` 和 `--quality` 生成完整本地 JSONL 与 `*.summary.json`，但不会上传对象。脚本会自动读取 `.env.cos`，也可用 `--env-file /path/to/.env.cos` 指定；`TENCENT_COS_PREFIX` 可覆盖默认 `waimai-gallery` 前缀。远程 JSONL 每行至少包含 `canonical`、`canonical_norm`、`dish`、`name`、`category`、`style`、`background`、`match_family`、`match_kind`、`match_category`、`source`、`local_path`、`cos_bucket`、`cos_region`、`cos_key`/`object_key`、`url`/`public_url`/`remote_url`、`reusable`、`reference_only`、`watermark`/`watermark_state`/`watermark_status`。图片对象 key 固定为 `waimai-gallery/clean/<store>/<sha1>.jpg` 和 `waimai-gallery/watermark/<store>/<sha1>.jpg`；线上索引 key 固定为 `waimai-gallery/index/library_index.jsonl`。

dry-run 里的 `url/public_url/remote_url` 是“计划中的 COS 地址”，用于人工核对 key 和 Render 配置，不代表对象已经存在；每条记录会写 `upload_state=dry_run_planned`、`uploaded=false`，summary 会写 `uploadStatus=dry_run`、`remoteReady=false`、`indexUploaded=false`。缺少 bucket 时仍可 dry-run 扫描并生成 `cos_key`，但 URL 字段会为空且 summary 会给出 warning。

summary 会输出 `totalImages`、`reusableImages`、`watermarkedReferenceImages`、`uploadedSuccess`、`skippedImages`、`errorImages`，并在 `sync` 下保留同名明细。`renderEnv.COS_LIBRARY_INDEX_URL` 是上传成功后要填到 Render 环境变量里的线上索引地址。缺少腾讯云 bucket、region 或 Secret 时，`--no-dry-run` 会返回清晰 JSON 错误，明确说明没有上传任何 COS 对象，不会打印密钥或 Python traceback。live 上传时，脚本只有在全部图片上传成功后才上传 `index/library_index.jsonl`；如果任一图片失败，会写本地 summary、返回失败状态，并跳过远程索引发布，避免线上读取半截图库。

确认 summary 后，显式加 `--no-dry-run` 才会上传图片和索引。首次 live check 请限制 3 张以内：

```bash
PYTHONPATH=.codex_deps:. python3 scripts/sync_gallery_to_cos.py \
  --prefix waimai-gallery \
  --limit 3 \
  --no-dry-run
```

小批量成功后，确认 summary 里 `uploadStatus=uploaded`、`remoteReady=true`、`indexUploaded=true`，再把 `renderEnv.COS_LIBRARY_INDEX_URL` 填到 Render，然后重启服务或等待自动部署；线上 `/api/library-status` 应显示 `remoteIndex=true` 且 `remoteImages/indexImages` 大于 0。正式全量上传前先检查 `*.summary.json`，脚本不会打印 SecretId/SecretKey。COS 桶如果是私有读，线上读取索引后还需要按业务侧策略生成签名 URL 或通过后端代理读取。

从 Mac 的 `cleanpic` 生成线上真实种子图库：

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
data/library/   图库与演示图库
data/library/seed_*/   可部署到 Render 的真实无水印种子图库
data/library_index/   本地图库 JSONL 索引和缩略图
data/exports/   导出图片包
data/object_store/   本地对象存储占位
data/app.db   本地积分账本
```

这些目录已加入 `.gitignore`，不要把真实客户菜单和真实图库提交到 GitHub。

默认本地真实资料目录：

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

最近一次本地验证：

```text
56 个模块单测通过
匹配引擎单测通过
24 个真实菜单解析通过，共 3036 个菜品
真实图库扫描 2316 张：clean 842，watermark 1474，可复用 842，仅参考 1474，sha1 去重后 2203，重复文件 113
Render 可用真实种子图库 359 张
线上端到端烟测通过：真实图库风格、腾讯 ReplaceBackground 免费样图、正式生图 job、两平台 JPG ZIP 导出、下载 ZIP
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
- 对象存储：真实图库不要放 Render 硬盘。当前商品背景生成已支持腾讯 COS 临时图，生产图库也建议迁到腾讯 COS、阿里 OSS 或 Cloudflare R2。
- 数据库：当前是 SQLite，商用后菜单、订单、积分流水、导出记录需要迁到 PostgreSQL。
- 图库清洗后台：自动识别品牌水印、菜品名水印、可复用图、需抠图图。
- AI 接口：普通出图已接腾讯云混元；精修出图还需要后续接 Gemini/OpenAI 或其他高质量编辑模型。
- 异步任务队列：正式批量出图应由 Worker 后台处理，前端轮询进度。
- 平台尺寸复核：上线前用美团/淘宝/京东商家后台最新规则再确认一次。

## 腾讯云生图配置

当前版本已经接入腾讯云混元生图 API：

- `SubmitTextToImageJob` + `QueryTextToImageJob`：混元生图 3.0，用于没有可复用图库图时的正式文生图。
- `TextToImageLite`：只作为 3.0 失败时的临时降级兜底。
- `ReplaceBackground`：用于已有菜品图可公网访问时，按所选风格替换背景。

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
```

说明：

- 商品背景生成要求腾讯云能下载 `ProductUrl`。Render 域名在腾讯云侧可能下载失败，所以正式联调建议配置腾讯 COS。
- 当前腾讯 COS 临时图桶是 `waimai-image-tool-inputs-1311836560`，地域是 `ap-guangzhou`。
- `TENCENT_COS_BUCKET` 需要使用完整 bucket 名，例如 `waimai-image-tool-125xxxxxxx`。
- 如果 COS bucket 是私有读，程序会上传临时 JPG 后生成 1 小时有效的签名 URL 给商品背景接口使用。

- `PUBLIC_BASE_URL` 用于把图库图片地址拼成腾讯云可下载的公网 URL。
- `TENCENT_HUNYUAN_MODE=auto` 会优先尝试商品背景生成，条件不满足时走混元生图 3.0 文生图。
- `TENCENT_HUNYUAN_SYNC_LIMIT` 是同步请求内最多真实调用腾讯云的图片数，默认 6。正式商用时不要在 Web 请求里一次生成 100 多张，应改成后台任务队列。
- `TENCENT_IMAGE3_ENABLED=true` 表示缺图文生图优先使用混元生图 3.0。
- `TENCENT_IMAGE3_FALLBACK_TO_LITE=true` 表示 3.0 失败时可临时降级到极速版，避免整单中断；正式商用可改成 `false`，让失败直接暴露并重试。
- 本地未配置腾讯云密钥时，系统会自动使用本地演示图兜底，保证上传、预览、导出流程不断。

检查环境变量是否生效：

```bash
curl https://waimai-image-tool-1.onrender.com/api/tencent-status
```

返回里的 `configured` 为 `true` 才代表 Render 已读取到密钥。

## 2026-06-20 交付说明

本轮把产品主流程改成更接近真实交付版本：

- 上传菜单、风格预览、正式出图、充值、单张保存、打包导出都会显示运行中提示，避免用户误以为页面卡死。
- 整店风格改为 6 张背景图，两行三列展示，并统一命名为「一号背景」到「六号背景」。
- 免费样图预览单独成区，不再和背景风格混在一起。
- 添加品牌水印预览改成真实图片比例画布，文字水印和 PNG Logo 直接叠在图上，不再额外套圆形底。
- 正式出图接入腾讯云混元：有图库原图时走换背景，没有图库原图时走文生图；腾讯云已配置时不会再用本地假图冒充成功。
- 风格背景和免费样图已改为真实生成链路：腾讯云已配置时，背景图和免费样图都会调用混元并缓存；菜品匹配增加严格语义过滤，不再用不相关图库图硬凑。
- 导出接口会过滤未真实生成完成的图片；未完成、模型失败、待正式生成的图片不会混进 ZIP。
- Render 可用种子图库已重新导入，当前内置 6 套风格、约 419 张可复用图。

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

- `TENCENT_HUNYUAN_SYNC_LIMIT=6` 表示一次网页请求最多同步真实生成 6 张，适合演示和小批量验证。正式卖给客户前，批量 100 张以上应改成后台队列异步生成。
- 现在还没有真实登录、微信/支付宝支付、订单系统和对象存储；这些是商业化版本下一阶段要补的。
- 如果腾讯云额度、权限或接口报错，前端会显示「模型生成失败」或「待正式生成」，不会再假装已经生成成功。
