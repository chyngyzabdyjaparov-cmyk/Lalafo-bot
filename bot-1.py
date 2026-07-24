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
import re
import time
import json
import logging
import requests
import statistics
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lalafo_bot")

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8858673828:AAHOVPtMhYwr5ytzjd9Qkdx5-FcN0NU3mmM")
CHAT_ID = os.environ.get("CHAT_ID", "882643640")

# Реальная страница категории на Lalafo (проверено через поиск)
CATEGORY_URL = "https://lalafo.kg/bishkek/mobilnye-telefony-i-aksessuary/mobilnye-telefony"

DISCOUNT_THRESHOLD = 0.20
CHECK_INTERVAL = 600
SAMPLE_SIZE = 15
SEEN_FILE = "seen_ads.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# ---------------------------------------------------------------------------


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    trimmed = list(seen)[-5000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


def fetch_category_html():
    try:
        r = requests.get(CATEGORY_URL, headers=HEADERS, timeout=20)
        log.info("Запрос к Lalafo: статус %s, длина ответа %d символов", r.status_code, len(r.text))
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.error("Ошибка запроса к Lalafo: %s", e)
        return None


def extract_json_blob(html):
    """Ищет встроенный JSON с данными объявлений в HTML-странице
    (типично для Next.js/Nuxt: __NEXT_DATA__ или __NUXT__)."""
    # Вариант 1: Next.js
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if m:
        log.info("Найден блок __NEXT_DATA__ (Next.js), размер %d символов", len(m.group(1)))
        try:
            return json.loads(m.group(1))
        except Exception as e:
            log.error("Не удалось распарсить __NEXT_DATA__: %s", e)

    # Вариант 2: любой большой JSON-блок со словом "price" рядом с "items"/"ads"
    candidates = re.findall(r'\{.{200,}?\}', html, re.DOTALL)
    log.info("Блок __NEXT_DATA__ не найден. Найдено %d JSON-подобных фрагментов для проверки", len(candidates))
    for c in candidates:
        if '"price"' in c and ('"items"' in c or '"ads"' in c):
            try:
                return json.loads(c)
            except Exception:
                continue

    log.warning("Не удалось найти встроенный JSON с объявлениями на странице")
    return None


def find_items_in_json(obj, found=None):
    """Рекурсивно ищет в JSON списки словарей, похожие на объявления (есть 'price' и 'id')."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        for v in obj.values():
            find_items_in_json(v, found)
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "price" in obj[0] and ("id" in obj[0] or "adId" in obj[0]):
            found.extend(obj)
        else:
            for v in obj:
                find_items_in_json(v, found)
    return found


def parse_item(raw):
    try:
        price_val = raw.get("price")
        if isinstance(price_val, dict):
            price_val = price_val.get("value") or price_val.get("amount")
        item_id = str(raw.get("id") or raw.get("adId"))
        title = raw.get("title") or raw.get("name") or "Без названия"
        return {
            "id": item_id,
            "title": title,
            "price": float(price_val or 0),
            "currency": raw.get("currency", "KGS"),
            "url": f"https://lalafo.kg/kyrgyzstan/o/{item_id}",
            "model_key": normalize_model(title),
        }
    except Exception:
        return None


def normalize_model(title: str) -> str:
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
    html = fetch_category_html()
    if not html:
        return seen

    blob = extract_json_blob(html)
    if not blob:
        return seen

    raw_items = find_items_in_json(blob)
    log.info("Найдено %d потенциальных объявлений в JSON-данных страницы", len(raw_items))

    items = [parse_item(x) for x in raw_items]
    items = [x for x in items if x and x["price"] > 0]
    log.info("После разбора получилось %d объявлений с валидной ценой", len(items))

    if not items:
        log.warning("0 валидных объявлений — структура страницы отличается от ожидаемой")
        return seen

    by_model = defaultdict(list)
    for it in items:
        by_model[it["model_key"]].append(it["price"])

    medians = {
        model: statistics.median(prices[:SAMPLE_SIZE])
        for model, prices in by_model.items()
        if len(prices) >= 3
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
    seen = load_seen()
    log.info("Бот запущен. Проверка каждые %d сек.", CHECK_INTERVAL)

    while True:
        seen = run_once(seen)
        save_seen(seen)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
