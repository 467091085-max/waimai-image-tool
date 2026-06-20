from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

import app as app_module


def make_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), (220, 120, 80)).save(path)


class StrictMatchingTests(unittest.TestCase):
    def test_unrelated_food_and_drink_images_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong_snack = root / "style-1" / "黄乐条.jpg"
            wrong_rice = root / "style-1" / "一碗米饭.jpg"
            wrong_prompt = root / "style-1" / "北京炒合菜提示勿点.jpg"
            right_drink = root / "style-2" / "金桔柠檬水.jpg"
            for path in (wrong_snack, wrong_rice, wrong_prompt, right_drink):
                make_image(path)
            library = [
                app_module.LibraryImage("wrong-snack", wrong_snack, "测试店", "黄乐条", app_module.normalize("黄乐条"), app_module.grams(app_module.normalize("黄乐条")), "style-1"),
                app_module.LibraryImage("wrong-rice", wrong_rice, "测试店", "一碗米饭", app_module.normalize("一碗米饭"), app_module.grams(app_module.normalize("一碗米饭")), "style-1"),
                app_module.LibraryImage(
                    "wrong-prompt",
                    wrong_prompt,
                    "测试店",
                    "北京炒合菜提示勿点",
                    app_module.normalize("北京炒合菜提示勿点"),
                    app_module.grams(app_module.normalize("北京炒合菜提示勿点")),
                    "style-1",
                ),
                app_module.LibraryImage("right-drink", right_drink, "测试店", "金桔柠檬水", app_module.normalize("金桔柠檬水"), app_module.grams(app_module.normalize("金桔柠檬水")), "style-2"),
            ]

            drink_item = {"row": 1, "name": "手打金桔柠檬水", "norm": app_module.normalize("手打金桔柠檬水")}
            food_item = {"row": 2, "name": "北京炒合菜", "norm": app_module.normalize("北京炒合菜")}
            crisp_item = {"row": 3, "name": "火爆双脆", "norm": app_module.normalize("火爆双脆")}

            drink_candidates = app_module.top_candidates(drink_item, library)
            food_candidates = app_module.top_candidates(food_item, library)
            crisp_candidates = app_module.top_candidates(crisp_item, library)

            self.assertEqual([c["dishName"] for c in drink_candidates], ["金桔柠檬水"])
            self.assertEqual(food_candidates, [])
            self.assertEqual(crisp_candidates, [])

    def test_close_alias_still_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "style-1" / "辣椒小炒肉盖饭.jpg"
            make_image(path)
            library = [
                app_module.LibraryImage("alias", path, "测试店", "辣椒小炒肉盖饭", app_module.normalize("辣椒小炒肉盖饭"), app_module.grams(app_module.normalize("辣椒小炒肉盖饭")), "style-1")
            ]
            item = {"row": 1, "name": "老长沙辣椒炒肉盖码饭", "norm": app_module.normalize("老长沙辣椒炒肉盖码饭")}

            candidates = app_module.top_candidates(item, library)

            self.assertEqual(candidates[0]["dishName"], "辣椒小炒肉盖饭")
            self.assertGreaterEqual(candidates[0]["score"], 70)

    def test_first_alias_batch_matches(self) -> None:
        alias_cases = [
            ("小炒黄牛肉", "爆炒黄牛肉"),
            ("番茄炒蛋", "西红柿炒鸡蛋"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = []
            for idx, (_, image_name) in enumerate(alias_cases, start=1):
                path = root / "style-1" / f"{image_name}.jpg"
                make_image(path)
                library.append(
                    app_module.LibraryImage(
                        f"alias-{idx}",
                        path,
                        "测试店",
                        image_name,
                        app_module.normalize(image_name),
                        app_module.grams(app_module.normalize(image_name)),
                        "style-1",
                    )
                )

            for menu_name, image_name in alias_cases:
                item = {"row": 1, "name": menu_name, "norm": app_module.normalize(menu_name)}
                candidates = app_module.top_candidates(item, library)
                self.assertEqual(candidates[0]["dishName"], image_name)
                self.assertGreaterEqual(candidates[0]["score"], 70)


if __name__ == "__main__":
    unittest.main()
