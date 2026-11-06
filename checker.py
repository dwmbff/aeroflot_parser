"""
Проверка элементов страницы по xpath из elements.json и запись результатов в log.txt.

Модуль отвечает только за ответ на вопрос «элемент на странице есть или нет»
и за журнал. Сами действия (клики, ввод) — в browser.py, поиск с запасными
вариантами — в resolver.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import config

CHECK_TIMEOUT_MS = 5000


@dataclass
class StageCheckResult:
    missing: list[dict[str, Any]] = field(default_factory=list)
    ok_count: int = 0


def load_elements() -> list[dict[str, Any]]:
    with config.ELEMENTS_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def get_element_by_name(elements: list[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
    for el in elements:
        if el.get("name") == name:
            return el
    return None


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_for_log(text: str, limit: int = 600) -> str:
    """
    Привести произвольный текст (ответ модели, текст ошибки) к одной строке.
    Без этого переводы строк в ответе внешнего сервиса позволили бы подделать
    чужие записи в журнале (log injection).
    """
    one_line = " ".join(str(text).split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


def write_log_line(line: str) -> None:
    with config.LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{_timestamp()}] {line}\n")


def log_event(message: str) -> None:
    """Служебная запись в log.txt (запуск, ошибки, попапы и т.д.)."""
    write_log_line(f"[EVENT] {sanitize_for_log(message)}")


def _log_check_result(status: str, name: str, xpath: str) -> None:
    write_log_line(f"[{status}] {name}: {xpath}")


def _is_action_ready(element: dict[str, Any], completed_actions: set[str]) -> bool:
    action_before = element.get("action_before")
    if not action_before:
        return True
    return action_before in completed_actions


def _is_optional(element: dict[str, Any]) -> bool:
    return bool(element.get("optional"))


def _element_on_page(page: Page, xpath: str, timeout_ms: int = CHECK_TIMEOUT_MS) -> bool:
    locator = page.locator(f"xpath={xpath}")
    try:
        locator.first.wait_for(state="attached", timeout=timeout_ms)
        return locator.count() > 0
    except PlaywrightTimeoutError:
        return False


def _element_visible(page: Page, xpath: str, timeout_ms: int = CHECK_TIMEOUT_MS) -> bool:
    locator = page.locator(f"xpath={xpath}")
    try:
        locator.first.wait_for(state="visible", timeout=timeout_ms)
        return locator.count() > 0 and locator.first.is_visible()
    except PlaywrightTimeoutError:
        return False


def _eligible_elements(
    elements: list[dict[str, Any]],
    stage: str,
    completed_actions: set[str],
) -> list[dict[str, Any]]:
    """Элементы стадии, которые уже можно проверять (их action_before выполнен)."""
    return [
        el
        for el in elements
        if el.get("stage") == stage
        and not el.get("inline_only")
        and _is_action_ready(el, completed_actions)
    ]


def _global_error_element_found(
    page: Page,
    element: dict[str, Any],
    elements: list[dict[str, Any]],
) -> bool:
    """Для global_error: box считается найденным только вместе с видимым заголовком."""
    name = element.get("name", "")
    xpath = element.get("xpath", "")
    if name == "something_went_wrong_box":
        title = get_element_by_name(elements, "something_went_wrong_title")
        if title is None:
            return _element_visible(page, xpath)
        title_visible = _element_visible(page, title.get("xpath", ""), timeout_ms=2000)
        if not title_visible:
            return False
        return _element_visible(page, xpath)
    return _element_visible(page, xpath)


def _evaluate_element(
    page: Page,
    element: dict[str, Any],
    *,
    elements: Optional[list[dict[str, Any]]] = None,
) -> tuple[bool, bool]:
    """
    Проверить, что элемент виден на странице.
    Возвращает (found, optional_absent_ok): второе значение True, если элемента
    нет, но он помечен optional и его отсутствие — штатная ситуация.
    """
    xpath = element.get("xpath", "")
    if element.get("stage") == "global_error" and elements is not None:
        found = _global_error_element_found(page, element, elements)
    else:
        found = _element_visible(page, xpath)
    if found:
        return True, False
    return False, _is_optional(element)


def log_inactive_branch(element: dict[str, Any]) -> None:
    """Залогировать элемент ветки результатов, которая в этом прогоне не активна."""
    _log_check_result(
        "OK - not present, branch inactive",
        element.get("name", ""),
        element.get("xpath", ""),
    )


def check_element_inline(
    page: Page,
    element: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """
    Проверить элемент в конкретный момент сценария (вне check_elements по stage).
    Возвращает element только если он обязателен и не найден.
    """
    name = element.get("name", "")
    xpath = element.get("xpath", "")
    found, optional_absent = _evaluate_element(page, element)

    if found:
        _log_check_result("OK", name, xpath)
        return None
    if optional_absent:
        _log_check_result("OK - not present, optional", name, xpath)
        return None

    _log_check_result("MISSING", name, xpath)
    return element


def check_elements(
    page: Page,
    elements: list[dict[str, Any]],
    current_stage: str,
    completed_actions: Optional[set[str]] = None,
) -> StageCheckResult:
    """
    Проверить элементы текущего stage с учётом action_before.

    - Пропускает элементы, чей action_before ещё не выполнен.
    - Логирует [OK] / [OK - not present, optional] / [MISSING] в log.txt.
    - Возвращает missing-элементы и число успешных проверок.
    """
    actions = completed_actions if completed_actions is not None else set()
    result = StageCheckResult()

    for element in _eligible_elements(elements, current_stage, actions):
        name = element.get("name", "")
        xpath = element.get("xpath", "")

        found, optional_absent = _evaluate_element(
            page,
            element,
            elements=elements if current_stage == "global_error" else None,
        )
        if found:
            _log_check_result("OK", name, xpath)
            result.ok_count += 1
        elif optional_absent:
            _log_check_result("OK - not present, optional", name, xpath)
            result.ok_count += 1
        else:
            _log_check_result("MISSING", name, xpath)
            result.missing.append(element)

    return result
