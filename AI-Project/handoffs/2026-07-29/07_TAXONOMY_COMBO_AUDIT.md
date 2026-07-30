# Taxonomy, Matching, and Combo Audit

## Status

- Excel parsing: partial pass. All 24 local workbooks parsed, 3,036 rows, zero parser failures.
- Approximately 40 useful product categories: fail. Current runtime has five menu-level rules and three category-specific background prompt families.
- Similar-name recognition: fail. Only three explicit aliases exist and fuzzy matching has known shape/category collisions.
- Drinks/snacks/desserts/sides: fail. They share one coarse type and runtime reclassification can change parser output.
- Combo recognition and generation: fail. Quantity, role, required/optional choice groups, and component-level visual verification are absent.
- Per-product category background mapping: fail. Current category is primarily menu-level.

## Measured Gaps

- The second classifier in `app.py` changed 694 of 3,036 parser classifications (22.9%).
- The audit observed 799 runtime combos; 66 had fewer than two parsed components.
- 526 non-combo rows still carried two or more component-like fragments.
- Only 13 of 24 menus mapped to an existing category-specific prompt family; nine mapped to a category without a dedicated prompt and two remained uncertain.
- A first-pass 40-category keyword scan covered only 60.5%, produced 19.9% multi-category collisions, and missed 39.5%. It is a labeling starting point, not a production classifier.

## Root Causes

1. `menu_parser.py` and `app.py` use different kind classifiers; the latter discards Excel category context.
2. Alias normalization is too small for common Chinese dish variants.
3. Shape/staple constraints are incomplete, allowing collisions such as noodle/rice, burger/rice, or drink-name ingredients.
4. Combo components are unstructured strings without role, quantity, required state, or choice group.
5. Component matches can enter candidate pools intended for complete product images.
6. One menu category cannot correctly select backgrounds for mixed-category menus.

## Target Data Model

```yaml
item:
  kind: single|combo|drink|snack|dessert|side
  primary_category_id: string
  cuisine_tags: []
  ingredient_tags: []
  cooking_method_tags: []
  confidence: 0.0
alias:
  canonical_id: string
  positive_aliases: []
  required_tokens: []
  forbidden_tokens: []
  category_scope: []
  version: string
component:
  canonical_id: string
  role: main|staple|side|drink|sauce|gift
  quantity: 1
  unit: string
  required: true
  choice_group: null
```

## Candidate 40-Category Topology

1. topped rice, mixed rice, porridge/soup rice, rice noodles
2. wheat noodles, dumpling/wonton, buns/dim sum, Chinese wraps
3. malatang/maocai, hotpot/skewers, barbecue, fried chicken
4. burger/hotdog, pizza, sandwich/bagel, light food
5. pasta/steak, Japanese, Korean, Southeast Asian
6. Sichuan/Hunan, Cantonese roast, Jiangsu/Zhejiang, Northeast Chinese
7. Northwest/Xinjiang, Northern/Lu, Fujian/Taiwan, home stir-fry
8. fish/seafood, beef/lamb pot, braised/cooked food, soup/stew
9. steamed/claypot, milk/fruit tea, coffee/cocoa, bottled drinks
10. fresh drinks, dessert/bakery, fried snacks, fruit/cut fruit

Each leaf category requires a versioned background profile containing six photographic scene variants.

## Combo Hard Rules

- A combo may reuse only a `kind=combo` asset with an identical structured component fingerprint.
- Component matches are composition inputs, never complete combo candidates.
- Missing exact combo assets must trigger generation/composition from all required components.
- Cache keys include normalized components, quantity, choice groups, background SHA, prompt/model version, and quality.
- Verification must confirm required components, reject extra products, and reject pixel-identical reuse of any single-product asset.

## Implementation Order

1. Add versioned taxonomy, aliases, and background profiles as data files.
2. Replace the two kind classifiers with one shared recognition service.
3. Parse component role, quantity, required/optional state, and choice groups.
4. Match in order: exact alias, category/shape constraints, fuzzy candidate, otherwise generate.
5. Enforce combo no-single-product reuse before provider calls and cache writes.
6. Classify per product and choose six backgrounds from that product/category profile.
7. Run a private, manually labeled golden set before enabling automatic reuse and charging.

## Acceptance Boundary

Recommended gates are automatic-reuse precision at least 99.5%, combo recall at least 99%, zero single-product reuse for combos, and 100% background-profile coverage. The 240 visual category/background combinations require real generation plus human or configured vision-model review; unit tests alone cannot prove them.

No customer menu content is included in this report. The audit did not modify, upload, commit, push, or deploy code.
