from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import config

CHECK_TIMEOUT_MS = 5000

ALT_FLOW_STAGES = frozenset()


@dataclass
class StageCheckResult:
    missing: list[dict[str, Any]] = field(default_factory=list)
    ok_count: int = 0
    skipped_count: int = 0


def load_elements() -> list[dict[str, Any]]:
    with config.ELEMENTS_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def get_elements_by_stage(elements: list[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
    return [el for el in elements if el.get("stage") == stage]


def get_element_by_name(elements: list[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
    for el in elements:
        if el.get("name") == name:
            return el
    return None


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_log_line(line: str) -> None:
    with config.LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_event(message: str) -> None:
    """Служебная запись в log.txt (запуск, ошибки, попапы и т.д.)."""
    _write_log_line(f"[{_timestamp()}] [EVENT] {message}")


def _log_check_result(status: str, name: str, xpath: str) -> None:
    _write_log_line(f"[{_timestamp()}] [{status}] {name}: {xpath}")


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
    require_visible: bool = False,
    elements: Optional[list[dict[str, Any]]] = None,
) -> tuple[bool, bool]:
    """
    Проверить один элемент.
    require_visible управляет строгостью поиска (видимость vs просто наличие в DOM)
    и НЕ связан с тем, обязателен элемент или нет — это отдельная проверка
    через _is_optional() ниже.
    Возвращает (found, optional_absent_ok).
    """
    xpath = element.get("xpath", "")
    if require_visible and element.get("stage") == "global_error" and elements is not None:
        found = _global_error_element_found(page, element, elements)
    else:
        found = (
            _element_visible(page, xpath)
            if require_visible
            else _element_on_page(page, xpath)
        )
    if found:
        return True, False
    if _is_optional(element):
        return False, True
    return False, False


def check_branch_inactive_element(page: Page, element: dict[str, Any]) -> None:
    """Залогировать элемент альтернативной ветки результатов, не активной в этом прогоне."""
    name = element.get("name", "")
    xpath = element.get("xpath", "")
    _log_check_result("OK - not present, branch inactive", name, xpath)


def check_element_inline(
    page: Page,
    element: dict[str, Any],
    *,
    require_visible: bool = True,
) -> Optional[dict[str, Any]]:
    """
    Проверить элемент в конкретный момент сценария (вне check_elements по stage).
    Возвращает element dict только если элемент обязателен и не найден.
    """
    name = element.get("name", "")
    xpath = element.get("xpath", "")
    found, optional_absent = _evaluate_element(
        page, element, require_visible=require_visible, elements=None
    )

    if found:
        _log_check_result("OK", name, xpath)
        return None
    if optional_absent:
        _log_check_result("OK - not present, optional", name, xpath)
        return None

    _log_check_result("MISSING", name, xpath)
    return element


def _opposite_alt_flow(stage: str) -> Optional[str]:
    if stage == "results_flow_1":
        return "results_flow_2"
    if stage == "results_flow_2":
        return "results_flow_1"
    return None


def _is_opposite_flow_active(
    page: Page,
    elements: list[dict[str, Any]],
    stage: str,
    completed_actions: set[str],
) -> bool:
    """Пропуск alt-flow только если противоположная ветка уже была выполнена на этой странице."""
    opposite = _opposite_alt_flow(stage)
    if opposite is None:
        return False

    opposite_entry_actions = {
        "results_flow_1": "click_after_results_1",
        "results_flow_2": "open_result2_panel",
    }
    entry_action = opposite_entry_actions.get(opposite)
    if not entry_action or entry_action not in completed_actions:
        return False

    for el in _eligible_elements(elements, opposite, completed_actions):
        if _element_on_page(page, el.get("xpath", ""), timeout_ms=CHECK_TIMEOUT_MS):
            return True
    return False


def check_elements(
    page: Page,
    elements: list[dict[str, Any]],
    current_stage: str,
    completed_actions: Optional[set[str]] = None,
) -> StageCheckResult:
    """
    Проверить элементы текущего stage с учётом action_before.

    - Пропускает элементы, чей action_before ещё не выполнен.
    - Логирует [OK] / [MISSING] / [SKIPPED - alt flow active] в log.txt.
    - Возвращает missing элементы (без SKIPPED) и счётчики ok/skipped.
    """
    actions = completed_actions if completed_actions is not None else set()
    to_check = _eligible_elements(elements, current_stage, actions)
    missing: list[dict[str, Any]] = []
    ok_count = 0
    skipped_count = 0

    opposite_active = (
        _is_opposite_flow_active(page, elements, current_stage, actions)
        if current_stage in ALT_FLOW_STAGES
        else False
    )

    for element in to_check:
        name = element.get("name", "")
        xpath = element.get("xpath", "")

        found, optional_absent = _evaluate_element(
            page,
            element,
            require_visible=True,
            elements=elements if current_stage == "global_error" else None,
        )
        if found:
            _log_check_result("OK", name, xpath)
            ok_count += 1
            continue
        if optional_absent:
            _log_check_result("OK - not present, optional", name, xpath)
            ok_count += 1
            continue

        if current_stage in ALT_FLOW_STAGES and opposite_active:
            _log_check_result("SKIPPED - alt flow active", name, xpath)
            skipped_count += 1
            continue

        _log_check_result("MISSING", name, xpath)
        missing.append(element)

    return StageCheckResult(missing=missing, ok_count=ok_count, skipped_count=skipped_count)


class ElementChecker:
    """Обёртка над check_elements с отслеживанием выполненных action_before."""

    def __init__(self, elements: list[dict[str, Any]]) -> None:
        self.elements = elements
        self.completed_actions: set[str] = set()

    def mark_action(self, action: str) -> None:
        self.completed_actions.add(action)

    def check_stage(self, page: Page, stage: str) -> StageCheckResult:
        """Проверить stage и вернуть результат с missing элементами."""
        return check_elements(page, self.elements, stage, self.completed_actions)

    def element_exists(self, page: Page, xpath: str, *, visible: bool = False) -> bool:
        locator = page.locator(f"xpath={xpath}")
        if locator.count() == 0:
            return False
        if visible:
            return locator.first.is_visible()
        return True

    def require(self, page: Page, name: str, *, visible: bool = False) -> bool:
        element = get_element_by_name(self.elements, name)
        if element is None:
            raise KeyError(f"Элемент '{name}' не описан в elements.json")
        if visible:
            locator = page.locator(f"xpath={element['xpath']}")
            return locator.count() > 0 and locator.first.is_visible()
        return _element_on_page(page, element["xpath"])
