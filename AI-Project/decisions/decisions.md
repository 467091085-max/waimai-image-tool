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

## Selected Background Identity
- Tencent TokenHub `hy-image-v3.0` supports up to three reference images through the `images` request field, but the official API does not promise pixel-preserving background identity and exposes no background lock, reference weight, input fidelity, or image-mask control.
- Passing the selected background through `images` may be described only as reference-conditioned generation. It must not be presented as using the exact same background without model-output evidence.
- If the acceptance criterion requires the selected background pixels to remain unchanged, use deterministic composition: keep the selected background as the immutable base, generate or extract a dish foreground plus mask, composite it, and verify that pixels outside the foreground region were not modified.
- The immutable base must be a server-side byte snapshot named by SHA-256, not the mutable per-style preview path. In-flight jobs keep the snapshot pointer even if the style preview is regenerated.
- Foreground and Mask caches are reusable only when pipeline version, selected-background identity, and stored file SHA-256 values all match.
- The canonical exact-background output is a lossless PNG whose persisted bytes are re-read and verified before promotion. Platform JPEG files are derivatives and must not be described as pixel-identical to the selected background.
- TokenHub V3 reference conditioning belongs in a worker-only provider adapter. Product/API orchestration should pass authorized asset IDs, and the worker should resolve short-lived URLs; the fixed public `/generate` contract must remain unchanged.

## Generation Batch Contract
- A generation job must freeze the authenticated user, menu upload, selected-background asset, billing quote, platform set, watermark choice, and private logo reference before enqueue.
- Billing points are calculated by the server and included in the immutable request digest; client-supplied prices or debit amounts are never authoritative.
- The same authenticated user, idempotency key, and canonical request digest must resolve to the same task. Reusing the key with a different digest is a conflict.
- Live customer task creation, status, cancel, and export use the authenticated server session as the owner. Browser `X-User-Id`, query `userId`, and raw `jobId` values are not ownership authorities.
- Internal service tokens may act as trusted principals. Header-based demo identity remains allowed only behind explicit local-demo generation flags.
- Until a separately authorized database migration adds a dedicated owner column, new menu uploads store `ownerUserId` and `parserVersion` only inside private server metadata in `parsed_summary_json`; public responses must strip that metadata.
- Resolving a menu upload for generation must verify the authenticated owner, private object key, database SHA-256, and the current object bytes. Ownerless legacy records are accessible only in explicit local-demo mode.
- Formal generation pricing is calculated only from the frozen server menu count, quality, watermark, and platform set. The customer frontend may display a quote but no longer creates the debit or refund order.
- Debit uses the frozen job's deterministic order ID before enqueue. A confirmed enqueue failure receives a bounded compensation tied to that original debit; partial Worker delivery refunds only undelivered image points, while zero delivery, cancellation, or Worker exception refunds the full frozen charge.
- A refund endpoint must not create arbitrary credit from browser-supplied points. Refunds resolve an owned source debit and cannot exceed that debit.
- Generation compensation uses a cumulative refund target. Concurrent/repeated finalizers credit only the unpaid remainder, and aggregate refunds cannot exceed the original debit.

