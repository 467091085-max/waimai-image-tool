# 40 x 6 Background Catalog Contract

## Scope

- Catalog version: `background-catalog.v1`
- Taxonomy version: `2026-07-30.v2`
- Prompt/pipeline version: `style-background.v11`
- Total target: 40 categories x 6 fixed slots = 240 approved assets
- `mixed` is a review state, not a 41st catalog category.
- Customer Web reads a complete approved six-slot category manifest only. It
  never fills missing catalog slots with synchronous generation or color
  placeholders.

## Categories

| # | Taxonomy ID | Customer label |
|---:|---|---|
| 1 | `topped_rice` | 盖饭/盖码饭 |
| 2 | `mixed_rice` | 炒饭/拌饭 |
| 3 | `porridge_soup_rice` | 粥/汤饭 |
| 4 | `rice_noodles` | 米粉/米线 |
| 5 | `wheat_noodles` | 面食 |
| 6 | `dumpling_wonton` | 饺子/馄饨 |
| 7 | `buns_dim_sum` | 包子/点心 |
| 8 | `chinese_wraps` | 中式卷饼 |
| 9 | `malatang_maocai` | 麻辣烫/冒菜 |
| 10 | `hotpot_skewers` | 火锅/串串 |
| 11 | `barbecue` | 烧烤 |
| 12 | `fried_chicken` | 炸鸡 |
| 13 | `burger_hotdog` | 汉堡/热狗 |
| 14 | `pizza` | 披萨 |
| 15 | `sandwich_bagel` | 三明治/贝果 |
| 16 | `light_food` | 轻食/沙拉 |
| 17 | `pasta_steak` | 意面/牛排 |
| 18 | `japanese` | 日料 |
| 19 | `korean` | 韩餐 |
| 20 | `southeast_asian` | 东南亚菜 |
| 21 | `sichuan_hunan` | 川湘菜 |
| 22 | `cantonese_roast` | 粤式烧味 |
| 23 | `jiangzhe` | 江浙菜 |
| 24 | `northeast_chinese` | 东北菜 |
| 25 | `northwest_xinjiang` | 西北/新疆菜 |
| 26 | `northern_lu` | 北方/鲁菜 |
| 27 | `fujian_taiwan` | 闽台菜 |
| 28 | `home_stir_fry` | 家常小炒 |
| 29 | `fish_seafood` | 鱼/海鲜 |
| 30 | `beef_lamb_pot` | 牛羊锅 |
| 31 | `braised_cooked_food` | 卤味/熟食/凉菜 |
| 32 | `soup_stew` | 汤羹/炖品 |
| 33 | `steamed_claypot` | 蒸菜/煲仔 |
| 34 | `milk_fruit_tea` | 奶茶/果茶 |
| 35 | `coffee_cocoa` | 咖啡/可可 |
| 36 | `bottled_drinks` | 瓶装/酒水饮料 |
| 37 | `fresh_drinks` | 鲜榨饮品 |
| 38 | `dessert_bakery` | 甜品/烘焙 |
| 39 | `fried_snacks` | 炸物小食 |
| 40 | `fruit` | 水果/果切 |

## Fixed Style Slots

| Style ID | Slot ID | Meaning |
|---|---|---|
| `style-1` | `solid-warm` | 暖色无缝纯色棚拍 |
| `style-2` | `solid-cool` | 冷色无缝纯色棚拍 |
| `style-3` | `table-light-stone` | 明亮浅石桌面 |
| `style-4` | `table-dark-stone` | 深色石材桌面 |
| `style-5` | `table-natural-wood` | 温暖天然木桌 |
| `style-6` | `table-commercial-neutral` | 冷中性商业桌面 |

All six backgrounds are 4:3 empty commercial-photo sets. Table surfaces must
be flat and extend to the left, right, and lower frame edges. Food, drinks,
plants, flowers, tableware, cloth, boards, trays, podiums, plinths, platforms,
steps, frames, small center images, text, logos, watermarks, people, and hands
are rejection conditions.

## Storage And Review

Immutable asset key:

```text
ai-assets/waimai-shared/background-catalog/background-catalog.v1/
  {taxonomyVersion}/{categoryId}/{styleId}/{promptVersion}/
  prompt-{promptSha16}/{assetSha256}.jpg
```

Each generated image is uploaded with a SHA-256 read-back check and registered
as `pending`. Generation never auto-approves an asset. A category is customer
ready only when exactly one approved record exists for every one of its six
slots. Missing or duplicate approved records fail the category closed.

