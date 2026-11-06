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

import ai_fallback
from checker import load_elements, log_event

POLL_MS = 500

_state: dict[str, list[dict[str, Any]]] = {"elements": []}
_logged: set[tuple[str, str]] = set()

# Имена элементов, для которых AI уже вызывали (чтобы не дёргать API повторно)
ai_attempted: set[str] = set()
# Имена элементов, для которых AI нашёл рабочий xpath
ai_fixed: set[str] = set()
# Имена элементов, у которых xpath устарел, но элемент нашёлся запасным селектором
fallback_restored: set[str] = set()

MAX_AI_TRIES = 2  # первый ответ + одна попытка исправить его по замечанию проверки


class ElementNotFound(LookupError):
    """Элемент не найден ни по xpath, ни по запасным селекторам, ни через AI."""


def set_elements(elements: list[dict[str, Any]]) -> None:
    """Общий список элементов: AI правит xpath прямо в нём, и все видят правку. Начинает новый прогон."""
    _state["elements"] = elements
    for registry in (_logged, ai_attempted, ai_fixed, fallback_restored):
        registry.clear()


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


def _find_via_fallbacks(
    page: Page, element: dict[str, Any]
) -> Optional[tuple[str, list[Locator]]]:
    """Только запасные селекторы (без основного xpath)."""
    for selector in element.get("fallbacks", []):
        matches = _visible_matches(page.locator(selector))
        if matches:
            return selector, matches
    return None


def _check_ai_candidate(page: Page, element: dict[str, Any], xpath: str) -> Optional[str]:
    """
    Проверить xpath от модели. None — подходит, иначе строка с причиной отказа.
    Одной видимости мало: модель может вернуть иконку рядом с полем, на которую
    нельзя кликнуть. Поэтому у элементов с заданным "tag" проверяются тип и доступность.
    """
    try:
        matches = _visible_matches(page.locator(f"xpath={xpath}"))
    except Exception:
        return "некорректный xpath"
    if not matches:
        return "xpath не находит видимый элемент"
    expected = element.get("tag")
    if expected:
        try:
            tag = matches[0].evaluate("e => e.tagName.toLowerCase()")
            enabled = matches[0].is_enabled()
        except Exception:
            return "элемент недоступен для проверки"
        if tag != expected:
            return f"найден элемент <{tag}>, а нужен <{expected}>"
        if not enabled:
            return "элемент отключён (disabled)"
    return None


def try_ai_fix(page: Page, element: dict[str, Any], *, apply: bool = True) -> bool:
    """
    Попросить AI подобрать xpath и проверить его на странице.
    Если проверка отклонила ответ, модели возвращается причина отказа и даётся
    ещё одна попытка (самокоррекция). apply=True — подставить найденный xpath
    в element (дальше его будут использовать все).
    """
    name = element.get("name", "")
    ai_attempted.add(name)
    info = {
        "name": name,
        "xpath": element.get("xpath", ""),
        "notes": element.get("notes", ""),
        "tag": element.get("tag", ""),
    }
    digest = ai_fallback.build_digest(page, info["notes"])

    feedback = ""
    for _ in range(MAX_AI_TRIES):
        result = ai_fallback.find_alternative_xpath(info, page.content(), digest, feedback)
        if result is None:
            return False

        reason = _check_ai_candidate(page, element, result.new_xpath)
        if reason is None:
            ai_fallback.write_successful_xpath(name, info["xpath"], result.new_xpath, result.confidence)
            if apply:
                element["xpath"] = result.new_xpath
            ai_fixed.add(name)
            return True

        log_event(f"{name}: xpath от AI отклонён ({reason}): {result.new_xpath}")
        feedback = (
            f"Твой предыдущий ответ {result.new_xpath} не подошёл: {reason}. "
            f"Предложи другой xpath."
        )
    return False


def restore(page: Page, element: dict[str, Any]) -> Optional[str]:
    """
    Восстановить пропавший элемент самым дешёвым способом:
    сначала запасные селекторы, и только если они не помогли — ИИ.
    Возвращает "fallback", "ai" или None, если элемент не найден ни одним способом.
    """
    name = element.get("name", "")
    found = _find_via_fallbacks(page, element)
    if found:
        selector = found[0]
        fallback_restored.add(name)
        log_event(f"{name}: xpath устарел, элемент найден запасным селектором {selector}")
        ai_fallback.write_successful_xpath(name, element.get("xpath", ""), selector, "fallback")
        return "fallback"
    if name in ai_attempted:
        return None
    return "ai" if try_ai_fix(page, element) else None


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
