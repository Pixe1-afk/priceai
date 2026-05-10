from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import numpy as np
import json
import os
import threading
from datetime import datetime, timedelta
import math
import random
import time
import requests as http_requests
import csv as _csv
import io as _io
from bs4 import BeautifulSoup

# ── Окружение ────────────────────────────────────────────────────────────────
IS_PRODUCTION = os.environ.get("FLASK_DEBUG", "0") != "1"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
HTTP_VERIFY_TLS = True  # для исходящих запросов к Росстат/MultiGO/MOEX/RSS

app = Flask(__name__)
# CORS: в проде — только перечисленные домены, иначе фронт и бэк на одном origin
if ALLOWED_ORIGINS:
    CORS(app, origins=ALLOWED_ORIGINS)
elif not IS_PRODUCTION:
    CORS(app)  # dev: открыто
# В проде без ALLOWED_ORIGINS — CORS не подключаем (same-origin only)


# ── Security headers ─────────────────────────────────────────────────────────
@app.after_request
def _add_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if IS_PRODUCTION:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # CSP подходит для текущего фронта (Chart.js с jsdelivr, MOEX/RSS не идут с фронта)
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self' data:; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
    return resp


# ── Простой rate-limit на IP (in-memory) ─────────────────────────────────────
_rl_lock = threading.Lock()
_rl_state: dict = {}      # {ip: [(timestamp, ...), ...]}
_RL_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))


@app.before_request
def _rate_limit():
    if request.path == "/" or request.path.startswith("/static/"):
        return
    # IP из X-Forwarded-For (PaaS / nginx ставят его)
    fwd = request.headers.get("X-Forwarded-For", "")
    ip  = fwd.split(",")[0].strip() if fwd else request.remote_addr or "?"
    now = time.time()
    with _rl_lock:
        bucket = _rl_state.setdefault(ip, [])
        # окно — последние 60 сек
        cutoff = now - 60
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= _RL_LIMIT_PER_MIN:
            return jsonify({"error": "Too many requests"}), 429
        bucket.append(now)

# --- Данные: магазины и продукты ---
STORES = {
    "pyaterochka": "Пятёрочка",
    "magnit": "Магнит",
    "vkusvill": "ВкусВилл",
    "perekrestok": "Перекрёсток",
    "lenta": "Лента",
}

# Цены актуализированы по данным Аналитического центра Москвы (март 2026)
# и мониторинга розничных цен сетей Москвы (апрель 2026)
PRODUCTS = {
    # Хлеб пшеничный ~156 р/кг по Москве (ac.mos.ru, 23.03.2026) → 500г ≈ 78 р. средняя
    "bread":      {"name": "Хлеб белый (500г)",
                   "base_prices": {"pyaterochka": 68, "magnit": 65, "vkusvill": 95, "perekrestok": 78, "lenta": 67}},
    # Молоко ~102 р/л по Москве (ac.mos.ru)
    "milk":       {"name": "Молоко 3.2% (1л)",
                   "base_prices": {"pyaterochka": 93, "magnit": 89, "vkusvill": 119, "perekrestok": 105, "lenta": 91}},
    # Яйца С1 ~124 р/10шт по Москве (ac.mos.ru)
    "eggs":       {"name": "Яйца С1 (10шт)",
                   "base_prices": {"pyaterochka": 115, "magnit": 110, "vkusvill": 149, "perekrestok": 126, "lenta": 112}},
    # Сахар ~75 р/кг по Москве (ac.mos.ru)
    "sugar":      {"name": "Сахар (1кг)",
                   "base_prices": {"pyaterochka": 70, "magnit": 68, "vkusvill": 86, "perekrestok": 74, "lenta": 69}},
    # Гречка ~111 р/кг по Москве (ac.mos.ru)
    "buckwheat":  {"name": "Гречка (1кг)",
                   "base_prices": {"pyaterochka": 103, "magnit": 99, "vkusvill": 128, "perekrestok": 112, "lenta": 101}},
    # Макароны/вермишель ~165 р/кг по Москве (ac.mos.ru) → 500г ≈ 82 р.
    "pasta":      {"name": "Макароны (500г)",
                   "base_prices": {"pyaterochka": 75, "magnit": 71, "vkusvill": 102, "perekrestok": 82, "lenta": 73}},
    # Масло сливочное: ~180-220 р/200г (Магнит акция: 85р/Простоквашино пример; оценка)
    "butter":     {"name": "Масло сливочное (200г)",
                   "base_prices": {"pyaterochka": 185, "magnit": 178, "vkusvill": 239, "perekrestok": 196, "lenta": 181}},
    # Масло подсолнечное ~149 р/л по Москве (ac.mos.ru)
    "sunflower_oil": {"name": "Масло подсолнечное (1л)",
                   "base_prices": {"pyaterochka": 139, "magnit": 134, "vkusvill": 169, "perekrestok": 148, "lenta": 136}},
    # Картофель ~58 р/кг по Москве (ac.mos.ru)
    "potato":     {"name": "Картофель (1кг)",
                   "base_prices": {"pyaterochka": 54, "magnit": 51, "vkusvill": 76, "perekrestok": 59, "lenta": 52}},
    # Курица ~258 р/кг по Москве (ac.mos.ru)
    "chicken":    {"name": "Курица (1кг)",
                   "base_prices": {"pyaterochka": 238, "magnit": 229, "vkusvill": 305, "perekrestok": 261, "lenta": 233}},
    # Говядина ~870 р/кг по Москве (ac.mos.ru)
    "beef":       {"name": "Говядина (1кг)",
                   "base_prices": {"pyaterochka": 810, "magnit": 790, "vkusvill": 980, "perekrestok": 870, "lenta": 800}},
    # Сыр: ~520 р/кг по России (3pulse, фев 2026) → 200г ≈ 104-130 р. Москва дороже ~+20%
    "cheese":     {"name": "Сыр Российский (200г)",
                   "base_prices": {"pyaterochka": 175, "magnit": 168, "vkusvill": 225, "perekrestok": 185, "lenta": 171}},
    # Рис: ~90-100 р/кг (3pulse даёт 59 р. по России, Москва +40%)
    "rice":       {"name": "Рис (1кг)",
                   "base_prices": {"pyaterochka": 96, "magnit": 92, "vkusvill": 118, "perekrestok": 101, "lenta": 94}},
    # Яблоки: от 90 р. (globalprice.info)
    "apple":      {"name": "Яблоки (1кг)",
                   "base_prices": {"pyaterochka": 109, "magnit": 104, "vkusvill": 148, "perekrestok": 118, "lenta": 106}},
    # Лук репчатый (оценка на основе картофеля ~58 р, лук немного дешевле)
    "onion":      {"name": "Лук репчатый (1кг)",
                   "base_prices": {"pyaterochka": 42, "magnit": 39, "vkusvill": 65, "perekrestok": 47, "lenta": 40}},
}

# --- Города России с коэффициентами цен ---
# food_k  — множитель цен на продукты (Москва = 1.0)
# fuel_k  — множитель цен на топливо  (Москва = 1.0)
# Источники: Росстат, ac.mos.ru, petrolplus.ru, региональные данные 2026
CITIES = {
    "moscow":           {"name": "Москва",              "food_k": 1.00, "fuel_k": 1.00},
    "spb":              {"name": "Санкт-Петербург",      "food_k": 0.95, "fuel_k": 0.99},
    "novosibirsk":      {"name": "Новосибирск",          "food_k": 0.87, "fuel_k": 1.02},
    "ekaterinburg":     {"name": "Екатеринбург",         "food_k": 0.89, "fuel_k": 1.01},
    "kazan":            {"name": "Казань",               "food_k": 0.86, "fuel_k": 0.98},
    "nizhniy_novgorod": {"name": "Нижний Новгород",      "food_k": 0.85, "fuel_k": 0.99},
    "chelyabinsk":      {"name": "Челябинск",            "food_k": 0.84, "fuel_k": 1.01},
    "samara":           {"name": "Самара",               "food_k": 0.86, "fuel_k": 0.97},
    "rostov":           {"name": "Ростов-на-Дону",       "food_k": 0.88, "fuel_k": 0.97},
    "ufa":              {"name": "Уфа",                  "food_k": 0.85, "fuel_k": 0.96},
    "krasnodar":        {"name": "Краснодар",            "food_k": 0.87, "fuel_k": 0.97},
    "voronezh":         {"name": "Воронеж",              "food_k": 0.82, "fuel_k": 0.98},
    "krasnoyarsk":      {"name": "Красноярск",           "food_k": 0.92, "fuel_k": 1.04},
    "perm":             {"name": "Пермь",                "food_k": 0.86, "fuel_k": 1.00},
    "volgograd":        {"name": "Волгоград",            "food_k": 0.83, "fuel_k": 0.97},
    "omsk":             {"name": "Омск",                 "food_k": 0.84, "fuel_k": 1.01},
    "vladivostok":      {"name": "Владивосток",          "food_k": 1.18, "fuel_k": 1.09},
    "khabarovsk":       {"name": "Хабаровск",            "food_k": 1.14, "fuel_k": 1.07},
    "irkutsk":          {"name": "Иркутск",              "food_k": 0.91, "fuel_k": 0.88},
    "norilsk":          {"name": "Норильск",             "food_k": 1.40, "fuel_k": 1.22},
}

# --- Данные: АЗС и виды топлива ---
# Цены по данным driff.ru / petrolplus.ru на 13-14 апреля 2026
# Средние по России: АИ-92 — 64.18, АИ-95 — 68.43, АИ-98 — 87.95, ДТ — 77.75
# Москва в среднем на ~1-2% ниже за счёт высокой конкуренции
FUEL_STATIONS = {
    "lukoil":       "Лукойл",
    "gazpromneft":  "Газпромнефть",
    "rosneft":      "Роснефть",
    "tatneft":      "Татнефть",
    "bashneft":     "Башнефть",
}

FUELS = {
    # АИ-92: ср. по России 64.18 р/л; Москва ~63-66
    "ai92": {
        "name": "АИ-92 (1л)",
        "unit": "₽/л",
        "base_prices": {
            "lukoil": 65.5, "gazpromneft": 64.8, "rosneft": 63.9,
            "tatneft": 62.5, "bashneft": 63.2,
        },
    },
    # АИ-95: ср. по России 68.43 р/л; Москва ~67-71
    "ai95": {
        "name": "АИ-95 (1л)",
        "unit": "₽/л",
        "base_prices": {
            "lukoil": 70.0, "gazpromneft": 69.2, "rosneft": 68.5,
            "tatneft": 67.0, "bashneft": 67.8,
        },
    },
    # АИ-98: ср. по России 87.95 р/л; Москва ~87-92
    "ai98": {
        "name": "АИ-98 (1л)",
        "unit": "₽/л",
        "base_prices": {
            "lukoil": 91.5, "gazpromneft": 90.0, "rosneft": 88.5,
            "tatneft": 87.0, "bashneft": 87.5,
        },
    },
    # АИ-100: Москва ~102-108 (премиум)
    "ai100": {
        "name": "АИ-100 (1л)",
        "unit": "₽/л",
        "base_prices": {
            "lukoil": 108.0, "gazpromneft": 105.5, "rosneft": 103.0,
            "tatneft": 101.5, "bashneft": 102.0,
        },
    },
    # Дизель ДТ: ср. по России 77.75 р/л; Москва ~76-80
    "dt": {
        "name": "Дизель ДТ (1л)",
        "unit": "₽/л",
        "base_prices": {
            "lukoil": 79.5, "gazpromneft": 78.8, "rosneft": 77.5,
            "tatneft": 76.0, "bashneft": 76.8,
        },
    },
}

