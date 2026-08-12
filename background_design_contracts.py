from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryBackgroundDirection:
    primary_color: str
    contrast_color: str
    light_surface: str
    warm_surface: str
    contemporary_surface: str
    premium_surface: str
    lighting_mood: str


# These directions describe original art direction, not competitor assets. Brand
# research lives in AI-Project/research and is never embedded in generation prompts.
CATEGORY_BACKGROUND_DIRECTIONS = {
    "topped_rice": CategoryBackgroundDirection("暖象牙白", "克制钴蓝", "浅米色细纹洞石", "蜂蜜色白蜡木", "低饱和钴蓝矿物台面", "炭灰细纹板岩", "明亮利落的左侧窗光"),
    "mixed_rice": CategoryBackgroundDirection("暖赭米色", "低饱和陶土红", "浅暖灰石灰岩", "温润胡桃木", "哑光赭色陶瓷台面", "深炭灰矿物台面", "暖中性的前上方大柔光"),
    "porridge_soup_rice": CategoryBackgroundDirection("燕麦奶油色", "柔和蜜桃色", "奶油色石灰岩", "浅白蜡木", "低饱和燕麦色微水泥", "温暖灰褐石材", "高调柔和的晨间侧光"),
    "rice_noodles": CategoryBackgroundDirection("海盐暖白", "陶土橙红", "浅灰细纹石材", "深胡桃木", "低饱和苔绿色矿物台面", "黑褐细纹岩板", "有层次的暖色左上侧光"),
    "wheat_noodles": CategoryBackgroundDirection("麦芽米色", "克制辣椒红", "浅砂岩", "深红棕木纹", "暖灰哑光陶面", "深棕黑石材", "温暖定向柔光"),
    "dumpling_wonton": CategoryBackgroundDirection("面皮象牙白", "浅竹青灰", "暖白细砂石", "浅蜂蜜色竹木", "淡青灰陶瓷台面", "深灰细石材", "柔亮的左前上方光"),
    "buns_dim_sum": CategoryBackgroundDirection("奶油白", "柔和桃橙", "浅米白石材", "浅竹木", "淡金米色陶面", "温暖深褐木面", "轻盈高调的暖柔光"),
    "chinese_wraps": CategoryBackgroundDirection("麦黄金", "清新草绿色", "浅暖灰石材", "蜂蜜色木面", "哑光草绿矿物台面", "深炭灰石材", "均匀明快的正面柔光"),
    "malatang_maocai": CategoryBackgroundDirection("暖奶油白", "低饱和朱红", "浅暖灰石材", "深胡桃木", "陶土红哑光陶面", "炭黑板岩", "高对比但不刺眼的暖侧光"),
    "hotpot_skewers": CategoryBackgroundDirection("暖灰", "低饱和薄荷绿", "浅灰矿物台面", "浅木纹", "暗红哑光陶面", "炭灰石板", "方向明确的左前上方光"),
    "barbecue": CategoryBackgroundDirection("焦糖褐", "克制暗红", "浅暖灰粗纹石材", "烟熏深木纹", "焦褐哑光矿物台面", "黑色细纹板岩", "带轻微轮廓的暖侧光"),
    "fried_chicken": CategoryBackgroundDirection("暖奶油白", "芥末黄", "浅暖灰石材", "浅蜂蜜木纹", "低饱和番茄红陶面", "炭灰哑光石材", "高调暖色顶前柔光"),
    "burger_hotdog": CategoryBackgroundDirection("暖奶油黄", "番茄红", "浅灰细石材", "浅橡木", "低饱和芥末黄矿物台面", "石墨灰哑光台面", "清楚利落的高调顶前光"),
    "pizza": CategoryBackgroundDirection("奶油白", "赤陶红", "浅米色洞石", "温暖橡木", "橄榄灰陶面", "深褐石板", "突出烘焙质感的暖顶侧光"),
    "sandwich_bagel": CategoryBackgroundDirection("燕麦白", "鼠尾草绿", "浅暖灰石材", "浅白蜡木", "鼠尾草灰陶面", "深灰矿物台面", "自然日光感的柔侧光"),
    "light_food": CategoryBackgroundDirection("暖白", "鼠尾草绿", "浅灰白石材", "浅白蜡木", "低饱和鼠尾草绿陶面", "中性深灰石材", "清透高调的自然侧光"),
    "pasta_steak": CategoryBackgroundDirection("象牙白", "克制酒红", "浅灰大理石", "深胡桃木", "暖灰陶瓷台面", "炭黑细纹板岩", "精致暖侧光加弱轮廓光"),
    "japanese": CategoryBackgroundDirection("月白", "墨灰", "细腻暖白石材", "浅原木", "灰绿色和纸质感矿物台面", "墨黑细纹木石台面", "低对比高调漫射光"),
    "korean": CategoryBackgroundDirection("暖白", "克制朱红", "浅灰石材", "浅木纹", "石墨灰陶面", "深炭灰石材", "温暖侧前柔光"),
    "southeast_asian": CategoryBackgroundDirection("暖白", "珊瑚橙", "浅灰白矿物台面", "浅柚木", "低饱和蕉叶绿陶面", "深青灰石材", "明亮中性日光"),
    "sichuan_hunan": CategoryBackgroundDirection("暖灰", "克制朱红", "浅暖灰石材", "深胡桃木", "暗红陶瓷台面", "炭黑岩板", "食欲感强的暖上侧光"),
    "cantonese_roast": CategoryBackgroundDirection("蜜糖米白", "墨绿灰", "浅暖石材", "深蜂蜜木纹", "低饱和琥珀陶面", "深褐石板", "突出琥珀高光的暖侧逆光"),
    "jiangzhe": CategoryBackgroundDirection("月白", "黛青", "浅米灰石材", "浅榉木", "淡青灰矿物台面", "深黛灰石材", "雅致低反差的自然柔光"),
    "northeast_chinese": CategoryBackgroundDirection("暖米色", "砖红", "浅暖灰石材", "厚实暖木纹", "琥珀棕陶面", "深灰石板", "明亮朴实的上侧柔光"),
    "northwest_xinjiang": CategoryBackgroundDirection("浅沙金", "铁锈红", "浅砂岩", "粗纹暖木", "低饱和靛青矿物台面", "炭黑粗细适中石材", "中等对比的暖桌面侧光"),
    "northern_lu": CategoryBackgroundDirection("暖白", "枣红", "浅灰白石材", "深木纹", "克制枣红陶面", "炭灰织纹石材", "端正均匀的中性柔光"),
    "fujian_taiwan": CategoryBackgroundDirection("海盐白", "浅青", "细腻白石材", "浅竹木", "浅青灰陶面", "深灰细石材", "高调清爽的漫射光"),
    "home_stir_fry": CategoryBackgroundDirection("暖米白", "陶土红", "浅暖石材", "真实暖木纹", "淡薄荷灰陶面", "深灰褐石材", "自然温暖的家庭窗光"),
    "fish_seafood": CategoryBackgroundDirection("海盐白", "浅蓝灰", "冷白细纹石材", "漂白浅木", "低饱和海蓝灰陶面", "深冷灰岩板", "清凉通透的侧逆柔光"),
    "beef_lamb_pot": CategoryBackgroundDirection("暖焦糖色", "暗红", "浅暖灰石材", "深红棕木纹", "古铜灰陶面", "炭黑耐热石材", "暖色定向上侧光"),
    "braised_cooked_food": CategoryBackgroundDirection("暖焦糖米色", "卤酱红棕", "浅暖石材", "深蜂蜜木纹", "暗红棕陶面", "黑色细纹板岩", "突出卤汁光泽的柔侧光"),
    "soup_stew": CategoryBackgroundDirection("奶油白", "浅杏色", "浅暖灰石材", "浅木纹", "温暖灰褐陶面", "深暖灰石材", "压低反光的高调漫射侧光"),
    "steamed_claypot": CategoryBackgroundDirection("陶土米色", "焦糖棕", "浅暖灰石材", "暖木条纹", "哑光陶土色台面", "深灰耐热石材", "锅沿清楚的宽幅上方柔光"),
    "milk_fruit_tea": CategoryBackgroundDirection("奶油白", "淡桃粉", "浅色细砂石", "浅白蜡木", "低饱和浅青绿陶面", "深青灰石材", "强调杯壁通透感的侧逆柔光"),
    "coffee_cocoa": CategoryBackgroundDirection("奶油白", "可可棕", "浅灰石材", "深胡桃木", "森林绿灰陶面", "石墨灰矿物台面", "控制反光的大型侧上柔光"),
    "bottled_drinks": CategoryBackgroundDirection("冷白", "金属灰", "浅冷灰石材", "浅灰木纹", "低饱和深蓝灰陶面", "深石墨灰台面", "勾勒瓶缘与冷凝感的后侧柔光"),
    "fresh_drinks": CategoryBackgroundDirection("暖白", "青柠绿", "白色细砂石", "浅白木纹", "低饱和淡橙陶面", "深青灰石材", "突出果肉透光的顶部漫射光"),
    "dessert_bakery": CategoryBackgroundDirection("奶油白", "浅莓粉", "浅灰白石材", "浅蜂蜜木纹", "开心果浅绿陶面", "深可可灰石材", "保留奶油和酥皮细节的高调柔光"),
    "fried_snacks": CategoryBackgroundDirection("暖奶油黄", "暖红橙", "浅暖灰石材", "蜂蜜色木纹", "低饱和辣椒红陶面", "炭黑矿物台面", "突出脆壳的方向性暖侧光"),
    "fruit": CategoryBackgroundDirection("冷白", "清水蓝", "浅冷灰石材", "浅白蜡木", "低饱和嫩绿色陶面", "深青灰石材", "呈现果肉透光和汁水的自然侧逆光"),
}


