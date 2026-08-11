from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any


DIRECT_SCORE = 70.0
REVIEW_SCORE = 45.0
DEFAULT_MIN_SCORE = REVIEW_SCORE / 100
TAXONOMY_VERSION = "2026-07-30.v2"
TAXONOMY_UNKNOWN = "unknown"
TAXONOMY_COMBO = "combo"

# Stable, ordered leaf taxonomy. Longer, more specific keyword matches win.
TAXONOMY_RULES = (
    ("topped_rice", "盖饭/盖码饭", ("盖码饭", "盖浇饭", "木桶饭", "卤肉饭", "鸡腿饭", "牛肉饭", "咖喱饭", "盖饭")),
    ("mixed_rice", "炒饭/拌饭", ("石锅拌饭", "扬州炒饭", "蛋炒饭", "炒饭", "拌饭", "烤肉饭")),
    ("porridge_soup_rice", "粥/汤饭", ("艇仔粥", "砂锅粥", "汤饭", "泡饭", "稀饭", "粥")),
    ("rice_noodles", "米粉/米线", ("螺蛳粉", "酸辣粉", "土豆粉", "红薯粉", "米线", "米粉", "河粉", "粉丝", "粉条")),
    ("wheat_noodles", "面食", ("刀削面", "热干面", "炸酱面", "担担面", "荞麦面", "拉面", "汤面", "拌面", "炒面", "面条")),
    ("dumpling_wonton", "饺子/馄饨", ("锅贴", "馄饨", "云吞", "抄手", "水饺", "蒸饺", "煎饺", "虾饺", "饺子")),
    ("buns_dim_sum", "包子/点心", ("小笼包", "灌汤包", "生煎包", "烧卖", "烧麦", "肠粉", "包子", "馒头", "粽子", "点心")),
    ("chinese_wraps", "中式卷饼", ("煎饼果子", "肉夹馍", "手抓饼", "鸡蛋灌饼", "鸡蛋饼", "葱花饼", "卷饼", "春饼")),
    ("malatang_maocai", "麻辣烫/冒菜", ("麻辣烫", "冒菜", "麻辣拌", "钵钵鸡")),
    ("hotpot_skewers", "火锅/串串", ("串串香", "小火锅", "涮羊肉", "火锅", "串串", "锅底")),
    ("barbecue", "烧烤", ("烤羊肉串", "牛肉串", "羊肉串", "烤五花肉", "烤生蚝", "火山石烤肠", "烤肠", "烧烤", "烤串", "炭烤", "烤肉")),
    ("fried_chicken", "炸鸡", ("韩式炸鸡", "炸鸡桶", "炸鸡", "鸡排", "鸡米花", "鸡块", "翅根")),
    ("burger_hotdog", "汉堡/热狗", ("芝士汉堡", "鸡腿堡", "牛肉堡", "汉堡", "热狗")),
    ("pizza", "披萨", ("比萨饼", "比萨", "披萨")),
    ("sandwich_bagel", "三明治/贝果", ("帕尼尼", "三明治", "贝果", "吐司")),
    ("light_food", "轻食/沙拉", ("波奇饭", "健康碗", "能量碗", "轻食", "沙拉", "鸡胸餐", "减脂餐")),
    ("pasta_steak", "意面/牛排", ("意大利面", "通心粉", "意面", "牛排", "西餐")),
    ("japanese", "日料", ("鳗鱼饭", "亲子丼", "天妇罗", "寿喜烧", "刺身", "寿司", "乌冬", "照烧", "日式")),
    ("korean", "韩餐", ("石锅拌饭", "部队锅", "辣炒年糕", "泡菜汤", "韩式", "韩国")),
    ("southeast_asian", "东南亚菜", ("冬阴功", "海南鸡饭", "越南河粉", "泰式", "越南", "东南亚", "咖喱鸡")),
    ("sichuan_hunan", "川湘菜", ("剁椒鱼头", "辣椒炒肉", "小炒黄牛肉", "水煮鱼", "回锅肉", "鱼香肉丝", "川菜", "湘菜")),
    ("cantonese_roast", "粤式烧味", ("烧鹅", "烧鸭", "叉烧", "白切鸡", "豉油鸡", "烧腊", "粤式")),
    ("jiangzhe", "江浙菜", ("东坡肉", "西湖醋鱼", "龙井虾仁", "油焖笋", "江浙", "杭帮菜", "本帮菜")),
    ("northeast_chinese", "东北菜", ("锅包肉", "地三鲜", "猪肉炖粉条", "小鸡炖蘑菇", "东北菜", "东北")),
    ("northwest_xinjiang", "西北/新疆菜", ("大盘鸡", "新疆炒米粉", "羊肉串", "手抓饭", "肉夹馍", "西北", "新疆")),
    ("northern_lu", "北方/鲁菜", ("九转大肠", "糖醋鲤鱼", "葱烧海参", "鲁菜", "山东菜", "京酱肉丝")),
    ("fujian_taiwan", "闽台菜", ("沙茶面", "蚵仔煎", "卤肉饭", "佛跳墙", "姜母鸭", "闽南", "台湾")),
    ("home_stir_fry", "家常小炒", ("番茄炒蛋", "辣椒炒肉", "小炒黄牛肉", "麻婆豆腐", "宫保鸡丁", "青椒肉丝", "酸辣土豆丝", "糖醋里脊", "干锅花菜", "干锅土豆片", "豆角炒茄子", "小炒肉", "炒牛肉", "炒鸡", "家常菜", "小炒", "炒蛋", "炒肉")),
    ("fish_seafood", "鱼/海鲜", ("酸菜鱼", "烤鱼", "水煮鱼", "剁椒鱼头", "清蒸鲈鱼", "白灼虾", "香辣大虾", "小龙虾", "龙虾", "鱿鱼", "海鲜", "生蚝", "螃蟹", "鱼片")),
    ("beef_lamb_pot", "牛羊锅", ("牛杂煲", "羊蝎子", "牛腩煲", "羊肉煲", "牛肉锅", "羊肉锅")),
    ("braised_cooked_food", "卤味/熟食/凉菜", ("夫妻肺片", "红油耳片", "蒜泥白肉", "凉拌皮蛋", "冷吃兔", "冷吃牛肉", "卤味", "卤鸡", "卤鸭", "卤牛肉", "猪蹄", "鸭脖", "盐水鸭", "熟食", "凉菜")),
    ("soup_stew", "汤羹/炖品", ("佛跳墙", "鸡汤", "排骨汤", "蛋花汤", "老火汤", "炖汤", "羹", "汤")),
    ("steamed_claypot", "蒸菜/煲仔", ("煲仔饭", "蒸排骨", "蒸鸡", "蒸鱼", "蒸菜", "砂锅", "煲仔")),
    ("milk_fruit_tea", "奶茶/果茶", ("杨枝甘露", "水果茶", "柠檬茶", "奶盖茶", "奶茶", "珍珠奶茶", "果茶")),
    ("coffee_cocoa", "咖啡/可可", ("美式咖啡", "拿铁", "卡布奇诺", "摩卡", "咖啡", "可可")),
    ("bottled_drinks", "瓶装/酒水饮料", ("矿泉水", "纯净水", "冰红茶", "王老吉", "加多宝", "果粒橙", "红牛", "汽水", "啤酒", "白酒", "芬达", "雪碧", "可乐", "瓶装水")),
    ("fresh_drinks", "鲜榨饮品", ("鲜榨果汁", "手作果蔬汁", "酸奶昔", "椰子水", "鲜牛乳", "酸梅汤", "豆浆", "柠檬水", "果蔬汁", "果汁")),
    ("dessert_bakery", "甜品/烘焙", ("提拉米苏", "芝士蛋糕", "蛋挞", "红糖凉糕", "凉糕", "冰沙", "面包", "蛋糕", "甜品", "布丁", "酸奶杯")),
    ("fried_snacks", "炸物小食", ("炸薯条", "炸年糕", "炸春卷", "炸鸡柳", "炸鲜奶", "芝士球", "包浆豆腐", "洋葱圈", "薯条", "小酥肉", "小油条", "油条", "炸物", "小食")),
    ("fruit", "水果/果切", ("水果拼盘", "鲜果切", "果切", "水果", "西瓜", "哈密瓜")),
)
TAXONOMY_LABELS = {taxonomy_id: label for taxonomy_id, label, _ in TAXONOMY_RULES}
TAXONOMY_LABELS.update({TAXONOMY_COMBO: "套餐/组合", TAXONOMY_UNKNOWN: "未知/需生成"})