The catalog can read approval state from PostgreSQL or, for the current Render
test service that has private COS but no PostgreSQL, from the category manifest
stored at the versioned COS manifest key. The COS backend also verifies the
current prompt SHA, immutable object key, image SHA, file size, category, style,
taxonomy, and top-level approval before exposing any asset.

Each category is now its own paid-work checkpoint. The pending six-slot
manifest is written immediately after style 6, and a resumed run re-reads and
hashes all six COS objects before skipping provider calls. A SHA-addressed 3 x 2
review contact sheet is stored beside the manifest. Neither provider success,
remote resume, nor the contact sheet grants approval.

## Operator Sequence

1. Confirm the exact plan without spending provider credits:

   ```bash
   python scripts/build_background_catalog.py
   ```

2. Generate only the light-food pilot in the staging environment:

   ```bash
   python scripts/build_background_catalog.py \
     --category light_food \
     --execute \
     --upload-pending
   ```

   When PostgreSQL is available, the same run can additionally register the
   pending records:

   ```bash
   python scripts/build_background_catalog.py \
     --category light_food \
     --execute \
     --register-pending \
     --actor-user-id background-catalog-builder
   ```

3. Review all six pilot images. Reject any prop, raised platform, color-card
   appearance, blurred outer frame, incorrect aspect ratio, unsafe center area,
   duplicate composition, or low-resolution output.
4. Approve exactly one asset per pilot slot by locking all six reviewed hashes:

   ```bash
   python scripts/review_background_catalog.py \
     --category light_food \
     --reviewer catalog-visual-review \
     --expected-sha style-1=2c8a2644609a73191f0b605210a8defcc87d9954f6f08fc3b423e7d189d02673 \
     --expected-sha style-2=7d4ebf83b7b39606393284ca70c9f9ef40a6654a32ef1b7c7463aa3c17de2cb4 \
     --expected-sha style-3=d0af5a7981b5baaf6433f2ec8ade9a04efad21ee9d2b192d15e8a71cce12bc20 \
     --expected-sha style-4=198c0a8c167958dd84b2810fa9d9cac3d2fa937bf2a65de75fdac7c38206c2b5 \
     --expected-sha style-5=7bf3d33ceb883e4a01ebd338ee200b7d4b2a9a769b8c6ab6e328fec21bbc657a \
     --expected-sha style-6=61516e0d3645c0f01a834273d99cf26edc33a390c6ef12b08ecec64331c7e3e6 \
     --approve-reviewed
   ```

5. Verify a real Excel upload,
   six-background retrieval, free samples, selected-background identity, formal
   generation, export, private-media authorization, and billing behavior.
6. Generate and review the remaining 39 categories only after the pilot passes.

## Verified Local Evidence

- Exact dry plan: 40 categories, 6 slots, 240 assets.
- Builder/catalog focused tests after v10 hardening: 61 passed, 1 skipped.
- Full default regression after v10 hardening: 1304 passed, 20 skipped.
- Real local Excel classification: 24/24 workbooks resolved to explicit leaf
  taxonomies, with zero `mixed` results after the store/menu conflict gate.
- A real `style-background.v9` `light_food` pilot generated six assets and
  uploaded them to private COS with SHA-256 read-back. Visual review rejected
  the set: styles 1, 2, and 4 contain raised plinths; style 5 contains a white
  rectangular mat; only styles 3 and 6 are usable. All six remain `pending`.
- The paid v10 pilot generated and uploaded 6/6 assets but was rejected: prompt
  rewriting turned one solid slot into overlapping paper sheets and exposed
  table-front slabs in multiple table slots. All remain pending.
- Prompt v11 sends Hunyuan 3.0 `Revise=0` plus a deterministic positive seed
  per category/style, keeps the Lite negative-prompt field off the v3 request,
  and removes paper wording and multi-color palettes from solid slots. No v11
  image is visually approved yet, so the remaining 39-category batch remains
  intentionally gated.
- Prompt v11 focused verification passed `91 passed, 1 skipped`; full default
  regression passed `1306 passed, 20 skipped`; scoped Python compilation and
  `git diff --check` passed.
- The exact v11 light-food set generated 6/6, uploaded with matching SHA-256,
  and passed manual visual review: two empty single-color seamless spaces plus
  four ordinary full-size dining tables, with no props, mats, paper rolls,
  small product plinths, inset images, or blurred frames.
- Remote resume, immediate category checkpoint, review contact-sheet, changed
  object rejection, and hash-locked approval verification passed `98 passed, 1
  skipped`; full default regression passed `1313 passed, 20 skipped`.
