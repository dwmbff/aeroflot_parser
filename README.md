# Aeroflot XPath Checker

Скрипт для автоматической проверки наличия HTML-элементов на сайте
aeroflot.ru по заданным xpath, с автоматическим поиском альтернативного
xpath через ИИ при обнаружении поломки.

**Полное описание логики работы, примеры реальных срабатываний и
известные ограничения — в [ANALYTICS.md](ANALYTICS.md).** Этот файл
содержит только краткую инструкцию по запуску.

## Что делает скрипт

1. Проходит сценарий поиска авиабилетов туда-обратно на aeroflot.ru
2. Проверяет 20 ключевых элементов интерфейса по xpath из `elements.json`
3. Ищет элементы устойчиво к смене вёрстки, в три шага (`resolver.py`):
   xpath → запасные селекторы `fallbacks` (по placeholder, тексту, роли) → ИИ
4. Если xpath не сработал, получает HTML страницы и запрашивает у ИИ
   (OpenRouter, модель `google/gemma-4-31b-it:free`) альтернативный xpath;
   найденный xpath проверяется на странице и подставляется в сценарий
5. Формирует `log.txt`, `new_xpath.txt` и аналитический отчёт

Сценарий не падает из-за одного сломанного xpath: действия идут через
запасные селекторы, а сам устаревший xpath попадает в отчёт как MISSING
(код возврата 1).

## Установка

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
playwright install chromium
```
## Настройка
Скопируйте .env.example → .env, впишите ключ OpenRouter:
```bash
OPENROUTER_API_KEY=ваш_ключ_с_openrouter.ai
OPENROUTER_MODEL=google/gemma-4-31b-it:free
```
## Запуск
```bash
python test_openrouter.py        # проверка ключа API
python main.py                    # с видимым браузером
python main.py --headless          # без интерфейса
python main.py --force-test-ai      # тест AI-фолбэка на сломанном элементе
python -m unittest discover -s tests   # быстрые тесты без браузера и сети
```
## Структура проекта
| Путь                                                                 | Назначение          |
| -------------------------------------------------------------------- | ------------------- |
| `main.py`, `config.py`, `browser.py`, `checker.py`, `ai_fallback.py` | код                 |
| `tests/`                                                             | юнит-тесты          |
| `resolver.py`                                                        | поиск элементов: xpath → fallbacks → ИИ |
| `elements.json`                                                      | xpath и запасные селекторы |
| `log.txt`, `new_xpath.txt`                                           | результаты прогона  |
| `ANALYTICS.md`                                                       | ← основной документ |
| `demo/`                                                              | демо AI-фолбэка     |
Подробности — в ANALYTICS.md.