MARKETING_WORDS = (
    "招牌",
    "爆款",
    "热销",
    "人气",
    "福利",
    "特惠",
    "优惠",
    "新品",
    "必点",
    "现炒",
    "现煎",
    "秘制",
    "正宗",
    "经典",
    "老长沙",
    "限量",
    "推荐",
    "店长推荐",
)

CANONICAL_REPLACEMENTS = (
    ("西红柿", "番茄"),
    ("马铃薯", "土豆"),
    ("洋芋", "土豆"),
    ("肉沫", "肉末"),
    ("宫爆", "宫保"),
    ("紫菜鸡蛋汤", "紫菜蛋花汤"),
    ("炒鸡蛋", "炒蛋"),
    ("意大利面", "意面"),
    ("云吞", "馄饨"),
    ("抄手", "馄饨"),
    ("盖码饭", "盖饭"),
    ("盖浇饭", "盖饭"),
    ("木桶饭", "盖饭"),
    ("小炒肉", "炒肉"),
    ("农家一碗香", "一碗香"),
)

FORMAT_WORDS = (
    "单人餐",
    "双人餐",
    "三人餐",
    "多人餐",
    "家庭餐",
    "分享餐",
    "套餐",
    "组合",
    "双拼",
    "三拼",
    "四拼",
    "多拼",
)

COMPONENT_DROP_WORDS = (
    "单人餐",
    "双人餐",
    "三人餐",
    "多人餐",
    "家庭餐",
    "分享餐",
    "套餐",
    "组合",
)

