# 外卖菜品图一键生成工具产品建设计划

## 1. 产品定位

本产品不是纯 AI 批量生图工具，而是“历史图库检索 + 少量 AI 二次加工”的外卖菜品图工作台。

核心流程：

```text
上传菜单 -> 解析菜品 -> 匹配历史图库 -> 展示 6 套真实风格 -> 免费样图预览
-> 客户确认风格/质量/平台/水印 -> 扣积分正式出图 -> 单张换版/自定义修改
-> 按美团/淘宝外卖/京东外卖导出 JPG/ZIP
```

核心利润来源：

- 已有图库可复用时，不调用模型或只做低成本换背景。
- 只有缺图、跨风格、套餐组合、客户精修时才调用模型。
- 客户按图片张数和增值服务付费。

## 2. 当前资料和代码位置

本机真实资料目录：

```text
/Users/guiguixiaxia/Documents/menus
/Users/guiguixiaxia/Documents/cleanpic
/Users/guiguixiaxia/Documents/watermarkpic
```

当前主仓库：

```text
/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy
```

核心代码：

```text
app.py              Flask 接口、出图编排、腾讯云调用、计费接口
menu_parser.py      Excel 菜单解析
matching_engine.py  菜品标准化和图库匹配
library_index.py    图库扫描、索引、缩略图
image_pipeline.py   水印、平台尺寸、导出 ZIP
billing.py          积分账户、充值、扣费、退款
storage_db.py       SQLite/PostgreSQL-ready 数据结构和对象存储接口
templates/index.html
static/app.js
static/styles.css   客户端主流程 UI
admin_panel.py      内部后台
```

## 3. 模块规划

### 3.1 菜单解析模块

目标：

- 支持 `.xls`、`.xlsx`。
- 支持客户上传非标准菜单。
- 提供标准模板下载。
- 解析出菜品名、分类、价格、类型、套餐组件。

当前代码：

- `menu_parser.py`
- `app.py /upload-menu`
- `app.py /download-menu-template`

核心逻辑：

```text
读取 Excel 多 Sheet
-> 过滤成本表、活动表、统计表等噪声
-> 找菜单主表
-> 识别表头和数据起始行
-> 映射菜品名/分类/价格/规格/套餐内容列
-> 清洗重复项和空行
-> 判断单品/套餐/其他
-> 输出结构化 menu_items
```

输出结构：

```json
{
  "row": 12,
  "category": "热销",
  "name": "辣椒炒肉盖码饭",
  "price": "19.8",
  "kind": "单品",
  "components": [],
  "norm": "辣椒炒肉盖码饭"
}
```

验收标准：

- 真实 `menus` 目录至少 90% 文件可解析。
- 菜品数、单品数、套餐数和页面展示一致。
- 解析失败时返回明确原因，不出现空白页面。

### 3.2 图库资产模块

目标：

- 把 `cleanpic` 和 `watermarkpic` 变成可检索资产库。
- clean 图优先复用。
- watermark 图只做参考，不直接交付。
- 后续迁移到腾讯 COS。

当前代码：

- `library_index.py`
- `scripts/import_seed_library.py`
- `app.py library_images()`

核心数据字段：

```text
image_id
store_name
dish_name
canonical_dish_id
style_id
source=clean/watermark/seed/generated
has_brand_watermark
has_dish_text
reusable
quality_score
file_url
sha1
width
height
```

核心逻辑：

```text
扫描图片目录
-> 解析店铺名和菜品名
-> 标记 clean/watermark 来源
-> 生成 sha1 去重
-> 生成缩略图
-> 推断风格 id
-> 写入 JSONL 或数据库
-> 前端/后台按风格和菜品检索
```

验收标准：

- 后台能看到图库总数、clean 数、watermark 数、风格数。
- 线上 Render 不依赖 Mac 本地目录，也能展示真实种子图库。
- 风格卡不能展示假示意图。

### 3.3 水印识别和图库清洗模块

目标：

- 自动区分纯净图、菜品名文字图、品牌水印图、低质图。
- 品牌水印图不能直接交付。

第一版策略：

```text
来源为 watermarkpic -> has_brand_watermark=true
来源为 cleanpic -> reusable=true
文件名含提示/勿点/米饭/饮料等泛词 -> 降低展示优先级
```

第二版策略：

```text
OCR 检测文字区域
视觉模型检测 logo/品牌名
人工后台确认
```

验收标准：

- 有品牌水印的图不会被直接导出给客户。
- 后台可批量审核疑似水印图。

### 3.4 菜品标准化和匹配模块

目标：

- 让“辣椒炒肉盖码饭”“辣椒炒肉盖饭”“农家小炒肉饭”匹配到同一类菜。
- 避免饮料匹配炒菜、米饭匹配套餐等严重错误。