if len(CATEGORY_BACKGROUND_DIRECTIONS) != 40:
    raise RuntimeError("commercial background directions require 40 categories")


STYLE_BACKGROUND_DIRECTIONS = {
    "style-1": ("纯色暖调无缝棚拍", "primary_color", "连续哑光无缝承托面，不出现墙地分界"),
    "style-2": ("纯色对比无缝棚拍", "contrast_color", "连续哑光无缝承托面，不出现墙地分界"),
    "style-3": ("明亮真实桌面", "light_surface", "单一材质桌面从四边铺满画面"),
    "style-4": ("温暖木质桌面", "warm_surface", "单一连续木质桌面从四边铺满画面"),
    "style-5": ("现代品类色桌面", "contemporary_surface", "单一当代餐饮材质桌面从四边铺满画面"),
    "style-6": ("高级深色桌面", "premium_surface", "单一深色高级材质桌面从四边铺满画面"),
}


UPRIGHT_PRODUCT_CATEGORIES = frozenset(
    {"milk_fruit_tea", "coffee_cocoa", "bottled_drinks", "fresh_drinks"}
)
TOP_DOWN_PRODUCT_CATEGORIES = frozenset(
    {"pizza", "light_food", "steamed_claypot", "fruit"}
)


__all__ = [
    "CATEGORY_BACKGROUND_DIRECTIONS",
    "CategoryBackgroundDirection",
    "STYLE_BACKGROUND_DIRECTIONS",
    "TOP_DOWN_PRODUCT_CATEGORIES",
    "UPRIGHT_PRODUCT_CATEGORIES",
]