GENERIC_COMPONENTS = {
    "主食",
    "餐具",
    "任选",
    "自选",
    "套餐内容",
    "组合内容",
    "海陆空",
    "全家福",
    "酱料",
    "口味",
    "豪华",
    "土豪",
}

DRINK_SNACK_WORDS = (
    "可乐",
    "雪碧",
    "芬达",
    "矿泉水",
    "纯净水",
    "王老吉",
    "冰红茶",
    "绿茶",
    "豆浆",
    "果汁",
    "奶茶",
    "酸梅汤",
    "饮品",
    "饮料",
    "小食",
    "小吃",
    "汤",
)

MAIN_FOOD_WORDS = (
    "盖饭",
    "盖码饭",
    "木桶饭",
    "拌饭",
    "炒饭",
    "汤饭",
    "米饭",
    "米粉",
    "米线",
    "面",
    "粥",
    "抄手",
    "饺",
    "包子",
)

COMBO_WORDS = (
    "套餐",
    "组合",
    "双拼",
    "三拼",
    "四拼",
    "多拼",
    "拼盘",
    "全家福",
    "全家桶",
    "套装",
    "大礼包",
    "定食套餐",
    "单人餐",
    "双人餐",
    "三人餐",
    "多人餐",
    "家庭餐",
    "分享餐",
)

CHOICE_WORDS = ("任选", "自选", "可选", "二选一", "三选一", "2选1", "3选1", "味由您定")
INCLUSION_WORDS = ("套餐内容", "组合内容", "包含", "含有", "内含", "搭配", "附赠", "赠送", "赠")
SNACK_TAXONOMIES = {
    "milk_fruit_tea",
    "coffee_cocoa",
    "bottled_drinks",
    "fresh_drinks",
    "dessert_bakery",
    "fried_snacks",
    "fruit",
}
SPLIT_RE = re.compile(r"[+＋#&＆/／、,，|丨;；]+|\s+(?:配|加|和|含)\s+")
PAREN_RE = re.compile(r"[（(]([^）)]{0,80})[）)]")
BRACKET_RE = re.compile(r"[【\[]([^】\]]{0,80})[】\]]")
TOP_LEVEL_PLUS_RE = re.compile(r"[+＋]")
CHOICE_RE = re.compile(r"(?:\d+|[一二三四五六七八九十]+)选(?:\d+|[一二三四五六七八九十]+)|(?:^|[^A-Za-z])or(?:[^A-Za-z]|$)|或者")
PEOPLE_MEAL_RE = re.compile(r"(?:\d+|[一二三四五六七八九十单双]+)人[^,，;；]{0,12}餐|\d+\s*件套")


def _taxonomy_signal_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"(粉丝|宠粉|关注)[^\s，,。；;]{0,6}福利", "福利", text)
    text = re.sub(r"(火锅|烧烤|咖喱|麻辣烫)味", "口味", text)
    return text


def normalize_dish(text: str) -> str:
    """Return a stable key for dish-name comparison."""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = re.sub(r"[【\[].*?[】\]]", "", text)
    text = re.sub(r"[（(][^）)]{0,40}[）)]", "", text)
    text = re.sub(r"\d+(\.\d+)?\s*(元|ml|毫升|l|克|g|kg|斤|个|只|份|瓶|罐|盒|两)", "", text)
    text = re.sub(r"(买一送一|第二份半价|限时|折扣|满减|赠|送)", "", text)
    text = re.sub(r"(单人|双人|三人|多人|家庭|分享)?套餐", "", text)
    for source, target in CANONICAL_REPLACEMENTS:
        text = text.replace(source, target)
    for word in MARKETING_WORDS + FORMAT_WORDS:
        text = text.replace(word, "")
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text).strip()


def grams(text: str) -> set[str]:
    norm = str(text or "")
    if not norm:
        return set()
    out = {norm}
    for size in (2, 3):
        if len(norm) >= size:
            out.update(norm[i : i + size] for i in range(len(norm) - size + 1))
    return out


def _clean_component_label(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"[【\[].*?[】\]]", "", text)
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"^\s*(含有?|内含|配|加|赠送?|另附|包含|搭配)[:：]?\s*", "", text)
    text = re.sub(r"\d+(\.\d+)?\s*(元|ml|毫升|l|克|g|kg|斤|个|只|份|瓶|罐|盒|两)", "", text)
    text = re.sub(r"\s*[xX×*]\s*\d+\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -_·:：")
    text = re.sub(r"(双拼|三拼|四拼|多拼).*$", "", text)
    text = re.sub(r"(单人|双人|三人|多人|家庭|分享)?套餐$", "", text)
    for word in COMPONENT_DROP_WORDS:
        text = text.replace(word, "")
    return text.strip(" -_·:：")


