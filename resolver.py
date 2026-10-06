"""
Поиск элементов страницы, устойчивый к смене вёрстки.

Элемент ищется в три шага (останавливаемся на первом успешном):
  1. xpath из elements.json;
  2. запасные селекторы "fallbacks" из того же файла (привязка к смыслу:
     placeholder, текст, роль — а не к положению в дереве);
  3. AI: модель предлагает новый xpath по HTML страницы, мы проверяем,
     что он реально находит видимый элемент, и запоминаем его.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from playwright.sync_api import Locator, Page

from checker import load_elements, log_event

POLL_MS = 500

_state: dict[str, list[dict[str, Any]]] = {"elements": []}
_logged: set[tuple[str, str]] = set()

# Имена элементов, для которых AI уже вызывали (чтобы не дёргать API повторно)
ai_attempted: set[str] = set()
# Имена элементов, для которых AI нашёл рабочий xpath
ai_fixed: set[str] = set()


class ElementNotFound(LookupError):
    """Элемент не найден ни по xpath, ни по запасным селекторам, ни через AI."""


def set_elements(elements: list[dict[str, Any]]) -> None:
    """Общий список элементов: AI правит xpath прямо в нём, и все видят правку."""
    _state["elements"] = elements


def get_elements() -> list[dict[str, Any]]:
    if not _state["elements"]:
        _state["elements"] = load_elements()
    return _state["elements"]


def get_element(name: str) -> dict[str, Any]:
    for element in get_elements():
        if element.get("name") == name:
            return element
    raise KeyError(f"Элемент '{name}' не описан в elements.json")


def get_xpath(name: str) -> str:
    return get_element(name)["xpath"]


def _visible_matches(locator: Locator) -> list[Locator]:
    """Все видимые совпадения (на сайте бывают скрытые дубликаты в DOM)."""
    try:
        count = locator.count()
    except Exception:
        return []
    visible: list[Locator] = []
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                visible.append(candidate)
        except Exception:
            continue
    return visible


def _find_now(
    page: Page, element: dict[str, Any]
) -> Optional[tuple[str, list[Locator]]]:
    """Один проход по xpath и запасным селекторам без ожидания."""
    selectors = [("xpath", f"xpath={element['xpath']}")]
    selectors += [("fallback", sel) for sel in element.get("fallbacks", [])]
    for kind, selector in selectors:
        matches = _visible_matches(page.locator(selector))
        if matches:
            _note_strategy(element["name"], kind, selector)
            return kind, matches
    return None


def _note_strategy(name: str, kind: str, selector: str) -> None:
    """Записать в лог, что xpath не сработал и элемент найден запасным путём."""
    if kind == "xpath" or (name, selector) in _logged:
        return
    _logged.add((name, selector))
    log_event(f"{name}: xpath не сработал, найден запасным селектором {selector}")


def find_visible(page: Page, name: str) -> Optional[Locator]:
    """Первый видимый элемент прямо сейчас (без ожидания и без AI) или None."""
    found = _find_now(page, get_element(name))
    return found[1][0] if found else None


def try_ai_fix(page: Page, element: dict[str, Any], *, apply: bool = True) -> bool:
    """
    Попросить AI подобрать xpath и проверить его на странице.
    apply=True — подставить найденный xpath в element (дальше его будут использовать все).
    """
    # Импорт здесь: ai_fallback сам импортирует browser, а browser — этот модуль
    from ai_fallback import find_alternative_xpath, write_successful_xpath

    name = element.get("name", "")
    ai_attempted.add(name)
    info = {
        "name": name,
        "xpath": element.get("xpath", ""),
        "notes": element.get("notes", ""),
    }
    result = find_alternative_xpath(info, page.content())
    if result is None:
        return False

    try:
        works = bool(_visible_matches(page.locator(f"xpath={result.new_xpath}")))
    except Exception:
        works = False
    if not works:
        log_event(f"{name}: xpath от AI не находит видимый элемент — {result.new_xpath}")
        return False

    write_successful_xpath(name, info["xpath"], result.new_xpath, result.confidence)
    if apply:
        element["xpath"] = result.new_xpath
    ai_fixed.add(name)
    return True


def resolve_all(
    page: Page,
    name: str,
    *,
    timeout_ms: int = 15000,
    use_ai: bool = True,
) -> list[Locator]:
    """Все видимые совпадения элемента; ждёт до timeout_ms, затем пробует AI."""
    element = get_element(name)
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        found = _find_now(page, element)
        if found:
            return found[1]
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(POLL_MS)

    if use_ai and name not in ai_attempted and try_ai_fix(page, element):
        found = _find_now(page, element)
        if found:
            return found[1]
    raise ElementNotFound(f"{name}: не найден за {timeout_ms}ms (xpath, fallbacks, AI)")


def resolve(
    page: Page,
    name: str,
    *,
    timeout_ms: int = 15000,
    use_ai: bool = True,
) -> Locator:
    """Первый видимый элемент по имени из elements.json."""
    return resolve_all(page, name, timeout_ms=timeout_ms, use_ai=use_ai)[0]