# ── Акцизы 2026 (руб/т → руб/л) и НДД ────────────────────────────────────────
# Источник: НК РФ ст.193, приказы Минфина 2025-2026
# Плотность: АИ-92/95/98 ≈ 0.730-0.740 кг/л, ДТ ≈ 0.830 кг/л → ~1370 л/т и ~1202 л/т
FUEL_EXCISE = {
    # АИ-92: акциз 14 326 руб/т, НДС 20% сверху
    "ai92":  {"excise": 10.46, "ndd": 3.8},
    # АИ-95/98/100: акциз 15 357 руб/т
    "ai95":  {"excise": 11.21, "ndd": 4.1},
    "ai98":  {"excise": 11.21, "ndd": 4.3},
    "ai100": {"excise": 11.21, "ndd": 4.5},
    # ДТ: акциз 10 425 руб/т
    "dt":    {"excise":  8.67, "ndd": 3.5},
}

# ── Кэш Brent (Yahoo Finance) ──────────────────────────────────────────────────
_brent_cache: dict = {}
_brent_cache_ts: float = 0.0
_BRENT_CACHE_TTL = 3600

# ── Кэш USD/RUB (ЦБ РФ) ───────────────────────────────────────────────────────
_cbr_cache: dict = {}
_cbr_cache_ts: float = 0.0
_CBR_CACHE_TTL = 3600


# ══════════════════════════════════════════════════════════════════════════════
#  СИСТЕМА РЕАЛЬНЫХ ЦЕН
# ══════════════════════════════════════════════════════════════════════════════

PRICES_REAL_FILE = os.path.join(os.path.dirname(__file__), 'prices_real.json')
PRICES_HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'prices_history.json')

# Реальные цены: {product_id: {city_id: {store_id: float}}}
_real_prices: dict = {}
_real_prices_lock = threading.Lock()
_real_prices_updated_at: str = ''

# История цен: {product_id: {city_id: {"dates": [...], "stores": {store_id: [price,...]}}}}
_prices_history: dict = {}
_prices_history_lock = threading.Lock()


def _migrate_real_prices(data: dict) -> dict:
    """Старый формат {product: {store: price}} → новый {product: {city: {store: price}}}."""
    if not data:
        return {}
    sample = next(iter(data.values()), None)
    if not isinstance(sample, dict):
        return {}
    sample_inner = next(iter(sample.values()), None)
    # Если внутренний слой — словарь {store: price}, формат уже новый.
    # Если внутренний слой — число, значит старый формат: распространим на все города через food_k.
    if isinstance(sample_inner, dict):
        return data
    new = {}
    for product_id, store_prices in data.items():
        if product_id not in PRODUCTS or not isinstance(store_prices, dict):
            continue
        new[product_id] = {}
        for city_id, city in CITIES.items():
            k = city['food_k']
            new[product_id][city_id] = {
                store_id: round(float(p) * k, 2)
                for store_id, p in store_prices.items()
                if store_id in STORES
            }
    print(f'[prices] Migrated {len(new)} products to per-city format')
    return new


def load_real_prices():
    global _real_prices, _real_prices_updated_at
    if not os.path.exists(PRICES_REAL_FILE):
        return
    try:
        with open(PRICES_REAL_FILE, encoding='utf-8') as f:
            data = json.load(f)
        loaded = _migrate_real_prices(data.get('prices', {}))
        with _real_prices_lock:
            _real_prices = loaded
            _real_prices_updated_at = data.get('updated_at', '')
        print(f'[prices] Loaded {len(_real_prices)} products from file')
    except Exception as e:
        print(f'[prices] Load error: {e}')


def save_real_prices():
    with _real_prices_lock:
        snapshot = {'prices': dict(_real_prices), 'updated_at': datetime.now().isoformat()}
    try:
        with open(PRICES_REAL_FILE, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'[prices] Save error: {e}')


def get_product_price(product_id: str, store_id: str, city_id: str = 'moscow') -> float:
    """Реальная цена для (продукт, магазин, город). Fallback: base × food_k."""
    with _real_prices_lock:
        real = _real_prices.get(product_id, {}).get(city_id, {}).get(store_id)
    if real is not None:
        return float(real)
    base = PRODUCTS[product_id]['base_prices'][store_id]
    k = CITIES.get(city_id, CITIES['moscow'])['food_k']
    return round(base * k, 2)


# ── История цен: загрузка / сохранение / снапшоты ────────────────────────────

def load_prices_history():
    global _prices_history
    if not os.path.exists(PRICES_HISTORY_FILE):
        return
    try:
        with open(PRICES_HISTORY_FILE, encoding='utf-8') as f:
            data = json.load(f)
        with _prices_history_lock:
            _prices_history = data
        n = sum(len(c) for c in data.values()) if data else 0
        print(f'[history] Loaded {n} (product,city) series')
    except Exception as e:
        print(f'[history] Load error: {e}')


def save_prices_history():
    with _prices_history_lock:
        snapshot = dict(_prices_history)
    try:
        with open(PRICES_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False)
    except Exception as e:
        print(f'[history] Save error: {e}')


def seed_history_for(product_id: str, city_id: str):
    """Создаёт начальную 365-дневную историю для (продукт, город), если её нет."""
    today = datetime.now()
    dates = [(today - timedelta(days=365 - i - 1)).strftime('%Y-%m-%d') for i in range(365)]
    k = CITIES.get(city_id, CITIES['moscow'])['food_k']
    stores_hist = {}
    for store_id in STORES:
        base = round(PRODUCTS[product_id]['base_prices'][store_id] * k, 2)
        stores_hist[store_id] = generate_history(base, 365)
    with _prices_history_lock:
        _prices_history.setdefault(product_id, {})[city_id] = {
            'dates': dates,
            'stores': stores_hist,
        }


def ensure_history_seeded():
    """При первом запуске пред-заполнить историю на 365 дней для всех (продукт, город)."""
    seeded = 0
    with _prices_history_lock:
        existing = {(p, c) for p, cities in _prices_history.items() for c in cities}
    for product_id in PRODUCTS:
        for city_id in CITIES:
            if (product_id, city_id) not in existing:
                seed_history_for(product_id, city_id)
                seeded += 1
    if seeded:
        save_prices_history()
        print(f'[history] Pre-seeded {seeded} (product,city) histories')


def append_today_snapshot():
    """Добавляет цену "сегодняшнего дня" в историю для всех (продукт, город, магазин)."""
    today_str = datetime.now().strftime('%Y-%m-%d')
    changed = False
    with _prices_history_lock:
        for product_id in PRODUCTS:
            for city_id in CITIES:
                series = _prices_history.setdefault(product_id, {}).get(city_id)
                if not series:
                    continue
                if series['dates'] and series['dates'][-1] == today_str:
                    continue
                series['dates'].append(today_str)
                if len(series['dates']) > 1500:  # ограничим ~4 года
                    series['dates'] = series['dates'][-1500:]
                k = CITIES[city_id]['food_k']
                for store_id, prices in series['stores'].items():
                    # Берём текущую (или захардкоженную × food_k) с лёгким случайным дрейфом
                    with _real_prices_lock:
                        real = _real_prices.get(product_id, {}).get(city_id, {}).get(store_id)
                    if real is not None:
                        new_price = float(real)
                    else:
                        prev = prices[-1] if prices else PRODUCTS[product_id]['base_prices'][store_id] * k
                        # лёгкий дневной случайный walk +/- 0.4%
                        new_price = round(prev * random.uniform(0.996, 1.004), 2)
                    prices.append(new_price)
                    if len(prices) > 1500:
                        prices[:] = prices[-1500:]
                changed = True
    if changed:
        save_prices_history()


def get_history_series(product_id: str, city_id: str, days: int) -> dict:
    """Срез последних N дней истории. Возвращает {dates, stores: {store: [...]}, source}."""
    with _prices_history_lock:
        series = _prices_history.get(product_id, {}).get(city_id)
    if series and series.get('dates') and len(series['dates']) >= days:
        d = series['dates'][-days:]
        s = {sid: prices[-days:] for sid, prices in series['stores'].items()}
        return {'dates': d, 'stores': s, 'source': 'recorded'}
    # Fallback: синтетика на лету
    today = datetime.now()
    d = [(today - timedelta(days=days - i - 1)).strftime('%Y-%m-%d') for i in range(days)]
    k = CITIES.get(city_id, CITIES['moscow'])['food_k']
    s = {}
    for store_id in STORES:
        base = round(PRODUCTS[product_id]['base_prices'][store_id] * k, 2)
        s[store_id] = generate_history(base, max(days, 365))[-days:]
    return {'dates': d, 'stores': s, 'source': 'synthetic'}


# ── Росстат: маппинг названий товаров ─────────────────────────────────────────
# Росстат публикует еженедельный мониторинг цен: https://rosstat.gov.ru/price
# Названия товаров в их таблицах (частичное совпадение)
_ROSSTAT_MAP = {
    'хлеб': 'bread',
    'молоко пастер': 'milk',
    'яйца куриные': 'eggs',
    'сахар': 'sugar',
    'гречн': 'buckwheat',
    'макарон': 'pasta',
    'масло слив': 'butter',
    'масло подсол': 'sunflower_oil',
    'картофель': 'potato',
    'птицы': 'chicken',
    'говядина': 'beef',
    'сыр': 'cheese',
    'рис': 'rice',
    'яблоки': 'apple',
    'лук реп': 'onion',
}

_rosstat_last_fetch: float = 0.0
_ROSSTAT_CACHE_TTL = 7 * 24 * 3600  # раз в неделю


def fetch_rosstat_prices() -> dict:
    """
    Парсит страницу еженедельного мониторинга цен Росстата.
    Возвращает {product_id: avg_price_russia}.
    """
    global _rosstat_last_fetch
    now = time.time()
    if now - _rosstat_last_fetch < _ROSSTAT_CACHE_TTL:
        return {}

    result = {}
    try:
        r = http_requests.get(
            'https://rosstat.gov.ru/price',
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
            timeout=20,
            verify=HTTP_VERIFY_TLS,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'html.parser')

        for row in soup.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) < 2:
                continue
            name = cells[0].get_text(strip=True).lower()
            for keyword, product_id in _ROSSTAT_MAP.items():
                if keyword in name:
                    # Берём последнюю числовую колонку (последняя неделя)
                    for cell in reversed(cells[1:]):
                        raw = cell.get_text(strip=True).replace(',', '.').replace('\xa0', '').replace(' ', '')
                        try:
                            price = float(raw)
                            if 5 < price < 5000:  # санитарная проверка диапазона
                                result[product_id] = price
                                break
                        except ValueError:
                            continue
                    break

        if result:
            _rosstat_last_fetch = now
            print(f'[rosstat] Fetched {len(result)} prices: {list(result.keys())}')
        else:
            print('[rosstat] Page parsed but no prices found (HTML structure may have changed)')

    except Exception as e:
        print(f'[rosstat] Fetch error: {e}')

    return result


def apply_rosstat_to_stores(rosstat: dict):
    """
    Конвертирует среднероссийские цены Росстата в цены ПО ГОРОДАМ И МАГАЗИНАМ.
    Сохраняет пропорции магазинов из base_prices и применяет food_k для каждого города.
    """
    if not rosstat:
        return

    updated = []
    with _real_prices_lock:
        for product_id, avg_russia in rosstat.items():
            if product_id not in PRODUCTS:
                continue
            base_prices = PRODUCTS[product_id]['base_prices']
            base_avg = sum(base_prices.values()) / len(base_prices)
            if base_avg == 0:
                continue
            # Москва в среднем на ~15% дороже средней по России
            moscow_avg = avg_russia * 1.15
            _real_prices[product_id] = {}
            for city_id, city in CITIES.items():
                k = city['food_k']
                city_avg = moscow_avg * k
                _real_prices[product_id][city_id] = {
                    store_id: round(city_avg * (base_price / base_avg), 2)
                    for store_id, base_price in base_prices.items()
                }
            updated.append(product_id)

    if updated:
        save_real_prices()
        print(f'[prices] Updated {len(updated)} products × {len(CITIES)} cities from Росстат')