## Redis Worker Reliability
- Enqueue idempotency, task creation, and queue insertion must be one Redis Lua operation.
- Public non-idempotent enqueue must also create the task and queue receipt in one Redis Lua operation.
- A Worker claim must atomically move the queue receipt to a processing list and assign the fencing lease token in the same Lua operation. Completion and failure acknowledgement require the current lease token and remove that exact receipt.
- Expired leases and receipts orphaned between move and claim are recovered from the processing list. An older Worker cannot overwrite the result after another Worker owns the renewed lease.
- Legacy stale-task recovery must ignore tasks carrying an active reliable-queue lease.
- Queue, processing, task, dead-letter, and idempotency keys share one Redis Cluster hash tag; queue order is FIFO.
- Lease timestamps come from Redis `TIME`, and provider execution heartbeats throughout the call.
- Prompt generation and authenticated menu batches use separate Redis queues. The fixed anonymous `/status/<task_id>` endpoint cannot read product-batch tasks.
- A Redis product Worker resolves immutable menu/background objects, calls providers, and writes a request-bound private manifest. It must not debit, refund, or update the customer product database.
- The Web process settles billing and customer-visible job state only after verifying the Redis terminal state and private manifest digest. A missing Redis receipt for a persisted nonterminal job is republished from the frozen contract.
- A pending product task may be canceled atomically. A running cancellation is a request that preserves the current lease; Worker guard checks stop promotion, and success acknowledgement fails closed when cancellation won the race.
- Result manifests use a deterministic request-SHA object key and first-writer semantics. Retries reuse the verified manifest instead of calling the paid provider again.
- Redis terminal task and idempotency records have bounded retention. Durable customer history remains in the product database and private object storage.
- Multi-Web production deployment still requires the PostgreSQL job/outbox/settlement store and a transactional dispatcher; SQLite plus an in-process lock is not a production consistency claim.
- Task lease heartbeats and service liveness are separate signals. The Worker must publish a queue-scoped Redis heartbeat with a short TTL even while idle; live readiness is green only while that real heartbeat exists.
- A declared Worker environment variable is configuration intent, not runtime evidence. Missing, expired, corrupt, or unreadable service heartbeat data must fail live readiness closed.

## Final SaaS Architecture Constitution
- API Server is Render-facing and must stay lightweight: accept HTTP requests, create task IDs, push tasks to Redis, and return task status/result pointers only.
- API Server must not perform AI inference, image generation, long-running jobs, file processing, or background work.
- Worker is a separate process and is the only runtime allowed to call external AI/image providers and upload generated images.
- Redis queue/status is the source of truth for generation task state.
- Fixed public generation API contract: `POST /generate` accepts `{ "prompt": "string" }` and returns `{ "task_id": "uuid" }`; `GET /status/<task_id>` returns `{ "status": "pending|running|done|failed", "image_url": "string" }`.
- The fixed JSON contract does not mean anonymous access. Both prompt-generation routes require the server-configured `PROMPT_API_TOKEN`; missing configuration fails closed before Redis access.
- Do not add transitional compatibility endpoints to the API Server unless this constitution is explicitly replaced.

## Render Test-Site Recovery
- The existing single Render Web Service must continue serving the customer website while the immediate `混元未配置` defect is repaired.
- Do not replace its `gunicorn app:app` start command with the API-only server until separate web, API, Redis, and Worker services are provisioned and the website entry point is preserved.
- The immediate recovery is limited to creating/configuring a TokenHub test credential, restarting the authorized Render test service, and verifying real generation.
- Do not create a paid Render service or change a production environment without separate authorization.

## Growth And Agent Rules
- Agent and consumer rewards use only one direct relationship level. A second or deeper compensation level is forbidden; criminal or administrative enforcement thresholds are not product-safe permission boundaries.
- Agent commission rule target: first purchase 20%, repurchase 10% of the directly bound customer's eligible net cash payment, unless later replaced by a legal-reviewed rule.
- C-end invite rewards target: inviter 100 credits, invitee 20 credits.
- Registration rewards need anti-abuse controls: phone number, SMS verification, device/IP/risk checks.
- C-end rewards should be credits only, not cash withdrawal.
- The frozen rule version is `growth-direct-v2-2026-07-30`; payment/refund business events must persist this version.
- Before production launch, China-qualified counsel must review the actual contracts, marketing language, withdrawal policy, and operational workflow.

## Payment Catalog Boundary
- Customer payment order creation requires an authenticated session.
- The browser may submit only a versioned server package ID. User identity, amount, points, provider, and order ID are server-owned.
- Payment idempotency uses the `Idempotency-Key` header.
- A successful provider callback must match the frozen server order amount before points can be credited.
- Customer UI must not call the local demo recharge-credit endpoint as a production payment flow.

