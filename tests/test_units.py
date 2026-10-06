"""Быстрые тесты без браузера и без сети: python -m unittest discover -s tests"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_fallback  # noqa: E402
import checker  # noqa: E402
import resolver  # noqa: E402

# Элементы, на которые ссылается код сценария
NAMES_USED_BY_CODE = {
    "from_input", "to_input", "return_input", "calendar_open_btn",
    "calendar_month_spans", "search_btn", "suggest_items",
    "results_ready_indicator", "no_flights_message",
    "click_after_results_1", "open_result2_panel", "click_result2_button",
    "popup_ok_btn", "something_went_wrong_title", "something_went_wrong_box",
}


class ElementsFileTests(unittest.TestCase):
    def setUp(self):
        self.elements = json.loads((ROOT / "elements.json").read_text(encoding="utf-8"))

    def test_required_fields_and_unique_names(self):
        names = [e["name"] for e in self.elements]
        self.assertEqual(len(names), len(set(names)), "имена элементов должны быть уникальны")
        for element in self.elements:
            for key in ("name", "xpath", "stage"):
                self.assertTrue(element.get(key), f"{element.get('name')}: нет поля {key}")

    def test_all_elements_used_by_code_exist(self):
        names = {e["name"] for e in self.elements}
        self.assertFalse(NAMES_USED_BY_CODE - names)

    def test_form_fields_are_not_absolute_xpath(self):
        for name in ("from_input", "to_input", "return_input", "calendar_open_btn", "search_btn"):
            element = next(e for e in self.elements if e["name"] == name)
            self.assertFalse(element["xpath"].startswith("/html"), name)
            self.assertTrue(element.get("fallbacks"), f"{name}: нет запасных селекторов")


class AiResponseParsingTests(unittest.TestCase):
    def test_plain_json(self):
        result = ai_fallback._parse_model_response(
            '{"found": true, "new_xpath": " //a ", "confidence": "high", "reasoning": "ok"}'
        )
        self.assertTrue(result.found)
        self.assertEqual(result.new_xpath, "//a")

    def test_markdown_fences_are_stripped(self):
        result = ai_fallback._parse_model_response(
            '```json\n{"found": false, "new_xpath": "", "confidence": "low", "reasoning": "x"}\n```'
        )
        self.assertIsNotNone(result)
        self.assertFalse(result.found)

    def test_garbage_returns_none(self):
        self.assertIsNone(ai_fallback._parse_model_response("не json"))
        self.assertIsNone(ai_fallback._parse_model_response("[1, 2]"))


class HtmlPreparationTests(unittest.TestCase):
    def test_scripts_styles_comments_removed(self):
        html = "<!-- c --><script>x()</script><style>a{}</style><p>текст</p>"
        self.assertEqual(ai_fallback._clean_html(html), "<p>текст</p>")

    def test_truncate_keeps_fragment_around_keyword(self):
        html = "a" * 40000 + "«Откуда»" + "b" * 40000
        snippet = ai_fallback._truncate_html(html, "Поле «Откуда»", max_chars=1000)
        self.assertLessEqual(len(snippet), 1000)
        self.assertIn("Откуда", snippet)

    def test_truncate_prefers_keyword_inside_placeholder(self):
        text_mention = "обратно " + "a" * 30000
        field = '<input placeholder="Обратно">' + "b" * 30000
        snippet = ai_fallback._truncate_html(text_mention + field, "Поле «Обратно»", max_chars=2000)
        self.assertIn('placeholder="Обратно"', snippet)


class CheckerAndResolverTests(unittest.TestCase):
    ELEMENTS = [
        {"name": "a", "xpath": "//a", "stage": "s1", "action_before": None},
        {"name": "b", "xpath": "//b", "stage": "s1", "action_before": "go"},
        {"name": "c", "xpath": "//c", "stage": "s2", "inline_only": True},
    ]

    def test_action_before_blocks_until_done(self):
        names = lambda done: [e["name"] for e in checker._eligible_elements(self.ELEMENTS, "s1", done)]
        self.assertEqual(names(set()), ["a"])
        self.assertEqual(names({"go"}), ["a", "b"])

    def test_inline_only_not_checked_by_stage(self):
        self.assertEqual(checker._eligible_elements(self.ELEMENTS, "s2", set()), [])

    def test_resolver_lookup_and_ai_patch_is_shared(self):
        resolver.set_elements(self.ELEMENTS)
        self.assertEqual(resolver.get_xpath("a"), "//a")
        self.ELEMENTS[0]["xpath"] = "//patched"  # так AI подменяет xpath
        self.assertEqual(resolver.get_xpath("a"), "//patched")
        with self.assertRaises(KeyError):
            resolver.get_element("нет такого")


class SecurityTests(unittest.TestCase):
    def test_no_api_key_in_tracked_templates(self):
        import re
        for name in (".env.example", "README.md", "ANALYTICS.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"sk-or-v1-[A-Za-z0-9]{10,}", text), name)

    def test_env_files_are_git_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", ignore)

    def test_log_text_is_single_line(self):
        # ответ внешнего сервиса не должен уметь дописывать «чужие» строки в журнал
        evil = "ok\n[2026-01-01 00:00:00] [OK] fake_element: //x"
        self.assertNotIn("\n", checker.sanitize_for_log(evil))

    def test_long_log_text_is_truncated(self):
        self.assertLessEqual(len(checker.sanitize_for_log("a" * 5000, limit=100)), 101)

    def test_implausible_xpath_from_model_is_dropped(self):
        parse = ai_fallback._parse_model_response
        def xpath_of(raw_xpath):
            data = json.dumps({"found": True, "new_xpath": raw_xpath, "confidence": "high", "reasoning": ""})
            return parse(data).new_xpath
        self.assertEqual(xpath_of("//input[@placeholder='x']"), "//input[@placeholder='x']")
        self.assertEqual(xpath_of("rm -rf /"), "")
        self.assertEqual(xpath_of("//a" + "b" * 600), "")
        self.assertEqual(xpath_of("//a\n//b"), "")

    def test_unknown_confidence_becomes_low(self):
        data = json.dumps({"found": True, "new_xpath": "//a", "confidence": "100%", "reasoning": ""})
        self.assertEqual(ai_fallback._parse_model_response(data).confidence, "low")

    def test_new_xpath_file_line_cannot_be_split(self):
        import config
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            original = config.NEW_XPATH_FILE
            config.NEW_XPATH_FILE = Path(tmp) / "new_xpath.txt"
            try:
                ai_fallback.write_successful_xpath("el", "//old", "//new\nfake | //x | high", "high")
                lines = config.NEW_XPATH_FILE.read_text(encoding="utf-8").splitlines()
            finally:
                config.NEW_XPATH_FILE = original
        self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