def _daily_snapshot_loop():
    """Фоновый поток: добавляет дневной снапшот цен в историю."""
    time.sleep(10)
    last_date = ''
    while True:
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            if today != last_date:
                append_today_snapshot()
                last_date = today
        except Exception as e:
            print(f'[snapshot] Error: {e}')
        time.sleep(3600)  # проверка раз в час


def _auto_update_loop():
    """Фоновый поток: обновляет цены из Росстата раз в неделю."""
    time.sleep(5)  # дать Flask запуститься
    while True:
        try:
            rosstat = fetch_rosstat_prices()
            apply_rosstat_to_stores(rosstat)
        except Exception as e:
            print(f'[auto-update] Error: {e}')
        time.sleep(3600)  # проверяем каждый час


def fetch_brent() -> dict:
    """История и текущая цена нефти Brent (USD/баррель) — Yahoo Finance."""
    global _brent_cache, _brent_cache_ts
    now = time.time()
    if _brent_cache and (now - _brent_cache_ts) < _BRENT_CACHE_TTL:
        return _brent_cache
    try:
        r = http_requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?interval=1d&range=6mo",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        res = d["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        current = float(res["meta"].get("regularMarketPrice", closes[-1]))
        avg30  = round(sum(closes[-30:]) / len(closes[-30:]), 2)
        avg90  = round(sum(closes[-90:]) / len(closes[-90:]), 2)
        data = {"current": round(current, 2), "avg30": avg30, "avg90": avg90,
                "history": [round(c, 2) for c in closes[-90:]]}
        _brent_cache = data
        _brent_cache_ts = now
    except Exception:
        pass
    return _brent_cache


def fetch_usd_rub() -> dict:
    """Текущий курс USD/RUB от ЦБ РФ (XML API)."""
    global _cbr_cache, _cbr_cache_ts
    now = time.time()
    if _cbr_cache and (now - _cbr_cache_ts) < _CBR_CACHE_TTL:
        return _cbr_cache
    try:
        from xml.etree import ElementTree as ET
        r = http_requests.get("https://www.cbr.ru/scripts/XML_daily.asp", timeout=8)
        r.raise_for_status()
        root = ET.fromstring(r.content.decode("windows-1251"))
        for v in root.findall("Valute"):
            if v.find("CharCode").text == "USD":
                rate = float(v.find("Value").text.replace(",", ".")) / int(v.find("Nominal").text)
                data = {"rate": round(rate, 4), "date": root.attrib.get("Date", "")}
                _cbr_cache = data
                _cbr_cache_ts = now
                return data
    except Exception:
        pass
    return _cbr_cache or {"rate": USD_RUB, "date": ""}


# ── MultiGO парсер цен на топливо ─────────────────────────────────────────────
_fuel_price_cache: dict = {}
_fuel_cache_ts: float = 0.0
_FUEL_CACHE_TTL = 3600  # обновление раз в час

_MULTIGO_FUEL_MAP = {
    "Дт":          "dt",     # Дт
    "Аи 92":       "ai92",   # Аи 92
    "Аи 95":       "ai95",   # Аи 95
    "Аи 98":       "ai98",   # Аи 98
    "Аи 100":      "ai100",  # Аи 100
}


