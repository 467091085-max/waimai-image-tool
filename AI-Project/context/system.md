# Codex Long Task Rules

Before doing any work:
1. Read `AI-Project/state/current.md`.
2. Read `AI-Project/decisions/decisions.md` if the task touches an existing decision.
3. Never assume missing context from memory.

During work:
- Work in steps no larger than about 15 minutes.
- Update `AI-Project/state/current.md` after each step.
- Append to `AI-Project/logs/log-YYYY-MM-DD.md` after each meaningful change.
- Keep patches minimal and tied to the current step.
- Do not continue writing code after a step without writing state.

After finishing:
- Update status in `AI-Project/state/current.md`.
- Write a concise summary and verification result to the daily log.
- Commit each completed step when the user requests Git-backed checkpoints or when deployment requires a push.

Operating model:
- Obsidian files are long-term memory.
- Codex is the executor.
- Git is the timeline.
