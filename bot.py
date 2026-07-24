"""
Lalafo Phone Deals Bot
======================
Отслеживает раздел "Телефоны" на Lalafo (Бишкек) и присылает в Telegram
объявления, цена которых заметно ниже медианной цены по этой же модели.

ВАЖНО ПРО API:
Lalafo не публикует официальный API. Этот скрипт использует внутренний
JSON-эндпоинт, которым пользуется сам сайт lalafo.kg (тот же, что грузит
объявления при скролле страницы). Такие эндпоинты иногда меняются.

Если бот перестанет находить объявления:
1. Откройте https://lalafo.kg/kyrgyzstan/telefony в браузере
2. Откройте DevTools (F12) -> вкладка Network -> Fetch/XHR
3. Обновите страницу, найдите запрос, который возвращает JSON со списком
   объявлений (обычно содержит "items" или "ads" в ответе)
4. Скопируйте его URL и подставьте в переменную FEED_URL ниже,
   поправьте разбор полей в функции parse_item() под реальную структуру JSON.
"""

import os
import time
import json
import logging
import requests
import statistics
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lalafo_bot")

# ---------------------------------------------------------------------------
# НАСТРОЙКИ — заполните перед запуском (или задайте как переменные окружения)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8858673828:AAHOVPtMhYwr5ytzjd9Qkdx5-FcN0NU3mmM")
CHAT_ID = os.environ.get("CHAT_ID", "882643640")

# Категория "Телефоны" на Lalafo Бишкек — id категории в URL сайта
CATEGORY_ID = 508  # категория "Мобильные телефоны" (проверьте актуальность на сайте)
CITY_SLUG = "bishkek"

# Порог "хорошей цены": объявление должно быть дешевле медианы минимум на этот %
DISCOUNT_THRESHOLD = 0.20  # 20%

# Как часто проверять новые объявления (в секундах)
CHECK_INTERVAL = 600  # 10 минут

# Сколько последних объявлений по каждой модели использовать для расчёта медианы
SAMPLE_SIZE = 15

SEEN_FILE = "seen_ads.json"
FEED_URL = "https://lalafo.kg/api/search/v3/feed"

# ---------------------------------------------------------------------------


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    # храним только последние 5000, чтобы файл не рос бесконечно
    trimmed = list(seen)[-5000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


def fetch_ads(page=1, per_page=50):
    """Запрашивает страницу объявлений категории. Подстройте под реальный API."""
    params = {
        "category_id": CATEGORY_ID,
        "city": CITY_SLUG,
        "page": page,
        "per_page": per_page,
        "sort": "date",
    }
    try:
        r = requests.get(FEED_URL, params=params, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PhoneDealsBot/1.0)"
        })
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Ошибка запроса к Lalafo: %s", e)
        return None


def parse_item(raw):
    """Извлекает нужные поля из одного объявления.
    Поправьте ключи под реальный формат ответа, если он отличается."""
    try:
        return {
            "id": str(raw.get("id")),
            "title": raw.get("title", "Без названия"),
            "price": float(raw.get("price", 0) or 0),
            "currency": raw.get("currency", "KGS"),
            "url": f"https://lalafo.kg/kyrgyzstan/o/{raw.get('id')}",
            "model_key": normalize_model(raw.get("title", "")),
        }
    except Exception:
        return None


def normalize_model(title: str) -> str:
    """Грубая нормализация названия для группировки одинаковых моделей.
    Например 'iPhone 13 Pro 256gb новый' -> 'iphone 13 pro'."""
    t = title.lower()
    for junk in ["новый", "б/у", "бу", "срочно", "торг", "gb", "гб", "тб", "tb"]:
        t = t.replace(junk, "")
    words = [w for w in t.split() if not w.isdigit() or len(w) <= 3]
    return " ".join(words[:4]).strip()


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }, timeout=10)
    except Exception as e:
        log.error("Ошибка отправки в Telegram: %s", e)


def run_once(seen):
    data = fetch_ads()
    if not data:
        return seen

    raw_items = data.get("items") or data.get("ads") or data.get("data") or []
    items = [parse_item(x) for x in raw_items]
    items = [x for x in items if x and x["price"] > 0]

    if not items:
        log.warning("Не удалось получить ни одного объявления — проверьте FEED_URL/parse_item")
        return seen

    # группируем по модели, считаем медиану
    by_model = defaultdict(list)
    for it in items:
        by_model[it["model_key"]].append(it["price"])

    medians = {
        model: statistics.median(prices[:SAMPLE_SIZE])
        for model, prices in by_model.items()
        if len(prices) >= 3  # нужно хотя бы немного объявлений для честной медианы
    }

    new_seen = set(seen)
    deals_found = 0

    for it in items:
        if it["id"] in seen:
            continue
        new_seen.add(it["id"])

        median_price = medians.get(it["model_key"])
        if not median_price:
            continue

        if it["price"] <= median_price * (1 - DISCOUNT_THRESHOLD):
            discount_pct = round((1 - it["price"] / median_price) * 100)
            text = (
                f"🔥 <b>Цена ниже рынка на {discount_pct}%</b>\n\n"
                f"{it['title']}\n"
                f"💰 {it['price']:.0f} {it['currency']} "
                f"(медиана по модели: ~{median_price:.0f})\n"
                f"{it['url']}"
            )
            send_telegram(text)
            deals_found += 1
            log.info("Отправлено объявление %s (скидка %s%%)", it["id"], discount_pct)

    if deals_found == 0:
        log.info("Новых выгодных объявлений не найдено (проверено %d шт.)", len(items))

    return new_seen


def main():
    if "ВСТАВЬТЕ" in TELEGRAM_TOKEN or "ВСТАВЬТЕ" in CHAT_ID:
        log.error("Заполните TELEGRAM_TOKEN и CHAT_ID перед запуском!")
        return

    seen = load_seen()
    log.info("Бот запущен. Проверка каждые %d сек.", CHECK_INTERVAL)

    while True:
        seen = run_once(seen)
        save_seen(seen)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