def fetch_multigo_prices() -> dict:
    """Парсит MultiGO и возвращает средние цены по Москве {fuel_id: price}."""
    global _fuel_price_cache, _fuel_cache_ts
    now = time.time()
    if _fuel_price_cache and (now - _fuel_cache_ts) < _FUEL_CACHE_TTL:
        return _fuel_price_cache
    try:
        r = http_requests.get(
            "https://business.multigo.ru/fuelcards/averageprices",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser", from_encoding="utf-8")
        tags = soup.find_all("div", class_=lambda c: c and "fuelPriceTag" in c)
        result = {}
        for tag in tags:
            head = tag.find("div", class_=lambda c: c and "head" in c)
            body = tag.find("div", class_=lambda c: c and "body" in c)
            if head and body:
                fuel_id = _MULTIGO_FUEL_MAP.get(head.text.strip())
                if fuel_id:
                    result[fuel_id] = float(body.text.strip())
        if result:
            _fuel_price_cache = result
            _fuel_cache_ts = now
    except Exception:
        pass
    return _fuel_price_cache


def get_fuel_station_price(fuel_id: str, station_id: str) -> float:
    """
    Реальная цена MultiGO (Москва) × спред конкретной АЗС.
    Если MultiGO недоступен — fallback на захардкоженные значения.
    """
    live = fetch_multigo_prices()
    hardcoded = FUELS[fuel_id]["base_prices"]
    if fuel_id in live:
        hard_avg = sum(hardcoded.values()) / len(hardcoded)
        spread = hardcoded[station_id] / hard_avg
        return round(live[fuel_id] * spread, 2)
    return hardcoded[station_id]


def _anchor(prices: list, target: float) -> list:
    """
    Масштабирует список цен так, чтобы последнее значение равнялось target.
    Форма кривой сохраняется, только общий уровень сдвигается.
    """
    if not prices or prices[-1] == 0:
        return prices
    factor = target / prices[-1]
    dec = 8 if target < 0.0001 else (6 if target < 0.01 else (4 if target < 1 else 2))
    return [round(p * factor, dec) for p in prices]


# --- Генерация истории цен на топливо (специфика: зависит от нефти, сезонность другая) ---
def generate_fuel_history(base_price: float, days: int = 365) -> list:
    """История цен на топливо: тренд +5-7%/год, нефтяные скачки, летний пик."""
    random.seed(int(base_price * 100))
    prices = []
    price = base_price * 0.94

    annual_growth = random.uniform(0.05, 0.08)

    for i in range(days):
        trend = price * (annual_growth / 365)
        season = base_price * 0.025 * np.sin(2 * np.pi * i / 365 - np.pi / 2)
        noise = random.gauss(0, base_price * 0.002)
        spike = 0
        if random.random() < 0.015:
            spike = base_price * random.uniform(-0.06, 0.10)

        price = max(price + trend + noise + spike, base_price * 0.75)
        prices.append(round(price + season, 2))

    return _anchor(prices, base_price)


# --- Генерация исторических данных с трендом, сезонностью и шумом ---
def generate_history(base_price: float, days: int = 365) -> list:
    """Генерирует реалистичную историю цен."""
    random.seed(base_price)
    prices = []
    price = base_price * 0.88

    annual_growth = random.uniform(0.08, 0.12)

    for i in range(days):
        trend = price * (annual_growth / 365)
        season = base_price * 0.04 * np.sin(2 * np.pi * i / 365 + np.pi)
        noise = random.gauss(0, base_price * 0.005)
        spike = 0
        if random.random() < 0.02:
            spike = base_price * random.uniform(-0.08, 0.12)

        price = max(price + trend + noise + spike, base_price * 0.5)
        prices.append(round(price + season, 2))

    return _anchor(prices, base_price)


# --- Старт фоновых задач (после того, как generate_history определён) ---
load_real_prices()
load_prices_history()
ensure_history_seeded()
threading.Thread(target=_auto_update_loop, daemon=True).start()
threading.Thread(target=_daily_snapshot_loop, daemon=True).start()


# --- Улучшенная ML модель прогноза ---
def predict_prices_correlated(histories: dict, future_days: int) -> dict:
    """
    Прогноз с учётом корреляции между рядами (магазины, АЗС).
    Каждый ряд прогнозируется независимо, затем нормализованные прогнозы
    смешиваются через рыночный консенсус (70/30).
    Нормализация происходит ПОСЛЕ предсказания — predict_prices никогда
    не запускается на безразмерных индексах.
    """
    arrays = {k: np.array(v, dtype=float) for k, v in histories.items()}

    # Независимые прогнозы для каждого ряда
    individual = {k: predict_prices(arr.tolist(), future_days) for k, arr in arrays.items()}

    # Нормализуем прогнозы к текущей цене (относительное изменение)
    norm_preds = {}
    for k, arr in arrays.items():
        current = float(arr[-1])
        if current > 0:
            norm_preds[k] = [p / current for p in individual[k]['predictions']]
        else:
            norm_preds[k] = [1.0] * future_days

    # Рыночный консенсус: среднее нормализованных прогнозов
    market_norm = np.mean(list(norm_preds.values()), axis=0)

    results = {}
    for k, arr in arrays.items():
        current = float(arr[-1])
        blended_norm = 0.70 * market_norm + 0.30 * np.array(norm_preds[k])
        preds = [round(current * float(b), 2) for b in blended_norm]
        results[k] = {
            'predictions': preds,
            'upper':       individual[k]['upper'],
            'lower':       individual[k]['lower'],
        }
    return results


def predict_prices(history: list, future_days: int) -> dict:
    """
    Модель: взвешенное смешение краткосрочного и долгосрочного трендов +
    правильная сезонность (на деtrended ряде) + доверительный интервал
    на основе реальных остатков (не произвольные проценты).

    Возвращает dict: {"predictions": [...], "upper": [...], "lower": [...]}.
    """
    prices = np.array(history, dtype=float)
    n = len(prices)
    x_all = np.arange(n, dtype=float)

    # 1. Долгосрочный тренд (МНК по всей истории)
    coeffs_long = np.polyfit(x_all, prices, 1)
    slope_long = float(coeffs_long[0])

    # 2. Краткосрочный тренд (последние 45 дней с экспоненциальными весами)
    #    Захватывает текущий «моментум» цены
    window = min(45, n)
    x_w = np.arange(window, dtype=float)
    ew = np.exp(np.linspace(0, 2, window))          # свежие точки важнее
    coeffs_recent = np.polyfit(x_w, prices[-window:], 1, w=ew)
    slope_recent = float(coeffs_recent[0])

    # 3. Сезонность: считаем на деtrended ряде, чтобы не смешивать с трендом
    trend_line = np.polyval(coeffs_long, x_all)
    detrended = prices - trend_line
    seasonal = np.zeros(future_days)
    period = 365
    if n >= 60:
        for i in range(future_days):
            idx = (n + i) % period
            same_phase = [j for j in range(n) if j % period == idx]
            if same_phase:
                seasonal[i] = float(np.mean(detrended[same_phase]))

    # 4. Реальная волатильность: стандартное отклонение остатков от тренда
    # Ограничиваем сверху 2.5% цены — иначе при «плохих» seed'ах накапливается
    # нереалистичный шум за 30-90 шагов (цены типа топлива/еды очень стабильны).
    residuals = prices - trend_line
    residual_std = float(np.clip(np.std(residuals),
                                 float(prices[-1]) * 0.002,
                                 float(prices[-1]) * 0.025))

    # 5. Прогноз
    # Шум независимый на каждом шаге (от базовой цены, не накапливающийся).
    # Это предотвращает случайный дрейф при неудачных seed'ах.
    rng = np.random.default_rng(int(prices[-1] * 997 + n) % (2**31))
    base_price = float(prices[-1])
    cumulative_trend = 0.0
    predictions = []

    for i in range(future_days):
        # Плавный переход от краткосрочного к долгосрочному тренду (за 60 дней)
        mix = min(1.0, i / 60.0)
        blended_slope = slope_recent * (1 - mix) + slope_long * mix

        # Затухание тренда на очень длинных горизонтах
        decay = max(0.2, 1.0 - i / (365 * 3))
        trend_step = blended_slope * decay

        # Сезонность убывает с горизонтом; ограничиваем вклад ≤0.3%/день
        # чтобы фазовый сдвиг на синтетических данных не давал аномальных спадов
        season_weight = max(0.0, 1.0 - i / (365 * 2))
        season_raw  = seasonal[i] * season_weight * 0.1
        season_step = float(np.clip(season_raw, -float(prices[-1]) * 0.003, float(prices[-1]) * 0.003))

        cumulative_trend += trend_step + season_step

        # Независимый шум от стартовой цены, нарастает медленно с горизонтом
        horizon_frac = math.sqrt((i + 1) / max(future_days, 30))
        noise = rng.normal(0, residual_std * 0.25 * horizon_frac)

        current = base_price + cumulative_trend + noise
        current = max(current, base_price * 0.75)
        predictions.append(round(current, 2))

    # 6. Доверительный интервал 95% на основе residual_std (расширяется с горизонтом)
    upper = []
    lower = []
    for i, p in enumerate(predictions):
        horizon_factor = 1.0 + (i / max(future_days, 1)) * 1.5
        margin = residual_std * 1.96 * horizon_factor
        upper.append(round(p + margin, 2))
        lower.append(round(max(p - margin, float(prices[-1]) * 0.3), 2))

    return {"predictions": predictions, "upper": upper, "lower": lower}


# --- API эндпоинты ---

@app.route("/")
def index():
    return render_template("index.html")


###WFP_REMOVED###
WFP_CSV_URL = (
    "https://data.humdata.org/dataset/4fdcd4dc-5c2f-43af-a1e4-93c9b6539a27"
    "/resource/12d7c8e3-eff9-4db0-93b7-726825c4fe9a/download/wfpvam_foodprices.csv"
)
WFP_CACHE_FILE = os.path.join(os.path.dirname(__file__), "wfp_cache.json")
WFP_CACHE_TTL  = 7 * 24 * 3600

WFP_PRODUCTS = {
    "eggs":          "Яйца",
    "potato":        "Картофель",
    "onion":         "Лук",
    "beef":          "Говядина",
    "sunflower_oil": "Масло растительное",
    "rice":          "Рис",
    "sugar":         "Сахар",
    "bread":         "Пшеничная мука",
}

WFP_COMMODITY_MAP = {
    "Eggs - Retail":                           "eggs",
    "Potatoes - Retail":                       "potato",
    "Onions - Retail":                         "onion",
    "Meat (beef) - Retail":                    "beef",
    "Oil (vegetable) - Retail":                "sunflower_oil",
    "Rice - Retail":                           "rice",
    "Rice (imported) - Retail":                "rice",
    "Rice (local) - Retail":                   "rice",
    "Sugar - Retail":                          "sugar",
    "Wheat flour - Retail":                    "bread",
    "Wheat flour (high grade) - Wholesale":    "bread",
}

WFP_COUNTRIES = {
    # G20 в датасете WFP
    "Argentina":          {"name": "🇦🇷 Аргентина",         "currency": "ARS"},
    "China":              {"name": "🇨🇳 Китай",              "currency": "CNY"},
    "Indonesia":          {"name": "🇮🇩 Индонезия",          "currency": "IDR"},
    "Japan":              {"name": "🇯🇵 Япония",             "currency": "JPY"},
    "Mexico":             {"name": "🇲🇽 Мексика",            "currency": "MXN"},
    "Russian Federation": {"name": "🇷🇺 Россия",             "currency": "RUB"},
    "South Africa":       {"name": "🇿🇦 ЮАР",               "currency": "ZAR"},
    "Turkey":             {"name": "🇹🇷 Турция",             "currency": "TRY"},
    # Дополнительные страны с хорошим покрытием WFP
    "Armenia":            {"name": "🇦🇲 Армения",            "currency": "AMD"},
    "Ukraine":            {"name": "🇺🇦 Украина",            "currency": "UAH"},
    "Kyrgyzstan":         {"name": "🇰🇬 Кыргызстан",         "currency": "KGS"},
    "Tajikistan":         {"name": "🇹🇯 Таджикистан",        "currency": "TJS"},
    "Jordan":             {"name": "🇯🇴 Иордания",           "currency": "JOD"},
    "Iraq":               {"name": "🇮🇶 Ирак",               "currency": "IQD"},
    "Algeria":            {"name": "🇩🇿 Алжир",              "currency": "DZD"},
    "Egypt":              {"name": "🇪🇬 Египет",             "currency": "EGP"},
}

# Только страны с данными в WFP
G20_SELECTOR = {
    "all":          "🌍 Все страны с данными",
    "Argentina":    "🇦🇷 Аргентина",
    "China":        "🇨🇳 Китай",
    "Indonesia":    "🇮🇩 Индонезия",
    "Japan":        "🇯🇵 Япония",
    "Mexico":       "🇲🇽 Мексика",
    "Russia":       "🇷🇺 Россия",
    "South Africa": "🇿🇦 ЮАР",
    "Turkey":       "🇹🇷 Турция",
    "Armenia":      "🇦🇲 Армения",
    "Ukraine":      "🇺🇦 Украина",
    "Kyrgyzstan":   "🇰🇬 Кыргызстан",
    "Tajikistan":   "🇹🇯 Таджикистан",
    "Jordan":       "🇯🇴 Иордания",
    "Iraq":         "🇮🇶 Ирак",
    "Algeria":      "🇩🇿 Алжир",
    "Egypt":        "🇪🇬 Египет",
}

# Ключ селектора → WFP-ключ
G20_TO_WFP = {
    "Argentina":    "Argentina",
    "China":        "China",
    "Indonesia":    "Indonesia",
    "Japan":        "Japan",
    "Mexico":       "Mexico",
    "Russia":       "Russian Federation",
    "South Africa": "South Africa",
    "Turkey":       "Turkey",
    "Armenia":      "Armenia",
    "Ukraine":      "Ukraine",
    "Kyrgyzstan":   "Kyrgyzstan",
    "Tajikistan":   "Tajikistan",
    "Jordan":       "Jordan",
    "Iraq":         "Iraq",
    "Algeria":      "Algeria",
    "Egypt":        "Egypt",
}

_wfp_data:       dict = {}
_wfp_currencies: dict = {}
_wfp_loaded:     bool = False
_wfp_lock = threading.Lock()


def _wfp_load_cache() -> bool:
    global _wfp_data, _wfp_currencies, _wfp_loaded
    if not os.path.exists(WFP_CACHE_FILE):
        return False
    if time.time() - os.path.getmtime(WFP_CACHE_FILE) > WFP_CACHE_TTL:
        return False
    try:
        with open(WFP_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        with _wfp_lock:
            _wfp_data       = cache["data"]
            _wfp_currencies = cache["currencies"]
            _wfp_loaded     = True
        print(f"[WFP] Loaded from cache: {len(_wfp_data)} countries")
        return True
    except Exception as e:
        print(f"[WFP] Cache error: {e}")
        return False


def _wfp_fetch():
    global _wfp_data, _wfp_currencies, _wfp_loaded
    try:
        print("[WFP] Downloading CSV (~215 MB)…")
        r = http_requests.get(
            WFP_CSV_URL, headers={"User-Agent": "Mozilla/5.0"},
            timeout=240, verify=HTTP_VERIFY_TLS,
        )
        r.raise_for_status()
        content = r.content.decode("utf-8", errors="replace")
        reader  = _csv.DictReader(_io.StringIO(content))

        data: dict = {k: {} for k in WFP_COUNTRIES}
        currencies: dict = {}

        for row in reader:
            country = row.get("adm0_name", "")
            if country not in WFP_COUNTRIES:
                continue
            pid = WFP_COMMODITY_MAP.get(row.get("cm_name", ""))
            if not pid:
                continue
            try:
                y = int(row["mp_year"])
                m = int(row["mp_month"])
                p = float(row["mp_price"] or 0)
            except (ValueError, KeyError):
                continue
            if p <= 0:
                continue
            data[country].setdefault(pid, {}).setdefault((y, m), []).append(p)
            currencies[country] = row.get("cur_name", "?")

        # Усредняем по месяцу (несколько рынков) и сортируем
        for country in data:
            for pid in data[country]:
                data[country][pid] = sorted(
                    [[y, m, round(sum(ps) / len(ps), 4)]
                     for (y, m), ps in data[country][pid].items()],
                    key=lambda x: (x[0], x[1]),
                )

        with _wfp_lock:
            _wfp_data       = data
            _wfp_currencies = currencies
            _wfp_loaded     = True

        with open(WFP_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"data": data, "currencies": currencies,
                       "downloaded_at": datetime.now().isoformat()}, f, ensure_ascii=False)
        print(f"[WFP] Done: {sum(len(v) for v in data.values())} series")
    except Exception as e:
        print(f"[WFP] Fetch error: {e}")


def _wfp_monthly_to_daily(monthly: list) -> list:
    if len(monthly) < 2:
        return [monthly[0][2]] * 30 if monthly else []
    daily = []
    for i in range(len(monthly) - 1):
        y1, m1, p1 = monthly[i]
        y2, m2, p2 = monthly[i + 1]
        n = max(1, (datetime(y2, m2, 1) - datetime(y1, m1, 1)).days)
        for d in range(n):
            daily.append(round(p1 + (p2 - p1) * d / n, 4))
    daily.append(monthly[-1][2])
    return daily


def _wfp_start():
    time.sleep(3)
    if not _wfp_load_cache():
        _wfp_fetch()

threading.Thread(target=_wfp_start, daemon=True).start()


# ── WFP endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/wfp/status")
def wfp_status():
    with _wfp_lock:
        loaded = _wfp_loaded
        countries = list(_wfp_data.keys()) if loaded else []
    return jsonify({"loaded": loaded, "countries": countries})


@app.route("/api/wfp/products")
def wfp_get_products():
    return jsonify(WFP_PRODUCTS)


@app.route("/api/wfp/countries")
def wfp_get_countries():
    return jsonify({k: v["name"] for k, v in WFP_COUNTRIES.items()})


@app.route("/api/wfp/g20")
def wfp_g20():
    """G20-список для UI: какие страны есть в WFP, какие нет."""
    with _wfp_lock:
        loaded = _wfp_loaded
        available = set(_wfp_data.keys()) if loaded else set()
    result = {}
    for key, label in G20_SELECTOR.items():
        wfp_key = G20_TO_WFP.get(key)
        has_data = wfp_key in available if wfp_key else False
        result[key] = {"label": label, "wfp_key": wfp_key, "has_data": has_data}
    return jsonify(result)


@app.route("/api/wfp/history-all")
def wfp_history_all():
    product_id     = request.args.get("product", "eggs")
    months         = int(request.args.get("months", 48))
    country_filter = request.args.get("country", "all")

    if not _wfp_loaded:
        return jsonify({"error": "loading"}), 503

    # Определяем какие WFP-ключи показывать
    if country_filter != "all":
        wfp_key = G20_TO_WFP.get(country_filter)
        if not wfp_key:
            return jsonify({"error": "no_wfp_data", "country": country_filter,
                            "message": f"Данные WFP недоступны для: {G20_SELECTOR.get(country_filter, country_filter)}"}), 404
        show_keys = {wfp_key}
    else:
        show_keys = None  # все

    cutoff_year = datetime.now().year - months // 12 - 1
    all_dates   = set()
    result      = {}

    with _wfp_lock:
        for cid, cinfo in WFP_COUNTRIES.items():
            if show_keys and cid not in show_keys:
                continue
            series = _wfp_data.get(cid, {}).get(product_id, [])
            if not series:
                continue
            filtered = [[y, m, p] for y, m, p in series if y >= cutoff_year] or series[-months:]
            pm = {f"{y}-{m:02d}": p for y, m, p in filtered}
            all_dates.update(pm.keys())
            last = filtered[-1]
            result[cid] = {
                "country":    cinfo["name"],
                "currency":   _wfp_currencies.get(cid, "?"),
                "prices_map": pm,
                "last_price": last[2],
                "last_date":  f"{last[0]}-{last[1]:02d}",
            }

    sorted_dates = sorted(all_dates)
    for cid in result:
        pm = result[cid].pop("prices_map")
        result[cid]["prices"] = [pm.get(d) for d in sorted_dates]

    return jsonify({"dates": sorted_dates, "countries": result,
                    "product": WFP_PRODUCTS.get(product_id, product_id)})


@app.route("/api/wfp/predict-all")
def wfp_predict_all():
    product_id     = request.args.get("product", "eggs")
    period         = request.args.get("period", "month")
    country_filter = request.args.get("country", "all")
    future_days    = PERIOD_DAYS.get(period, 30)

    if not _wfp_loaded:
        return jsonify({"error": "loading"}), 503

    if country_filter != "all":
        wfp_key = G20_TO_WFP.get(country_filter)
        if not wfp_key:
            return jsonify({"error": "no_wfp_data", "country": country_filter,
                            "message": f"Данные WFP недоступны для: {G20_SELECTOR.get(country_filter, country_filter)}"}), 404
        show_keys = {wfp_key}
    else:
        show_keys = None

    today   = datetime.now()
    indices = sample_indices(future_days)
    dates   = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in indices]

    result      = {}
    ref_history = None

    with _wfp_lock:
        for cid, cinfo in WFP_COUNTRIES.items():
            if show_keys and cid not in show_keys:
                continue
            series = _wfp_data.get(cid, {}).get(product_id, [])
            if len(series) < 6:
                continue  # недостаточно данных для прогноза (нужно ≥6 месяцев)
            daily = _wfp_monthly_to_daily(series[-48:])
            if len(daily) < 10:
                continue
            pred    = predict_prices(daily, future_days)
            cur_p   = daily[-1]
            fin_p   = pred["predictions"][-1]
            change  = round(fin_p - cur_p, 4)
            chg_pct = round((change / cur_p) * 100, 1) if cur_p else 0
            result[cid] = {
                "country":       cinfo["name"],
                "currency":      _wfp_currencies.get(cid, "?"),
                "predictions":   [pred["predictions"][i] for i in indices],
                "current_price": round(cur_p, 4),
                "final_price":   round(fin_p, 4),
                "change":        change,
                "change_pct":    chg_pct,
            }
            if ref_history is None:
                ref_history = daily

    all_probs      = calibrate_accuracy(ref_history or [1]*30, future_days)
    accuracy_probs = [all_probs[i] if i < len(all_probs) else 3 for i in indices]

    return jsonify({
        "dates":          dates,
        "countries":      result,
        "period_label":   PERIOD_LABELS.get(period, period),
        "product":        WFP_PRODUCTS.get(product_id, product_id),
        "accuracy_probs": accuracy_probs,
    })


