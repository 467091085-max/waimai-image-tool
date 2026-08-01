# ChatGPT Pro Background Reviews

## Handoff Identity

- Source baseline: `8f9afd6db1ea724ebec5982258b4261d6d192e16`
- Sanitized ZIP: `/tmp/waimai-background-architecture-8f9afd6db1ea.zip`
- ZIP size: 303,524 bytes
- ZIP SHA-256:
  `5394b3f88d938412ea37575112e4e40e5882c27283ca32a51b181a0a819825a6`
- Secret scan: no high-risk credential pattern found

## Conversations

- Architecture:
  `https://chatgpt.com/c/6a6dc7fa-9140-83e8-84ff-3f038953ce8b`
- Visual prompt system:
  `https://chatgpt.com/c/6a6dc847-c370-83e8-bb28-93b3f3191a66`
- Provider/UI reliability:
  `https://chatgpt.com/c/6a6dc81d-0b38-83e8-bfa2-5fb67fde2e94`

## Accepted Architecture Findings

- Keep the existing 40 leaf taxonomy IDs as the catalog keys. Do not restore
  the superseded 25-category intermediate layer.
- Treat `mixed/review` as an ineligible classification state rather than a 41st
  category.
- Generate backgrounds outside the customer request path and publish only an
  atomic, complete six-slot approved manifest.
- Bind category, style, taxonomy version, prompt version, prompt SHA, image SHA,
  model, provider, review state and immutable private object key.
- When approved-only mode is enabled, incomplete or unavailable manifests must
  not fall back to live generation or a local color block.
- Preserve the selected-background immutable snapshot and SHA verification in
  formal generation.

## Accepted Reliability Findings

- A successful provider HTTP response can still be shown as a provider failure
  when private-media materialization throws and the frontend catch block labels
  every exception `ProviderError`.
- Provider generation, private-media loading, storage, timeout and stale-menu
  errors need distinct stable codes and retryability.
- One style failure must not overwrite successful sibling styles, and responses
  from an older menu must not mutate the current menu state.

This root cause had already been independently reproduced and fixed before the
Pro report arrived. Render commit `d6cbf15` was verified in a real browser with
six background cards, six Blob images and no console errors.

The suggested fallback that returns the original protected private-media URL
after a fetch failure was rejected. It can preserve an unusable URL and blur an
authorization failure. The implemented behavior reports `MediaLoadError`
separately and keeps the provider result classification truthful.

## Accepted Visual Findings

- Generate 1024x768 4:3 empty photographic sets.
- Use a natural light camera perspective near 25 degrees downward for stable
  foreground placement.
- Reserve a clear center/lower placement area of at least 60% width and 50%
  height.
- Require flat surfaces that reach the frame edges, a single coherent primary
  light direction, and no phantom or unsupported shadows.
- Reject food, drinks, containers, tableware, text, people, plants, cloth,
  trays, boards, podiums, platforms, frames, blurred outer frames, color-card
  appearance and duplicate styles.

The visual review's numeric Laplacian, pHash, hue and detector thresholds are
unvalidated suggestions. They must be calibrated against real accepted and
rejected images before becoming automated approval gates.

## Rejected Or Incomplete Pro Output

- The visual response did not deliver the requested complete 40-category YAML;
  it listed 25 renamed categories and ended with a placeholder.
- Its recommendation to remove category information from every prompt conflicts
  with the product requirement that each catalog category have a suitable
  palette and material language. The code keeps category-specific color,
  material and light profiles while excluding food and prop nouns.
- The architecture review proposed broader worker, database, manifest pointer,
  combo-v2 and API replacements. These are useful roadmap items, not a minimal
  patch for the current background-catalog pilot and were not copied blindly.
- Pro-reported tests were run against the older sanitized package and are not
  acceptance evidence for the current branch. Codex reran the current source
  independently.

## Codex Resolution

- Current catalog remains exactly 40 categories x 6 fixed slots = 240 assets.
- Prompt/pipeline is now `style-background.v9`.
- The test deployment supports PostgreSQL manifests and a strict private-COS
  manifest backend; pending, missing, duplicate or tampered manifests fail
  closed.
- The next paid step is only the six-image `light_food` pilot. The other 234
  images remain blocked until those six pass real visual review.
