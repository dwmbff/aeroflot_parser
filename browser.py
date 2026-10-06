from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

import config
import resolver
from checker import check_element_inline, log_event

ResultsBranch = Literal["flights", "empty"]

# --- Известное ограничение: защита сайта от автоматизации -----------------------
# aeroflot.ru показывает «Доступ к сайту временно ограничен», если видит
# стандартный headless-Chromium (проверено: без этих настроек страница с формой
# не открывается). Для учебной задачи используются три настройки ниже — как в
# исходном варианте проекта. Это обход защиты сайта, поэтому: скрипт делает один
# прогон за запуск с паузами между действиями, ничего не покупает и не
# авторизуется; для регулярного или промышленного использования нужно
# разрешение владельца сайта или официальный API (см. ANALYTICS.md, раздел 11).
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)

# Атрибут, которым JS помечает найденную кнопку дня, чтобы Playwright кликнул по ней «по-настоящему»
_DAY_MARK = "data-parser-target"

# Поиск дня в календаре без привязки к классам и индексам колонок:
# идём по DOM в порядке документа, запоминаем последний заголовок месяца,
# а каждой кнопке-числу сопоставляем этот месяц. Год считаем по порядку месяцев.
_FIND_DAY_JS = r"""
({day, month, year, refMonth, refYear, mark}) => {
  const PREFIXES = {
    'янв': 1, 'фев': 2, 'мар': 3, 'апр': 4, 'май': 5, 'мая': 5, 'июн': 6,
    'июл': 7, 'авг': 8, 'сен': 9, 'окт': 10, 'ноя': 11, 'дек': 12,
  };
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const monthOf = (text) => {
    const m = text.trim().toLowerCase().match(/^([а-яё]+)(?:\s+(\d{4}))?\s*г?\.?$/);
    if (!m) return null;
    const n = PREFIXES[m[1].slice(0, 3)];
    return n ? {n, year: m[2] ? Number(m[2]) : null} : null;
  };

  document.querySelectorAll('[' + mark + ']').forEach((el) => el.removeAttribute(mark));

  const months = [];            // все найденные названия месяцев (список слева и заголовки)
  for (const el of document.body.querySelectorAll('*')) {
    if (!visible(el)) continue;
    if (el.children.length === 0) {
      const info = monthOf(el.textContent || '');
      if (info) { months.push({...info, el, days: []}); continue; }
    }
    const isCell = el.tagName === 'BUTTON' || el.getAttribute('role') === 'gridcell';
    if (isCell && months.length && !el.disabled) {
      const label = (el.innerText || '').split('\n')[0].trim();
      if (/^\d{1,2}$/.test(label)) months[months.length - 1].days.push({label, el});
    }
  }

  // Настоящие панели месяцев — только те, под которыми есть дни
  const panels = months.filter((m) => m.days.length > 0);
  let y = refYear, prev = null;
  for (const p of panels) {
    if (p.year) y = p.year;
    else if (prev !== null && p.n < prev) y += 1;
    else if (prev === null && p.n < refMonth) y += 1;
    p.resolvedYear = y;
    prev = p.n;
  }

  const panel = panels.find((p) => p.n === month && p.resolvedYear === year);
  if (!panel) {
    // Месяц ещё не подгружен: пробуем кликнуть по его названию в боковом списке
    const side = months.find((m) => m.days.length === 0 && m.n === month);
    if (side) { side.el.click(); return 'clicked_side_list'; }
    return 'month_not_found: ' + panels.map((p) => p.n + '.' + p.resolvedYear).join(', ');
  }
  const cell = panel.days.find((d) => Number(d.label) === day);
  if (!cell) return 'day_not_found';
  cell.el.setAttribute(mark, '1');
  cell.el.scrollIntoView({block: 'center'});
  return 'ok';
}
"""


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


def _pause(page: Page, ms: int = 500) -> None:
    page.wait_for_timeout(ms)


def _click_with_retry(
    page: Page,
    name: str,
    *,
    attempts: int = 3,
    wait_ms: int = 2500,
    initial_wait_ms: int = 60000,
) -> None:
    """
    Дождаться элемента (результаты могут грузиться долго), затем кликнуть с повторами.
    Элемент каждый раз ищется заново через resolver: после ререндера старый locator устаревает.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            target = resolver.resolve(
                page,
                name,
                timeout_ms=initial_wait_ms if attempt == 1 else 10000,
            )
            target.scroll_into_view_if_needed(timeout=10000)
            target.click(timeout=10000)
            return
        except Exception as exc:
            last_exc = exc
            log_event(f"{name}: попытка {attempt}/{attempts} не удалась — {exc}")
            _pause(page, wait_ms)
    if last_exc:
        raise last_exc


def _wait_search_form_ready(page: Page) -> None:
    """Дождаться видимой формы поиска (любое из ключевых полей) и прокрутить к ней."""
    deadline = time.monotonic() + config.NAVIGATION_TIMEOUT / 1000
    last_error: Optional[Exception] = None

    while time.monotonic() < deadline:
        accept_cookies(page)
        check_and_close_popup(page)
        for name in ("from_input", "to_input", "calendar_open_btn", "search_btn"):
            try:
                field = resolver.find_visible(page, name)
                if field is not None:
                    field.scroll_into_view_if_needed()
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
    """Проверить попап «Хорошо» и закрыть, если виден."""
    closed = accept_cookies(page)

    popup = resolver.find_visible(page, "popup_ok_btn")
    if popup is not None:
        popup.click()
        _pause(page, 500)
        closed = True
    return closed


def check_error_state(page: Page) -> ErrorState:
    """Проверить блок ошибки «Что-то пошло не так»."""
    title_visible = resolver.find_visible(page, "something_went_wrong_title") is not None
    box_visible = resolver.find_visible(page, "something_went_wrong_box") is not None
    # box-xpath слишком общий и даёт ложные срабатывания — ошибка только при видимом заголовке
    return ErrorState(
        has_error=title_visible,
        title_visible=title_visible,
        box_visible=box_visible,
    )


def _fill_city_field(
    page: Page,
    field_name: str,
    city_name: str,
    *,
    suggest_element: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    _wait_search_form_ready(page)
    field = resolver.resolve(page, field_name)
    field.scroll_into_view_if_needed()
    field.click()
    field.fill("")
    field.type(city_name, delay=70)
    _pause(page, 1200)

    missing: Optional[dict[str, Any]] = None
    if suggest_element is not None:
        missing = check_dropdown_visible(page, suggest_element)

    suggestions = resolver.resolve_all(
        page, "suggest_items", timeout_ms=config.BROWSER_TIMEOUT
    )
    word = city_name.split()[0].lower()
    chosen = next(
        (s for s in suggestions if word in (s.inner_text() or "").lower()),
        suggestions[0],
    )
    chosen.click()
    _pause(page, 500)

    # Проверяем результат: в поле должен остаться выбранный город
    value = field.input_value().lower()
    if word not in value:
        raise RuntimeError(
            f"Поле {field_name}: после выбора подсказки значение {value!r}, ожидали {city_name!r}"
        )
    return missing


def check_dropdown_visible(page: Page, suggest_element: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Проверить видимость дропдауна подсказок до выбора города."""
    return check_element_inline(page, suggest_element)


