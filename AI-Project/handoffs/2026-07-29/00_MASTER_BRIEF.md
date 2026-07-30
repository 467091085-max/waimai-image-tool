# ChatGPT Pro Engineering Handoff

## Role And Authority

You are an external senior engineer working from the attached source archive. Codex is the integration owner and final reviewer. Your conclusions and test claims will be independently checked.

You may inspect and modify only the supplied source. Do not assume access to local files, private Git repositories, cloud dashboards, credentials, Render, Redis, Tencent Cloud, Alipay, or production data.

## Product Goal

Turn the current food-delivery image tool into a locally verifiable, production-oriented product:

- Read every Excel menu in `/Users/guiguixiaxia/Documents/menus`.
- Correctly understand dish names, similar names, categories, and combo contents.
- Generate six category-appropriate background choices.
- Use the selected background consistently for free samples and every formal product image.
- Generate one correct full-frame image for every menu item, including combo meals.
- Support real single-image refinement.
- Preserve account, points, billing, agent/invite, anti-abuse, anti-theft, storage, audit, and admin capabilities.

The local menu corpus currently contains 24 Excel files and 3,036 parsed menu items. The Excel files are private test inputs and are not included in the source archive.

## Current Architecture

- Python 3.11 / Flask application.
- Legacy customer Web application in `app.py` still owns menu upload, planning, style selection, billing, export, admin, and older generation jobs.
- Separate SaaS skeleton:
  - `api-server/app.py`: lightweight HTTP API only.
  - `worker/worker.py`: separate generation worker.
  - `shared/redis_queue.py`: Redis queue and status source.
  - `shared/generator.py`: worker-only provider boundary.
- SQLite is the local MVP database.
- Tencent TokenHub HY-Image 3.0 and legacy Tencent Cloud adapters exist in the legacy application.
- COS object storage, Alipay, OTP/SMS, points ledger, direct-agent commission, invite rewards, audit, and risk modules are partially implemented.
- Test baseline: 354 pytest tests pass.

## Non-Negotiable Architecture Boundaries

- Preserve the fixed public SaaS generation contract:
  - `POST /generate` accepts only `{ "prompt": "string" }` and returns only `{ "task_id": "uuid" }`.
  - `GET /status/<task_id>` returns only `{ "status": "...", "image_url": "..." }`.
- `api-server` must not import or perform AI inference, image generation, long-running processing, Excel parsing, file processing, or background jobs.
- Only an independent worker process may call external AI/image providers and write generated image results.
- Redis remains the queue/status source of truth for worker generation tasks.
- Real providers must fail closed when credentials are missing. Do not present mock, SVG, color blocks, reused wrong dishes, or local placeholders as real generation success.
- Generated food must fill the final canvas as a normal commercial product image. A small framed image over a blurred background is a failure.
- The selected background style must be applied consistently to samples and all formal images.
- Avoid unrelated refactors. Preserve existing billing, security, storage, and audit behavior unless the assigned task requires a compatible extension.
- Do not add secrets, customer menus, generated customer images, runtime databases, caches, browser state, or local absolute credentials to source.

## Known Gaps That Must Not Be Misrepresented

- Only five category detector rules and three category-specific prompt templates currently exist.
- The frontend still uses legacy monolith generation routes in important paths.
- The existing custom-edit UI charges points but has no real image-edit backend.
- Production Redis, TokenHub credentials, object-storage credentials, and payment credentials are not available in the archive.
- Passing mocked provider tests is not proof of real Tencent generation.

## Required Deliverables

For your assigned task, return:

1. A concise engineering report with root causes and design choices.
2. A unified diff against archive baseline commit `4d3214bbd251914fa314265d5ac98d12c1a302fa`.
3. A complete modified-source ZIP if the interface permits file download.
4. A manifest of changed files.
5. Exact commands run and unedited pass/fail summaries.
6. Explicit separation of:
   - code-verified behavior;
   - mocked/fake-provider verification;
   - external production behavior not verified.
7. Any migration or environment-variable requirement, without secret values.

## Mandatory Test Gates

Run all gates available in the supplied environment:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile app.py admin_panel.py billing.py image_pipeline.py storage_db.py menu_parser.py matching_engine.py library_index.py api-server/app.py worker/worker.py shared/redis_queue.py shared/generator.py
node --check static/app.js
node --check static/admin.js
git diff --check
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider -q
```

Add focused tests for every behavior you change. If Redis or a real image provider is unavailable, test the provider boundary with a deterministic fake but label that result accurately.

## Prohibited Actions And Claims

- Do not deploy, push Git, create a PR, migrate a database, change production configuration, or operate real user data.
- Do not request or fabricate API keys, cookies, passwords, OTP codes, or cloud credentials.
- Do not claim the 24 private menus or real Tencent generation were tested unless the actual files/provider were available.
- Do not weaken security checks merely to make tests pass.
- Do not silently replace the fixed public SaaS API contract.
- Do not return an architectural essay without implementable code and tests.

## Acceptance Standard

Codex will accept the work only when:

- the diff is reviewable and applies cleanly;
- existing tests and new focused tests pass;
- no secret or customer data is introduced;
- all assigned backend flows are real code paths rather than UI-only placeholders;
- generated-task planning covers every parsed menu item exactly once;
- category, similar-name, combo, background consistency, points, and authorization invariants are covered by tests;
- real-provider and production-only claims remain clearly unverified until independently exercised.