def split_components(name: str, attrs: str = "", category: str = "") -> list[str]:
    """Split combo/set-meal names into matchable dish components."""
    name_source = unicodedata.normalize("NFKC", str(name or ""))
    explicit_combo = any(word in name_source for word in COMBO_WORDS) or bool(PEOPLE_MEAL_RE.search(name_source))
    bracket_sources: list[str] = []
    parenthetical_sources: list[str] = []

    def keep_component_bracket(match: re.Match[str]) -> str:
        content = match.group(1)
        if explicit_combo and SPLIT_RE.search(content):
            bracket_sources.append(content)
        return " "

    def keep_component_parenthetical(match: re.Match[str]) -> str:
        content = match.group(1)
        if any(word in content for word in INCLUSION_WORDS) or (explicit_combo and SPLIT_RE.search(content)):
            parenthetical_sources.append(content)
        return " "

    name_source = BRACKET_RE.sub(keep_component_bracket, name_source)
    name_source = PAREN_RE.sub(keep_component_parenthetical, name_source)
    if bracket_sources:
        name_source = re.sub(
            r"^\s*[^+＋#&＆/／、,，|丨;；]{0,30}(?:双拼|三拼|四拼|多拼)\s*",
            "",
            name_source,
        )
    sources = [*bracket_sources, name_source, *parenthetical_sources]
    attrs_source = unicodedata.normalize("NFKC", str(attrs or ""))
    if attrs_source and any(word in attrs_source for word in INCLUSION_WORDS):
        sources.append(attrs_source)

    out: list[str] = []
    seen: set[str] = set()
    generic_norms = {normalize_dish(value) for value in GENERIC_COMPONENTS}
    for source in sources:
        use_broad_split = explicit_combo or any(word in source for word in INCLUSION_WORDS)
        source = re.sub(r"(套餐内容|组合内容|内容|包含|含有|内含)[:：]?", "+", source)
        source = re.sub(r"(搭配|附赠|赠送|另赠)[:：]?", "+", source)
        source = re.sub(r"(?<=\S)含(?=\S)", "+", source)
        source = re.sub(r"(饮料|饮品)\s*(小食|小吃)", r"\1+\2", source)
        if source.count("加") >= 2:
            source = re.sub(r"(?<=\S)加(?=\S)", "+", source)
        splitter = SPLIT_RE if use_broad_split else TOP_LEVEL_PLUS_RE
        for raw in splitter.split(source):
            label = _clean_component_label(raw)
            norm = normalize_dish(label)
            if len(norm) < 2 or norm in generic_norms or norm in seen:
                continue
            seen.add(norm)
            out.append(label)
    return out[:8]


def _has_combo_signal(name: str, attrs: str = "", category: str = "") -> bool:
    name_text = unicodedata.normalize("NFKC", str(name or ""))
    full_text = unicodedata.normalize("NFKC", f"{name_text} {attrs or ''}")
    if any(word in name_text for word in COMBO_WORDS) or PEOPLE_MEAL_RE.search(name_text):
        return True
    if re.search(r"(含|配|搭配|赠).*(饮料|饮品).*(小食|小吃)|(含|配|搭配|赠).*(小食|小吃).*(饮料|饮品)", full_text):
        return True

    top_level_name = PAREN_RE.sub(" ", name_text)
    if TOP_LEVEL_PLUS_RE.search(top_level_name):
        raw_parts = [part.strip() for part in TOP_LEVEL_PLUS_RE.split(top_level_name) if normalize_dish(part)]
        has_choice = any(word in full_text for word in CHOICE_WORDS) or bool(CHOICE_RE.search(full_text.lower()))
        if has_choice and len(raw_parts) < 3:
            return False
        return len(raw_parts) >= 2
    has_choice = any(word in full_text for word in CHOICE_WORDS) or bool(CHOICE_RE.search(full_text.lower()))
    if has_choice:
        return False
    if top_level_name.count("加") >= 2:
        return len(split_components(name, attrs, category)) >= 2
    if any(word in f"{name_text} {attrs or ''}" for word in INCLUSION_WORDS):
        return len(split_components(name, attrs, category)) >= 2
    return False


def _classify_leaf_taxonomy(text: str) -> str:
    best_taxonomy = TAXONOMY_UNKNOWN
    best_score = (0, 0, 0)
    for taxonomy_id, _label, keywords in TAXONOMY_RULES:
        hits = [keyword for keyword in keywords if keyword.lower() in text]
        if not hits:
            continue
        score = (max(len(keyword) for keyword in hits), len(hits), sum(len(keyword) for keyword in hits))
        if score > best_score:
            best_taxonomy = taxonomy_id
            best_score = score
    return best_taxonomy


