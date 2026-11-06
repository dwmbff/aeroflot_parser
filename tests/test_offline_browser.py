"""
Тесты в настоящем браузере, но без сайта Аэрофлота: страница берётся из
tests/fixtures/search_form.html. Проверяют самые рискованные места —
запасные селекторы resolver и выбор даты в календаре (включая переход через год).
Если Chromium не установлен (playwright install chromium), тесты пропускаются.

Запуск: python -m unittest discover -s tests
"""

import copy
import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

import browser  # noqa: E402
import resolver  # noqa: E402

FIXTURE = (ROOT / "tests" / "fixtures" / "search_form.html").read_text(encoding="utf-8")
ELEMENTS = json.loads((ROOT / "elements.json").read_text(encoding="utf-8"))


class OfflineBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Exception as exc:  # браузер не установлен
            cls.pw.stop()
            raise unittest.SkipTest(f"Chromium недоступен: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page(locale="ru-RU")
        self.page.set_content(FIXTURE)
        self.elements = copy.deepcopy(ELEMENTS)
        resolver.set_elements(self.elements)
        resolver.ai_attempted.clear()
        resolver.ai_fixed.clear()

    def tearDown(self):
        self.page.close()

    def _break_xpath(self, name):
        next(e for e in self.elements if e["name"] == name)["xpath"] = "/html/body/div[99]/span"

    def test_xpath_found_directly(self):
        field = resolver.resolve(self.page, "from_input", timeout_ms=2000, use_ai=False)
        self.assertEqual(field.get_attribute("placeholder"), "Откуда")

    def test_fallback_used_when_xpath_is_broken(self):
        self._break_xpath("to_input")
        field = resolver.resolve(self.page, "to_input", timeout_ms=2000, use_ai=False)
        self.assertEqual(field.get_attribute("placeholder"), "Куда")

    def test_to_input_does_not_match_from_input(self):
        # «Куда» — часть слова «Откуда»: запасной селектор обязан сравнивать целиком
        self._break_xpath("to_input")
        field = resolver.resolve(self.page, "to_input", timeout_ms=2000, use_ai=False)
        self.assertNotEqual(field.get_attribute("placeholder"), "Откуда")

    def test_missing_element_raises_without_ai(self):
        self._break_xpath("search_btn")
        element = next(e for e in self.elements if e["name"] == "search_btn")
        element["fallbacks"] = []
        with self.assertRaises(resolver.ElementNotFound):
            resolver.resolve(self.page, "search_btn", timeout_ms=600, use_ai=False)

    def test_select_depart_and_return_dates(self):
        depart = date.today() + timedelta(days=7)
        back = date.today() + timedelta(days=14)
        browser.select_date(self.page, depart)
        browser.select_date(self.page, back, "return_input")
        iso = lambda d: f"{d.year}-{d.month}-{d.day}"
        self.assertEqual(self.page.get_by_placeholder("Туда", exact=True).input_value(), iso(depart))
        self.assertEqual(self.page.get_by_placeholder("Обратно", exact=True).input_value(), iso(back))

    def test_select_date_across_year_boundary(self):
        # Через ~10 месяцев год почти наверняка другой: проверяем, что год вычисляется верно
        target = date.today() + timedelta(days=300)
        browser.select_date(self.page, target)
        self.assertEqual(
            self.page.get_by_placeholder("Туда", exact=True).input_value(),
            f"{target.year}-{target.month}-{target.day}",
        )

    def test_date_outside_calendar_raises(self):
        with self.assertRaises(RuntimeError):
            browser.select_date(self.page, date.today() + timedelta(days=700))


    # ---- восстановление пропавшего элемента: запасные селекторы -> ИИ ----

    def _patch_ai(self, answers):
        """Подменить обращение к модели: отдаёт заготовленные xpath по очереди и запоминает замечания."""
        import ai_fallback
        calls = []
        queue = list(answers)

        def fake(info, html, digest="", feedback=""):
            calls.append(feedback)
            return ai_fallback.FallbackResult(True, queue.pop(0), "high", "тест")

        original = ai_fallback.find_alternative_xpath
        ai_fallback.find_alternative_xpath = fake
        self.addCleanup(setattr, ai_fallback, "find_alternative_xpath", original)
        return calls

    def _use_temp_new_xpath_file(self):
        import tempfile
        import config
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = config.NEW_XPATH_FILE
        config.NEW_XPATH_FILE = Path(tmp.name) / "new_xpath.txt"
        self.addCleanup(setattr, config, "NEW_XPATH_FILE", original)

    def test_restore_uses_fallback_before_ai(self):
        self._use_temp_new_xpath_file()
        calls = self._patch_ai([])  # если ИИ вызовут, queue.pop упадёт
        self._break_xpath("from_input")
        element = next(e for e in self.elements if e["name"] == "from_input")
        self.assertEqual(resolver.restore(self.page, element), "fallback")
        self.assertEqual(calls, [])
        self.assertIn("from_input", resolver.fallback_restored)

    def test_ai_answer_with_wrong_tag_is_rejected_and_corrected(self):
        self._use_temp_new_xpath_file()
        # 1-й ответ — кнопка вместо поля ввода, 2-й (после замечания) — правильный
        calls = self._patch_ai(["//button", "//input[@placeholder='Откуда']"])
        element = next(e for e in self.elements if e["name"] == "from_input")
        element["xpath"] = "/html/body/div[99]/span"
        element["fallbacks"] = []
        self.assertEqual(resolver.restore(self.page, element), "ai")
        self.assertEqual(element["xpath"], "//input[@placeholder='Откуда']")
        self.assertEqual(len(calls), 2)
        self.assertIn("не подошёл", calls[1])

    def test_ai_gives_up_after_two_bad_answers(self):
        self._use_temp_new_xpath_file()
        self._patch_ai(["//button", "//nonexistent"])
        element = next(e for e in self.elements if e["name"] == "from_input")
        element["xpath"] = "/html/body/div[99]/span"
        element["fallbacks"] = []
        self.assertIsNone(resolver.restore(self.page, element))
        self.assertNotIn("from_input", resolver.ai_fixed)
        self.assertEqual(element["xpath"], "/html/body/div[99]/span")  # чужой xpath не подставлен


if __name__ == "__main__":
    unittest.main()
