from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from menu_parser import audit_menus, parse_menu


def save_workbook(path: Path, sheets: dict[str, list[list[object]]]) -> None:
    workbook = Workbook()
    first = True
    for title, rows in sheets.items():
        worksheet = workbook.active if first else workbook.create_sheet()
        first = False
        worksheet.title = title
        for row in rows:
            worksheet.append(row)
    workbook.save(path)


class MenuParserTests(unittest.TestCase):
    def test_parse_operation_export_with_blank_rows_and_deduping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "运营数据_测试店.xlsx"
            save_workbook(
                path,
                {
                    "调研结果": [
                        [],
                        ["", "", ""],
                        ["一级分类", "二级分类", "菜单名", "规格名", "条码", "活动价", "原价", "月销", "最小购买", "属性", "描述", "折扣"],
                        ["热销", "", "辣椒炒肉盖码饭", "", "", "19.8", "22", "100", "1", "", "", ""],
                        ["热销", "", "辣椒炒肉盖码饭", "一份", "", "19.8", "22", "100", "1", "份量#一份##", "", ""],
                        ["套餐", "", "牛肉+鸡胸双拼能量碗", "", "", "27.8", "30", "100", "1", "", "", ""],
                        ["小食", "", "冰红茶", "", "", "4", "4", "100", "1", "", "", ""],
                    ],
                },
            )

            menu = parse_menu(path)

        self.assertEqual(menu["store"], "测试店")
        self.assertEqual(menu["count"], 3)
        self.assertEqual(menu["kindCounts"], {"single": 1, "combo": 1, "snack": 1, "total": 3})
        self.assertEqual(menu["taxonomyVersion"], "2026-07-30.v2")
        self.assertEqual(sum(menu["taxonomyCounts"].values()), menu["count"])
        self.assertEqual(
            menu["taxonomyCounts"],
            {"bottled_drinks": 1, "combo": 1, "topped_rice": 1},
        )
        self.assertEqual(menu["sheets"][0]["sheet"], "调研结果")
        self.assertEqual(menu["sheets"][0]["headerRow"], 3)
        for item in menu["items"]:
            self.assertGreaterEqual(
                item.keys(),
                {
                    "row",
                    "category",
                    "name",
                    "price",
                    "kind",
                    "norm",
                    "taxonomy",
                    "taxonomyLabel",
                    "taxonomyVersion",
                    "components",
                },
            )

    def test_parse_prefers_menu_sheet_over_noise_and_cost_sheets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "谨食·沙拉轻食活动及菜单方案.xlsx"
            save_workbook(
                path,
                {
                    "活动及注意事项": [
                        ["线下注意事项"],
                        ["不要取消订单"],
                    ],
                    "菜单": [
                        ["分类名称", "", "商品名称", "名称调整", "折扣", "原价", "属性", "描述"],
                        ["热销", "", "香煎鸡胸能量碗", "嫩烤鸡胸能量碗", "25.88", "29.88", "基底#五色糙米饭##", "主菜"],
                        ["饮品", "", "水果燕麦酸奶杯", "", "5.88", "6.88", "", "低脂"],
                    ],
                    "成本": [
                        ["", "", "商品成本登记表"],
                        ["序号", "分类名称", "*商品名称", "规格", "价格", "*成本价"],
                        [1, "热销", "成本表里的重复菜", "一份", "9.9", "4"],
                    ],
                },
            )

            menu = parse_menu(path)

        self.assertEqual(menu["store"], "谨食·沙拉轻食")
        self.assertEqual(menu["count"], 2)
        self.assertEqual({item["sheet"] for item in menu["items"]}, {"菜单"})
        self.assertEqual(menu["items"][0]["price"], "25.88")

    def test_parse_real_meizizi_rows_into_structured_generation_semantics(self) -> None:
        flavor_and_gift = (
            "口味自选#人气香辣（粉）#蜜汁味（酱）#甜辣味（酱）##"
            "赠品三选一#热狗肠#煎蛋#随机饮品##"
        )
        three_main_choices = (
            "食材自选一#烤肉#烤排#鸡排#腿排#猪排##"
            "食材自选二#烤排#鸡排#腿排#烤肉#猪排##"
            "食材自选三#鸡排#腿排#烤肉#烤排#猪排##"
            "口味自选#人气香辣味（粉）#蜜汁味（酱）##"
            "赠品五选一#热狗肠#煎蛋#骨肉相连#川香鸡柳#随机饮品##"
        )
        final_combo_attrs = (
            "肉自选#烤肉#烤排#鸡排#腿排##"
            "口味#蜜汁味（酱）#麻辣味（酱）##"
            "饮品#随机饮品##"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "运营数据_美滋滋烤肉拌饭（成都店）.xlsx"
            save_workbook(
                path,
                {
                    "调研结果": [
                        ["一级分类", "二级分类", "菜单名", "规格名", "条码", "活动价", "原价", "月销", "最小购买", "属性", "描述", "折扣"],
                        ["|进店|必点", "", "每日专享招牌烤排饭+煎蛋／热狗肠／饮品三选一", "", "", "19.98", "32", "10", "1", flavor_and_gift, "", ""],
                        ["|进店|必点", "", "豪华三拼【烤肉+烤排+鸡排】+煎蛋／热狗肠／饮品三选一", "", "", "21.98", "40", "300", "1", flavor_and_gift, "", ""],
                        ["|豪气|双拼", "", "【霸气任选】 三拼饭+赠品五选一", "", "", "23.98", "45", "100", "1", three_main_choices, "", ""],
                        ["美滋滋神枪手", "", "招牌拌饭套餐自选+大鸡腿+饮品自选", "", "", "24", "24", "100", "1", final_combo_attrs, "", ""],
                    ],
                },
            )

            menu = parse_menu(path)

        rice, fixed_combo, configurable_combo, drink_combo = menu["items"]
        self.assertEqual(rice["taxonomy"], "topped_rice")
        self.assertEqual(rice["requiredComponents"], ["每日专享招牌烤排饭"])
        self.assertEqual(rice["choiceGroups"][0]["selected"], "煎蛋")
        self.assertNotIn("热狗肠", rice["requiredComponents"])

        self.assertEqual(fixed_combo["requiredComponents"], ["烤肉", "烤排", "鸡排"])
        self.assertEqual(fixed_combo["components"], ["烤肉", "烤排", "鸡排", "煎蛋"])
        self.assertEqual(fixed_combo["flavorModifiers"], ["人气香辣", "蜜汁味", "甜辣味"])

        self.assertEqual(configurable_combo["requiredComponents"], [])
        self.assertEqual(
            [group["selected"] for group in configurable_combo["choiceGroups"]],
            ["烤肉", "烤排", "鸡排", "热狗肠"],
        )
        self.assertEqual(configurable_combo["components"], ["烤肉", "烤排", "鸡排", "热狗肠"])
        self.assertNotIn("品", configurable_combo["components"])

        self.assertEqual(drink_combo["requiredComponents"], ["招牌拌饭", "大鸡腿"])
        self.assertEqual(drink_combo["choiceGroups"][-1]["role"], "drink")
        self.assertEqual(drink_combo["choiceGroups"][-1]["selected"], "随机饮品")

    def test_audit_menus_reports_each_valid_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(2):
                save_workbook(
                    root / f"运营数据_测试店{index}.xlsx",
                    {
                        "调研结果": [
                            ["一级分类", "菜单名", "活动价"],
                            ["热销", f"菜品{index}", "12"],
                        ],
                    },
                )
            (root / "ignore.txt").write_text("not a menu", encoding="utf-8")

            audit = audit_menus(root)

        self.assertEqual(audit["files"], 2)
        self.assertEqual(audit["parsed"], 2)
        self.assertEqual(audit["failed"], 0)
        self.assertEqual(audit["totalItems"], 2)
        self.assertTrue(all(menu["taxonomyVersion"] == "2026-07-30.v2" for menu in audit["menus"]))
        self.assertTrue(all(sum(menu["taxonomyCounts"].values()) == menu["count"] for menu in audit["menus"]))


if __name__ == "__main__":
    unittest.main()