## Taxonomy Reuse Boundary
- The 40-leaf taxonomy is a conservative reuse gate, not an accuracy claim.
- Cross-taxonomy fuzzy library reuse is forbidden.
- `unknown` items may reuse an asset only when normalized dish names are exactly equal; otherwise they require generation.
- Combo product reuse requires the complete normalized component fingerprint, including staple and drink components. Component images are composition inputs, never a complete combo product image.

## Background Catalog Taxonomy
- The background catalog uses the existing 40 leaf taxonomy IDs directly; the
  proposed 25-category intermediate layer is superseded and must not coexist.
- Each taxonomy has exactly six versioned background style slots, for a total
  target of 240 approved assets.
- `mixed` and low-confidence classification mean manual review; they are not a
  41st catalog category and must not trigger arbitrary background reuse.
- Customer requests may read only a complete approved six-slot manifest.
  Missing slots are created by an offline/admin batch, never synchronously
  filled by the customer Web request.

## Background Catalog Visual Gate
- Real provider success and COS upload do not mean visual approval. Every
  generated asset starts as `pending`, and a category remains unavailable
  until exactly six reviewed assets form a complete approved manifest.
- Prompt v9 is rejected for the `light_food` pilot: four of six outputs created
  a raised platform or rectangular mat even though the negative prompt named
  those objects.
- Prompt v10 removed product-display wording, but its paid pilot is also
  rejected because provider-side prompt rewriting still produced paper sheets
  and visible table-front slabs.
- Prompt v11 is the first catalog contract that explicitly sends Hunyuan 3.0
  `Revise=0` and a deterministic positive seed. This preserves the reviewed
  prompt and makes each category/style output reproducible. `NegativePrompt`
  remains Lite-only because it is not part of the documented v3 input schema.
- A prompt version may never mix seeds or image bytes from an older version.
- A normal full-size dining table may show a broad front edge or legs when its
  tabletop occupies the lower composition area and can hold the dish. This is
  a valid `flat-table` background, as explicitly required by the product.
  A small central product plinth, isolated slab, mat, board, or inset rectangle
  remains a rejection condition; those are not equivalent to a dining table.
- A paid catalog category is the restart checkpoint. Its six-slot manifest is
  written immediately after the sixth object, and future runs may skip provider
  calls only after re-reading and hashing all six exact COS objects.
- Approval is hash locked: an operator must submit the six reviewed SHA-256
  values, and any missing, duplicate, replaced, or tampered object fails the
  write. Provider success and a contact sheet never auto-approve a category.
- Do not spend the remaining 39-category batch until the six-slot v11 pilot
  passes exact-object visual review.
- Approved v11 prompt bytes are immutable compatibility data. `light_food`,
  `topped_rice`, and `mixed_rice` keep their exact 18 prompt SHA-256 values so
  later prompt tuning cannot invalidate their hash-locked COS manifests.
- For every still-unapproved category, flat-table prompts use one table
  material and one table color only. Category palette belongs on the wall and
  may not create patchwork, mosaic, inlay, panels, color blocking, or mixed
  materials on the tabletop.
- The second seamless-solid slot uses the second palette color for unapproved
  categories. This avoids treating a third palette value such as `浅木色` or
  `深木色` as a physical wood panel or decorative arch.
- `fried_chicken`, `burger_hotdog`, and `pizza` use the profile-v11 single-hue
  solid-slot contract. Their exact approved prompt bytes are immutable along
  with the earlier 12 approved categories; future tuning may affect only
  unapproved categories.
- Seamless-solid prompts must translate material-like palette labels such as
  `浅木`, `原木`, `暖木`, `胡桃木`, `金属灰`, and `冷石色` into plain color names.
  These labels are visual palette intent, not permission to render wood, metal,
  stone texture, wall panels, or stepped display planes.
- The exact approved profile-v12 prompt bytes for `sandwich_bagel` and
  `japanese` are immutable. Their frozen compatibility path must continue
  applying material-label normalization; hash locking must never restore raw
  material-like palette words.