def fill_from_input(
    page: Page,
    city_name: str,
    suggest_element: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Заполнить «Откуда», проверить дропдаун, выбрать подсказку."""
    return _fill_city_field(
        page,
        "from_input",
        city_name,
        suggest_element=suggest_element,
    )


def fill_to_input(page: Page, city_name: str) -> None:
    """Заполнить «Куда» и выбрать подсказку из дропдауна."""
    _fill_city_field(page, "to_input", city_name)


def _calendar_is_open(page: Page) -> bool:
    return resolver.find_visible(page, "calendar_month_spans") is not None


def open_calendar(page: Page, field_name: str = "calendar_open_btn") -> None:
    """Клик по полю даты («Туда» или «Обратно») для открытия календаря."""
    field = resolver.resolve(page, field_name)
    field.scroll_into_view_if_needed()
    field.click()
    _pause(page, 1000)
    resolver.resolve(page, "calendar_month_spans", timeout_ms=config.BROWSER_TIMEOUT)


def select_date(
    page: Page,
    target_date: date,
    field_name: str = "calendar_open_btn",
) -> date:
    """
    Выбрать дату в календаре и убедиться, что она попала в поле field_name
    («calendar_open_btn» — Туда, «return_input» — Обратно).
    Если календарь закрыт — сначала открывает его кликом по полю.
    """
    if not _calendar_is_open(page):
        open_calendar(page, field_name)

    today = date.today()
    status = ""
    for _ in range(3):
        status = page.evaluate(
            _FIND_DAY_JS,
            {
                "day": target_date.day,
                "month": target_date.month,
                "year": target_date.year,
                "refMonth": today.month,
                "refYear": today.year,
                "mark": _DAY_MARK,
            },
        )
        if status == "ok":
            break
        if status != "clicked_side_list":
            raise RuntimeError(f"Не удалось выбрать {target_date.isoformat()}: {status}")
        _pause(page, 800)  # даём календарю прокрутиться к месяцу и пробуем ещё раз
    else:
        raise RuntimeError(f"Календарь не прокрутился к {target_date.isoformat()}")

    page.locator(f"[{_DAY_MARK}='1']").first.click(timeout=config.BROWSER_TIMEOUT)

    # Проверка: после клика поле даты должно перестать быть пустым
    field = resolver.resolve(page, field_name)
    deadline = time.monotonic() + config.BROWSER_TIMEOUT / 1000
    while time.monotonic() < deadline:
        if field.input_value().strip():
            return target_date
        _pause(page, 200)
    raise RuntimeError(f"Дата {target_date.isoformat()} не появилась в поле {field_name}")


def click_search(page: Page) -> None:
    """Клик по кнопке «Найти»."""
    btn = resolver.resolve(page, "search_btn")
    btn.scroll_into_view_if_needed()
    btn.click()
    _pause(page, 2500)


def wait_results_ready(page: Page, timeout: int = 60000) -> ResultsBranch:
    """
    Дождаться results_ready_indicator или no_flights_message.
    Возвращает 'flights' или 'empty'.
    """
    deadline = time.monotonic() + timeout / 1000

    while time.monotonic() < deadline:
        if resolver.find_visible(page, "results_ready_indicator") is not None:
            return "flights"
        if resolver.find_visible(page, "no_flights_message") is not None:
            return "empty"
        _pause(page, 800)

    raise TimeoutError("Результаты поиска не загрузились в отведённое время")


def click_after_results_1(page: Page) -> None:
    """Клик по первому результату (ветка results_flow_1)."""
    _click_with_retry(page, "click_after_results_1")
    _pause(page, 1500)


def open_result2_panel(page: Page) -> None:
    """Открыть панель тарифов (ветка results_flow_2)."""
    _click_with_retry(page, "open_result2_panel")
    _pause(page, 1500)


def click_result2_button(page: Page) -> None:
    """Клик для раскрытия деталей (ветка results_flow_2)."""
    _click_with_retry(page, "click_result2_button")
    _pause(page, 1500)