def classify_taxonomy(name: str, attrs: str = "", category: str = "") -> str:
    """Return a stable taxonomy id, preserving unknown instead of guessing."""
    if _has_combo_signal(name, attrs, category):
        return TAXONOMY_COMBO
    name_text = _taxonomy_signal_text(f"{category or ''} {name or ''}")
    text = _taxonomy_signal_text(f"{name_text} {attrs or ''}")
    for source, target in CANONICAL_REPLACEMENTS:
        name_text = name_text.replace(source, target)
        text = text.replace(source, target)
    compact_name = normalize_dish(name)
    if "锅贴" in name_text:
        return "dumpling_wonton"
    if any(word in name_text for word in ("炒饭", "拌饭", "烤肉饭")):
        return "mixed_rice"
    if any(word in name_text for word in ("汤饭", "泡饭", "稀饭", "粥")):
        return "porridge_soup_rice"
    if any(word in name_text for word in ("螺蛳粉", "酸辣粉", "土豆粉", "红薯粉", "米线", "米粉", "河粉", "粉丝", "粉条")):
        return "rice_noodles"
    if any(word in name_text for word in ("刀削面", "热干面", "炸酱面", "担担面", "荞麦面", "拉面", "汤面", "拌面", "炒面", "面条")):
        return "wheat_noodles"
    if re.search(r"(盖饭|焖饭|烩饭|饭)$", compact_name):
        return "topped_rice"
    if re.search(r"(面条|面)$", compact_name):
        return "wheat_noodles"
    if re.search(r"(米线|米粉|河粉|粉丝|粉条|粉)$", compact_name):
        return "rice_noodles"
    taxonomy = _classify_leaf_taxonomy(text)
    if taxonomy != TAXONOMY_UNKNOWN:
        return taxonomy
    return TAXONOMY_UNKNOWN


def taxonomy_label(taxonomy_id: str) -> str:
    return TAXONOMY_LABELS.get(str(taxonomy_id or ""), TAXONOMY_LABELS[TAXONOMY_UNKNOWN])


def component_fingerprint(components: Sequence[str]) -> tuple[str, ...]:
    normalized = {normalize_dish(component) for component in components}
    return tuple(sorted(value for value in normalized if len(value) >= 2))


def taxonomy_compatible(
    left_taxonomy: str,
    right_taxonomy: str,
    left_norm: str,
    right_norm: str,
) -> bool:
    """Require exact identity for unknowns and the same leaf for fuzzy reuse."""
    if left_norm and left_norm == right_norm:
        return True
    if TAXONOMY_UNKNOWN in {left_taxonomy, right_taxonomy}:
        return False
    return bool(left_taxonomy and left_taxonomy == right_taxonomy)


def classify_kind(name: str, attrs: str = "", category: str = "") -> str:
    """Classify a menu item as single dish, combo, or snack/drink."""
    if _has_combo_signal(name, attrs):
        return "套餐/组合"
    taxonomy = classify_taxonomy(name, attrs)
    text = unicodedata.normalize("NFKC", str(name or ""))
    norm = normalize_dish(name)
    if re.fullmatch(r"(白)?米饭|杂粮饭|糙米饭|珍珠饭", norm):
        return "饮品/小食"
    if re.search(r"(酱|汁|蘸料)$", norm) and len(norm) <= 8:
        return "饮品/小食"
    if taxonomy in SNACK_TAXONOMIES:
        return "饮品/小食"
    if taxonomy == "soup_stew" and not any(word in text for word in ("汤饭", "汤面", "汤粉", "汤锅", "汤包")):
        return "饮品/小食"
    if taxonomy != TAXONOMY_UNKNOWN or any(word in text for word in MAIN_FOOD_WORDS):
        return "单品"
    if any(word in text for word in DRINK_SNACK_WORDS):
        return "饮品/小食"
    return "单品"


@lru_cache(maxsize=16_384)
def _name_match_profile(name: str) -> tuple[str, str, tuple[str, ...]]:
    kind = classify_kind(name)
    taxonomy = classify_taxonomy(name)
    components = component_fingerprint(split_components(name)) if kind == "套餐/组合" else ()
    return kind, taxonomy, components


def similarity(
    menu_name: str,
    image_name: str,
    menu_norm: str | None = None,
    image_norm: str | None = None,
    menu_grams: set[str] | None = None,
    image_grams: set[str] | None = None,
) -> float:
    """Score two dish names in the range 0.0-1.0."""
    left = menu_norm if menu_norm is not None else normalize_dish(menu_name)
    right = image_norm if image_norm is not None else normalize_dish(image_name)
    if not left or not right:
        return 0.0
    left_kind, left_taxonomy, left_fingerprint = _name_match_profile(str(menu_name or ""))
    right_kind, right_taxonomy, right_fingerprint = _name_match_profile(str(image_name or ""))
    if left_kind != right_kind:
        return 0.0
    if left_kind == "套餐/组合":
        if left_fingerprint or right_fingerprint:
            if not left_fingerprint or left_fingerprint != right_fingerprint:
                return 0.0
            return 1.0
        elif left != right:
            return 0.0
    elif not taxonomy_compatible(left_taxonomy, right_taxonomy, left, right):
        return 0.0
    left_grams = menu_grams if menu_grams is not None else grams(left)
    right_grams = image_grams if image_grams is not None else grams(right)
    seq = SequenceMatcher(None, left, right).ratio()
    jac = len(left_grams & right_grams) / max(1, len(left_grams | right_grams))
    contains = 0.22 if left in right or right in left else 0.0
    prefix = 0.06 if left[:2] == right[:2] else 0.0
    length_gap = abs(len(left) - len(right)) / max(len(left), len(right), 1)
    score = seq * 0.48 + jac * 0.40 + contains + prefix - length_gap * 0.08
    return max(0.0, min(1.0, score))


