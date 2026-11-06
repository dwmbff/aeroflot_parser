"""
Сценарий поиска авиабилетов на aeroflot.ru с проверкой xpath на каждом этапе.
Маршрут «туда-обратно»: сначала выбирается дата вылета, затем дата возврата,
после поиска проверяются обе ветки результатов — туда (flow_1) и обратно (flow_2).
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

import config
from ai_fallback import find_alternative_xpath, write_successful_xpath
from browser import (
    accept_cookies,
    check_and_close_popup,
    check_error_state,
    click_after_results_1,
    click_result2_button,
    click_search,
    fill_from_input,
    fill_to_input,
    launch_browser,
    open_calendar,
    open_result2_panel,
    select_date,
    wait_results_ready,
)
from checker import (
    check_branch_inactive_element,
    check_element_inline,
    check_elements,
    get_element_by_name,
    load_elements,
    log_event,
)
from playwright.sync_api import Page


@dataclass
class RunSummary:
    ok: int = 0
    missing: int = 0
    skipped: int = 0
    ai_resolved: int = 0
    all_missing: list[dict[str, Any]] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Парсер xpath-элементов Aeroflot")
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Запуск браузера без UI (по умолчанию False для отладки)",
    )
    parser.add_argument(
        "--force-test-ai",
        action="store_true",
        help=(
            "После обычного прогона искусственно пометить один случайный OK-элемент "
            "как missing и прогнать через AI fallback (для проверки OpenRouter)"
        ),
    )
    return parser.parse_args()


def _safety_checks(page: Page) -> bool:
    """Попапы и ошибки между шагами. Возвращает True, если страница в ошибке."""
    accept_cookies(page)
    check_and_close_popup(page)
    error = check_error_state(page)
    if error.has_error:
        log_event(
            f"Обнаружена ошибка на странице: "
            f"title={error.title_visible}, box={error.box_visible}"
        )
        return True
    return False


def _check_stage(
    page: Page,
    elements: list[dict[str, Any]],
    stage: str,
    completed_actions: set[str],
    summary: RunSummary,
) -> None:
    result = check_elements(page, elements, stage, completed_actions)
    summary.ok += result.ok_count
    summary.skipped += result.skipped_count
    summary.all_missing.extend(result.missing)


def _record_inline_check(
    summary: RunSummary,
    missing: Optional[dict[str, Any]],
) -> None:
    if missing is not None:
        summary.all_missing.append(missing)
    else:
        summary.ok += 1


def _dedupe_missing(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for element in elements:
        name = element.get("name", "")
        if name in seen:
            continue
        seen.add(name)
        unique.append(element)
    return unique


def _run_flow_1(
    page: Page,
    elements: list[dict[str, Any]],
    completed_actions: set[str],
    summary: RunSummary,
) -> None:
    """Ветка ТУДА: выбор тарифа для рейса в прямом направлении."""
    print("\n[results_flow_1 - ТУДА]")
    _safety_checks(page)
    
      
    try:
        click_after_results_1(page)
    except Exception as exc:
        log_event(f"results_flow_1 (туда): click_after_results_1 не выполнен — {exc}")

    completed_actions.add("click_after_results_1")
    _safety_checks(page)
    _check_stage(page, elements, "results_flow_1", completed_actions, summary)


def _run_flow_2(
    page: Page,
    elements: list[dict[str, Any]],
    completed_actions: set[str],
    summary: RunSummary,
) -> None:
    """
    Ветка ОБРАТНО: после выбора тарифа «туда» сайт сам показывает рейсы 
    обратного направления на той же странице, без перезагрузки.
    """
    print("\n[results_flow_2 - ОБРАТНО]")
    _safety_checks(page)

    try:
        open_result2_panel(page)
    except Exception as exc:
        log_event(f"results_flow_2 (обратно): open_result2_panel не выполнен — {exc}")

    try:
        click_result2_button(page)
    except Exception as exc:
        log_event(f"results_flow_2 (обратно): click_result2_button не выполнен — {exc}")

    completed_actions.update({"open_result2_panel", "click_result2_button"})
    _safety_checks(page)
    _check_stage(page, elements, "results_flow_2", completed_actions, summary)


def _run_ai_fallback(
    page: Page,
    missing_elements: list[dict[str, Any]],
    summary: RunSummary,
) -> None:
    seen: set[str] = set()
    for element in missing_elements:
        if element.get("optional"):
            continue
        name = element.get("name", "")
        if name in seen:
            continue
        seen.add(name)

        element_info = {
            "name": name,
            "xpath": element.get("xpath", ""),
            "notes": element.get("notes", ""),
        }
        page_html = page.content()
        result = find_alternative_xpath(element_info, page_html)
        if result is None:
            continue

        write_successful_xpath(
            element_name=name,
            old_xpath=element_info["xpath"],
            new_xpath=result.new_xpath,
            confidence=result.confidence,
        )
        summary.ai_resolved += 1


def _run_force_test_ai(
    page: Page,
    elements: list[dict[str, Any]],
    summary: RunSummary,
) -> None:
    """Искусственно отправить один случайный обязательный элемент в AI fallback."""
    candidates = [
        el for el in elements
        if not el.get("optional") and not el.get("inline_only")
    ]
    if not candidates:
        log_event("force-test-ai: нет подходящих элементов для теста")
        print("\n[force-test-ai] Нет подходящих элементов для теста")
        return

    picked = random.choice(candidates)
    name = picked.get("name", "")
    log_event(f"force-test-ai: искусственно помечен как MISSING: {name}")
    print(f"\n[force-test-ai] Тест AI fallback для элемента: {name}")

    before_resolved = summary.ai_resolved
    _run_ai_fallback(page, [picked], summary)

    if summary.ai_resolved > before_resolved:
        log_event(f"force-test-ai: AI fallback успешен для {name}")
        print(f"[force-test-ai] AI fallback успешен для {name}")
    else:
        log_event(f"force-test-ai: AI fallback не дал xpath для {name}")
        print(f"[force-test-ai] AI fallback не дал xpath для {name}")
        summary.all_missing.append(picked)


def _print_summary(summary: RunSummary) -> None:
    summary.all_missing = _dedupe_missing(summary.all_missing)
    summary.missing = len(summary.all_missing)
    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print(f"  OK:                 {summary.ok}")
    print(f"  MISSING:            {summary.missing}")
    print(f"  SKIPPED (alt flow): {summary.skipped}")
    print(f"  AI resolved:        {summary.ai_resolved}")
    print("=" * 50)
    print(f"  Лог:         {config.LOG_FILE}")
    print(f"  Новые xpath: {config.NEW_XPATH_FILE}")


def run_scenario(headless: bool = False, force_test_ai: bool = False) -> int:
    elements = load_elements()
    completed_actions: set[str] = set()
    summary = RunSummary()

    config.LOG_FILE.write_text("", encoding="utf-8")
    if force_test_ai:
        config.NEW_XPATH_FILE.write_text("", encoding="utf-8")
    log_event(
        f"Запуск сценария: {config.FROM_CITY} → {config.TO_CITY}, "
        f"headless={headless}, force_test_ai={force_test_ai}"
    )
    print(f"Открываем {config.BASE_URL} (headless={headless})")

    session = launch_browser(headless=headless)
    page = session.page

    try:
        if _safety_checks(page):
            _check_stage(page, elements, "global_error", completed_actions, summary)
            _run_ai_fallback(page, summary.all_missing, summary)
            _print_summary(summary)
            return 1

        _check_stage(page, elements, "global_popup", completed_actions, summary)
        _check_stage(page, elements, "global_error", completed_actions, summary)
        _safety_checks(page)

        print("\n[search_form]")
        _check_stage(page, elements, "search_form", completed_actions, summary)
        _safety_checks(page)

       
        print(f"  Заполняем «Откуда»: {config.FROM_CITY}")
        suggest_el = get_element_by_name(elements, "suggest_items")
        inline_missing = fill_from_input(page, config.FROM_CITY, suggest_el)
        _record_inline_check(summary, inline_missing)
        completed_actions.add("fill_from_input")
        _safety_checks(page)

        print(f"  Заполняем «Куда»: {config.TO_CITY}")
        fill_to_input(page, config.TO_CITY)
        completed_actions.add("fill_to_input")
        _safety_checks(page)

        print("\n[calendar]")
        open_calendar(page)
        completed_actions.add("open_calendar")
        _safety_checks(page)
        _check_stage(page, elements, "calendar", completed_actions, summary)

        target = date.today() + timedelta(days=config.DAYS_AHEAD)
        print(f"  Выбираем дату (туда): {target.isoformat()}")
        select_date(page, target)
        completed_actions.add("select_date")
        _safety_checks(page)

        return_target = date.today() + timedelta(days=config.RETURN_DAYS_AHEAD)
        print(f"  Выбираем дату (обратно): {return_target.isoformat()}")
        select_date(page, return_target)
        completed_actions.add("select_return_date")
        _safety_checks(page)

        search_btn_el = get_element_by_name(elements, "search_btn")
        if search_btn_el:
            _record_inline_check(
                summary,
                check_element_inline(page, search_btn_el, require_visible=True),
            )

        print("\n[click_search]")
        _safety_checks(page)
        click_search(page)
        completed_actions.add("click_search")
        _safety_checks(page)

        print("\n[results_branch]")
        branch = wait_results_ready(page, timeout=config.NAVIGATION_TIMEOUT)
        completed_actions.add("wait_results_ready")
        _safety_checks(page)

        no_flights_el = get_element_by_name(elements, "no_flights_message")
        if branch == "empty":
            print("  Ветка: рейсов нет")
            _check_stage(page, elements, "results_branch_empty", completed_actions, summary)
            results_ready_el = get_element_by_name(elements, "results_ready_indicator")
            if results_ready_el:
                check_branch_inactive_element(page, results_ready_el)
                summary.ok += 1
        else:
            print("  Ветка: рейсы найдены")
            _check_stage(page, elements, "results_branch_flights", completed_actions, summary)
            if no_flights_el:
                check_branch_inactive_element(page, no_flights_el)
                summary.ok += 1

            # ТУДА → ОБРАТНО, без перезагрузки страницы:
            # после выбора тарифа "туда" сайт сам показывает рейсы обратного направления
            _run_flow_1(page, elements, completed_actions, summary)
            _run_flow_2(page, elements, completed_actions, summary)

        summary.all_missing = _dedupe_missing(summary.all_missing)
        if summary.all_missing:
            print(f"\n[ai_fallback] Обработка {len(summary.all_missing)} missing элементов...")
            _run_ai_fallback(page, summary.all_missing, summary)

        if force_test_ai:
            _run_force_test_ai(page, elements, summary)

        log_event(
            f"Сценарий завершён: OK={summary.ok}, "
            f"MISSING={len(summary.all_missing)}, AI={summary.ai_resolved}"
        )
        _print_summary(summary)
        return 0 if not summary.all_missing or summary.ai_resolved == len(summary.all_missing) else 1

    except Exception as exc:
        screenshot_path = config.BASE_DIR / "error_screenshot.png"
        try:
            page.screenshot(path=str(screenshot_path), full_page=False, timeout=10000)
            log_event(f"Скриншот ошибки сохранён: {screenshot_path}")
        except Exception as shot_exc:
            log_event(f"Не удалось сохранить скриншот: {shot_exc}")
        log_event(f"Критическая ошибка: {exc}")
        print(f"\nКритическая ошибка: {exc}", file=sys.stderr)
        print(f"Скриншот: {screenshot_path}", file=sys.stderr)
        if config.LOG_FILE.exists():
            print(f"\n--- log.txt (последние 30 строк) ---", file=sys.stderr)
            lines = config.LOG_FILE.read_text(encoding="utf-8").splitlines()
            for line in lines[-30:]:
                print(line, file=sys.stderr)
        _print_summary(summary)
        return 2
    finally:
        session.close()


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(run_scenario(headless=args.headless, force_test_ai=args.force_test_ai))
    except KeyboardInterrupt:
        log_event("Прервано пользователем")
        print("\nПрервано пользователем.")
        sys.exit(130)