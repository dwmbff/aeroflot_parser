import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

ELEMENTS_FILE = BASE_DIR / "elements.json"
LOG_FILE = BASE_DIR / "log.txt"
NEW_XPATH_FILE = BASE_DIR / "new_xpath.txt"


def _int_env(name: str, default: int) -> int:
    """Целое из переменной окружения; при опечатке в .env — понятная ошибка вместо traceback."""
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Ошибка конфигурации: {name} должно быть целым числом, а не {raw!r}")


BASE_URL = os.getenv("AEROFLOT_URL", "https://www.aeroflot.ru/ru-ru/")
FROM_CITY = os.getenv("FROM_CITY", "Москва")
TO_CITY = os.getenv("TO_CITY", "Санкт-Петербург")
DAYS_AHEAD = _int_env("DAYS_AHEAD", 7)
RETURN_DAYS_AHEAD = _int_env("RETURN_DAYS_AHEAD", 14)

# Таймауты в миллисекундах
BROWSER_TIMEOUT = _int_env("BROWSER_TIMEOUT", 30000)
NAVIGATION_TIMEOUT = _int_env("NAVIGATION_TIMEOUT", 60000)

# Ключ задаётся только в .env (он в .gitignore); в коде и в .env.example его нет
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")

# Максимум символов HTML, отправляемых модели (см. ai_fallback._truncate_html)
AI_HTML_MAX_CHARS = _int_env("AI_HTML_MAX_CHARS", 15000)
