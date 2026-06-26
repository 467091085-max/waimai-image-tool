from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CustomerUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_customer_workbench_has_four_clear_steps(self) -> None:
        self.assertIn("外卖菜品图一键生成工具", self.template)
        self.assertEqual(self.template.count('class="round-step'), 4)
        for label in ["上传菜单", "选择风格/样图", "正式出图", "导出图片"]:
            self.assertIn(label, self.template)
        self.assertNotIn("sampleShortcutBtn", self.template)
        self.assertIn('["", "上传菜单", "选择风格/样图", "正式出图", "导出"]', self.app_js)

    def test_points_and_quality_are_customer_facing(self) -> None:
        for required in ["积分余额", "充值积分", "预计消耗", "普通出图", "精修出图", "100积分/张", "200积分/张"]:
            self.assertIn(required, self.template)
        for forbidden in ["混元", "Gemini", "人民币", "1元/张", "2元/张"]:
            self.assertNotIn(forbidden, self.template)

    def test_delivery_platforms_are_not_default_locked(self) -> None:
        platform_inputs = re.findall(r'<input class="platform-check"[^>]+>', self.template)
        self.assertEqual(len(platform_inputs), 3)
        self.assertTrue(all("checked" not in field for field in platform_inputs))
        for label in ["美团", "淘宝/饿了么", "京东"]:
            self.assertIn(label, self.template)


if __name__ == "__main__":
    unittest.main()
