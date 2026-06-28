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
            right_drink = root / "style-2" / "金桔柠檬水.jpg"
            for path in (wrong_snack, wrong_rice, right_drink):
                make_image(path)
            library = [
                app_module.LibraryImage("wrong-snack", wrong_snack, "测试店", "黄乐条", app_module.normalize("黄乐条"), app_module.grams(app_module.normalize("黄乐条")), "style-1"),
                app_module.LibraryImage("wrong-rice", wrong_rice, "测试店", "一碗米饭", app_module.normalize("一碗米饭"), app_module.grams(app_module.normalize("一碗米饭")), "style-1"),
                app_module.LibraryImage("right-drink", right_drink, "测试店", "金桔柠檬水", app_module.normalize("金桔柠檬水"), app_module.grams(app_module.normalize("金桔柠檬水")), "style-2"),
            ]

            drink_item = {"row": 1, "name": "手打金桔柠檬水", "norm": app_module.normalize("手打金桔柠檬水")}
            food_item = {"row": 2, "name": "北京炒合菜", "norm": app_module.normalize("北京炒合菜")}

            drink_candidates = app_module.top_candidates(drink_item, library)
            food_candidates = app_module.top_candidates(food_item, library)

            self.assertEqual([c["dishName"] for c in drink_candidates], ["金桔柠檬水"])
            self.assertEqual(food_candidates, [])

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


if __name__ == "__main__":
    unittest.main()