def _value(record: Any, *keys: str, default: Any = "") -> Any:
    for key in keys:
        if isinstance(record, Mapping) and key in record:
            value = record[key]
            if value is not None:
                return value
        if hasattr(record, key):
            value = getattr(record, key)
            if value is not None:
                return value
    return default


def _path_stem(record: Any) -> str:
    path = _value(record, "path", "file", default="")
    if path:
        return Path(str(path)).stem
    return ""


def _record_name(record: Any) -> str:
    return str(
        _value(
            record,
            "dishName",
            "dish_name",
            "dish",
            "name",
            "title",
            default=_path_stem(record),
        )
        or ""
    )


def _record_norm(record: Any) -> str:
    name_norm = normalize_dish(_record_name(record))
    return name_norm or normalize_dish(str(_value(record, "norm", "normalized", "canonical", default="") or ""))


def _record_style(record: Any) -> str:
    return str(_value(record, "styleId", "style_id", "style", default="style-upload") or "style-upload")


def _record_source(record: Any) -> str:
    return str(_value(record, "source", "store", "provider", "batch", default="library") or "library")


def _record_id(record: Any, name: str, style_id: str) -> str:
    value = _value(record, "imageId", "image_id", "id", default="")
    if value:
        return str(value)
    seed = "|".join([str(_value(record, "path", "url", default="")), name, style_id])
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:18]


def _normalized_kind(value: Any, name: str = "", attrs: str = "", category: str = "") -> str:
    kind = str(value or "").strip().lower()
    if kind in {"combo", "set", "set_meal", "套餐", "套餐/组合"}:
        return "套餐/组合"
    if kind in {"drink", "snack", "side", "dessert", "饮品", "小食", "饮品/小食"}:
        return "饮品/小食"
    if kind in {"single", "dish", "单品"}:
        return "单品"
    return classify_kind(name, attrs, category)


def _component_values(record: Any, name: str, attrs: str) -> list[str]:
    values = _value(record, "components", "componentNames", "component_names", default=[])
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        components = [str(value).strip() for value in values if str(value).strip()]
        if components:
            return components
    return split_components(name, attrs)


def _candidate(record: Any, score: float, matched_name: str, match_type: str, component: str = "") -> dict[str, Any]:
    dish_name = _record_name(record)
    style_id = _record_style(record)
    source = _record_source(record)
    path = _value(record, "path", default="")
    url = _value(record, "url", "publicUrl", "public_url", default="")
    attrs = str(_value(record, "attrs", "attributes", "spec", "description", default="") or "")
    category = str(_value(record, "category", "cat", default="") or "")
    kind = _normalized_kind(_value(record, "kind", "type", default=""), dish_name, attrs, category)
    taxonomy = str(_value(record, "taxonomy", "primary_category_id", default="") or classify_taxonomy(dish_name, attrs, category))
    candidate = {
        "imageId": _record_id(record, dish_name, style_id),
        "score": round(score * 100, 1),
        "dishName": dish_name,
        "kind": kind,
        "taxonomy": taxonomy,
        "styleId": style_id,
        "source": source,
        "store": source,
        "matchType": match_type,
        "matchedName": matched_name,
    }
    if component:
        candidate["component"] = component
    if url:
        candidate["url"] = str(url)
    if path:
        candidate["path"] = str(path)
    return candidate


def _prepared_records(records: Sequence[Any]) -> list[dict[str, Any]]:
    prepared = []
    for record in records:
        name = _record_name(record)
        norm = _record_norm(record)
        if not norm:
            continue
        attrs = str(_value(record, "attrs", "attributes", "spec", "description", default="") or "")
        category = str(_value(record, "category", "cat", default="") or "")
        components = _component_values(record, name, attrs)
        kind = _normalized_kind(_value(record, "kind", "type", default=""), name, attrs, category)
        taxonomy = str(_value(record, "taxonomy", "primary_category_id", default="") or classify_taxonomy(name, attrs, category))
        prepared.append(
            {
                "record": record,
                "name": name,
                "norm": norm,
                "grams": grams(norm),
                "kind": kind,
                "taxonomy": taxonomy,
                "componentFingerprint": component_fingerprint(components),
            }
        )
    return prepared


def _candidate_allowed(
    *,
    query_kind: str,
    query_taxonomy: str,
    query_norm: str,
    query_fingerprint: tuple[str, ...],
    prepared: Mapping[str, Any],
) -> bool:
    candidate_kind = str(prepared["kind"])
    candidate_norm = str(prepared["norm"])
    if query_kind == "套餐/组合":
        if candidate_kind != "套餐/组合":
            return False
        candidate_fingerprint = tuple(prepared["componentFingerprint"])
        if query_fingerprint or candidate_fingerprint:
            return bool(query_fingerprint and query_fingerprint == candidate_fingerprint)
        return bool(query_norm and query_norm == candidate_norm)
    if candidate_kind == "套餐/组合" or candidate_kind != query_kind:
        return False

    candidate_taxonomy = str(prepared["taxonomy"])
    if query_norm == candidate_norm:
        return True
    if TAXONOMY_UNKNOWN in {query_taxonomy, candidate_taxonomy}:
        return False
    return query_taxonomy == candidate_taxonomy


