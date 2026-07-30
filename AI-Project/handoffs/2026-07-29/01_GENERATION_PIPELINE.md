# Task 1: Menu Intelligence, Full Generation, And Refinement

Read `00_MASTER_BRIEF.md` first.

## Objective

Implement the smallest coherent patch that turns menu parsing and image planning into a correct all-item generation pipeline, including six category backgrounds, consistent style use, combo generation, similar-name understanding, and real single-image refinement through the worker/provider boundary.

## Required Research

- Trace the complete path from uploaded Excel to parsed rows, category detection, matching, six background options, free samples, formal generation, export, and custom edit.
- Identify all places where local library reuse can produce a wrong dish or wrong background.
- Compare `app.py`, `menu_parser.py`, `matching_engine.py`, `shared/generator.py`, `worker/worker.py`, `static/app.js`, and existing tests.
- Establish a taxonomy strategy that covers the 24-menu corpus without relying on only five hard-coded shop categories. Prefer explainable category/family evidence and safe fallback over false certainty.

## Required Implementation

- Every parsed menu row must produce exactly one formal-generation work item unless explicitly excluded as a non-sellable header/noise row.
- Similar names must normalize to a canonical dish family without collapsing materially different dishes.
- Combo meals must extract components and produce a combo-specific prompt. A combo may not reuse a single-dish image.
- Six background prompts must be category-appropriate and visibly distinct while sharing the same product category.
- Selected background/style identity must flow unchanged into free samples, formal images, retries, cache keys, and refinement.
- Wrong-library matches must fail safe into text-to-image generation.
- Final images must be full-frame commercial food images, with no inner frame, blurred filler canvas, text, price, logo, watermark, people, or hands.
- Implement a real custom-refinement task path. It must:
  - reference the authorized source image;
  - validate a non-empty edit instruction;
  - enqueue worker-side image editing;
  - preserve style/category metadata;
  - charge points idempotently;
  - refund on terminal provider failure;
  - write a distinct output asset and audit/job record.
- Keep provider code worker-only and fail closed when the provider is not configured.
- Add a deterministic batch-planning command or test helper that Codex can run against all 24 local Excel files without calling the paid provider. It must report file, item count, categories, combo count, generated task count, and validation errors.

## Required Tests

- Taxonomy tests across representative names for all major food families present in the menu corpus.
- Similar-name positive and negative cases.
- Combo component extraction and combo prompt tests.
- Exactly-one-task-per-menu-row invariant.
- Six distinct category background prompts.
- Selected-background identity in sample, formal, retry, cache, and refine tasks.
- Full-frame/placeholder quality-gate behavior.
- Custom-refinement debit, idempotency, authorization, failure refund, and output-asset tests.
- Existing full test suite.

## Acceptance Criteria

- No color-block or SVG placeholder can be returned as generated success.
- No generic or same image may satisfy different unmatched dish rows.
- No combo row may be planned as a single dish.
- No custom-edit action may debit points without creating a real queued edit task or refunding a terminal failure.
- Report which checks are prompt/contract tests versus actual model-output checks.
