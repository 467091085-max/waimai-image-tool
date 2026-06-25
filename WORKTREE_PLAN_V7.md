# WORKTREE PLAN V7

目标：把“外卖菜品图一键生成工具”拆成可并行推进、可单独验收、可安全合并的模块。

主仓库：

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy`

v7 worktree 根目录：

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v7`

真实本地素材：

- 菜单模板：`/Users/guiguixiaxia/Documents/menus`
- 无品牌水印图库：`/Users/guiguixiaxia/Documents/cleanpic`
- 有品牌水印图库：`/Users/guiguixiaxia/Documents/watermarkpic`

## Subagent 1: Gallery COS Runtime

Worktree:

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v7/gallery-cos-runtime`

Branch:

`feature/v7-gallery-cos-runtime`

Owner files:

- `library_index.py`
- `app.py` only for gallery/upload/status endpoints
- `scripts/push_gallery_via_app.py`
- `tests/test_*gallery*`
- `README.md` gallery section

任务：

1. 确认 Render 上真实图库上传通道可用。
2. 上传 Mac 的 `cleanpic` 和 `watermarkpic` 到 COS。
3. 生成 `library_index.jsonl`，并让线上 `/api/library-status` 显示 remote gallery。
4. 禁止在图库未上传时伪装成真实图库。
5. 输出小批量和全量上传命令、成功数量、失败样本。

验收：

- `/api/library-status` 中 `remoteIndex=true`。
- `remoteImages > 0`。
- 风格候选不再只来自 internal seed。

## Subagent 2: Menu Matching Accuracy

Worktree:

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v7/menu-matching-accuracy`

Branch:

`feature/v7-menu-matching-accuracy`

Owner files:

- `menu_parser.py`
- `matching_engine.py`
- `test_matching_engine.py`
- `tests/test_*menu*`
- `tests/test_*matching*`

任务：

1. 支持 `.xls`、`.xlsx`、`.csv`，并兼容非模板菜单。
2. 菜品名提取失败时给出明确原因，不乱识别。
3. 严格避免错配：不能把饮品匹配成炸物，不能把菜匹配成米饭。
4. 识别单品、套餐、小吃/加料/规格类项目数量。
5. 套餐拆解只做结构识别，不强行生成不存在的单品。

验收：

- 用户菜单上传后显示：单品多少张、套餐多少张、小吃/其他多少张、正式图片总数、出图所需积分。
- 低置信匹配进入“需生成”，不进入“可复用”。

## Subagent 3: Style Samples Preview