def _score_candidates(
    query_name: str,
    prepared_records: list[dict[str, Any]],
    *,
    limit: int,
    min_score: float,
    match_type: str,
    component: str = "",
    query_kind: str = "",
    query_taxonomy: str = "",
    query_components: Sequence[str] = (),
) -> list[dict[str, Any]]:
    query_norm = normalize_dish(query_name)
    query_grams = grams(query_norm)
    effective_min_score = max(DEFAULT_MIN_SCORE, float(min_score))
    normalized_query_kind = _normalized_kind(query_kind, query_name)
    normalized_query_taxonomy = query_taxonomy or classify_taxonomy(query_name)
    query_fingerprint = component_fingerprint(query_components)
    scored: list[tuple[float, Any]] = []
    for prepared in prepared_records:
        score = similarity(query_name, prepared["name"], query_norm, prepared["norm"], query_grams, prepared["grams"])
        if score >= effective_min_score and _candidate_allowed(
            query_kind=normalized_query_kind,
            query_taxonomy=normalized_query_taxonomy,
            query_norm=query_norm,
            query_fingerprint=query_fingerprint,
            prepared=prepared,
        ):
            scored.append((score, prepared["record"]))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [_candidate(record, score, query_name, match_type, component) for score, record in scored[:limit]]


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        key = str(candidate.get("imageId") or candidate.get("path") or candidate.get("url") or candidate.get("dishName"))
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _sort_candidates(candidates: list[dict[str, Any]], selected_style: str = "") -> list[dict[str, Any]]:
    if selected_style:
        return sorted(candidates, key=lambda c: (c.get("styleId") == selected_style, float(c.get("score") or 0)), reverse=True)
    return sorted(candidates, key=lambda c: float(c.get("score") or 0), reverse=True)


