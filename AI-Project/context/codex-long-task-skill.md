# Obsidian Long Task Memory Skill

Use this skill whenever a task may span multiple turns, windows, worktrees, agents, or deployments.

## Required Workflow
1. Read `AI-Project/state/current.md`.
2. Read `AI-Project/decisions/decisions.md` when the task touches architecture, product rules, pricing, generation strategy, deployment, storage, security, credits, or agent/invite rules.
3. Execute one small step.
4. Update `AI-Project/state/current.md`.
5. Append the action and result to `AI-Project/logs/log-YYYY-MM-DD.md`.
6. Continue only after state is written.

## Guardrails
- Do not rely on chat history as source of truth.
- Do not silently replace previous decisions.
- Do not batch unrelated modules into one step.
- Do not continue from memory when `current.md` is missing or stale; update it first.