- The exact approved prompt bytes for `sichuan_hunan`, `cantonese_roast`,
  `jiangzhe`, `northeast_chinese`, and `northwest_xinjiang` are immutable.
  Their compatibility path keeps the single-hue seamless rule and material
  label normalization active, bringing the frozen checkpoint to 25 categories
  / 150 approved assets.
- The exact approved prompt bytes for `northern_lu`, `fujian_taiwan`,
  `home_stir_fry`, `fish_seafood`, and `beef_lamb_pot` are immutable. Their
  compatibility path keeps the same single-hue seamless rule and material
  label normalization active, bringing the frozen checkpoint to 30 categories
  / 180 approved assets.
- The exact approved prompt bytes for `braised_cooked_food`, `soup_stew`,
  `steamed_claypot`, `milk_fruit_tea`, and `coffee_cocoa` are immutable. Their
  compatibility path preserves the single-hue seamless rule and material
  label normalization, bringing the frozen checkpoint to 35 categories / 210
  approved assets.
- The exact approved prompt bytes for `bottled_drinks`, `fresh_drinks`,
  `dessert_bakery`, `fried_snacks`, and `fruit` are immutable. Their
  compatibility path preserves the same single-hue seamless rule and material
  label normalization, completing the frozen catalog at 40 categories / 240
  hash-lock approved assets.

## Staging Generation Queue
- Production and ordinary Render runtimes require Redis plus an independent
  worker for durable formal-generation jobs; absence of Redis must fail before
  point debit.
- The protected `staging-demo` service may use the in-process queue only when
  staging Basic Auth is configured and
  `ALLOW_STAGING_IN_PROCESS_GENERATION=true` is explicitly set. This exception
  is for single-worker paid-provider acceptance only and is not a production
  architecture substitute.
- Staging acceptance must keep Gunicorn at one worker so submission, polling,
  manifest lookup, and export see the same ephemeral queue state. The queue
  timeout may be raised by environment variable for a bounded full-menu run;
  production defaults remain unchanged.

## Menu Snapshot Classification Identity
- Immutable menu bytes remain addressed by their full SHA-256 directory, but
  the materialized Excel basename must preserve the sanitized original
  filename. Store/file naming is intentional taxonomy evidence and may not be
  replaced by a digest-only basename before re-parsing.
- When a frozen worker contract lacks `originalFilename`, recover the sanitized
  source basename from the private object key. Do not hardcode a category or
  silently choose a background from repeated add-on ingredient words.

## Free Preview Product Selection
- The six free samples must be six distinct normalized product names from the
  uploaded menu. Explicit announcements such as customer greetings, order
  notices, support instructions, or `勿拍` rows may not occupy a sample slot.
- Prefer up to two combos containing the selected menu taxonomy, then matching
  single dishes, remaining matching combos, and other real combos before
  unrelated single items. This keeps the preview representative while proving
  that combo-image generation is supported.
- Sample selection changes neither Excel row inclusion nor the formal
  generation manifest; filtering formal products is a separate product
  decision and must not be inferred from the preview rule.

## Verified Free Preview Reuse
- A `standard` formal image may reuse the exact free-preview PNG only when the
  normalized row, selected style, selected-background asset/SHA/menu identity,
  pipeline version, persisted output SHA-256, and exact-background verification
  all match.
- `premium` generation never reuses a standard free preview. It must execute
  the premium provider path.
- Preview reuse is an optional speed optimization. An unavailable private
  preview cache falls back to normal exact-background generation rather than
  failing the formal row or retrying the paid preview endpoint.
- A verified preview reused as a formal output counts as a successful cached
  result for manifest completion and billing settlement.

## Image Provider Routing And Privacy
- Hunyuan 3.0 remains the server-owned provider for category backgrounds,
  free samples, and formal menu generation. Gemini is server-owned only for
  single-image refinement; customers do not submit API keys, model IDs, or
  arbitrary provider endpoints.
- Provider readiness is exposed only as redacted capability/status metadata.
  Secret values never enter browser payloads, logs, repository files, or
  project memory.
