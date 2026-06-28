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
- GitHub push for Render deploy: done
- Render deployment: done
- Render `/api/plan` verification: done
- Render Hunyuan background generation verification: blocked by Tencent Cloud ResourceInsufficient
- ResourceInsufficient UI copy: done

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
- Render 新代码已上线，`/api/style-background` 不再 404。
- Render `/api/plan?quality=standard` 已验证 200，约 1.76 秒返回，6 张背景均为 `PendingGeneration`，不再同步生成导致阻塞。
- Render `/api/style-background?style=style-1&generate=1` 已验证 200，约 2.45 秒返回失败状态；腾讯云返回 `ResourceInsufficient`，需要开通资源包或后付费后才能真实出图。
- 前端已补充 ResourceInsufficient 显示：腾讯云额度不足时显示 `混元资源不足`，不再只显示笼统失败或继续转圈。

## Next Action
1. 同步并推送 ResourceInsufficient UI 文案补丁。
2. 在腾讯云控制台为混元/AIArt 开通可用资源包或后付费。
3. 资源开通后重新请求 `https://waimai-image-tool-1.onrender.com/api/style-background?style=style-1&generate=1`，确认返回真实图片 URL。
4. 若返回 URL，再验证前端 6 张背景逐张加载和选择背景后的样图生成。
