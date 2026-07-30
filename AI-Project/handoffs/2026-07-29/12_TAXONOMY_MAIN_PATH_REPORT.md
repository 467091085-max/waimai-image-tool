# Menu Taxonomy Main-Path Report

Date: 2026-07-30

## Scope

- Worktree: `/Users/guiguixiaxia/.codex/worktrees/de51/waimai-image-tool`
- User menu directory: `/Users/guiguixiaxia/Documents/menus`
- Code scope: `menu_parser.py`, `matching_engine.py`, and the permitted parser/matcher tests only.
- No changes to `app.py`, database code, frontend code, providers, user menus, deployment, Git history, or remotes.

## Result

PASS: the 40-leaf taxonomy now participates in the real customer matching path through the shared `matching_engine.similarity` entrypoint.

PASS: the parser and the customer wrapper now produce the same `kind` for every row in the 24 discovered menu files: 3,038 rows checked, 0 mismatches.

PASS: combo recognition is fail-closed. Punctuation, ingredient parentheses, vertical-bar marketing text, slash choices, and a combo section name alone do not force a combo. Exact multi-product structure remains supported.

PASS: a combo library candidate must have the exact normalized component fingerprint. Rice and optional-drink components are no longer discarded from that fingerprint.

## Real Call Path

1. `POST /api/upload-menu` saves the uploaded copy and calls `app.parse_menu(target)` at `app.py:11790-11804`.
2. `app.parse_menu` delegates Excel parsing to `menu_parser.parse_menu` at `app.py:3155-3162`.
3. `menu_parser._parse_candidate` writes `taxonomy`, `taxonomyLabel`, `taxonomyVersion`, `kind`, and `components` for every row at `menu_parser.py:358-398`.
4. `menu_parser.parse_menu` returns top-level `taxonomyVersion` and `taxonomyCounts` at `menu_parser.py:446-501`; the upload response preserves these non-item fields.
5. The customer plan does not call `match_menu_to_library`. `build_plan` calls `top_candidates` at `app.py:5746-5766`.
6. `top_candidates` calls the app wrapper `similarity` at `app.py:3267-3273`, and that wrapper delegates to `matching_engine.similarity`.
7. `matching_engine.similarity` now rejects cross-kind and cross-taxonomy candidates before fuzzy scoring at `matching_engine.py:425-455`.
8. The standalone `match_menu_to_library` path also consumes the parser-provided taxonomy and applies the same conservative candidate boundaries at `matching_engine.py:708-760`.

## Root Cause

1. The 40-category taxonomy existed in `matching_engine.py`, and parsed rows carried taxonomy fields, but the customer plan bypassed `match_menu_to_library`. Its actual candidate path only used the old fuzzy `similarity` score, so leaf taxonomy was not a mandatory reuse boundary.
2. The parser initially classified `kind` from raw Excel category/attribute text. The customer wrapper then classified the same row again from cleaned components. This produced unstable results for the same row.
3. Combo inference treated commas, slashes, vertical bars, `#`, and parenthetical ingredient descriptions as sufficient multi-item evidence. Marketing copy and flavor choices therefore became false combos.
4. Combo fingerprints dropped rice and optional-drink components, allowing an incomplete combo asset to look complete.

## Minimal Patch

- Kept exactly 40 stable leaf taxonomy IDs and advanced the rule version from `2026-07-30.v1` to `2026-07-30.v2`.
- Added a small set of explicit, food-specific keywords. No catch-all fallback was added.
- Added top-level `taxonomyVersion` and `taxonomyCounts` to parsed menu output and menu audits.
- Made parser `kind` use the same cleaned components that the customer wrapper receives.
- Added a taxonomy/kind gate to the shared similarity function, with a bounded name-profile cache to avoid repeated classification cost.
- Unknown names only fuzzy-match when their normalized names are exactly identical.
- Combo detection now accepts explicit combo words, clear top-level `+` structures, repeated `A加B加C`, or explicit inclusion/赠送 structures.
- A two-item choice such as `牛肉面+酸辣粉二选一` remains a single choice, while a three-part package such as `主食自选+赠小食+赠饮品` remains a combo.
- Combo product candidates require the same order-independent component fingerprint. Component candidates remain composition inputs and never become complete combo candidates.

## Taxonomy Definition And Distribution

The leaf count remains 40. `combo` and `unknown` are control states, not additional leaf categories.

The before snapshot was captured from taxonomy v1 immediately before this patch. The after snapshot was captured through the real `app.parse_menu(path)` customer wrapper with taxonomy v2.

