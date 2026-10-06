import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

ELEMENTS_FILE = BASE_DIR / "elements.json"
LOG_FILE = BASE_DIR / "log.txt"
NEW_XPATH_FILE = BASE_DIR / "new_xpath.txt"

BASE_URL = os.getenv("AEROFLOT_URL", "https://www.aeroflot.ru/ru-ru/")
FROM_CITY = os.getenv("FROM_CITY", "Москва")
TO_CITY = os.getenv("TO_CITY", "Санкт-Петербург")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
RETURN_DAYS_AHEAD = int(os.getenv("RETURN_DAYS_AHEAD", "14"))

HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "true", "yes")
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "30000"))
NAVIGATION_TIMEOUT = int(os.getenv("NAVIGATION_TIMEOUT", "60000"))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")

AI_HTML_MAX_CHARS = int(os.getenv("AI_HTML_MAX_CHARS", "15000"))