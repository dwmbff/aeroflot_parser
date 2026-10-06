"""Проверка OPENROUTER_API_KEY и доступности модели из .env."""

from __future__ import annotations

import sys

from openai import APIStatusError, AuthenticationError, OpenAI, PermissionDeniedError

import config

PROMPT = "Привет, ответь одним словом 'Работает', если ты меня слышишь"


def _build_client() -> OpenAI:
    if not config.OPENROUTER_API_KEY:
        raise ValueError(
            "OPENROUTER_API_KEY не задан. Добавьте ключ в файл .env в корне проекта."
        )
    return OpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
    )


def _format_api_error(exc: APIStatusError) -> str:
    if exc.status_code == 401:
        return (
            "Ошибка 401 Unauthorized: ключ API не принят OpenRouter. "
            "Проверьте OPENROUTER_API_KEY в .env — возможно, ключ неверный или отозван."
        )
    if exc.status_code == 403:
        return (
            "Ошибка 403 Forbidden: доступ к API запрещён. "
            "Проверьте права ключа и настройки аккаунта OpenRouter."
        )
    return f"Ошибка API {exc.status_code}: {exc.message}"


def main() -> int:
    model = config.OPENROUTER_MODEL
    print(f"Модель: {model}")
    print(f"Base URL: {config.OPENROUTER_BASE_URL}")
    print("Отправляем тестовый запрос...")

    try:
        client = _build_client()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": PROMPT}],
            temperature=0,
        )
        answer = (response.choices[0].message.content or "").strip()
        print("Успех! API ключ исправен.")
        print(f"Ответ модели: {answer}")
        return 0

    except AuthenticationError as exc:
        print(_format_api_error(exc))
        return 1
    except PermissionDeniedError as exc:
        print(_format_api_error(exc))
        return 1
    except APIStatusError as exc:
        print(_format_api_error(exc))
        return 1
    except ValueError as exc:
        print(f"Ошибка конфигурации: {exc}")
        return 1
    except Exception as exc:
        print(f"Неожиданная ошибка ({type(exc).__name__}): {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
