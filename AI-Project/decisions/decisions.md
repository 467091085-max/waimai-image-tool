# Technical Decisions

## Long Task Memory
- Obsidian-style markdown files are the source of truth for long-task memory.
- Codex is only the executor and must not rely on conversation memory for project state.
- Git remains the version timeline for code changes.
- `AI-Project/state/current.md` is the first file to read before every new work segment.
- `AI-Project/logs/log-YYYY-MM-DD.md` records execution history after each change.

## Render Background Generation
- `/api/plan` must not synchronously generate Hunyuan background images.
- Style background generation is split into per-style requests through `/api/style-background`.
- Frontend loads missing style backgrounds progressively with limited concurrency.
- Default fake local color/SVG background fallback is disabled.
- If Hunyuan is not configured, the UI must show explicit blocked state such as `混元未配置`, not a fake generated image.

## Product Image Correctness
- Generated backgrounds must match the uploaded menu category.
- Free sample images and final product images must use the selected background style.
- Product images must be full-frame food images, not a small framed image inside a blurred larger frame.
- If matching existing assets is unreliable, prefer Hunyuan text-to-image generation over forcing an incorrect library match.

## Growth And Agent Rules
- Agent mode uses two levels, not unlimited levels.
- Agent commission rule target: first purchase 20%, repurchase 10% unless later replaced by a legal-reviewed rule.
- C-end invite rewards target: inviter 100 credits, invitee 20 credits.
- Registration rewards need anti-abuse controls: phone number, SMS verification, device/IP/risk checks.
- C-end rewards should be credits only, not cash withdrawal.
