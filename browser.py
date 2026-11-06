from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Optional
from checker import check_element_inline, log_event

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

import config
from checker import check_element_inline

ResultsBranch = Literal["flights", "empty"]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)

# Русские названия месяцев в заголовках календаря (род./им. падеж)
_MONTH_PREFIXES: list[tuple[str, int]] = [
    ("январ", 1),
    ("феврал", 2),
    ("март", 3),
    ("апрел", 4),
    ("ма", 5),
    ("июн", 6),
    ("июл", 7),
    ("август", 8),
    ("сентябр", 9),
    ("октябр", 10),
    ("ноябр", 11),
    ("декабр", 12),
]


def _load_xpaths() -> dict[str, str]:
    with config.ELEMENTS_FILE.open(encoding="utf-8") as fh:
        items = json.load(fh)
    return {item["name"]: item["xpath"] for item in items}


_X = _load_xpaths()

X_FROM_INPUT = _X["from_input"]
X_TO_INPUT = _X["to_input"]
X_SUGGEST_ITEMS = _X["suggest_items"]
X_CAL_OPEN = _X["calendar_open_btn"]
X_CAL_MONTH_SPANS = _X["calendar_month_spans"]
X_CAL_MONTH_PANEL_BASE = _X["calendar_month_panel_base"]
X_SEARCH_BTN = _X["search_btn"]
X_RESULTS_READY = _X["results_ready_indicator"]
X_NO_FLIGHTS = _X["no_flights_message"]
X_CLICK_AFTER_RESULTS_1 = _X["click_after_results_1"]
X_OPEN_RESULT2_PANEL = _X["open_result2_panel"]
X_CLICK_RESULT2_BUTTON = _X["click_result2_button"]
X_POPUP_OK_BTN = _X["popup_ok_btn"]
X_SOMETHING_WENT_WRONG_TITLE = _X["something_went_wrong_title"]
X_SOMETHING_WENT_WRONG_BOX = _X["something_went_wrong_box"]

X_CAL_CONTAINER = (
    "/html/body/div/div[2]/div/div/div/div/div/div[3]/div/div[2]/div/div[2]"
    "/div/div/div/div[3]/div[2]"
)


@dataclass
class BrowserHandle:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page

    def close(self) -> None:
        self.context.close()
        self.browser.close()
        self.playwright.stop()


@dataclass
class ErrorState:
    has_error: bool
    title_visible: bool
    box_visible: bool


def xpath_locator(page: Page, xpath: str):
    return page.locator(f"xpath={xpath}")


def first_visible_xpath(page: Page, xpath: str):
    """Первый видимый элемент по xpath (на сайте бывают скрытые дубликаты в DOM)."""
    locator = xpath_locator(page, xpath)
    count = locator.count()
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return locator.first

from checker import check_element_inline, log_event  # добавьте log_event в этот импорт


def _click_locator_with_retry(
    page: Page,
    locator,
    *,
    label: str,
    attempts: int = 3,
    wait_ms: int = 2500,
    initial_wait_ms: int = 60000,
) -> None:
    """Дождаться появления элемента (может грузиться с задержкой), затем кликнуть с retry."""
    try:
        locator.first.wait_for(state="attached", timeout=initial_wait_ms)
    except Exception as exc:
        log_event(f"{label}: элемент не появился за {initial_wait_ms}ms — {exc}")
        raise

    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            locator.first.scroll_into_view_if_needed(timeout=10000)
            locator.first.wait_for(state="visible", timeout=10000)
            locator.first.click(timeout=10000)
            return
        except Exception as exc:
            last_exc = exc
            log_event(f"{label}: попытка {attempt}/{attempts} не удалась — {exc}")
            _pause(page, wait_ms)
    if last_exc:
        raise last_exc

def _pause(page: Page, ms: int = 500) -> None:
    page.wait_for_timeout(ms)


