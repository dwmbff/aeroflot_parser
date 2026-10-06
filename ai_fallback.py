from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from openai import APIStatusError, OpenAI
from playwright.sync_api import Page

import config
from browser import xpath_locator

HTML_MAX_CHARS = config.AI_HTML_MAX_CHARS
HTML_CONTEXT_RADIUS = 7500
RATE_LIMIT_RETRY_DELAY_SEC = 2.5
MAX_API_ATTEMPTS = 2


@dataclass
class FallbackResult:
    found: bool
    new_xpath: str
    confidence: str
    reasoning: str


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_ai_fallback_failed(element_name: str, details: str) -> None:
    with config.LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{_timestamp()}] [AI_FALLBACK_FAILED] {element_name}: {details}\n")


def _build_client() -> OpenAI:
    if not config.OPENROUTER_API_KEY:
        raise ValueError(
            "OPENROUTER_API_KEY не задан. Укажите ключ в .env для AI-подбора xpath."
        )
    return OpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
    )


def _clean_html(html: str) -> str:
    """Убрать script, style и HTML-комментарии."""
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    html = re.sub(
        r"<script\b[^>]*>.*?</script>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    html = re.sub(
        r"<style\b[^>]*>.*?</style>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return html


def _extract_keywords(notes: str) -> list[str]:
    keywords: list[str] = []
    keywords.extend(re.findall(r"«([^»]+)»", notes))
    keywords.extend(re.findall(r'"([^"]+)"', notes))
    keywords.extend(re.findall(r"'([^']+)'", notes))
    for token in re.findall(r"[А-Яа-яA-Za-z0-9]{3,}", notes):
        if token.lower() not in {"это", "если", "или", "для", "при", "что", "как", "the", "and"}:
            keywords.append(token)
    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        key = kw.lower()
        if key not in seen:
            seen.add(key)
            unique.append(kw)
    return unique


def _truncate_html(html: str, notes: str, max_chars: int = HTML_MAX_CHARS) -> str:
    """Обрезать HTML до релевантного фрагмента (~max_chars символов)."""
    if len(html) <= max_chars:
        return html

    lowered = html.lower()
    for keyword in _extract_keywords(notes):
        idx = lowered.find(keyword.lower())
        if idx < 0:
            continue
        start = max(0, idx - HTML_CONTEXT_RADIUS)
        end = min(len(html), idx + HTML_CONTEXT_RADIUS)
        snippet = html[start:end]
        if len(snippet) > max_chars:
            center = idx - start
            half = max_chars // 2
            snippet = snippet[max(0, center - half) : center - half + max_chars]
        return snippet

    body_match = re.search(r"<body\b[^>]*>(.*)</body>", html, flags=re.DOTALL | re.IGNORECASE)
    body = body_match.group(1) if body_match else html
    return body[:max_chars]


def prepare_html_for_ai(page_html: str, notes: str = "") -> str:
    """Очистить и обрезать HTML перед отправкой в модель."""
    cleaned = _clean_html(page_html)
    return _truncate_html(cleaned, notes)


def _build_prompt(element_info: dict[str, Any], html: str) -> str:
    name = element_info.get("name", "")
    xpath = element_info.get("xpath", element_info.get("old_xpath", ""))
    notes = element_info.get("notes", "")

    return (
        f"Ты анализируешь HTML веб-страницы. Дан элемент с именем {name}, "
        f"старым xpath {xpath} и описанием {notes}, который не найден на странице. "
        f"Проанализируй предоставленный HTML и найди наиболее вероятный аналогичный элемент. "
        f"Верни ТОЛЬКО валидный JSON без markdown-разметки: "
        '{"found": true/false, "new_xpath": "...", "confidence": "high/medium/low", '
        '"reasoning": "краткое объяснение"}\n\n'
        f"HTML:\n{html}"
    )


def _strip_markdown_fences(content: str) -> str:
    content = content.strip()
    if not content.startswith("```"):
        return content

    lines = content.splitlines()
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_model_response(content: str) -> Optional[FallbackResult]:
    content = _strip_markdown_fences(content)

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    found = bool(data.get("found"))
    new_xpath = data.get("new_xpath", "")
    confidence = data.get("confidence", "low")
    reasoning = data.get("reasoning", "")

    if not isinstance(new_xpath, str):
        new_xpath = ""
    if not isinstance(confidence, str):
        confidence = "low"
    if not isinstance(reasoning, str):
        reasoning = ""

    return FallbackResult(
        found=found,
        new_xpath=new_xpath.strip(),
        confidence=confidence.strip(),
        reasoning=reasoning.strip(),
    )


def _log_ai_success(element_name: str, raw_response: str, reasoning: str) -> None:
    with config.LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{_timestamp()}] [AI_OK] {element_name}: reasoning={reasoning}\n")
        f.write(f"[{_timestamp()}] [AI_RAW] {element_name}: {raw_response}\n")


