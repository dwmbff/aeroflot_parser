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


if __name__ == "__main__":
    unittest.main()