@app.route("/api/products")
def get_products():
    return jsonify({k: v["name"] for k, v in PRODUCTS.items()})


@app.route("/api/stores")
def get_stores():
    return jsonify(STORES)


@app.route("/api/current-prices")
def get_current_prices():
    """Текущие цены во всех магазинах для продукта."""
    product_id = request.args.get("product", "bread")
    if product_id not in PRODUCTS:
        return jsonify({"error": "Unknown product"}), 400

    product = PRODUCTS[product_id]
    result = {}
    for store_id, store_name in STORES.items():
        base = product["base_prices"][store_id]
        # Небольшая случайная вариация "сегодня"
        today_price = round(base * random.uniform(0.97, 1.03), 2)
        result[store_id] = {
            "store": store_name,
            "price": today_price,
        }
    return jsonify(result)


@app.route("/api/history")
def get_history():
    """История цен за последние N дней."""
    product_id = request.args.get("product", "bread")
    store_id = request.args.get("store", "pyaterochka")
    days = int(request.args.get("days", 90))

    if product_id not in PRODUCTS:
        return jsonify({"error": "Unknown product"}), 400

    base = get_product_price(product_id, store_id)
    full_history = generate_history(base, 365)
    history = full_history[-days:]

    today = datetime.now()
    dates = [(today - timedelta(days=days - i - 1)).strftime("%Y-%m-%d") for i in range(len(history))]

    return jsonify({"dates": dates, "prices": history})


@app.route("/api/predict")
def get_prediction():
    """Прогноз цены на выбранный период."""
    product_id = request.args.get("product", "bread")
    store_id = request.args.get("store", "pyaterochka")
    period = request.args.get("period", "week")  # day, week, month, year, 3years, 5years

    PERIOD_DAYS = {
        "day": 1,
        "week": 7,
        "month": 30,
        "year": 365,
        "3years": 365 * 3,
        "5years": 365 * 5,
    }

    future_days = PERIOD_DAYS.get(period, 7)
    if product_id not in PRODUCTS:
        return jsonify({"error": "Unknown product"}), 400

    base = get_product_price(product_id, store_id)
    history = generate_history(base, 365)
    pred_result = predict_prices(history, future_days)
    all_preds = pred_result["predictions"]
    all_upper = pred_result["upper"]
    all_lower = pred_result["lower"]

    today = datetime.now()
    indices = sample_indices(future_days)
    if future_days <= 30:
        indices = list(range(future_days))
    predictions_sampled = [all_preds[i] for i in indices]
    upper = [all_upper[i] for i in indices]
    lower = [all_lower[i] for i in indices]
    dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in indices]

    current_price = history[-1]
    final_price = all_preds[-1]
    change = round(final_price - current_price, 2)
    change_pct = round((change / current_price) * 100, 1)

    return jsonify({
        "dates": dates,
        "predictions": predictions_sampled,
        "upper_bound": upper,
        "lower_bound": lower,
        "current_price": round(current_price, 2),
        "final_price": round(final_price, 2),
        "change": change,
        "change_pct": change_pct,
        "period_label": {
            "day": "на 1 день",
            "week": "на 1 неделю",
            "month": "на 1 месяц",
            "year": "на 1 год",
            "3years": "на 3 года",
            "5years": "на 5 лет",
        }.get(period, period),
    })


PERIOD_DAYS = {
    "day": 1, "week": 7, "month": 30,
    "year": 365, "3years": 365 * 3, "5years": 365 * 5,
}
PERIOD_LABELS = {
    "day": "на 1 день", "week": "на 1 неделю", "month": "на 1 месяц",
    "year": "на 1 год", "3years": "на 3 года", "5years": "на 5 лет",
}

# Крипто: короткие горизонты в минутах
CRYPTO_PERIOD_MINUTES = {
    "1min": 1, "5min": 5, "10min": 10,
    "30min": 30, "1hour": 60, "24hours": 1440,
}
CRYPTO_PERIOD_LABELS = {
    "1min": "на 1 мин", "5min": "на 5 мин", "10min": "на 10 мин",
    "30min": "на 30 мин", "1hour": "на 1 час", "24hours": "на 24 часа",
}


def predict_crypto_intraday(current_price: float, minutes: int) -> dict:
    """GBM с минутной волатильностью для коротких горизонтов."""
    daily_sigma  = 0.038                          # ~3.8% дневная волатильность
    min_sigma    = daily_sigma / (1440 ** 0.5)    # перевод в минутную
    dec = 6 if current_price < 0.01 else (4 if current_price < 1 else 2)

    rng = np.random.default_rng(int(current_price * 997 + minutes) % (2**31))
    predictions, upper, lower = [], [], []
    price = current_price

    for i in range(minutes):
        price = price * float(np.exp(rng.normal(0, min_sigma)))
        price = max(price, current_price * 0.5)
        predictions.append(round(price, dec))

        t = float(i + 1)
        band = current_price * (float(np.exp(1.96 * min_sigma * t ** 0.5)) - 1)
        upper.append(round(price + band * 0.5, dec))
        lower.append(round(max(price - band * 0.4, current_price * 0.7), dec))

    return {"predictions": predictions, "upper": upper, "lower": lower}

def sample_indices(future_days):
    if future_days > 365:
        step = future_days // 100
    elif future_days > 30:
        step = max(1, future_days // 60)
    else:
        return list(range(future_days))
    return list(range(0, future_days, step))


def predict_fuel_with_factors(history: list, future_days: int, fuel_id: str) -> dict:
    """
    Прогноз цен на топливо с поправкой на внешние факторы:
      — Brent (Yahoo Finance): если Brent выше 30-дн. средней → цены идут вверх
      — USD/RUB (ЦБ РФ): девальвация рубля → нефть в рублях дороже
    Эффект затухает экспоненциально за ~30 дней (рынок адаптируется).
    """
    base = predict_prices(history, future_days)

    brent = fetch_brent()
    forex = fetch_usd_rub()
    if not brent or not forex:
        return {**base, "factors": None}

    usd_rub      = forex["rate"]
    brent_now    = brent["current"]
    brent_avg30  = brent["avg30"]

    # Отклонение Brent в рублях от 30-дн. средней
    brent_rub_now  = brent_now   * usd_rub
    brent_rub_avg  = brent_avg30 * usd_rub
    deviation = (brent_rub_now - brent_rub_avg) / brent_rub_avg  # доля

    # Эластичность розничной цены к Brent для РФ ~0.30-0.40
    ELASTICITY = 0.35

    preds = list(base["predictions"])
    upper = list(base["upper"])
    lower = list(base["lower"])

    for i in range(future_days):
        decay = math.exp(-i / 30.0)
        adj   = deviation * ELASTICITY * decay
        preds[i] = round(preds[i] * (1 + adj), 2)
        upper[i] = round(upper[i] * (1 + adj * 0.6), 2)
        lower[i] = round(lower[i] * (1 + adj * 0.6), 2)

    excise = FUEL_EXCISE.get(fuel_id, {})
    return {
        "predictions": preds,
        "upper": upper,
        "lower": lower,
        "factors": {
            "brent_usd":      brent_now,
            "brent_avg30":    brent_avg30,
            "usd_rub":        usd_rub,
            "cbr_date":       forex["date"],
            "brent_rub":      round(brent_rub_now, 1),
            "deviation_pct":  round(deviation * 100, 1),
            "excise":         excise.get("excise", 0),
            "ndd":            excise.get("ndd", 0),
        },
    }


def calibrate_accuracy(history: list, future_days: int, n_tests: int = 8) -> list:
    """
    Walk-forward бэктест predict_prices на исторических данных.
    Возвращает hit-rate (вероятность попасть в ±5% от реального значения)
    для каждого горизонта от 1 до future_days.
    """
    prices       = np.array(history, dtype=float)
    n            = len(prices)
    test_horizon = min(future_days, max(1, int(n * 0.22)))
    min_train    = int(n * 0.60)
    available    = n - min_train - test_horizon

    if available < 1 or test_horizon < 1:
        decay = 2.5 / max(future_days - 1, 1)
        return [max(3, min(97, int(round(90 * np.exp(-decay * i))))) for i in range(future_days)]

    step   = max(1, available // n_tests)
    errors = [[] for _ in range(test_horizon)]

    for t in range(min_train, n - test_horizon, step):
        preds   = predict_prices(prices[:t].tolist(), test_horizon)['predictions']
        actuals = prices[t:t + test_horizon]
        for h, (pred, actual) in enumerate(zip(preds, actuals)):
            if float(actual) > 0:
                errors[h].append(abs(pred - float(actual)) / float(actual))

    threshold = 0.05
    tested = []
    for h in range(test_horizon):
        if errors[h]:
            tested.append(sum(1 for e in errors[h] if e <= threshold) / len(errors[h]) * 100)
        else:
            tested.append(85.0)

    if future_days <= test_horizon:
        raw = tested[:future_days]
    else:
        p0    = max(tested[0],  1.0)
        p_end = max(tested[-1], 1.0)
        decay = -np.log(p_end / p0) / max(test_horizon - 1, 1)
        raw   = list(tested)
        for i in range(test_horizon, future_days):
            raw.append(max(3.0, tested[-1] * np.exp(-decay * (i - test_horizon + 1))))

    return [max(3, min(97, int(round(p)))) for p in raw]


def calibrate_accuracy_intraday(base_usd: float, minutes: int, n_tests: int = 5) -> list:
    """
    Аналитическая оценка точности на основе GBM: вероятность попасть в ±1%
    от текущей цены за t минут. Даёт реалистичный спад: ~97% на 1 мин → ~35% на 8 ч.
    """
    daily_sigma = 0.038
    min_sigma = daily_sigma / math.sqrt(1440)
    threshold = 0.01  # ±1% диапазон

    result = []
    for t in range(1, minutes + 1):
        sigma_t = min_sigma * math.sqrt(t)
        z = math.log(1 + threshold) / sigma_t
        prob = math.erf(z / math.sqrt(2))
        result.append(max(3, min(97, int(round(prob * 100)))))
    return result


@app.route("/api/cities")
def get_cities():
    return jsonify({k: v["name"] for k, v in CITIES.items()})


@app.route("/api/predict-all")
def get_prediction_all():
    """Прогноз цены на выбранный период для ВСЕХ магазинов сразу."""
    product_id = request.args.get("product", "bread")
    period     = request.args.get("period", "week")
    city_id    = request.args.get("city", "moscow")

    future_days = PERIOD_DAYS.get(period, 7)
    if product_id not in PRODUCTS:
        return jsonify({"error": "Unknown product"}), 400

    city  = CITIES.get(city_id, CITIES["moscow"])
    today = datetime.now()
    indices = sample_indices(future_days)
    dates   = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in indices]

    # Используем накопленную историю (минимум 365 дней) — основа прогноза
    series = get_history_series(product_id, city_id, 365)
    histories = series["stores"]

    corr_results = predict_prices_correlated(histories, future_days)

    stores_data = {}
    for store_id, store_name in STORES.items():
        pred_result   = corr_results[store_id]
        history       = histories[store_id]
        sampled       = [pred_result["predictions"][i] for i in indices]
        current_price = history[-1]
        final_price   = pred_result["predictions"][-1]
        stores_data[store_id] = {
            "store":         store_name,
            "predictions":   sampled,
            "current_price": round(current_price, 2),
            "final_price":   round(final_price, 2),
            "change":        round(final_price - current_price, 2),
            "change_pct":    round((final_price - current_price) / current_price * 100, 1),
        }

    return jsonify({
        "dates":          dates,
        "stores":         stores_data,
        "period_label":   PERIOD_LABELS.get(period, period),
        "city":           city["name"],
        "unit":           "₽",
        "data_source":    series["source"],
    })


@app.route("/api/history-all")
def get_history_all():
    """История цен за последние N дней для ВСЕХ магазинов (накопленная)."""
    product_id = request.args.get("product", "bread")
    days       = int(request.args.get("days", 90))
    city_id    = request.args.get("city", "moscow")

    if product_id not in PRODUCTS:
        return jsonify({"error": "Unknown product"}), 400

    series = get_history_series(product_id, city_id, days)
    stores_data = {
        store_id: {"store": STORES[store_id], "prices": prices}
        for store_id, prices in series["stores"].items()
    }
    return jsonify({
        "dates":       series["dates"],
        "stores":      stores_data,
        "unit":        "₽",
        "data_source": series["source"],
    })


@app.route("/api/city-prices")
def get_city_prices():
    """Текущие цены в выбранном городе по всем магазинам — для панели параметров."""
    product_id = request.args.get("product", "bread")
    city_id    = request.args.get("city", "moscow")
    if product_id not in PRODUCTS:
        return jsonify({"error": "Unknown product"}), 400

    city = CITIES.get(city_id, CITIES["moscow"])
    with _real_prices_lock:
        has_real = bool(_real_prices.get(product_id, {}).get(city_id))
    stores = {
        store_id: {
            "store": store_name,
            "price": get_product_price(product_id, store_id, city_id),
        }
        for store_id, store_name in STORES.items()
    }
    avg = round(sum(s["price"] for s in stores.values()) / len(stores), 2)
    return jsonify({
        "product":     PRODUCTS[product_id]["name"],
        "city":        city["name"],
        "stores":      stores,
        "avg":         avg,
        "unit":        "₽",
        "data_source": "Росстат" if has_real else "расчётные",
        "updated_at":  _real_prices_updated_at or None,
    })


@app.route("/api/compare")
def compare_stores():
    """Сравнение прогноза цен по всем магазинам."""
    product_id = request.args.get("product", "bread")
    period = request.args.get("period", "month")

    PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365, "3years": 365*3, "5years": 365*5}
    future_days = PERIOD_DAYS.get(period, 30)

    if product_id not in PRODUCTS:
        return jsonify({"error": "Unknown product"}), 400

    result = {}
    for store_id, store_name in STORES.items():
        base = get_product_price(product_id, store_id)
        history = generate_history(base, 365)
        pred_result = predict_prices(history, future_days)
        final_pred = pred_result["predictions"][-1]
        result[store_id] = {
            "store": store_name,
            "current": round(history[-1], 2),
            "predicted": round(final_pred, 2),
            "change_pct": round(((final_pred - history[-1]) / history[-1]) * 100, 1),
        }

    return jsonify(result)