def _wait_search_form_ready(page: Page) -> None:
    """Дождаться видимой формы поиска и прокрутить к ней."""
    deadline = time.monotonic() + config.NAVIGATION_TIMEOUT / 1000
    last_error: Optional[Exception] = None

    while time.monotonic() < deadline:
        accept_cookies(page)
        check_and_close_popup(page)
        for placeholder_text in ("Откуда", "Куда", "Туда"):
            try:
                placeholder = page.get_by_placeholder(placeholder_text, exact=False)
                for index in range(placeholder.count()):
                    candidate = placeholder.nth(index)
                    if candidate.is_visible():
                        candidate.scroll_into_view_if_needed()
                        _pause(page, 500)
                        return
            except Exception as exc:
                last_error = exc

        for field_xpath in (X_FROM_INPUT, X_TO_INPUT, X_CAL_OPEN):
            try:
                locator = xpath_locator(page, field_xpath)
                for index in range(locator.count()):
                    candidate = locator.nth(index)
                    try:
                        if candidate.is_visible():
                            candidate.scroll_into_view_if_needed()
                            _pause(page, 500)
                            return
                    except Exception as exc:
                        last_error = exc
            except Exception as exc:
                last_error = exc

        for label in ("Найти", "Поиск билетов"):
            try:
                marker = page.get_by_role("button", name=label) if label == "Найти" else page.get_by_text(label, exact=False)
                for index in range(marker.count()):
                    candidate = marker.nth(index)
                    if candidate.is_visible():
                        candidate.scroll_into_view_if_needed()
                        _pause(page, 500)
                        return
            except Exception as exc:
                last_error = exc

        try:
            page.evaluate("window.scrollTo(0, 0)")
        except Exception as exc:
            last_error = exc
        _pause(page, 1500)

    raise RuntimeError(f"Форма поиска не появилась: {last_error}")


def launch_browser(headless: bool = False) -> BrowserHandle:
    """Запуск Playwright Chromium с настройками для aeroflot.ru."""
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1920, "height": 1080},
        user_agent=_USER_AGENT,
        extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"},
    )
    context.add_init_script(_STEALTH_INIT_SCRIPT)
    page = context.new_page()
    page.set_default_timeout(config.BROWSER_TIMEOUT)

    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            page.goto(
                config.BASE_URL,
                wait_until="load",
                timeout=config.NAVIGATION_TIMEOUT,
            )
            _pause(page, 5000 + attempt * 2000)
            accept_cookies(page)
            _pause(page, 1000)
            accept_cookies(page)
            check_and_close_popup(page)
            _wait_search_form_ready(page)
            return BrowserHandle(
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
            )
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                _pause(page, 2000)

    browser.close()
    playwright.stop()
    raise RuntimeError(f"Не удалось открыть страницу Aeroflot: {last_error}")


def accept_cookies(page: Page) -> bool:
    """Закрыть баннер cookie/согласия при первом заходе, если он виден."""
    selectors = [
        "button:has-text('Принять')",
        "button:has-text('Принять все')",
        "button:has-text('Согласен')",
        "button:has-text('Согласиться')",
        "button:has-text('Accept')",
        "button:has-text('Хорошо')",
        "button:has-text('Accept all')",
        "[data-testid='cookie-accept']",
        "[class*='cookie'] button:has-text('Принять')",
        "[class*='Cookie'] button:has-text('Принять')",
        "[class*='consent'] button",
    ]
    for sel in selectors:
        btn = page.locator(sel)
        if btn.count() == 0:
            continue
        try:
            candidate = btn.first
            if candidate.is_visible(timeout=1500):
                candidate.click()
                _pause(page, 800)
                return True
        except Exception:
            continue
    return False


def check_and_close_popup(page: Page) -> bool:
    """Проверить X_POPUP_OK_BTN и закрыть, если виден."""
    closed = accept_cookies(page)

    popup = xpath_locator(page, X_POPUP_OK_BTN)
    if popup.count() > 0 and popup.first.is_visible():
        popup.first.click()
        _pause(page, 500)
        closed = True
    return closed


def check_error_state(page: Page) -> ErrorState:
    """Проверить блок ошибки «Что-то пошло не так»."""
    title = xpath_locator(page, X_SOMETHING_WENT_WRONG_TITLE)
    box = xpath_locator(page, X_SOMETHING_WENT_WRONG_BOX)
    title_visible = title.count() > 0 and title.first.is_visible()
    box_visible = box.count() > 0 and box.first.is_visible()
    # box-xpath слишком общий и даёт ложные срабатывания — ошибка только при видимом заголовке
    return ErrorState(
        has_error=title_visible,
        title_visible=title_visible,
        box_visible=box_visible,
    )