def _log_ai_api_error(element_name: str, exc: Exception) -> None:
    """Полный текст ошибки API в log.txt для диагностики rate limit и др."""
    parts = [f"{type(exc).__name__}: {exc}"]
    if isinstance(exc, APIStatusError):
        parts.append(f"status_code={exc.status_code}")
        if exc.body is not None:
            body = exc.body if isinstance(exc.body, str) else str(exc.body)
            parts.append(f"body={body}")
    if hasattr(exc, "response") and exc.response is not None:
        parts.append(f"response={exc.response}")
    full_text = " | ".join(parts)
    with config.LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{_timestamp()}] [AI_API_ERROR] {element_name}: {full_text}\n")


def _call_openrouter(prompt: str, element_name: str = "unknown") -> str:
    client = _build_client()
    last_error: Optional[Exception] = None

    for attempt in range(MAX_API_ATTEMPTS):
        try:
            response = client.chat.completions.create(
                model=config.OPENROUTER_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "Ты эксперт по XPath и анализу HTML. Отвечай только JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            return response.choices[0].message.content or ""
        except APIStatusError as exc:
            last_error = exc
            _log_ai_api_error(element_name, exc)
            if exc.status_code == 429 and attempt < MAX_API_ATTEMPTS - 1:
                time.sleep(RATE_LIMIT_RETRY_DELAY_SEC)
                continue
            raise
        except Exception as exc:
            last_error = exc
            _log_ai_api_error(element_name, exc)
            raise

    if last_error:
        raise last_error
    return ""


def find_alternative_xpath(
    element_info: dict[str, Any],
    page_html: str,
) -> Optional[FallbackResult]:
    """
    Запросить у OpenRouter альтернативный xpath для элемента.

    При found=false или ошибке парсинга пишет [AI_FALLBACK_FAILED] в log.txt
    и возвращает None.
    """
    name = element_info.get("name", "unknown")
    notes = element_info.get("notes", "")

    try:
        prepared_html = prepare_html_for_ai(page_html, notes)
        prompt = _build_prompt(element_info, prepared_html)
        raw_response = _call_openrouter(prompt, element_name=name)
    except Exception as exc:
        _log_ai_fallback_failed(name, f"API request failed: {type(exc).__name__}")
        return None

    parsed = _parse_model_response(raw_response)
    if parsed is None:
        _log_ai_fallback_failed(name, f"JSON parse failed: {raw_response[:300]!r}")
        return None

    if not parsed.found or not parsed.new_xpath:
        reason = parsed.reasoning or "model returned found=false"
        _log_ai_fallback_failed(name, reason)
        return None

    _log_ai_success(name, raw_response, parsed.reasoning)
    return parsed


def write_successful_xpath(
    element_name: str,
    old_xpath: str,
    new_xpath: str,
    confidence: str,
) -> None:
    """Записать успешный результат в new_xpath.txt."""
    line = f"{element_name} | {old_xpath} | {new_xpath} | {confidence}"
    with config.NEW_XPATH_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def find_and_save_alternative(page: Page, element: dict[str, Any]) -> Optional[str]:
    """Получить HTML страницы, запросить xpath у AI и сохранить при успехе."""
    element_info = {
        "name": element.get("name", ""),
        "xpath": element.get("xpath", ""),
        "notes": element.get("notes", ""),
    }
    result = find_alternative_xpath(element_info, page.content())
    if result is None:
        return None

    old_xpath = element.get("xpath", "")
    write_successful_xpath(
        element_name=element_info["name"],
        old_xpath=old_xpath,
        new_xpath=result.new_xpath,
        confidence=result.confidence,
    )

    if xpath_locator(page, result.new_xpath).count() > 0:
        element["xpath"] = result.new_xpath

    return result.new_xpath
