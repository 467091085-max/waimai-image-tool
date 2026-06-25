from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from menu_parser import audit_menus, classify_basic_category, parse_menu


REAL_MENU_DIR = Path("/Users/guiguixiaxia/Documents/menus")


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
        self.assertEqual(menu["kindCounts"], {"single": 1, "combo": 1, "snack": 1, "other": 0, "total": 3})
        self.assertEqual(menu["basicCategoryCounts"]["炒菜/盖饭"], 1)
        self.assertEqual(menu["basicCategoryCounts"]["套餐"], 1)
        self.assertEqual(menu["basicCategoryCounts"]["饮品"], 1)
        self.assertEqual(menu["sheets"][0]["sheet"], "调研结果")
        self.assertEqual(menu["sheets"][0]["headerRow"], 3)
        for item in menu["items"]:
            self.assertGreaterEqual(item.keys(), {"row", "category", "name", "price", "kind", "basicCategory", "norm", "components"})

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

    def test_basic_category_distinguishes_risky_non_main_items(self) -> None:
        cases = {
            "一碗米饭": "主食",
            "加鸡蛋": "小料",
            "餐具": "其他",
            "收藏福利": "其他",
            "手打金桔柠檬水": "饮品",
            "经典螺蛳粉": "米粉/米线",
            "北京炒合菜": "炒菜/盖饭",
        }
        for name, expected in cases.items():
            self.assertEqual(classify_basic_category(name), expected)

    def test_parse_failure_reports_actionable_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "不是菜单.xlsx"
            save_workbook(path, {"说明": [["门店", "备注"], ["测试店", "只有说明没有菜品表头"]]})

            with self.assertRaisesRegex(ValueError, "菜品名/商品名称/菜单名"):
                parse_menu(path)

    @unittest.skipUnless(REAL_MENU_DIR.exists(), "真实菜单目录不存在")
    def test_real_menu_directory_samples_parse_including_xls(self) -> None:
        audit = audit_menus(REAL_MENU_DIR)

        self.assertGreaterEqual(audit["files"], 20)
        self.assertEqual(audit["parsed"], audit["files"])
        self.assertEqual(audit["failed"], 0, audit["errors"])
        self.assertGreater(audit["totalItems"], 2500)
        self.assertTrue(any(record["file"].endswith(".xls") for record in audit["menus"]))
        self.assertTrue(any(record["basicCategoryCounts"].get("米粉/米线", 0) for record in audit["menus"]))


if __name__ == "__main__":
    unittest.main()