- Gemini image refinement uses the official Google Interactions endpoint with
  `store=false`. A configured endpoint outside
  `generativelanguage.googleapis.com/v1beta/interactions` fails closed before
  any customer image or API key is transmitted.
- Permanent provider rejections do not enter the Worker retry loop. Transient
  quota, rate-limit, timeout, and server failures may use the existing bounded
  retry policy.

## Product Image Prompt Compiler
- Menu rows may not go directly from parsing to provider generation. The
  required flow is menu semantics, explicit-choice resolution, background
  scene contract, deterministic Prompt Compiler, Prompt QA, provider request,
  and visual QA.
- The Prompt Compiler is a pure deterministic module. It does not read files,
  environment variables, or network state, and it emits the resolved dish,
  full prompt, provider payload, seed, placement contract, and audit digests.
- Every foreground prompt must inherit the selected background camera pitch,
  lens, support plane, subject anchor, and lighting direction. A foreground
  generated without that scene contract is not eligible for exact-background
  composition.
- Prompt length limits are enforced by complete priority segments. Mandatory
  constraints may never be silently cut in the middle of a sentence.
- For structured menu choice groups, provider-visible dish identity is rebuilt
  from required components plus the selected option from each group. Raw menu
  phrases such as `饮品自选` may not reintroduce rejected choices. A drink/cup
  requirement is emitted only when the resolved component list actually
  contains a selected drink.
- Formal full-menu generation stays blocked until the six representative
  samples pass semantic, background, perspective, contact, and aesthetic QA.

## Generation Capacity And Gemini Contracts
- A 100-image formal store batch has a structural target of ten concurrent
  provider calls and completion within 3600 seconds. Configuration support is
  not production proof: provider quota and measured p95 latency must be stored
  separately before the one-hour target can be marked verified.
- The 100-image and 3600-second values are immutable product constants. Runtime
  environment variables may not weaken either acceptance threshold.
- TokenHub capacity covers the entire paid request until a terminal generation
  result. Redis-backed deployments request up to ten leased account-level slots
  shared by every process, but activate only the lesser of requested and
  verified account concurrency. An unverified account activates one slot;
  local/staging without Redis uses the same bounded rule process-locally.
- `TENCENT_HUNYUAN_SYNC_LIMIT` remains as a backward-compatible environment
  name, but its formal-batch default is 120 rather than the old six-image cap.
- TokenHub supports two explicit protocols: the existing legacy submit/query
  path and Tencent's current official synchronous `hy-image-v3` path. Protocol
  selection is frozen by deployment configuration; a paid POST is never
  automatically repeated after an ambiguous outcome.
- Gemini refinement jobs freeze provider, model, official endpoint, prompt
  version, output format, aspect-ratio policy, and canvas normalization into
  the idempotency digest before debit. Web and Revision Worker must share the
  same non-secret configuration, while the API key remains a Render secret.
- Gemini refinements use a dedicated Redis queue and Revision Worker so a
  100-image Hunyuan batch cannot head-of-line block single-image adjustments.
- Production throughput is verified only when all three values are present:
  real account concurrency of at least ten, provider P95 within the active-wave
  budget, and one complete 100-image end-to-end batch within 3600 seconds.
  Partial batches and projections remain diagnostics, never acceptance proof.
- The provider-slot lease must outlive request timeout plus legacy poll timeout
  with a 60-second margin. Local Chroma Mask extraction is the normal fast
  path; serialized cloud Mask extraction is only a validation fallback.
- Capacity readiness requires a SHA-256-bound real-mode evidence JSON for one
  complete 100-image batch. Scalar environment timings are diagnostic only.
  A lost Redis lease heartbeat or failed release retains the ambiguous slot
  until lease expiry instead of allowing a second paid request into that slot.
- Cloud Mask fallback is disabled by default. It may be enabled only with a
  separately verified Mask concurrency value; otherwise Chroma validation
  failure stops the row instead of serializing the whole store. In staging and
  production, enabled Mask fallback additionally requires a Redis-distributed
  account gate shared by Web and Worker processes.