| Taxonomy ID | Label | Before | After | Delta |
| --- | --- | ---: | ---: | ---: |
| `topped_rice` | 盖饭/盖码饭 | 116 | 112 | -4 |
| `mixed_rice` | 炒饭/拌饭 | 47 | 49 | +2 |
| `porridge_soup_rice` | 粥/汤饭 | 152 | 152 | 0 |
| `rice_noodles` | 米粉/米线 | 116 | 133 | +17 |
| `wheat_noodles` | 面食 | 103 | 104 | +1 |
| `dumpling_wonton` | 饺子/馄饨 | 91 | 99 | +8 |
| `buns_dim_sum` | 包子/点心 | 41 | 70 | +29 |
| `chinese_wraps` | 中式卷饼 | 7 | 13 | +6 |
| `malatang_maocai` | 麻辣烫/冒菜 | 9 | 9 | 0 |
| `hotpot_skewers` | 火锅/串串 | 2 | 2 | 0 |
| `barbecue` | 烧烤 | 19 | 59 | +40 |
| `fried_chicken` | 炸鸡 | 19 | 27 | +8 |
| `burger_hotdog` | 汉堡/热狗 | 5 | 9 | +4 |
| `pizza` | 披萨 | 0 | 0 | 0 |
| `sandwich_bagel` | 三明治/贝果 | 15 | 16 | +1 |
| `light_food` | 轻食/沙拉 | 93 | 92 | -1 |
| `pasta_steak` | 意面/牛排 | 13 | 14 | +1 |
| `japanese` | 日料 | 16 | 16 | 0 |
| `korean` | 韩餐 | 14 | 11 | -3 |
| `southeast_asian` | 东南亚菜 | 26 | 27 | +1 |
| `sichuan_hunan` | 川湘菜 | 28 | 22 | -6 |
| `cantonese_roast` | 粤式烧味 | 10 | 9 | -1 |
| `jiangzhe` | 江浙菜 | 0 | 0 | 0 |
| `northeast_chinese` | 东北菜 | 7 | 8 | +1 |
| `northwest_xinjiang` | 西北/新疆菜 | 3 | 3 | 0 |
| `northern_lu` | 北方/鲁菜 | 0 | 0 | 0 |
| `fujian_taiwan` | 闽台菜 | 2 | 2 | 0 |
| `home_stir_fry` | 家常小炒 | 64 | 111 | +47 |
| `fish_seafood` | 鱼/海鲜 | 29 | 66 | +37 |
| `beef_lamb_pot` | 牛羊锅 | 3 | 3 | 0 |
| `braised_cooked_food` | 卤味/熟食/凉菜 | 27 | 57 | +30 |
| `soup_stew` | 汤羹/炖品 | 74 | 75 | +1 |
| `steamed_claypot` | 蒸菜/煲仔 | 17 | 17 | 0 |
| `milk_fruit_tea` | 奶茶/果茶 | 4 | 5 | +1 |
| `coffee_cocoa` | 咖啡/可可 | 0 | 0 | 0 |
| `bottled_drinks` | 瓶装/酒水饮料 | 56 | 68 | +12 |
| `fresh_drinks` | 鲜榨饮品 | 33 | 40 | +7 |
| `dessert_bakery` | 甜品/烘焙 | 7 | 16 | +9 |
| `fried_snacks` | 炸物小食 | 37 | 57 | +20 |
| `fruit` | 水果/果切 | 0 | 0 | 0 |
| `combo` | 套餐/组合控制态 | 782 | 516 | -266 |
| `unknown` | 未知/需生成控制态 | 951 | 949 | -2 |
| **Total** |  | **3,038** | **3,038** | **0** |

`unknown` changed from 951/3,038 (31.30%) to 949/3,038 (31.24%). This is coverage, not accuracy. The small net change is expected: explicit keywords moved some known dishes into leaves, while false punctuation combos moved back to `unknown` instead of being guessed.

The sample contains no observed `pizza`, `jiangzhe`, `northern_lu`, `coffee_cocoa`, or `fruit` rows after parsing. A zero in this dataset is not evidence that those rules are broken.

## Required Representative Tests

- 饭: `土豆牛腩盖饭`
- 面: `兰州牛肉拉面`
- 粉: `柳州螺蛳粉`
- 粥: `皮蛋瘦肉粥`
- 小吃: `香酥薯条`
- 饮品: `冰镇可乐`
- 烧烤: `炭烤牛肉串`
- 火锅: `重庆牛油小火锅`
- 套餐: `牛肉面+可乐套餐`
- 套餐歧义: ingredient parentheses, slash choice, vertical-bar text, ampersand ingredients, marketing parentheses, category-only combo labels, two-item choices, and three-part packages.
- Conservative combo reuse: missing rice or optional-drink components forces generation.

## Verification

- Parser/matcher and customer-path regression: 94 tests passed.
- Real menus: 24 files discovered, 24 parsed, 0 failed, 3,038 rows.
- Parser/customer `kind` equality: 3,038 checked, 0 mismatches.
- Customer similarity gate: rice-vs-noodle, burger-vs-rice, drink-vs-snack, and combo-vs-single all scored `0.0`; canonical tomato-egg aliases scored `1.0`.
- User-menu integrity: SHA-256, size, and nanosecond mtime snapshots were identical before and after validation for all 24 files; 0 files changed.
- Python compilation and `git diff --check`: passed.

## Limits

- No labeled ground-truth image set was supplied, so these results do not claim image-match accuracy.
- `unknown` intentionally remains fail-closed and should route to generation rather than an unrelated reusable image.
- This patch does not address menu-row food/non-food filtering, background generation, providers, storage, billing, or UI.
- No commit, push, deployment, database migration, or user-menu write was performed.