# ══════════════════════════════════════════════
#  ТОПЛИВО — эндпоинты
# ══════════════════════════════════════════════

@app.route("/api/fuel/types")
def get_fuel_types():
    return jsonify({k: v["name"] for k, v in FUELS.items()})


@app.route("/api/fuel/stations")
def get_fuel_stations():
    return jsonify(FUEL_STATIONS)


@app.route("/api/fuel/predict-all")
def fuel_predict_all():
    """Прогноз цены топлива для всех АЗС сразу."""
    fuel_id = request.args.get("fuel", "ai95")
    period  = request.args.get("period", "week")
    city_id = request.args.get("city", "moscow")

    future_days = PERIOD_DAYS.get(period, 7)
    if fuel_id not in FUELS:
        return jsonify({"error": "Unknown fuel"}), 400

    city    = CITIES.get(city_id, CITIES["moscow"])
    k       = city["fuel_k"]
    today   = datetime.now()
    indices = sample_indices(future_days)
    dates   = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in indices]

    fuel_histories = {}
    for station_id in FUEL_STATIONS:
        base = round(get_fuel_station_price(fuel_id, station_id) * k, 2)
        fuel_histories[station_id] = generate_fuel_history(base, 365)

    # Базовый прогноз с корреляцией между АЗС
    corr_results = predict_prices_correlated(fuel_histories, future_days)

    # Фактор-скорректированный прогноз по ref-истории (применяем коэффициент ко всем АЗС)
    ref_history   = list(fuel_histories.values())[0]
    factor_result = predict_fuel_with_factors(ref_history, future_days, fuel_id)
    factors       = factor_result.get("factors")

    # Коэффициент поправки Brent/forex относительно base predict_prices
    base_preds = predict_prices(ref_history, future_days)["predictions"]
    factor_ratio = [
        (factor_result["predictions"][i] / base_preds[i]) if base_preds[i] else 1.0
        for i in range(future_days)
    ]

    all_probs      = calibrate_accuracy(ref_history, future_days)
    # Если Brent сильно отклонился → снижаем уверенность прогноза
    if factors and abs(factors.get("deviation_pct", 0)) > 5:
        penalty = min(0.15, abs(factors["deviation_pct"]) / 100)
        all_probs = [max(3, int(p * (1 - penalty))) for p in all_probs]
    accuracy_probs = [all_probs[i] if i < len(all_probs) else 3 for i in indices]

    stations_data = {}
    for station_id, station_name in FUEL_STATIONS.items():
        pred_result   = corr_results[station_id]
        history       = fuel_histories[station_id]
        # Применяем фактор-поправку к прогнозу каждой АЗС
        adj_preds  = [round(pred_result["predictions"][i] * factor_ratio[i], 2)
                      for i in range(future_days)]
        sampled       = [adj_preds[i] for i in indices]
        current_price = history[-1]
        final_price   = adj_preds[-1]
        stations_data[station_id] = {
            "station":       station_name,
            "predictions":   sampled,
            "current_price": round(current_price, 2),
            "final_price":   round(final_price, 2),
            "change":        round(final_price - current_price, 2),
            "change_pct":    round((final_price - current_price) / current_price * 100, 1),
        }

    return jsonify({
        "dates":          dates,
        "stations":       stations_data,
        "period_label":   PERIOD_LABELS.get(period, period),
        "unit":           FUELS[fuel_id]["unit"],
        "city":           city["name"],
        "accuracy_probs": accuracy_probs,
        "factors":        factors,
        "data_source":    "MultiGO (реальное время) · Росстат (еженедельно)",
    })


@app.route("/api/fuel/factors")
def fuel_factors():
    """Текущие внешние факторы для топлива: Brent, USD/RUB, акцизы."""
    fuel_id = request.args.get("fuel", "ai95")
    brent   = fetch_brent()
    forex   = fetch_usd_rub()
    excise  = FUEL_EXCISE.get(fuel_id, {})
    return jsonify({
        "brent_usd":   brent.get("current"),
        "brent_avg30": brent.get("avg30"),
        "brent_avg90": brent.get("avg90"),
        "usd_rub":     forex.get("rate"),
        "cbr_date":    forex.get("date"),
        "brent_rub":   round(brent.get("current", 0) * forex.get("rate", USD_RUB), 1) if brent else None,
        "excise":      excise.get("excise"),
        "ndd":         excise.get("ndd"),
        "data_source": "Brent: Yahoo Finance · USD/RUB: ЦБ РФ · Акцизы: НК РФ 2026",
    })


@app.route("/api/fuel/history-all")
def fuel_history_all():
    """История цен на топливо для всех АЗС."""
    fuel_id = request.args.get("fuel", "ai95")
    days    = int(request.args.get("days", 90))
    city_id = request.args.get("city", "moscow")

    if fuel_id not in FUELS:
        return jsonify({"error": "Unknown fuel"}), 400

    city  = CITIES.get(city_id, CITIES["moscow"])
    k     = city["fuel_k"]
    today = datetime.now()
    dates = [(today - timedelta(days=days - i - 1)).strftime("%Y-%m-%d") for i in range(days)]

    stations_data = {}
    for station_id, station_name in FUEL_STATIONS.items():
        base = round(get_fuel_station_price(fuel_id, station_id) * k, 2)
        full_history = generate_fuel_history(base, 365)
        stations_data[station_id] = {
            "station": station_name,
            "prices":  full_history[-days:],
        }

    return jsonify({"dates": dates, "stations": stations_data})


# ══════════════════════════════════════════════
#  КРИПТО — данные и эндпоинты
# ══════════════════════════════════════════════

# Курс USD/RUB (ЦБ РФ, апрель 2026)
USD_RUB = 76.16

# ── CoinGecko ──────────────────────────────────────────────
COINGECKO_IDS = {
    "btc":"bitcoin","eth":"ethereum","bnb":"binancecoin",
    "sol":"solana","xrp":"ripple","ton":"the-open-network",
    "doge":"dogecoin","avax":"avalanche-2","ltc":"litecoin",
    "link":"chainlink","dot":"polkadot","trx":"tron","ada":"cardano",
}
_price_cache: dict = {}
_cache_ts: float = 0.0
_CACHE_TTL = 3    # обновление каждые 3 сек

def fetch_live_prices() -> dict:
    global _price_cache, _cache_ts
    now = time.time()
    if _price_cache and (now - _cache_ts) < _CACHE_TTL:
        return _price_cache
    try:
        ids = ",".join(COINGECKO_IDS.values())
        resp = http_requests.get(
            f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd",
            timeout=8)
        resp.raise_for_status()
        data = resp.json()
        result = {k: float(data[v]["usd"]) for k, v in COINGECKO_IDS.items() if v in data}
        if result:
            _price_cache = result
            _cache_ts = now
    except Exception:
        pass
    return _price_cache

def get_crypto_price(coin_id: str) -> float:
    return fetch_live_prices().get(coin_id, CRYPTOS[coin_id]["price_usd"])

# ── Эндпоинт живой цены ────────────────────────────────────
# Биржи — небольшой спред между ценами (реальная разница 0.05–0.3%)
CRYPTO_EXCHANGES = {
    "bybit":   "Bybit",
    "okx":     "OKX",
    "mexc":    "MEXC",
    "gateio":  "Gate.io",
    "bingx":   "BingX",
}
# Множители цены относительно базового spot-курса
EXCHANGE_SPREADS = {
    "bybit":  1.0000,
    "okx":    0.9992,
    "mexc":   1.0012,
    "gateio": 1.0005,
    "bingx":  0.9996,
}