- Gemini's default HTTP transport never follows redirects. PostgreSQL outbox
  dispatch and settlement select `product-generate` versus `product-revision`
  from the frozen job type; Worker heartbeat alone is insufficient queue-flow
  evidence.

## Background Prompt Version Binding
- Background Scene Contract v2 includes the background's prompt version. A v11
  background uses its actual 25-degree camera contract; only v12 assets use the
  new high-angle style templates. Runtime may not relabel an old asset with a
  newer scene contract.
- A new background prompt version is promoted only after all six category
  assets pass paid generation, integrity, and human visual review. Until then,
  approved v11 assets remain active and formal generation remains visually
  blocked where applicable.
- Runtime v12 activation is scoped to `mixed_rice` through
  `MIXED_RICE_BACKGROUND_PROMPT_VERSION`; it cannot change another category.
  The selected background prompt version is duplicated at the frozen snapshot
  boundary, validated against Scene Contract v2, and restored from that
  contract by the Worker. Catalog prompt hashes must use the manifest's exact
  prompt version for both COS and PostgreSQL backends.
- Exact-background output compatibility is pipeline v6. Tencent output reuse
  requires the current row-specific compiler digest and scene digest; approved
  library reuse requires the exact current background scene digest. Future
  compiler compatibility changes must bump the pipeline version.

## Empty Background Prompt V14
- TokenHub Hunyuan v3 does not consume the Lite-only `NegativePrompt` field.
  Its positive prompt therefore uses a generic zero-object contract without
  naming food, vessels, dishes, or representative products, and provider
  success can never replace human visual review.
- Provider-visible empty-set prompts must not name the menu taxonomy or any
  representative dish. Category suitability is expressed only through the
  precomputed palette, material, camera, and lighting direction; this prevents
  a background request from being interpreted as a food-generation request.
- Background generation uses `Revise=0` and a deterministic seed bound to the
  catalog, taxonomy, prompt version, category, and style only in v14. Existing
  v11-v13 runtime payloads remain unchanged. Tencent's documented success
  response does not echo Seed/Revise, so audit metadata binds the exact values
  submitted by the TokenHub adapter and labels their source
  `submitted-request`; it must not claim a provider echo or applied control
  when none was returned. A prompt change gets a new version and cannot reuse
  prior bytes or metadata.
- A selected-background snapshot created immediately after generation must use
  that generation's explicit prompt version. It may not infer the version from
  a sidecar that belonged to the overwritten file.
- Category backgrounds remain `pending` after provider and technical quality
  success. Only explicit visual approval may make them reusable; product-image
  persistence keeps its existing status behavior.
- Offline catalog registration must use the generated entry's exact prompt
  version for both `prompt_version` and `pipeline_version`; it must not infer a
  version from the Web runtime environment.
- V14 generation is valid only with the exact TokenHub v3 model/protocol pair
  that forwards deterministic Seed/Revise controls. Tencent cloud fallback,
  TokenHub Lite, or a mismatched action fails closed before an asset can be
  saved or registered.
- Paid provider submission and result-image retrieval are separate retry
  domains. A download failure may retry only the idempotent image GET; it must
  never submit a second paid generation request.
- An explicitly versioned selected-background snapshot may inherit a catalog
  asset ID only when the sidecar carries the same normalized prompt version.
- V14 generation evidence is mandatory at every persistence and reuse boundary,
  not only immediately after the provider call. A local sidecar or remote
  manifest is invalid unless it records the exact TokenHub v3 action/model,
  exact submitted Seed/Revise controls, evidence source, and optional provider
  echo. A returned Seed must strictly match the requested integer; an omitted
  Seed remains valid only when the adapter proves the request carried the
  frozen Seed and `Revise=0`. Invalid v14 assets cannot be uploaded,
  registered, reused, manifested, or approved.
- The v14 evidence validator is shared runtime code in `background_catalog.py`.
  COS manifests persist all required evidence and are eligible after validation.
  The current PostgreSQL asset schema has no action/seed/revision columns, so
  PostgreSQL v14 catalog reads fail closed rather than inferring evidence from
  provider/model names. Enabling that backend requires an explicitly authorized
  schema migration that preserves the complete evidence contract.