def _status_for(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return "未找到"
    score = float(candidates[0].get("score") or 0)
    if score >= DIRECT_SCORE:
        return "直接可用"
    if score >= REVIEW_SCORE:
        return "需人工确认"
    return "弱匹配"


def _background_action(candidates: list[dict[str, Any]], selected_style: str = "") -> str:
    if not candidates:
        return "需要定制/生成"
    chosen = candidates[0]
    score = float(chosen.get("score") or 0)
    if selected_style:
        if chosen.get("styleId") == selected_style and score >= DIRECT_SCORE:
            return "背景一致，直接复用"
        if chosen.get("styleId") == selected_style and score >= REVIEW_SCORE:
            return "需人工确认"
        if score >= REVIEW_SCORE:
            return "需抠图换背景"
        return "智能补图"
    if score >= DIRECT_SCORE:
        return "优先复用图库图"
    if score >= REVIEW_SCORE:
        return "需人工确认"
    return "智能补图"


def _item_value(item: Any, *keys: str, default: Any = "") -> Any:
    return _value(item, *keys, default=default)


def match_menu_to_library(
    items: Sequence[Any],
    records: Sequence[Any],
    *,
    selected_style: str = "",
    limit: int = 6,
    component_limit: int = 3,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """Match menu items to image-library records.

    Each returned row contains dish candidates, per-component candidates for combo
    meals, score/status fields, style/source metadata, and a background action
    recommendation.
    """
    prepared = _prepared_records(records)
    results: list[dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        name = str(_item_value(item, "name", "dishName", "dish", "title", default=item if isinstance(item, str) else "") or "")
        attrs = str(_item_value(item, "attrs", "attributes", "spec", "description", default="") or "")
        category = str(_item_value(item, "category", "cat", default="") or "")
        row = _item_value(item, "row", "index", default=idx)
        components = list(_item_value(item, "components", default=[]) or split_components(name, attrs))
        kind = _normalized_kind(_item_value(item, "kind", "type", default=""), name, attrs, category)
        norm = normalize_dish(name) or normalize_dish(str(_item_value(item, "norm", "normalized", default="") or ""))
        taxonomy = str(
            _item_value(item, "taxonomy", "primary_category_id", default="")
            or classify_taxonomy(name, attrs, category)
        )

        dish_candidates = _score_candidates(
            name,
            prepared,
            limit=limit,
            min_score=min_score,
            match_type="dish",
            query_kind=kind,
            query_taxonomy=taxonomy,
            query_components=components,
        )
        component_matches = []
        if kind == "套餐/组合" and components:
            for component in components:
                component_kind = classify_kind(component)
                component_taxonomy = classify_taxonomy(component)
                matches = _score_candidates(
                    component,
                    prepared,
                    limit=component_limit,
                    min_score=min_score,
                    match_type="component",
                    component=component,
                    query_kind=component_kind,
                    query_taxonomy=component_taxonomy,
                )
                component_matches.append(
                    {
                        "name": component,
                        "norm": normalize_dish(component),
                        "kind": component_kind,
                        "taxonomy": component_taxonomy,
                        "status": _status_for(matches),
                        "candidates": matches,
                    }
                )

        # Component assets are composition inputs only; they are never complete
        # product candidates for a combo.
        candidates = _dedupe_candidates(dish_candidates)
        candidates = _sort_candidates(candidates, selected_style)[:limit]
        status = _status_for(candidates)
        background_action = _background_action(candidates, selected_style)
        results.append(
            {
                "row": row,
                "category": category,
                "name": name,
                "kind": kind,
                "norm": norm,
                "taxonomy": taxonomy,
                "taxonomyLabel": taxonomy_label(taxonomy),
                "taxonomyVersion": TAXONOMY_VERSION,
                "components": components,
                "status": status,
                "candidates": candidates,
                "componentMatches": component_matches,
                "backgroundAction": background_action,
                "selectedStyle": selected_style,
            }
        )
    return results


def _style_ids_from_results(results: Sequence[Mapping[str, Any]]) -> list[str]:
    style_ids = {
        str(candidate.get("styleId"))
        for row in results
        for candidate in row.get("candidates", [])
        if candidate.get("styleId")
    }
    return sorted(style_ids)


def _best_candidate_for_style(row: Mapping[str, Any], style_id: str) -> dict[str, Any] | None:
    candidates = [c for c in row.get("candidates", []) if c.get("styleId") == style_id]
    if not candidates:
        return None
    return max(candidates, key=lambda c: float(c.get("score") or 0))


def _component_style_status(row: Mapping[str, Any], style_id: str) -> str | None:
    components = row.get("componentMatches") or []
    if not components:
        return None
    best_scores = []
    for component in components:
        same_style = [c for c in component.get("candidates", []) if c.get("styleId") == style_id]
        if not same_style:
            return "bgReplace"
        best_scores.append(max(float(c.get("score") or 0) for c in same_style))
    if best_scores and all(score >= DIRECT_SCORE for score in best_scores):
        return "direct"
    if best_scores and all(score >= REVIEW_SCORE for score in best_scores):
        return "review"
    return "custom"


def style_coverage(
    items_or_matches: Sequence[Any],
    records: Sequence[Any] | None = None,
    *,
    limit: int = 6,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict[str, Any]]:
    """Summarize how well each style covers the matched menu."""
    if records is None:
        results = list(items_or_matches)
    else:
        results = match_menu_to_library(items_or_matches, records, limit=limit, min_score=min_score)
    total = max(1, len(results))
    options = []
    for style_id in _style_ids_from_results(results):
        direct = review = bg_replace = custom = count = 0
        sample = None
        for row in results:
            component_status = _component_style_status(row, style_id)
            same = _best_candidate_for_style(row, style_id)
            sample = sample or same
            if same:
                count += 1
            if component_status == "direct":
                direct += 1
                continue
            if component_status == "review":
                review += 1
                continue
            if component_status == "bgReplace":
                bg_replace += 1
                continue
            if component_status == "custom":
                custom += 1
                continue
            if same:
                score = float(same.get("score") or 0)
                if score >= DIRECT_SCORE:
                    direct += 1
                elif score >= REVIEW_SCORE:
                    review += 1
                else:
                    custom += 1
            elif row.get("candidates"):
                bg_replace += 1
            else:
                custom += 1
        options.append(
            {
                "id": style_id,
                "styleId": style_id,
                "count": count,
                "sample": sample,
                "direct": direct,
                "review": review,
                "bgReplace": bg_replace,
                "custom": custom,
                "directRate": round(direct / total * 100, 1),
                "processingRate": round((review + bg_replace) / total * 100, 1),
                "customRate": round(custom / total * 100, 1),
            }
        )
    return sorted(options, key=lambda item: (item["direct"], item["review"], item["count"]), reverse=True)


SAMPLE_MENU_ITEMS = [
    {"row": 1, "category": "热销", "name": "老长沙辣椒炒肉盖码饭", "price": "19.8"},
    {"row": 2, "category": "套餐", "name": "辣椒炒肉+茄子肉末盖码饭", "price": "24.8"},
    {"row": 3, "category": "饮品", "name": "康师傅冰红茶", "price": "4.0"},
]

SAMPLE_LIBRARY_RECORDS = [
    {"imageId": "sample-1", "dishName": "辣椒小炒肉盖饭", "styleId": "style-1", "source": "sample"},
    {"imageId": "sample-2", "dishName": "茄子肉末盖码饭", "styleId": "style-1", "source": "sample"},
    {"imageId": "sample-3", "dishName": "康师傅冰红茶", "styleId": "style-2", "source": "sample"},
]


def run_builtin_selftest() -> dict[str, Any]:
    matches = match_menu_to_library(SAMPLE_MENU_ITEMS, SAMPLE_LIBRARY_RECORDS, selected_style="style-1")
    coverage = style_coverage(matches)
    combo = next(row for row in matches if row["kind"] == "套餐/组合")
    ok = bool(
        normalize_dish("【热销】老长沙辣椒炒肉盖码饭") == "辣椒炒肉盖饭"
        and len(combo["componentMatches"]) >= 2
        and not combo["candidates"]
        and combo["backgroundAction"] == "需要定制/生成"
        and coverage
    )
    return {"ok": ok, "matches": matches, "coverage": coverage}


if __name__ == "__main__":
    result = run_builtin_selftest()
    print("ok" if result["ok"] else "failed")
