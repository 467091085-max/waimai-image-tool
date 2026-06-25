# Worktree Plan

主仓库只用于稳定演示、部署和合并后的最终版本：

```text
/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy
branch: main
current base: 1e64c37 Use Hunyuan image 3 for text generation
```

真实资料目录只在本机读取，不提交到 Git：

```text
/Users/guiguixiaxia/Documents/menus
/Users/guiguixiaxia/Documents/cleanpic
/Users/guiguixiaxia/Documents/watermarkpic
```

## V2 Worktrees

新一轮产品化开发统一放在：

```text
/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v2
```

| 子任务 | Branch | Worktree | 目标 |
|---|---|---|---|
| 图库资产层 | `feature/v2-asset-library` | `worktrees-v2/asset-library` | 把 cleanpic/watermarkpic 变成可检索、可迁移 COS 的真实图库资产 |
| 菜单解析和匹配 | `feature/v2-menu-matching` | `worktrees-v2/menu-matching` | 提升 Excel 解析、菜品标准化、严谨匹配，避免菜名和图片错配 |
| 风格预览 | `feature/v2-style-preview` | `worktrees-v2/style-preview` | 生成 6 套真实不同背景风格，并输出 6 张免费样图 |
| 生成引擎 | `feature/v2-generation-engine` | `worktrees-v2/generation-engine` | 接通混元生图 3.0 的真实文生图、换背景、套餐组合、失败重试 |
| 客户端 UI | `feature/v2-customer-ui` | `worktrees-v2/customer-ui` | 重做客户页面流程、loading、步骤跳转、视觉风格和可操作反馈 |
| 导出交付 | `feature/v2-export-delivery` | `worktrees-v2/export-delivery` | 按美团/淘宝外卖/京东尺寸导出 JPG/ZIP，并控制文件大小 |
| 积分和后台 | `feature/v2-billing-admin` | `worktrees-v2/billing-admin` | 完成积分、充值、扣费、退款、后台审核和运营配置 |

旧的 `worktrees/*` 是上一阶段分支，保留作参考，不再作为 v2 主开发入口。

## V3 Parallel Build

V3 是当前正式并行开发入口。所有 worktree 都从最新 `main` 分出，主仓库只负责集成、测试、部署。

```text
base commit: a5cbb84 Improve style preview selection and local export fallback
root: /Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v3
```

| 子任务 | Branch | Worktree | 写入边界 |
|---|---|---|---|
| 图库资产层 | `feature/v3-asset-library` | `worktrees-v3/asset-library` | `library_index.py`, `scripts/scan_library.py`, `scripts/import_seed_library.py`, `tests/test_library_index.py`, `README.md` 图库说明 |
| 菜单解析和匹配 | `feature/v3-menu-matching` | `worktrees-v3/menu-matching` | `menu_parser.py`, `matching_engine.py`, `tests/test_menu_parser.py`, `tests/test_strict_matching.py` |
| 风格预览 | `feature/v3-style-preview` | `worktrees-v3/style-preview` | 风格选择/样图相关逻辑和测试；优先写独立 helper，必要时小范围修改 `app.py` 的风格接口 |
| 生成引擎 | `feature/v3-generation-engine` | `worktrees-v3/generation-engine` | `generation_jobs.py`, 新增/拆分腾讯生图 helper, `tests/test_generation_jobs.py`, `tests/test_app_generation.py` |
| 客户端 UI | `feature/v3-customer-ui` | `worktrees-v3/customer-ui` | `templates/index.html`, `static/app.js`, `static/styles.css` |
| 导出交付 | `feature/v3-export-delivery` | `worktrees-v3/export-delivery` | `image_pipeline.py`, 导出接口相关测试, `tests/test_image_pipeline.py` |
| 积分和后台 | `feature/v3-billing-admin` | `worktrees-v3/billing-admin` | `billing.py`, `admin_panel.py`, `templates/admin.html`, `static/admin.js`, billing/admin 测试 |

集成策略：

1. 每个 worker 在自己的 worktree 内完成代码和测试，不直接推送。
2. worker 完成后提交本地 commit，并在结果里列出变更文件、测试命令、遗留问题。
3. 主线按“模块先行、`app.py` 最后”的顺序合并，解决冲突后统一跑全量测试。
4. 合并通过后推送 `main`，Render 自动部署，再做线上烟测。

## 子任务细分

### 1. 图库资产层

核心文件：

```text
library_index.py
scripts/scan_library.py
scripts/import_seed_library.py
storage_db.py
admin_panel.py
```

要做：

- 扫描 `cleanpic`、`watermarkpic`，生成图库索引。
- clean 图标记为可复用，watermark 图标记为参考图或不可直接交付。
- 记录菜品名、来源、尺寸、sha1、疑似水印、背景风格、缩略图。
- 生成可上传 COS 的对象 key，避免线上依赖 Mac 本地路径。
- 后台显示图库数量、clean 数、watermark 数、可复用数、风格数。

验收：

- 至少能稳定索引本机三类目录。
- 后台能看到真实图库，不再只靠假示意图。
- 有品牌水印的图不会被直接交付。

### 2. 菜单解析和匹配

核心文件：

```text
menu_parser.py
matching_engine.py
app.py
tests/test_menu_parser.py
tests/test_strict_matching.py
```

要做：

- 支持 `.xls`、`.xlsx`、非标准菜单。
- 提供标准菜单模板下载。
- 输出单品数量、套餐数量、小吃/饮品等分类数量、正式图片总张数、出图所需积分。
- 建立菜品标准化规则：去规格词、营销词、符号、平台词。
- 建立强约束匹配：饮料不能匹配炒菜，米饭不能匹配套餐，泛图不能匹配具体菜。
- 匹配失败时进入“智能补图”，不能乱配图。