当前代码：

- `matching_engine.py`
- `app.py top_candidates()`
- `app.py strict_match_allowed()`

核心逻辑：

```text
菜品名 normalize
-> 去营销词、规格词、符号
-> 按品类做别名映射
-> 计算文本相似度
-> 语义类别过滤：food/drink/soup/generic
-> 按同菜品、同风格、clean 来源排序
-> 输出候选图和处理动作
```

处理动作：

```text
直接可用：同菜品 + 同风格 + 无水印
智能统一风格：同菜品 + 不同风格，需要换背景
需去水印/重绘：有参考图但不可直接复用
智能补图：图库无图，文生图
套餐组合生成：套餐或组合图
```

验收标准：

- 不允许出现明显错配，例如饮品匹配炒菜。
- 每个菜单项最多返回高置信候选，不用低置信图片硬凑。

### 3.5 风格包模块

目标：

- 展示 6 张真实风格图，两行三列。
- 命名为一号背景到六号背景。
- 选中风格后生成 6 张免费单品样图。

当前代码：

- `app.py style_options()`
- `app.py style_sample_candidate()`
- `app.py preview_samples()`
- `static/app.js renderStyles()`
- `static/app.js renderStylePreview()`

核心逻辑：

```text
按 style_id 聚合图库图
-> 挑选代表图
-> 排除 demo、背景、米饭、饮料、提示图
-> 返回 6 个风格卡
-> 用户选择风格
-> 从菜单中选 6 个单品
-> 调用换背景或文生图生成免费样图
```

验收标准：

- 6 张风格图必须来自真实图库或真实模型生成缓存。
- 免费样图和菜品名必须匹配。
- 样图生成失败时显示失败原因。

### 3.6 AI 出图模块

目标：

- 普通出图走腾讯混元。
- 精修出图预留 Gemini/OpenAI 高质量通道。
- 批量生成不能卡死 Web 请求。

当前代码：

- `app.py tencent_text_to_image()`
- `app.py tencent_replace_background()`
- `app.py materialize_final_images()`

普通出图：

```text
10 积分/张
优先 ReplaceBackground
无可用商品图时 TextToImageLite
```

精修出图：

```text
20 积分/张
后续接 Gemini/OpenAI 或其他高质量图片编辑模型
```

核心逻辑：

```text
如果同菜品同风格 clean 图存在 -> 直接复用
否则如果同菜品不同风格 clean 图存在 -> ReplaceBackground
否则如果有 watermark 参考图 -> 重绘或换背景
否则 -> TextToImageLite
套餐 -> 按 components 生成组合图
```

验收标准：

- 模型失败不能返回假图冒充成功。
- 前端能区分已生成、待生成、生成失败。
- 失败可退款或重试。

### 3.7 异步任务队列模块

目标：

- 把 100 张以上正式出图从同步 Web 请求迁到后台任务。
- 前端通过进度轮询展示生成状态。

新增代码建议：

```text
generation_jobs.py
worker.py
tests/test_generation_jobs.py
```

数据库表：

```text
generation_jobs
generation_job_items
generation_results
```

接口设计：

```text
POST /api/jobs              创建生成任务
POST /api/jobs/<id>/pay     扣积分并开始
GET  /api/jobs/<id>         查询任务状态
GET  /api/jobs/<id>/items   查询每张图状态
POST /api/jobs/<id>/retry   重试失败项
```

任务状态：

```text
created
paid
queued
running
partially_failed
completed
refunded
cancelled
```

验收标准：

- 130 张图不会让网页请求超时。
- 刷新页面后仍能恢复进度。
- 单张失败不影响其他图片继续生成。

### 3.8 水印、尺寸和导出模块

目标：

- 支持文字水印、透明 PNG logo。
- 按平台导出 JPG。
- ZIP 可下载。

当前代码：

- `image_pipeline.py`
- `app.py export_zip()`
- `static/app.js exportImages()`

平台规则：

```text
美团外卖：800x600，4:3，JPG/PNG，≤5MB
淘宝外卖/饿了么：800x800，1:1，JPG/PNG，≤20MB
京东外卖/京东秒送：800x800，1:1，JPG/PNG/JPEG，≤5MB
```

当前产品统一导出：

```text
JPG
RGB
主体居中留边
按平台大小压缩
```

验收标准：

- 导出后客户可直接上传平台，不需要二次压缩。
- 水印不被平台尺寸裁掉。
- 支持单张保存、勾选导出、全选、单品、套餐。

### 3.9 积分、充值、支付模块

目标：

- 以积分作为统一支付单位。
- 后续接微信/支付宝真实收款。

当前代码：

- `billing.py`
- `app.py /api/recharge`
- `app.py /api/debit`
- `app.py /api/refund`

