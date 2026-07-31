# Render Image Staging Acceptance

## Scope

- Service: `waimai-image-tool-1`
- URL: `https://waimai-image-tool-1.onrender.com`
- Branch: `codex/render-image-staging`
- Accepted code commit: `37429f9844ddd4c3ea3d098e2a07d8bf09b5fda7`
- Production service and `main`: not changed
- Database migrations: not run

## Real Inputs

- Excel: `/Users/guiguixiaxia/Documents/menus/运营数据_蔬适圈·中式轻食健康餐（万达店）.xlsx`
- Parsed rows: 56
- Parse errors: 0
- Taxonomy examples: `light_food=19`, `topped_rice=15`, `unknown=9`

## External Readiness

- Tencent TokenHub `hy-image-v3.0`: ready
- Tencent COS private object storage: ready
- COS bucket: `waimai-image-tool-inputs-1311836560`, `ap-guangzhou`
- Gemini image refinement: not configured
- Redis/PostgreSQL product-worker topology: not configured on this isolated demo service
- WeChat Pay merchant integration: intentionally deferred; no fake live payment was exposed

## Background Acceptance

Commit `111c99f` was exercised with the frontend-equivalent two-request shape.
All six paid TokenHub requests succeeded without the prior provider-quota race,
and every downloaded SHA-256 matched API metadata. Visual review found no food,
text, watermark, or placeholder color block.

Pipeline v4 then regenerated and selected style 3:

- Asset ID: `bg_ab9aa9306e174f2ec9f778f7`
- SHA-256: `15b4559ae86a5d6c0605362e8e88cdd0e0583f7acd612ea45933c682546a8f86`
- Size: 1024 x 768
- Provider duration: 21.34 seconds
- Visual result: empty white/green studio surface, accepted

## Six-Sample Acceptance

All six outputs used exact-background pipeline v4 and the selected background
SHA above. Download SHA, selected-background identity, and persisted output
background verification passed for every item.

| # | Dish | Seconds | Mask path | Output SHA-256 |
|---|---|---:|---|---|
| 0 | 黑椒牛排能量碗 | 48.31 | local chroma | `871b70e43df9bbf95d93bc09bbe0b9a19245ec299d0a543968ac348278988804` |
| 1 | 煎蛋 | 46.15 | local chroma | `1500b5f373878f364dc3b04761a2383cf776a3f683cca67a7294df9e32661a8d` |
| 2 | 西蓝花加料 | 59.80 | one Aiart fallback (`chroma_spill_too_large`) | `c7801da93a974b2023a6f8a5e788e23b88046570074445f71a8fd1d3058476c9` |
| 3 | 奥尔良鸡胸沙拉 | 47.71 | local chroma | `661e506d587b67c4a33e83948cbd44d044c837430fdcd62c49de75900f5c5d92` |
| 4 | 奥尔良鸡胸沙拉 | 43.42 | local chroma | `ed0fc0dcd4450f1926af65d9485a075e544ac8c58a6d20ff53e700ef02fcbbc4` |
| 5 | 牛肉菌菇杂粮饭 | 47.71 | local chroma | `2345e662bb4724f97b067980fa853f84a0cf712194ccb01cbfd8509c421886b1` |

Final visual review: 6/6 accepted, with no cyan halo, erroneous transparency,
detached fragment, background remnant, border frame, or inset-card composition.

## Formal Probe

`POST /api/generate-final` returned HTTP 200 in 19.66 seconds. The first formal
row reused the verified foreground/mask cache and returned a pipeline-v4 exact
background output. This proves the formal compositor route, not a full-store
production batch.

The staging environment currently has `TENCENT_HUNYUAN_SYNC_LIMIT=1`; therefore
the probe truthfully reported `1 succeeded / 55 limited`. Full 56-row paid
formal generation is not accepted by this report.

## Local Verification

- Focused provider/chroma/background tests: `51 passed`
- Full regression: `1283 passed, 20 skipped`
- Python compilation: passed
- `git diff --check`: passed
- Browser smoke: page title and full UI rendered; top four circular steps are
  present; prices are 10/20 points; no `已接入真实图库` or `混元未配置` copy is shown

## ChatGPT Pro Review

- Conversation: `https://chatgpt.com/c/6a6cee10-d528-83e8-bf71-cedee173cc86`
- Review result: no P0 blocking this isolated staging run
- Codex independently rejected one v3 false-success cyan halo, calibrated the
  threshold from real evidence, deployed pipeline v4, and reran the six-image
  acceptance before recording this report