# Цены в USD на 14 апреля 2026 (CoinMarketCap)
CRYPTOS = {
    "btc":  {"name": "Bitcoin (BTC)",      "symbol": "BTC",  "price_usd": 74372.0},
    "eth":  {"name": "Ethereum (ETH)",     "symbol": "ETH",  "price_usd": 2373.0},
    "bnb":  {"name": "BNB",                "symbol": "BNB",  "price_usd": 616.11},
    "sol":  {"name": "Solana (SOL)",        "symbol": "SOL",  "price_usd": 85.83},
    "xrp":  {"name": "XRP (Ripple)",        "symbol": "XRP",  "price_usd": 1.36},
    "ton":  {"name": "Toncoin (TON)",        "symbol": "TON",  "price_usd": 1.43},
    "doge": {"name": "Dogecoin (DOGE)",     "symbol": "DOGE", "price_usd": 0.09453},
    "avax": {"name": "Avalanche (AVAX)",    "symbol": "AVAX", "price_usd": 9.39},
    "ltc":  {"name": "Litecoin (LTC)",      "symbol": "LTC",  "price_usd": 54.52},
    "link": {"name": "Chainlink (LINK)",    "symbol": "LINK", "price_usd": 9.18},
    "dot":  {"name": "Polkadot (DOT)",      "symbol": "DOT",  "price_usd": 1.18},
    "trx":  {"name": "TRON (TRX)",          "symbol": "TRX",  "price_usd": 0.3211},
    "ada":  {"name": "Cardano (ADA)",       "symbol": "ADA",  "price_usd": 0.2433},
}


def generate_crypto_history(base_usd: float, days: int = 365) -> list:
    """
    История цен криптовалюты в USD.
    Модель: случайное блуждание с дрейфом + высокая волатильность + редкие памп/дамп.
    """
    rng = random.Random(int(base_usd * 137 + days))
    prices = []
    # Стартуем ~год назад: крипто за год выросла в среднем на 30-80%
    annual_growth = rng.uniform(0.25, 0.75)
    price = base_usd / (1 + annual_growth * 0.8)

    for i in range(days):
        # Базовый тренд (пологий)
        trend = price * (annual_growth / 365) * 0.6
        # Высокая дневная волатильность (2–5%)
        volatility = rng.gauss(0, price * rng.uniform(0.018, 0.042))
        # Редкие памп (+15..+50%) или дамп (-20..-40%)
        spike = 0.0
        if rng.random() < 0.008:
            spike = price * rng.uniform(0.12, 0.45)
        elif rng.random() < 0.010:
            spike = -price * rng.uniform(0.15, 0.38)

        price = max(price + trend + volatility + spike, base_usd * 0.10)
        prices.append(round(price, 6 if base_usd < 0.01 else (4 if base_usd < 1 else 2)))

    return _anchor(prices, base_usd)


def predict_crypto(history: list, future_days: int) -> dict:
    """
    Прогноз крипты: геометрическое броуновское движение (GBM, log-normal).
    Параметры μ (дрейф) и σ (волатильность) оцениваются из реальных
    лог-доходностей истории, а не задаются вручную.

    Возвращает dict: {"predictions": [...], "upper": [...], "lower": [...]}.
    """
    prices = np.array(history, dtype=float)
    n = len(prices)
    dec = 6 if float(prices[-1]) < 0.01 else (4 if float(prices[-1]) < 1 else 2)

    # Оцениваем параметры GBM из лог-доходностей
    log_returns = np.diff(np.log(np.clip(prices, 1e-10, None)))
    mu    = float(np.clip(np.mean(log_returns), -0.008, 0.012))
    sigma = float(np.clip(np.std(log_returns),  0.010, 0.120))

    rng = np.random.default_rng(int(prices[-1] * 7 + n) % (2**31))
    current = float(prices[-1])
    predictions = []

    for _ in range(future_days):
        # GBM: S(t+1) = S(t) * exp((μ − σ²/2) + σ·Z)
        drift = mu - 0.5 * sigma ** 2
        noise = rng.normal(0, sigma)
        current = current * float(np.exp(drift + noise))
        current = max(current, float(prices[-1]) * 0.03)
        predictions.append(round(current, dec))

    # Доверительный интервал по аналитической формуле GBM (95%)
    S0 = float(prices[-1])
    upper = []
    lower = []
    for i, p in enumerate(predictions):
        t = float(i + 1)
        half = S0 * float(np.exp(mu * t)) * (float(np.exp(1.96 * sigma * np.sqrt(t))) - 1)
        half = min(half, p * 2.0)
        upper.append(round(p + half * 0.5, dec))
        lower.append(round(max(p - half * 0.4, S0 * 0.02), dec))

    return {"predictions": predictions, "upper": upper, "lower": lower}


