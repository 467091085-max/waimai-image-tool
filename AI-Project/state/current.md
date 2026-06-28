# Current Task

## Goal
把外卖菜品图工具做成可上线的产品级系统，并解决 Codex 长任务失忆和上下文断裂。

## Current Step
Step 2: 验证 Render 背景图异步生成补丁。

## Status
- Obsidian memory structure: done
- Render background generation failure reproduced: done
- Root cause located: done
- Async background generation patch: in progress
- Targeted async background tests: done
- Local tests after async patch: done
- Sync to deploy repository: done
- Deploy repository tests: done
- GitHub push for Render deploy: in progress
- Render verification: pending

## Constraints
- Codex 每次继续任务前必须先读 `AI-Project/state/current.md`。
- 和当前任务相关时必须读 `AI-Project/decisions/decisions.md`。
- 每个 step 控制在 15 分钟以内，完成后立刻更新 `current.md` 和当天 log。
- 不依赖模型记忆，不假设缺失上下文。
- 不回滚 worktree 中既有改动，不影响其他本地任务。
- Render 上 `/api/plan` 不能同步调用混元生成 6 张背景图。
- 禁止默认用色块、SVG 或图库假图冒充真实背景图。
- 用户要求背景图、样图、正式图优先使用混元生成以保证正确率。

## Current Context
- 当前 worktree: `/Users/guiguixiaxia/.codex/worktrees/de51/waimai-image-tool`
- Render 测试站: `https://waimai-image-tool-1.onrender.com`
- Local 测试站: `http://127.0.0.1:8791`
- Deploy repo: `/Users/guiguixiaxia/Documents/Codex/2026-06-15/33-excel-excel-300-5-4/outputs/waimai-image-tool-deploy`
- GitHub remote: `git@github.com:467091085-max/waimai-image-tool.git`

## Current Findings
- Render `/api/tencent-status` 曾显示混元和 COS 已配置。
- Render `/api/plan?quality=standard` 会长时间挂起并返回 500，之后普通接口也会超时。
- 根因是 `/api/plan` 通过 `style_options -> style_sample_candidate -> tencent_style_background` 在单个请求内同步生成 6 张混元背景图，Render 单 worker 被阻塞或崩溃。
- 本地已开始最小补丁：`/api/plan` 只返回 pending 背景卡片，新增 `/api/style-background?style=...&generate=1` 按单张风格背景异步生成，前端并发 2 个请求逐张加载。

## Next Action
1. 在 deploy repo 提交并 push 到 GitHub 触发 Render 部署。
2. 在线验证 Render `/api/plan` 快速返回，`/api/style-background` 能生成真实背景图。