- A v14 selected-background snapshot must bind canonical generation evidence to
  the exact selected image SHA-256. The current image must also match the
  sidecar's prompt, category, style, prompt hash, and output hash; otherwise it
  is rejected before preview, batch freeze, Worker restore, or refinement.
- Newly generated v14 backgrounds pass the current provider metadata directly
  into snapshot construction. Snapshot construction may not read a previous
  generation's sidecar before the new sidecar is atomically written.

## V14 Scenic Correction Before Catalog Activation
- The first fresh staging visual pilot proved that the active v13 prompt can
  satisfy file, provider, SHA, and persistence checks while still producing an
  unusable smooth gradient. Technical success is therefore never visual
  approval.
- V14 had not been activated by the live service and no v14 catalog was
  accepted. Its prompt may be corrected before activation while preserving the
  existing deterministic Seed/Revise and evidence contract. Prompt SHA-256
  invalidates any bytes made from an earlier draft.
- Paid v14 pilot outputs are unapproved and remain invalidated by prompt and
  scene-contract SHA-256 when the pilot specification changes. No v14 asset is
  reusable or approvable unless those exact hashes match the current pilot.
- V14 background prompts, foreground scene contracts, and catalog slot
  metadata must all derive from `EMPTY_SET_STYLE_DIRECTIONS`; they may not
  reuse the frozen v13 pure-color slot definitions. V11 and v13 remain
  unchanged.
- All six v14 slots use a single close-cropped tabletop extending beyond all
  four frame edges. Their structured fingerprints differ by material family,
  camera pitch, lens, light direction, and bounded category-specific accent
  zone. Walls, horizons, table edges, thickness, legs, under-table space,
  central boards, and second planes are prohibited.
- Competitor images are research references only and are never copied into COS
  or embedded in prompts.
- A background-specific offline gate must reject smooth gradients and
  low-information color blocks before metadata persistence. Human review still
  decides aesthetics, geometry, prohibited objects, and category fit before an
  asset can be approved or multiplied to the 40 x 6 catalog.
- Catalog approval must receive the exact prompt version explicitly and use it
  for manifest lookup, prompt hashes, review-sheet keys, approval write-back,
  and post-write validation. The builder's legacy default is not an approval
  namespace.
- A full-frame v14 tabletop is one seamless surface. Provider-visible prompts
  must prohibit seams, joints, cracks, grooves, cut edges, slab boundaries,
  and long dark lines; a central material boundary is a visual rejection even
  when generic texture and file-quality checks pass. Normal-product style-2
  uses a 50-degree high view so the selected background and generated dish
  share the same non-upright geometry.

## Structured Menu Drink Choices
- A selected structured choice group with `role=drink` requires exactly one
  unbranded cup even when the selected value has no generic drink keyword.
  Non-drink choice roles do not infer a cup from the group label alone.

## Product Contract Attestation
- The public idempotency SHA-256 detects accidental request changes but is not
  an authenticity boundary because an attacker can recompute it. Every new
  authenticated menu-batch and image-revision contract therefore carries a
  domain-separated HMAC-SHA256 attestation over the application context,
  contract domain, attestation version, and complete private contract.
- Web signs contracts with the existing object-signing secret before debit or
  enqueue. Product Worker and Revision Worker verify the attestation before app
  import, object storage, or provider work. A missing or shorter-than-32-byte
  secret fails closed; the attestation is never included in public payloads.
- V11-v13 and other legacy-background jobs remain functionally compatible only
  as newly created signed contracts. Unsigned queued contracts are not silently
  grandfathered because that would preserve the v14 downgrade bypass.
- Redis status reads, missing-task recovery, persisted SQLite/PostgreSQL reads,
  terminal mutation, refund, and settlement each verify the attestation before
  re-enqueue, object access, or account credit. A recomputed public digest can
  never substitute for this verification.