def _crypto_dates_indices(future_days: int):
    if future_days > 365:
        step = future_days // 100
    elif future_days > 30:
        step = max(1, future_days // 60)
    else:
        return list(range(future_days))
    return list(range(0, future_days, step))


@app.route("/api/live-price")
def live_price():
    category = request.args.get("category", "food")
    if category == "crypto":
        coin_id  = request.args.get("coin", "btc")
        currency = request.args.get("currency", "usd")
        if coin_id not in CRYPTOS:
            return jsonify({"error": "Unknown coin"}), 400
        price = get_crypto_price(coin_id)
        if currency == "rub":
            price = round(price * USD_RUB, 2)
        unit = "₽" if currency == "rub" else "$"
        source = "CoinGecko" if fetch_live_prices() else "расчётные"
        return jsonify({"price": round(price, 2), "unit": unit, "source": source})
    elif category == "stocks":
        ticker = request.args.get("ticker", "SBER").upper()
        if ticker not in STOCKS:
            return jsonify({"error": "Unknown ticker"}), 400
        price = fetch_stock_live(ticker)
        if price is None:
            return jsonify({"error": "MOEX unavailable"}), 503
        return jsonify({"price": round(price, 2), "unit": "₽", "source": "MOEX"})
    elif category == "fuel":
        fuel_id = request.args.get("fuel", "ai95")
        city_id = request.args.get("city", "moscow")
        if fuel_id not in FUELS:
            return jsonify({"error": "Unknown fuel"}), 400
        k = CITIES.get(city_id, CITIES["moscow"])["fuel_k"]
        prices = [get_fuel_station_price(fuel_id, s) * k for s in FUEL_STATIONS]
        live = fetch_multigo_prices()
        source = "MultiGO" if fuel_id in live else "расчётные"
        return jsonify({"price": round(sum(prices)/len(prices), 2),
                        "unit": "₽/л", "source": source})
    else:
        product_id = request.args.get("product", "bread")
        city_id    = request.args.get("city", "moscow")
        if product_id not in PRODUCTS:
            return jsonify({"error": "Unknown product"}), 400
        with _real_prices_lock:
            has_real = bool(_real_prices.get(product_id, {}).get(city_id))
        prices = [get_product_price(product_id, s, city_id) * random.uniform(0.997, 1.003)
                  for s in STORES]
        source = "Росстат" if has_real else "расчётные"
        return jsonify({"price": round(sum(prices)/len(prices), 2),
                        "unit": "₽", "source": source})


@app.route("/api/crypto/list")
def crypto_list():
    return jsonify({k: v["name"] for k, v in CRYPTOS.items()})


@app.route("/api/crypto/exchanges")
def crypto_exchanges():
    return jsonify(CRYPTO_EXCHANGES)


def _dec(base_usd):
    """Число знаков после запятой в зависимости от цены."""
    if base_usd < 0.0001: return 8
    if base_usd < 0.01:   return 6
    if base_usd < 1:      return 4
    return 2


@app.route("/api/crypto/predict-all")
def crypto_predict_all():
    coin_id  = request.args.get("coin", "btc")
    period   = request.args.get("period", "1hour")
    currency = request.args.get("currency", "usd")

    if coin_id not in CRYPTOS:
        return jsonify({"error": "Unknown coin"}), 400

    base_usd = get_crypto_price(coin_id)
    mult     = USD_RUB if currency == "rub" else 1.0
    unit     = "₽" if currency == "rub" else "$"
    today    = datetime.now()

    # Разбираем кастомный период "custom_Xmin" / "custom_Xhour"
    if period.startswith("custom_"):
        raw = period[7:]
        if raw.endswith("hour"):
            minutes = int(raw[:-4]) * 60
        else:
            minutes = int(raw[:-3])
        period_label = f"на {minutes} мин" if minutes < 60 else f"на {minutes//60} ч"
    elif period in CRYPTO_PERIOD_MINUTES:
        minutes      = CRYPTO_PERIOD_MINUTES[period]
        period_label = CRYPTO_PERIOD_LABELS[period]
    else:
        # Фолбэк на дневной прогноз если пришёл старый период
        minutes      = 60
        period_label = "на 1 час"

    cur_price = round(base_usd * mult, _dec(base_usd))

    # Прогноз (минутная модель GBM)
    result     = predict_crypto_intraday(base_usd, minutes)
    preds_raw  = result["predictions"]
    upper_raw  = result["upper"]
    lower_raw  = result["lower"]

    # Метки времени по минутам
    step = max(1, minutes // 60) if minutes > 60 else 1
    indices = list(range(0, minutes, step))
    if (minutes - 1) not in indices:
        indices.append(minutes - 1)

    all_probs      = calibrate_accuracy_intraday(base_usd, minutes)
    accuracy_probs = [all_probs[i] if i < len(all_probs) else 3 for i in indices]

    dates = [(today + timedelta(minutes=i)).strftime("%H:%M") for i in indices]

    preds_s = [round(preds_raw[i] * mult, 2) for i in indices]
    upper_s = [round(upper_raw[i] * mult, 2) for i in indices]
    lower_s = [round(lower_raw[i] * mult, 2) for i in indices]

    final_price = round(preds_raw[-1] * mult, 2)

    exchanges_data = {}
    for ex_id, ex_name in CRYPTO_EXCHANGES.items():
        sp     = EXCHANGE_SPREADS[ex_id]
        p_s    = [round(preds_raw[i] * sp * mult, 2) for i in indices]
        cur_ex = round(base_usd * sp * mult, 2)
        fin_ex = p_s[-1] if p_s else cur_ex
        exchanges_data[ex_id] = {
            "exchange":      ex_name,
            "predictions":   p_s,
            "current_price": cur_ex,
            "final_price":   fin_ex,
            "change":        round(fin_ex - cur_ex, 2),
            "change_pct":    round((fin_ex - cur_ex) / cur_ex * 100, 2) if cur_ex else 0,
        }

    return jsonify({
        "dates":          dates,
        "exchanges":      exchanges_data,
        "chart":          preds_s,
        "upper_bound":    upper_s,
        "lower_bound":    lower_s,
        "period_label":   period_label,
        "unit":           unit,
        "coin":           CRYPTOS[coin_id]["name"],
        "symbol":         CRYPTOS[coin_id]["symbol"],
        "current_price":  cur_price,
        "final_price":    final_price,
        "change":         round(final_price - cur_price, 2),
        "change_pct":     round((final_price - cur_price) / cur_price * 100, 2) if cur_price else 0,
        "accuracy_probs": accuracy_probs,
    })


@app.route("/api/crypto/history-all")
def crypto_history_all():
    coin_id  = request.args.get("coin", "btc")
    days     = int(request.args.get("days", 90))
    currency = request.args.get("currency", "usd")

    if coin_id not in CRYPTOS:
        return jsonify({"error": "Unknown coin"}), 400

    base_usd     = get_crypto_price(coin_id)
    mult         = USD_RUB if currency == "rub" else 1.0
    today        = datetime.now()
    dates        = [(today - timedelta(days=days - i - 1)).strftime("%Y-%m-%d") for i in range(days)]
    base_history = generate_crypto_history(base_usd, 365)

    exchanges_data = {}
    for ex_id, ex_name in CRYPTO_EXCHANGES.items():
        sp = EXCHANGE_SPREADS[ex_id]
        prices_disp = [round(v * sp * mult, 2) for v in base_history[-days:]]
        exchanges_data[ex_id] = {"exchange": ex_name, "prices": prices_disp}

    unit = "₽" if currency == "rub" else "$"
    # Единая линия для графика истории
    chart_prices = [round(v * mult, 2) for v in base_history[-days:]]
    return jsonify({"dates": dates, "exchanges": exchanges_data, "chart": chart_prices, "unit": unit})


def generate_crypto_history_intraday(base_usd: float, minutes: int) -> list:
    """История цен криптовалюты с минутным разрешением (идёт назад на minutes минут)."""
    daily_sigma = 0.038
    min_sigma = daily_sigma / (1440 ** 0.5)
    dec = _dec(base_usd)

    rng = random.Random(int(base_usd * 137 + minutes))
    prices = []
    price = base_usd * rng.uniform(0.97, 1.03)

    for _ in range(minutes):
        price = max(price * (1 + rng.gauss(0, min_sigma)), base_usd * 0.5)
        prices.append(round(price, dec))

    return _anchor(prices, base_usd)


@app.route("/api/crypto/history-intraday")
def crypto_history_intraday():
    coin_id  = request.args.get("coin", "btc")
    minutes  = int(request.args.get("minutes", 60))
    currency = request.args.get("currency", "usd")

    if coin_id not in CRYPTOS:
        return jsonify({"error": "Unknown coin"}), 400

    minutes  = max(1, min(minutes, 10_000))
    base_usd = get_crypto_price(coin_id)
    mult     = USD_RUB if currency == "rub" else 1.0
    unit     = "₽" if currency == "rub" else "$"

    history = generate_crypto_history_intraday(base_usd, minutes)

    now   = datetime.now()
    times = [(now - timedelta(minutes=minutes - i - 1)).strftime("%H:%M") for i in range(minutes)]
    chart = [round(v * mult, 2) for v in history]

    # Прореживаем для больших периодов
    if minutes > 120:
        step    = max(1, minutes // 100)
        indices = list(range(0, minutes, step))
        if (minutes - 1) not in indices:
            indices.append(minutes - 1)
        chart = [chart[i] for i in indices]
        times = [times[i] for i in indices]

    return jsonify({"times": times, "chart": chart, "unit": unit})


# ══════════════════════════════════════════════
#  НОВОСТНАЯ ЛЕНТА (RSS-агрегатор)
# ══════════════════════════════════════════════

NEWS_FEEDS = [
    ("Лента.ру",     "https://lenta.ru/rss/news/economics"),
    ("РБК",          "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"),
    ("Коммерсантъ",  "https://www.kommersant.ru/RSS/news.xml"),
    ("ТАСС",         "https://tass.ru/rss/v2.xml"),
    ("Интерфакс",    "https://www.interfax.ru/rss.asp"),
]

NEWS_CATEGORIES = {
    "food":   ["цены ", "цен на ", "продукт", "инфляц", "продоволь", "подорож",
               "продовольств", "ритейл", "ритейлер", "пятёрочк", "магнит",
               "вкусвилл", "перекрёст", "лента ", "росстат",
               "хлеб", "молок", "мясо", "яиц", "сахар", "масл"],
    "fuel":   ["бензин", "дизел", "топлив", "азс", "нефт", "brent", "опек",
               "опек+", "лукойл", "роснефт", "газпром нефт", "татнефт",
               "башнефт", "нпз", "акциз"],
    "stocks": ["акции", "биржа", "мосбирж", "moex", "сбер", "газпром",
               "яндекс", "норникель", "втб", "ммк", "новатэк", "полюс",
               "озон", "т-банк", "tcs ", "мтс", "котировк", "дивиденд",
               "ipo", "трейд", "брокер", "индекс мосбиржи"],
}

_news_cache: list = []
_news_cache_ts: float = 0.0
_NEWS_TTL = 300  # 5 минут


def _classify_news(title: str, summary: str = '') -> list:
    """Возвращает список категорий, под которые подходит новость."""
    text = (title + ' ' + summary).lower()
    cats = []
    for cat, keywords in NEWS_CATEGORIES.items():
        if any(kw.lower() in text for kw in keywords):
            cats.append(cat)
    return cats


def fetch_news() -> list:
    """Агрегирует RSS-ленты, оставляет релевантные новости. Кэш 5 мин."""
    global _news_cache, _news_cache_ts
    now = time.time()
    if _news_cache and (now - _news_cache_ts) < _NEWS_TTL:
        return _news_cache

    from xml.etree import ElementTree as ET
    items = []
    for source_name, url in NEWS_FEEDS:
        try:
            r = http_requests.get(url, timeout=8,
                                  headers={"User-Agent": "Mozilla/5.0"}, verify=HTTP_VERIFY_TLS)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            channel = root.find('channel') or root
            for it in channel.findall('item')[:25]:
                title = (it.findtext('title') or '').strip()
                link  = (it.findtext('link')  or '').strip()
                pub   = (it.findtext('pubDate') or '').strip()
                desc  = (it.findtext('description') or '').strip()[:300]
                if not title or not link:
                    continue
                cats = _classify_news(title, desc)
                if not cats:
                    continue
                # Парсим дату из RFC 822
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub)
                    ts = dt.timestamp()
                    time_str = dt.strftime('%H:%M')
                except Exception:
                    ts = now
                    time_str = ''
                items.append({
                    "title":  title,
                    "link":   link,
                    "source": source_name,
                    "time":   time_str,
                    "ts":     ts,
                    "tags":   cats,
                })
        except Exception as e:
            print(f"[news] {source_name} error: {e}")

    items.sort(key=lambda x: x["ts"], reverse=True)
    items = items[:40]
    _news_cache = items
    _news_cache_ts = now
    return items


@app.route("/api/news")
def get_news():
    return jsonify({"items": fetch_news()})


# ══════════════════════════════════════════════
#  АКЦИИ РФ (MOEX ISS API)
# ══════════════════════════════════════════════

# Топ-15 ликвидных акций Мосбиржи. Тикер -> название.
STOCKS = {
    "SBER":  "Сбербанк",
    "T":     "Т-Банк",
    "YDEX":  "Яндекс",
    "GAZP":  "Газпром",
    "LKOH":  "Лукойл",
    "ROSN":  "Роснефть",
    "GMKN":  "Норникель",
    "VTBR":  "ВТБ",
    "MGNT":  "Магнит",
    "MTSS":  "МТС",
    "PLZL":  "Полюс",
    "NVTK":  "Новатэк",
    "SIBN":  "Газпром нефть",
    "MOEX":  "Мосбиржа",
    "OZON":  "Озон",
}

_stock_history_cache: dict = {}   # {ticker: (ts, [(date,close),...])}
_stock_live_cache: dict    = {}   # {ticker: (ts, price)}
_STOCK_HIST_TTL = 3600
_STOCK_LIVE_TTL = 30


def fetch_stock_history(ticker: str, days: int = 365) -> list:
    """История дневных закрытий с MOEX. Возвращает [(date_str, close), ...] длиной до days."""
    now = time.time()
    cached = _stock_history_cache.get(ticker)
    if cached and (now - cached[0]) < _STOCK_HIST_TTL:
        return cached[1]
    try:
        # Берём с запасом: ~1.5x, чтобы дотянуть выходные
        from_dt = (datetime.now() - timedelta(days=int(days * 1.6) + 5)).strftime('%Y-%m-%d')
        till_dt = datetime.now().strftime('%Y-%m-%d')
        url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/securities/"
               f"{ticker}/candles.json?interval=24&from={from_dt}&till={till_dt}")
        r = http_requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        d = r.json()
        cols = d['candles']['columns']
        rows = d['candles']['data']
        ci, ei = cols.index('close'), cols.index('end')
        series = [(row[ei][:10], float(row[ci])) for row in rows if row[ci] is not None]
        _stock_history_cache[ticker] = (now, series)
        return series
    except Exception as e:
        print(f"[stocks] history error for {ticker}: {e}")
        return cached[1] if cached else []


def fetch_stock_live(ticker: str) -> float | None:
    """Текущая цена с MOEX (LAST). Кэш 30с."""
    now = time.time()
    cached = _stock_live_cache.get(ticker)
    if cached and (now - cached[0]) < _STOCK_LIVE_TTL:
        return cached[1]
    try:
        url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
               f"securities/{ticker}.json?iss.meta=off&iss.only=marketdata"
               f"&marketdata.columns=LAST")
        r = http_requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        rows = r.json().get('marketdata', {}).get('data', [])
        if rows and rows[0] and rows[0][0] is not None:
            price = float(rows[0][0])
            _stock_live_cache[ticker] = (now, price)
            return price
    except Exception as e:
        print(f"[stocks] live error for {ticker}: {e}")
    # Fallback: последняя свеча
    hist = fetch_stock_history(ticker, 30)
    return hist[-1][1] if hist else None


@app.route("/api/stocks/list")
def stocks_list():
    return jsonify(STOCKS)


@app.route("/api/stocks/history-all")
def stocks_history_all():
    ticker = request.args.get("ticker", "SBER").upper()
    days   = int(request.args.get("days", 90))
    if ticker not in STOCKS:
        return jsonify({"error": "Unknown ticker"}), 400

    hist = fetch_stock_history(ticker, days)
    if not hist:
        return jsonify({"error": "No data"}), 503

    hist = hist[-days:]
    dates  = [d for d, _ in hist]
    prices = [round(p, 2) for _, p in hist]
    return jsonify({
        "dates":       dates,
        "chart":       prices,
        "unit":        "₽",
        "ticker":      ticker,
        "name":        STOCKS[ticker],
        "data_source": "MOEX",
    })


@app.route("/api/stocks/predict-all")
def stocks_predict_all():
    ticker = request.args.get("ticker", "SBER").upper()
    period = request.args.get("period", "week")
    if ticker not in STOCKS:
        return jsonify({"error": "Unknown ticker"}), 400

    future_days = PERIOD_DAYS.get(period, 7)
    hist = fetch_stock_history(ticker, 365)
    if not hist or len(hist) < 30:
        return jsonify({"error": "Not enough history"}), 503

    closes = [p for _, p in hist]
    pred   = predict_prices(closes, future_days)
    preds  = pred["predictions"]
    indices = sample_indices(future_days)
    today   = datetime.now()
    dates_pred = [(today + timedelta(days=i + 1)).strftime("%Y-%m-%d") for i in indices]
    sampled    = [round(preds[i], 2) for i in indices]

    cur = round(closes[-1], 2)
    fin = round(preds[-1], 2)
    return jsonify({
        "ticker":         ticker,
        "name":           STOCKS[ticker],
        "current_price":  cur,
        "final_price":    fin,
        "change":         round(fin - cur, 2),
        "change_pct":     round((fin - cur) / cur * 100, 2),
        "dates":          dates_pred,
        "predictions":    sampled,
        "chart":          sampled,
        "period_label":   PERIOD_LABELS.get(period, period),
        "unit":           "₽",
        "data_source":    "MOEX",
    })


# ══════════════════════════════════════════════
#  УПРАВЛЕНИЕ ЦЕНАМИ — эндпоинты
# ══════════════════════════════════════════════

@app.route('/api/prices/status')
def prices_status():
    """Публичный статус: откуда берутся цены и когда обновлялись."""
    with _real_prices_lock:
        covered = list(_real_prices.keys())
        total = len(PRODUCTS)
    return jsonify({
        'real_prices_count': len(covered),
        'total_products': total,
        'coverage_pct': round(len(covered) / total * 100),
        'updated_at': _real_prices_updated_at or None,
        'covered_products': covered,
        'missing_products': [k for k in PRODUCTS if k not in covered],
    })


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port  = int(os.environ.get("PORT", 5000))
    if debug:
        app.run(debug=True, host="127.0.0.1", port=port)
    else:
        # Production: используем waitress (Windows-совместимо) или gunicorn (Linux/PaaS)
        try:
            from waitress import serve
            serve(app, host="0.0.0.0", port=port, threads=8)
        except ImportError:
            print("[warn] waitress not installed; falling back to dev server")
            app.run(debug=False, host="0.0.0.0", port=port)