def _fill_city_field(
    page: Page,
    input_xpath: str,
    city_name: str,
    *,
    suggest_element: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    _wait_search_form_ready(page)
    field = first_visible_xpath(page, input_xpath)
    field.scroll_into_view_if_needed()
    field.click()
    field.fill("")
    field.type(city_name, delay=70)
    _pause(page, 1200)

    missing: Optional[dict[str, Any]] = None
    if suggest_element is not None:
        missing = check_dropdown_visible(page, suggest_element)

    suggestions = xpath_locator(page, X_SUGGEST_ITEMS)
    suggestions.first.wait_for(state="visible", timeout=config.BROWSER_TIMEOUT)

    matched = suggestions.filter(has_text=city_name.split()[0])
    if matched.count() > 0:
        matched.first.click()
    else:
        suggestions.first.click()
    _pause(page, 500)
    return missing


def check_dropdown_visible(page: Page, suggest_element: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Проверить видимость дропдауна подсказок до выбора города."""
    return check_element_inline(page, suggest_element, require_visible=True)


def fill_from_input(
    page: Page,
    city_name: str,
    suggest_element: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Заполнить «Откуда», проверить дропдаун, выбрать подсказку."""
    return _fill_city_field(
        page,
        X_FROM_INPUT,
        city_name,
        suggest_element=suggest_element,
    )


def fill_to_input(page: Page, city_name: str) -> None:
    """Заполнить «Куда» и выбрать подсказку из дропдауна."""
    _fill_city_field(page, X_TO_INPUT, city_name)


def open_calendar(page: Page) -> None:
    """Клик по полю даты (X_CAL_OPEN) для открытия календаря."""
    field = first_visible_xpath(page, X_CAL_OPEN)
    field.scroll_into_view_if_needed()
    field.click()
    _pause(page, 1000)
    xpath_locator(page, X_CAL_MONTH_SPANS).first.wait_for(
        state="visible",
        timeout=config.BROWSER_TIMEOUT,
    )


def _parse_month_header(text: str, reference: date) -> tuple[int, int]:
    lowered = text.lower().strip()
    year_match = re.search(r"(20\d{2})", lowered)
    year = int(year_match.group(1)) if year_match else reference.year

    month_num: Optional[int] = None
    for prefix, num in _MONTH_PREFIXES:
        if prefix in lowered:
            month_num = num
            break
    if month_num is None:
        raise ValueError(f"Не удалось распознать месяц в заголовке: {text!r}")

    if year_match is None and month_num < reference.month:
        year += 1
    return month_num, year


def _read_visible_months(page: Page, reference: date) -> list[tuple[int, int, int]]:
    """
    Прочитать видимые месяцы календаря.
    Возвращает список (column_index, month, year).
    column_index — индекс среди X_CAL_MONTH_SPANS (0..9).
    """
    headers = xpath_locator(page, X_CAL_MONTH_SPANS)
    count = headers.count()
    result: list[tuple[int, int, int]] = []
    for idx in range(count):
        text = headers.nth(idx).inner_text().strip()
        if not text:
            continue
        month, year = _parse_month_header(text, reference)
        result.append((idx, month, year))
    return result


def _scroll_calendar(page: Page, direction: Literal["prev", "next"]) -> None:
    """
    Прокрутка ленты месяцев.
    Пустые кнопки cTamTk в контейнере календаря — стрелки «назад/вперёд».
    """
    nav_xpath = (
        f"{X_CAL_CONTAINER}//button[contains(@class,'cTamTk') "
        f"and normalize-space(.)='']"
    )
    buttons = xpath_locator(page, nav_xpath)
    if buttons.count() < 2:
        raise RuntimeError("Кнопки навигации календаря не найдены")

    index = 0 if direction == "prev" else 1
    buttons.nth(index).click()
    _pause(page, 700)


def _ensure_month_visible(page: Page, target: date) -> int:
    """
    Дождаться появления целевого месяца среди видимых колонок.
    Возвращает column_index (0-based среди X_CAL_MONTH_SPANS).
    """
    for _ in range(24):
        visible = _read_visible_months(page, target)
        for col_idx, month, year in visible:
            if month == target.month and year == target.year:
                return col_idx

        if not visible:
            raise RuntimeError("Календарь открыт, но заголовки месяцев не найдены")

        first = visible[0]
        last = visible[-1]
        first_key = first[2] * 12 + first[1]
        last_key = last[2] * 12 + last[1]
        target_key = target.year * 12 + target.month

        if target_key < first_key:
            _scroll_calendar(page, "prev")
        elif target_key > last_key:
            _scroll_calendar(page, "next")
        else:
            raise RuntimeError(
                f"Месяц {target.month:02d}.{target.year} не найден среди колонок календаря"
            )

    raise RuntimeError(
        f"Не удалось прокрутить календарь к {target.isoformat()} за 24 шага"
    )


def _click_day_in_month_column(page: Page, column_index: int, day: int) -> None:
    """
    Выбрать день в колонке месяца.

    X_CAL_MONTH_PANEL_BASE совпадает с несколькими div:
      [1] — строка дней недели (Пн..Вс),
      [2..11] — колонки месяцев с кнопками-днями (текст вида «9\\n5 640 ₽»).
    column_index из X_CAL_MONTH_SPANS (0..9) → xpath-индекс panel = column_index + 2.
    """
    clicked = page.evaluate(
        """({panelBase, colIdx, dayNum}) => {
            const panelXpath = `(${panelBase})[${colIdx + 2}]`;
            const panel = document.evaluate(
                panelXpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
            ).singleNodeValue;
            if (!panel) return false;
            for (const btn of panel.querySelectorAll('button')) {
                if (btn.className.includes('cTamTk')) continue;
                const label = (btn.innerText || '').split('\\n')[0].trim();
                if (label === String(dayNum)) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""",
        {"panelBase": X_CAL_MONTH_PANEL_BASE, "colIdx": column_index, "dayNum": day},
    )
    if not clicked:
        raise RuntimeError(
            f"День {day} не найден в колонке месяца {column_index}"
        )
    _pause(page, 600)


def select_date(page: Page, target_date: date) -> date:
    """
    Выбрать дату в открытом календаре.

    1. Найти колонку месяца по заголовкам X_CAL_MONTH_SPANS.
    2. При необходимости прокрутить ленту месяцев стрелками.
    3. Кликнуть кнопку дня внутри X_CAL_MONTH_PANEL_BASE[column].
    """
    if xpath_locator(page, X_CAL_MONTH_SPANS).count() == 0:
        open_calendar(page)

    column_index = _ensure_month_visible(page, target_date)
    _click_day_in_month_column(page, column_index, target_date.day)

    expected_fragment = str(target_date.day)
    deadline = page.evaluate("() => Date.now()") + config.BROWSER_TIMEOUT
    while page.evaluate("() => Date.now()") < deadline:
        value = xpath_locator(page, X_CAL_OPEN).input_value()
        if expected_fragment in value:
            return target_date
        _pause(page, 200)

    return target_date
  
    
def click_search(page: Page) -> None:
    """Клик по кнопке «Поиск»."""
    btn = first_visible_xpath(page, X_SEARCH_BTN)
    btn.scroll_into_view_if_needed()
    btn.click()
    _pause(page, 2500)


def wait_results_ready(page: Page, timeout: int = 60000) -> ResultsBranch:
    """
    Дождаться X_RESULTS_READY или X_NO_FLIGHTS.
    Возвращает 'flights' или 'empty'.
    """
    deadline = time.monotonic() + timeout / 1000

    while time.monotonic() < deadline:
        results = xpath_locator(page, X_RESULTS_READY)
        for index in range(results.count()):
            try:
                if results.nth(index).is_visible():
                    return "flights"
            except Exception:
                continue

        no_flights = xpath_locator(page, X_NO_FLIGHTS)
        if no_flights.count() > 0 and no_flights.first.is_visible():
            return "empty"

        _pause(page, 800)

    raise TimeoutError("Результаты поиска не загрузились в отведённое время")


def reload_results_page(page: Page, timeout: int = 60000) -> ResultsBranch:
    """Перезагрузить страницу результатов и дождаться готовности."""
    last_error: Optional[Exception] = None

    for attempt in range(2):
        try:
            page.reload(wait_until="domcontentloaded", timeout=min(timeout, 45000))
        except PlaywrightTimeoutError:
            pass
        accept_cookies(page)
        check_and_close_popup(page)
        _pause(page, 3000 + attempt * 2000)
        try:
            return wait_results_ready(page, timeout=timeout)
        except TimeoutError as exc:
            last_error = exc

    raise TimeoutError(
        str(last_error) if last_error else "Результаты поиска не загрузились в отведённое время"
    )


def click_after_results_1(page: Page) -> None:
    """Клик по первому результату (ветка results_flow_1)."""
    locator = xpath_locator(page, X_CLICK_AFTER_RESULTS_1)
    _click_locator_with_retry(page, locator, label="click_after_results_1")
    _pause(page, 1500)


def open_result2_panel(page: Page) -> None:
    """Открыть панель тарифов (ветка results_flow_2)."""
    locator = xpath_locator(page, X_OPEN_RESULT2_PANEL)
    _click_locator_with_retry(page, locator, label="open_result2_panel")
    _pause(page, 1500)


def click_result2_button(page: Page) -> None:
    """Клик для раскрытия деталей (ветка results_flow_2)."""
    locator = xpath_locator(page, X_CLICK_RESULT2_BUTTON)
    _click_locator_with_retry(page, locator, label="click_result2_button")
    _pause(page, 1500)