Worktree:

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v7/style-samples-preview`

Branch:

`feature/v7-style-samples-preview`

Owner files:

- `app.py` style/sample endpoints only
- `generation_engine.py` style helper functions only
- `templates/index.html` style preview section only
- `static/*` style preview JS/CSS only
- `tests/test_*style*`

任务：

1. 整店风格必须展示 6 张不同背景。
2. 如果图库里不足 6 个不同背景，调用混元补足。
3. 背景名称只用“一号背景”到“六号背景”。
4. 免费样图预览生成 6 张单品图，不生成套餐样图。
5. 样图必须使用客户已选背景，不能只显示原图或假示意图。

验收：

- 6 张背景图视觉上不同。
- 6 张样图单独成区，一行 3 张，两行。
- 样图每张有菜品名，菜品名和图片匹配可信。

## Subagent 4: Hunyuan Generation Pipeline

Worktree:

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v7/hunyuan-generation-pipeline`

Branch:

`feature/v7-hunyuan-generation-pipeline`

Owner files:

- `generation_engine.py`
- `image_pipeline.py`
- `app.py` generation endpoints only
- `tests/test_*hunyuan*`
- `scripts/smoke_hunyuan_live.py`

任务：

1. 严格区分三种出图路径：
   - 同菜同风格：直接复用。
   - 同菜不同风格：混元换背景。
   - 图库无菜：混元按菜名生成。
2. 提示词模板固定并写入代码，按普通出图/精修出图区分。
3. 普通出图：10 积分/张。
4. 精修出图：20 积分/张。
5. 生成结果必须记录 provider、action、requestId、status、error。
6. 失败时不能返回假图冒充成功。

验收：

- 至少一次真实 Hunyuan live smoke 成功。
- 生成图片背景与选中背景一致或接近。
- 线上接口能返回真实生成图 URL。

## Subagent 5: Async Job Progress

Worktree:

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v7/async-job-progress`

Branch:

`feature/v7-async-job-progress`

Owner files:

- `generation_jobs.py`
- `app.py` job endpoints only
- `templates/index.html` progress area only
- `static/*` polling/progress code only
- `tests/test_*job*`

任务：

1. 上传菜单、生成风格、生成样图、正式出图、打包导出都要有 loading 状态。
2. 正式出图用异步 job，前端轮询进度。
3. 每一步显示当前状态和失败原因。
4. 页面自动滚动到当前步骤，但不被顶部进度条遮挡。
5. 防止重复点击导致重复扣积分或重复 job。

验收：

- 用户每次点击后都能看到“正在处理”。
- job 失败时页面能显示可理解错误。
- 同一个任务重复点击不会生成两个并发任务。

## Subagent 6: Customer UI Flow

Worktree:

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v7/customer-ui-flow`

Branch:

`feature/v7-customer-ui-flow`

Owner files:

- `templates/index.html`
- `static/*`
- UI-only changes in `app.py` template context
- `tests/test_*ui*`

任务：

1. 网站名：外卖菜品图一键生成工具。
2. 页面采用更接近 Google 产品工作台的视觉：干净、明亮、清楚、有层级。
3. 顶部流程不是“看不出能点”的大圆圈，而是明确步骤导航。
4. 上传菜单后自动进入下一步，不要求用户回顶部点按钮。
5. 价格只显示积分，不显示人民币，不显示混元/Gemini 模型名。
6. 出图质量可直接选择普通/精修。
7. 免费重做额度要明显显示，换一版按钮要标明消耗积分。

验收：

- 客户不需要猜下一步点哪里。
- 首屏清楚显示余额、充值、预计消耗积分。
- 选择质量、平台、水印互相不阻塞。

## Subagent 7: Export Watermark Platforms

Worktree:

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v7/export-watermark-platforms`

Branch:

`feature/v7-export-watermark-platforms`

Owner files:

- `image_pipeline.py`
- `app.py` export endpoints only
- `templates/index.html` watermark/export sections only
- `tests/test_*export*`
- `tests/test_*watermark*`

任务：

1. 平台可任选：美团、淘宝/饿了么、京东，至少选一个。
2. 任一平台免费，第二个平台 +100 积分，第三个平台再 +100 积分。
3. 美团导出 800x600，4:3，单张不超过 5MB。
4. 淘宝/饿了么导出 800x800，1:1，单张不超过 20MB。
5. 京东导出 800x800，1:1，单张不超过 5MB。
6. 导出格式支持 JPG、PNG、JPEG，默认 JPG。
7. 水印文字支持黑/白，logo 只推荐 PNG 透明图，不额外套圆形或底板。
8. 打包导出按钮必须有 loading 和成功/失败提示。

验收：

- ZIP 可下载。
- 单张图片文件名按 Excel 菜品名命名。
- 水印不会在多平台裁切时被裁掉。

## Subagent 8: E2E Render Acceptance

Worktree:

`/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/worktrees-v7/e2e-render-acceptance`

Branch:

`feature/v7-e2e-render-acceptance`

Owner files:

- `scripts/smoke_product_flow.py`
- `scripts/smoke_hunyuan_live.py`
- `tests/test_*acceptance*`
- `PRODUCT_ACCEPTANCE.md`
- `DELIVERY_REPORT.md`

任务：

1. 本地跑通完整流程。
2. Render 跑通上传菜单、6 背景、6 样图、正式图、导出 ZIP。
3. 发现其他模块问题时只写验收报告，不抢改业务文件。
4. 建立上线前检查清单。

验收：

- `python3 -m pytest -q` 无致命错误。
- `scripts/smoke_product_flow.py` 线上通过。
- `scripts/smoke_hunyuan_live.py` 至少一张真实图通过。

## 合并顺序

1. `feature/v7-gallery-cos-runtime`
2. `feature/v7-menu-matching-accuracy`
3. `feature/v7-hunyuan-generation-pipeline`
4. `feature/v7-style-samples-preview`
5. `feature/v7-async-job-progress`
6. `feature/v7-export-watermark-platforms`
7. `feature/v7-customer-ui-flow`
8. `feature/v7-e2e-render-acceptance`

## 当前关键阻塞

Render 需要配置：

- `GALLERY_UPLOAD_TOKEN`
- `COS_LIBRARY_INDEX_URL`

配置完成后，先上传小批量图库，再上传全量图库。否则线上仍会只显示 seed/internal 假图库，客户上传菜单后不会产生真实图库效果。

## 并发规则

1. 每个 subagent 只能改自己的 Owner files。
2. 不允许删除用户素材目录。
3. 不允许把 seed 图冒充真实生成结果。
4. 不允许在失败时静默返回假图。
5. 所有 subagent 完成后必须说明：
   - 改了哪些文件。
   - 跑了哪些测试。
   - 还剩什么风险。

## 当前 Subagent 启动状态

已启动：

- Gallery COS Runtime: `019effba-d414-7aa3-bd52-b36b1ce6e763`
- Menu Matching Accuracy: `019effba-f63f-7e63-9d90-a14422a0a6d3`
- Style Samples Preview: `019effbb-1b8d-75b1-8a9c-d365ff33bf75`
- Hunyuan Generation Pipeline: `019effbb-3da9-7900-9b64-f67e6f81eca6`
- Async Job Progress: `019effbb-5f23-7ec0-b276-5761a681ee10`
- Customer UI Flow: `019effbb-86b1-77d3-8268-92a2875797ea`

待启动：

- Export Watermark Platforms: 当前 subagent 并发上限已满，等任一已启动 agent 完成后启动。
- E2E Render Acceptance: 放在最后启动，负责合并后的验收，不提前抢改业务代码。