验收：

- 真实 `menus` 样本中大多数菜单能解析出合理菜品数。
- 免费样图和正式图的菜名与图片不得出现明显错配。
- 匹配不到时明确走 AI 生成，而不是用错图凑数。

### 3. 风格预览

核心文件：

```text
app.py
matching_engine.py
library_index.py
static/app.js
templates/index.html
static/styles.css
```

要做：

- 从真实图库中抽取 6 套不同背景风格。
- 如果真实图库不足 6 个不同背景，用混元补足背景风格图。
- 风格命名只用“一号背景”到“六号背景”，不写不准确的风格描述。
- 风格卡只展示背景/整体风格，不混入正式样图。
- 选定风格后生成 6 张免费单品样图，单独区域展示，两行三列。

验收：

- 页面必须稳定显示 6 张风格图。
- 6 张背景不能四五张长得一样。
- 免费样图必须真实调用生成或真实换背景，不再是本地假图。

### 4. 生成引擎

核心文件：

```text
app.py
generation_jobs.py
image_pipeline.py
storage_db.py
tests/test_app_generation.py
tests/test_generation_jobs.py
```

要做：

- 普通出图：100 积分/张，走混元普通生成链路。
- 精修出图：200 积分/张，走更高质量链路，界面不暴露模型名称。
- 同菜同风格优先复用图库。
- 同菜不同风格走换背景。
- 有参考图但不可交付，走参考重绘或去水印重绘。
- 图库无图，走混元生图 3.0 文生图。
- 套餐图和单品图分开生成、分开展示。
- 加水印、平台尺寸、选定风格等必须进入正式生成参数。
- 每个长任务都有 job 状态、进度、错误原因、可重试。

验收：

- 生成正式图时能看到真实任务进度。
- 有图、换背景、无图补图三类场景都能跑通。
- 失败不会整批崩溃，失败项能单独重试。

### 5. 客户端 UI

核心文件：

```text
templates/index.html
static/app.js
static/styles.css
```

要做：

- 网站名：外卖菜品图一键生成工具。
- 上传菜单后自动解析，不再出现重复上传按钮。
- 每个运行阶段显示 loading、进度和当前动作。
- 步骤区不能挡住页面跳转位置。
- 上传菜单、选择风格、生成正式图、导出图片形成清晰主流程。
- 出图质量可以直接选择普通/精修，不依赖先点平台。
- 水印预览比例要跟当前平台图比例一致。
- 文案只面向客户，不暴露“复用图库、省钱逻辑、模型名称”。

验收：

- 客户不需要猜下一步点哪里。
- 每次点击后都有明确反馈。
- 手机和桌面打开都不明显错位。

### 6. 导出交付

核心文件：

```text
image_pipeline.py
app.py
static/app.js
tests/test_image_pipeline.py
```

平台规则：

```text
美团外卖: 800x600, 4:3, JPG/PNG, <=5MB
淘宝外卖/饿了么: 800x800, 1:1, JPG/PNG, <=20MB
京东外卖/京东秒送: 800x800, 1:1, JPG/PNG/JPEG, <=5MB
统一建议: 导出 RGB JPG
```

要做：

- 三个平台都可选，至少选一个。
- 任选一个平台不加价，第二个平台 +100 积分，第三个平台再 +100 积分。
- 导出格式默认 JPG。
- 单张下载、多选下载、单品下载、套餐下载、ZIP 打包下载。
- 水印位置要避开跨平台裁剪风险。
- ZIP 导出必须有 loading 和成功/失败提示。

验收：

- 打包导出按钮能返回真实 ZIP。
- JPG 尺寸、比例、RGB、大小符合平台要求。
- 多平台导出不裁掉菜品主体和水印。

### 7. 积分和后台

核心文件：

```text
billing.py
admin_panel.py
storage_db.py
app.py
templates/admin.html
static/admin.js
```

要做：

- 充值积分：自定义充值最低 100 积分。
- 49 元体验包：490 积分 + 赠送 10 积分。
- 99 元套餐：990 积分 + 赠送 50 积分。
- 299 元套餐：2990 积分 + 赠送 200 积分。
- 正式生成点击确认时扣积分。
- 生成失败按失败项退积分或不扣对应项。
- 单张换一版显示免费剩余次数和超出价格。
- 自定义修改 150 积分/张。
- 后台可查订单、积分流水、生成任务、失败原因。

验收：

- 扣费、退款、重复点击幂等。
- 页面显示积分，不显示人民币单价。
- 后台能追踪每一笔消耗对应哪个任务。

## 推荐合并顺序

第一批先解决“真图和真生成”：

1. `feature/v2-asset-library`
2. `feature/v2-menu-matching`
3. `feature/v2-generation-engine`
4. `feature/v2-style-preview`

第二批解决“客户能顺利用”：

5. `feature/v2-customer-ui`
6. `feature/v2-export-delivery`

第三批解决“能收费和运营”：

7. `feature/v2-billing-admin`

每个分支合并前必须至少通过：

```text
python3 -m py_compile app.py
python3 -m unittest discover -s tests -v
node --check static/app.js
node --check static/admin.js
git diff --check
```

## 当前优先级

最先开工顺序：

1. `worktrees-v2/asset-library`：让系统吃进真实图库。
2. `worktrees-v2/menu-matching`：先防止菜名和图片错配。
3. `worktrees-v2/generation-engine`：让混元 3.0 真正承担缺图/换背景。
4. `worktrees-v2/style-preview`：让 6 张风格图和免费样图变成真结果。

这四条完成后，产品才算从“演示页面”进入“能真实出图”的阶段。
