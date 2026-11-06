import json
import shutil
import tempfile
import unittest
from pathlib import Path

from server.content.catalog import ContentValidationError, load_catalogs


class ContentCatalogTests(unittest.TestCase):
    def test_catalog_meets_minimum_content_counts(self):
        catalog = load_catalogs()
        self.assertEqual(len(catalog.profile_questions), 21)
        self.assertGreaterEqual(len(catalog.questions), 150)
        self.assertGreaterEqual(len(catalog.mystery_templates), 40)
        self.assertEqual(len(catalog.profile_fields), 21)

    def test_question_bank_has_category_and_difficulty_variety(self):
        catalog = load_catalogs()
        categories = {item["category"] for item in catalog.questions}
        levels = {item["difficulty"] for item in catalog.questions}
        self.assertGreaterEqual(len(categories), 12)
        self.assertEqual(levels, {1, 2, 3})

    def test_all_questions_have_unique_options_and_both_use_modes(self):
        catalog = load_catalogs()
        for question in catalog.questions:
            self.assertGreaterEqual(len(question["options"]), 4)
            self.assertEqual(len({item["id"] for item in question["options"]}), len(question["options"]))
            self.assertEqual(len({item["text_ar"] for item in question["options"]}), len(question["options"]))
            self.assertTrue({"player_question", "live_choice"}.issubset(set(question["use_in"])))

    def test_every_mystery_uses_three_known_distinct_fields_and_ordered_clues(self):
        catalog = load_catalogs()
        for template in catalog.mystery_templates:
            self.assertEqual(len(template["required_fields"]), 3)
            self.assertEqual(len(set(template["required_fields"])), 3)
            self.assertTrue(set(template["required_fields"]).issubset(catalog.profile_fields))
            self.assertEqual([clue["level"] for clue in template["clues"]], [1, 2, 3])
            self.assertEqual([clue["field_id"] for clue in template["clues"]], template["required_fields"])

    def test_loader_rejects_duplicate_profile_field(self):
        source = Path(__file__).resolve().parents[1] / "server" / "content" / "data"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("profile_questions.json", "question_bank.json", "mystery_templates.json"):
                shutil.copy(source / name, root / name)
            path = root / "profile_questions.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["questions"][1]["field_id"] = data["questions"][0]["field_id"]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ContentValidationError):
                load_catalogs(root)

    def test_loader_rejects_unknown_mystery_field(self):
        source = Path(__file__).resolve().parents[1] / "server" / "content" / "data"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("profile_questions.json", "question_bank.json", "mystery_templates.json"):
                shutil.copy(source / name, root / name)
            path = root / "mystery_templates.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["templates"][0]["required_fields"][0] = "missing_private_field"
            data["templates"][0]["clues"][0]["field_id"] = "missing_private_field"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ContentValidationError):
                load_catalogs(root)


if __name__ == "__main__":
    unittest.main()