积分规则：

```text
1 元 = 10 积分
普通出图 = 10 积分/张
精修出图 = 20 积分/张
自定义修改 = 10 积分/张
品牌水印 = 50 积分/单
多平台导出 = 每多一个平台 +100 积分/单
```

充值包：

```text
49 元 -> 490 积分 + 10 赠送
99 元 -> 990 积分 + 50 赠送
299 元 -> 2990 积分 + 200 赠送
自定义充值 -> 100 积分起充
```

验收标准：

- 所有积分变动都有流水。
- 扣费幂等，刷新不会重复扣。
- 生成失败可以按订单退款。

### 3.10 前端工作台模块

目标：

- 客户能清楚知道下一步做什么。
- 所有耗时环节有 loading 和进度。

当前代码：

- `templates/index.html`
- `static/app.js`
- `static/styles.css`

页面状态：

```text
等待上传
菜单解析中
风格生成中
等待选择风格
免费样图生成中
等待确认扣费
正式生成中
可预览/可导出
```

验收标准：

- 上传、风格、样图、正式出图、导出都有明确运行提示。
- 页面不出现“点击后没反应”的状态。

### 3.11 后台管理模块

目标：

- 管理图库、菜单解析、失败任务、积分订单。

当前代码：

- `admin_panel.py`
- `templates/admin.html`
- `static/admin.js`
- `static/admin.css`

功能规划：

```text
图库统计
样例图片
菜单解析审计
水印疑似图审核
菜品名修正
别名表维护
生成失败任务查看
订单和积分流水查看
```

验收标准：

- 不进代码也能处理大部分错误数据。

### 3.12 存储和数据库模块

目标：

- 从本地文件 + SQLite 迁移到可生产使用的数据库和对象存储。

当前代码：

- `storage_db.py`
- `billing.py`
- `data/object_store`

生产架构：

```text
应用服务：Flask/Gunicorn
数据库：PostgreSQL
对象存储：腾讯 COS
任务队列：Redis + Worker
图片模型：腾讯混元普通出图，Gemini/OpenAI 精修预留
```

核心表：

```text
users
point_accounts
point_ledger
orders
menus
menu_items
library_images
style_packs
dish_aliases
generation_jobs
generation_job_items
generation_results
exports
```

验收标准：

- Render 重启不丢订单和任务。
- 真实图库不放 Render 磁盘。
- 本地开发和生产部署可以用同一套 repository 接口。

## 4. Worktree 开发安排

主分支保持可部署：

```text
main
```

并行开发分支：

```text
feature/async-jobs
feature/storage-db
```

### feature/async-jobs

负责：

- 后台生成任务模型。
- 任务状态机。
- 前端轮询接口。
- 生成失败、重试、退款边界。

主要文件：

```text
generation_jobs.py
worker.py
app.py
tests/test_generation_jobs.py
static/app.js
```

### feature/storage-db

负责：

- 完善 SQLite/PostgreSQL-ready schema。
- 图库、菜单、生成任务、导出记录 repository。
- 对象存储抽象。
- COS 生产迁移边界。

主要文件：

```text
storage_db.py
library_index.py
app.py
tests/test_storage_db.py
tests/test_library_index.py
```

## 5. 合并顺序

```text
1. feature/storage-db
2. feature/async-jobs
3. 集成 app.py 接口
4. 集成前端进度轮询
5. 全量测试
6. 推送 main 部署 Render
```

原因：

- 异步任务依赖稳定的 job/item/result 表。
- 先确定数据结构，再接 Worker 和前端轮询，返工最少。

## 6. 测试要求

每个分支必须通过：

```bash
PYTHONPATH=.codex_deps:. python3 -m py_compile app.py admin_panel.py billing.py image_pipeline.py storage_db.py menu_parser.py matching_engine.py library_index.py
PYTHONPATH=.codex_deps:. python3 -m unittest discover -s tests -v
PYTHONPATH=.codex_deps:. python3 test_matching_engine.py
node --check static/app.js
node --check static/admin.js
git diff --check
```

核心端到端验收：

```text
上传真实菜单
-> 生成 6 张真实风格图
-> 选择风格
-> 生成 6 张免费样图
-> 选择质量/平台/水印
-> 扣积分
-> 后台任务生成正式图
-> 页面展示进度
-> 单品/套餐分组预览
-> ZIP 导出
```

## 7. 当前优先级

P0：

- 不再显示假图。
- 批量出图改成异步任务。
- 存储和数据库结构生产化。

P1：

- 后台图库管理。
- 水印自动识别。
- 支付回调。

P2：

- Gemini/OpenAI 精修通道。
- 33 个品类别名库。
- 裂变积分和代理体系。
