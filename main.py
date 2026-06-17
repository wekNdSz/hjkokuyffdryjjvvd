import os
import time
import json
import re
import random
import threading
import html
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request
from groq import Groq
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# ==================== НАСТРОЙКИ ====================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
MODEL_NAME = "llama-3.3-70b-versatile"

WEBHOOK_SET = False

# Глобальный кэш и история публикаций
STEAM_DATA_CACHE = {
    "EUR": None,
    "USD": None,
    "RUB": None,
    "last_updated": 0
}

PUBLISHED_GAMES_HISTORY = {}  # {app_id: timestamp}

REGIONS = {
    "EUR": {"country": "DE", "symbol": "€", "lang": "en-US,en;q=0.9"},
    "USD": {"country": "US", "symbol": "$", "lang": "en-US,en;q=0.9"},
    "RUB": {"country": "RU", "symbol": "₽", "lang": "ru-RU,ru;q=0.9"}
}

CATEGORIES_MAP = {
    "discounts": "Скидки дня",
    "free_single": "Одиночные бесплатные игры",
    "coop_disc": "Кооперативные скидки",
    "coop_free": "Мультиплеерная халява",
    "upcoming": "Скоро станут бесплатными",
    "action_games": "🔥 Топ Экшены / Боевики",
    "strategy_games": "🧠 Тактика и Стратегии",
    "rpg_games": "🔮 Ролевые миры (RPG)",
    "simulator_games": "🚜 Симуляторы реальности",
    "adventure_games": "🧭 Приключения и Квесты"
}

BANNED_GAMES = ["pubg", "counter-strike", "dota", "apex legends", "warframe", "war thunder", "destiny 2", "rainbow six"]
FONT_PATH = "PressStart2P.ttf"

# ==================== УТИЛИТЫ ====================
def is_ignored(name):
    return any(banned in name.lower() for banned in BANNED_GAMES)

def download_pixel_font():
    if not os.path.exists(FONT_PATH):
        print("[Система] Скачивание шрифта...")
        try:
            r = requests.get("https://github.com/google/fonts/raw/main/ofl/pressstart2p/PressStart2P-Regular.ttf", timeout=15)
            with open(FONT_PATH, "wb") as f:
                f.write(r.content)
        except Exception as e:
            print(f"[Система] Ошибка загрузки шрифта: {e}")

def clean_price(price_str, currency_code):
    if not price_str:
        return 0.0
    txt = price_str.lower()
    if "free" in txt or "бесплатно" in txt or "испробовать" in txt:
        return 0.0
    
    p_text = price_str.replace("pуб.", "").replace("руб.", "").replace("₽", "").replace("€", "").replace("$", "")
    p_text = p_text.replace("&nbsp;", "").strip()
    
    if currency_code == "RUB":
        p_text = p_text.replace(" ", "").replace(",", ".")
        if p_text.count('.') > 1:
            parts = p_text.split('.')
            p_text = "".join(parts[:-1]) + "." + parts[-1]
    else:
        p_text = p_text.replace(" ", "").replace(",", ".")

    p_text = "".join(c for c in p_text if c.isdigit() or c == ".")
    try:
        return float(p_text) if p_text else 0.0
    except ValueError:
        return 0.0

def extract_game_id(row):
    if row.has_attr('data-ds-appid'):
        return row['data-ds-appid']
    href = row.get('href', '')
    match = re.search(r'/app/(\d+)', href)
    return match.group(1) if match else ""

def get_prices_for_all_regions(app_id):
    results = {}
    for code, cfg in REGIONS.items():
        cookies = {"wants_mature_content": "1", "birthtime": "288028801", "last_steam_country": cfg["country"]}
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": cfg["lang"]}
        time.sleep(0.3)
        try:
            r = requests.get(f"https://store.steampowered.com/app/{app_id}/", headers=headers, cookies=cookies, timeout=7)
            if r.status_code == 200:
                s = BeautifulSoup(r.text, "html.parser")
                p_div = s.find("div", class_="discount_final_price") or s.find("div", class_="game_purchase_price")
                d_div = s.find("div", class_="discount_pct")
                
                disc = d_div.text.strip() if d_div else "0%"
                if p_div:
                    txt = p_div.text.strip()
                    if "free" in txt.lower() or "бесплатно" in txt.lower():
                        results[code] = {"price": "FREE", "discount": "100%"}
                    else:
                        cleaned = clean_price(txt, code)
                        results[code] = {"price": f"{cleaned:.2f} {cfg['symbol']}", "discount": disc}
                else:
                    results[code] = {"price": "N/A", "discount": "0%"}
            else:
                results[code] = {"price": "N/A", "discount": "0%"}
        except:
            results[code] = {"price": "N/A", "discount": "0%"}
    return results

def parse_steam_category(url_params, global_currency, limit=25):
    cfg = REGIONS.get(global_currency, REGIONS["EUR"])
    cookies = {"wants_mature_content": "1", "birthtime": "288028801", "last_steam_country": cfg["country"]}
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": cfg["lang"]}
    games = []
    
    try:
        res = requests.get(f"https://store.steampowered.com/search/?{url_params}&ndl=1", headers=headers, cookies=cookies, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for row in soup.find_all("a", class_="search_result_row"):
                name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else "Unknown"
                if is_ignored(name): continue
                app_id = extract_game_id(row)
                img_tag = row.find("div", class_="search_capsule").find("img")
                img_url = img_tag["src"] if img_tag and img_tag.has_attr("src") else ""

                disc_div = row.find("div", class_="discount_pct")
                price_div = row.find("div", class_="discount_final_price") or row.find("div", class_="search_price")
                
                discount = disc_div.text.strip() if disc_div else "0%"
                if price_div:
                    p_txt = price_div.text.strip()
                    if "free" in p_txt.lower() or "бесплатно" in p_txt.lower():
                        price, discount = "FREE", "100%"
                    else:
                        if "\n" in p_txt: p_txt = p_txt.split("\n")[-1].strip()
                        price = f"{clean_price(p_txt, global_currency):.2f} {cfg['symbol']}"
                else:
                    price = "N/A"

                games.append({"id": app_id, "name": name, "discount": discount, "price": price, "img": img_url})
                if len(games) >= limit: break
    except: pass
    return games

def get_steam_data(global_currency="EUR"):
    return {
        "discounts": parse_steam_category("specials=1", global_currency, 25),
        "free_single": parse_steam_category("maxprice=free", global_currency, 25),
        "coop_disc": parse_steam_category("category2=38,9&specials=1", global_currency, 25),
        "coop_free": parse_steam_category("maxprice=free&category2=38,9", global_currency, 25),
        "upcoming": parse_steam_category("maxprice=free&filter=comingsoon", global_currency, 25),
        "action_games": parse_steam_category("tags=19&specials=1", global_currency, 25),
        "strategy_games": parse_steam_category("tags=9&specials=1", global_currency, 25),
        "rpg_games": parse_steam_category("tags=122&specials=1", global_currency, 25),
        "simulator_games": parse_steam_category("tags=597&specials=1", global_currency, 25),
        "adventure_games": parse_steam_category("tags=21&specials=1", global_currency, 25)
    }

def steam_cache_worker():
    while True:
        print("[Кэш] Обновление 10 категорий Steam...")
        for currency in REGIONS:
            try:
                STEAM_DATA_CACHE[currency] = get_steam_data(currency)
                time.sleep(2)
            except Exception as e:
                print(f"[Кэш] Ошибка: {e}")
        STEAM_DATA_CACHE["last_updated"] = time.time()
        time.sleep(1800)

def generate_game_card_image(game_name, category, prices, img_url, is_ultra_hot=False):
    img = Image.new("RGB", (1000, 600), "#dfd4c9")
    draw = ImageDraw.Draw(img)
    for x in range(0, 1000, 40): draw.line([(x, 0), (x, 600)], fill="#ebdcd0", width=2)
    for y in range(0, 600, 40): draw.line([(0, y), (1000, y)], fill="#ebdcd0", width=2)

    try:
        font_main = ImageFont.truetype(FONT_PATH, 16)
        font_title = ImageFont.truetype(FONT_PATH, 20)
        font_sub = ImageFont.truetype(FONT_PATH, 12)
    except:
        font_main = font_title = font_sub = ImageFont.load_default()

    draw.rectangle([40, 40, 960, 560], fill="#fffbf7", outline="#000000", width=5)
    header_bg = "#ff5722" if is_ultra_hot else "#ffffff"
    header_txt_color = "#ffffff" if is_ultra_hot else "#000000"
    header_text = "🔥 HOT TEMPORARY FREEBIE! 🔥" if is_ultra_hot else "STEAM HIDDEN GEMS RADAR"
    
    draw.rectangle([40, 40, 960, 100], fill=header_bg, outline="#000000", width=5)
    draw.text((65, 58), header_text, fill=header_txt_color, font=font_title)

    try:
        resp = requests.get(img_url, timeout=5)
        game_thumb = Image.open(BytesIO(resp.content)).convert("RGB").resize((360, 200), Image.Resampling.NEAREST)
        img.paste(game_thumb, (70, 140))
        draw.rectangle([67, 137, 433, 343], outline="#000000", width=4)
    except:
        draw.rectangle([70, 140, 430, 340], fill="#7a7a7a", outline="#000000", width=4)

    draw.text((470, 140), "GAME:", fill="#7a7a7a", font=font_sub)
    if len(game_name) > 24:
        draw.text((470, 165), game_name[:24], fill="#000000", font=font_title)
        draw.text((470, 195), game_name[24:48], fill="#000000", font=font_title)
        y_meta_start = 245
    else:
        draw.text((470, 165), game_name, fill="#000000", font=font_title)
        y_meta_start = 215

    draw.text((470, y_meta_start), "CATEGORY:", fill="#7a7a7a", font=font_sub)
    cat_text = f"#{category.upper().replace(' ', '_')}"
    try: text_w = font_main.getbbox(cat_text)[2]
    except: text_w = len(cat_text) * 16
        
    draw.rectangle([470, y_meta_start + 20, 490 + text_w, y_meta_start + 55], fill="#4caf50", outline="#000000", width=3)
    draw.text((480, y_meta_start + 28), cat_text, fill="#ffffff", font=font_main)
    draw.text((70, 375), "REGIONAL PRICES & DISCOUNTS:", fill="#7a7a7a", font=font_sub)
    
    y_offset = 410
    for reg, p_info in prices.items():
        draw.rectangle([70, y_offset, 930, y_offset + 40], fill="#ffffff", outline="#000000", width=2)
        draw.text((90, y_offset + 12), f"[{reg}]", fill="#7a7a7a", font=font_main)
        pr_str = p_info['price']
        draw.text((300, y_offset + 12), pr_str, fill="#ff5722" if pr_str != "FREE" else "#4caf50", font=font_main)
        disc_str = p_info['discount']
        if disc_str != "0%":
            draw.rectangle([750, y_offset + 5, 880, y_offset + 35], fill="#ffeb3b", outline="#000000", width=2)
            draw.text((775, y_offset + 12), disc_str, fill="#000000", font=font_main)
        else:
            draw.text((775, y_offset + 12), "REGULAR", fill="#000000", font=font_sub)
        y_offset += 50

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def run_telegram_autopost_logic():
    global PUBLISHED_GAMES_HISTORY
    now = time.time()
    PUBLISHED_GAMES_HISTORY = {k: v for k, v in PUBLISHED_GAMES_HISTORY.items() if now - v < 86400}
    
    cache = STEAM_DATA_CACHE.get("EUR")
    if not cache: return

    candidates = []
    for cat_name, items in cache.items():
        for item in items:
            if item["id"] not in PUBLISHED_GAMES_HISTORY:
                disc_val = abs(int(item.get("discount", "0%").replace('%','').replace('-','').strip())) if '%' in item.get("discount", "0%") else 0
                candidates.append({"item": item, "cat": CATEGORIES_MAP.get(cat_name, cat_name), "disc": disc_val})

    if not candidates: return
    candidates.sort(key=lambda x: x["disc"], reverse=True)
    selected = candidates[0]
    game, category = selected["item"], selected["cat"]
    
    PUBLISHED_GAMES_HISTORY[game["id"]] = now
    
    regional_prices = get_prices_for_all_regions(game["id"])
    photo = generate_game_card_image(game["name"], category, regional_prices, game["img"], selected["disc"] >= 90)

    ai_text = "Отличная игра со скидкой доступна в Steam."
    if GROQ_API_KEY:
        try:
            client = Groq(api_key=GROQ_API_KEY)
            ps = ", ".join([f"{k}: {v['price']}" for k, v in regional_prices.items()])
            prompt = f"Напиши 3 коротких предложения для ТГ-канала об игре '{game['name']}'. Категория: {category}. Цены: {ps}. Без эмодзи."
            res = client.chat.completions.create(model=MODEL_NAME, messages=[{"role": "user", "content": prompt}], timeout=15)
            ai_text = res.choices[0].message.content.strip()
        except Exception as e:
            print(f"[ИИ] Ошибка Groq: {e}")

    safe_cat = html.escape(category.replace(' ', '_'))
    safe_name = html.escape(game['name'])
    p_block = "\n".join([f"• <b>{k}:</b> {html.escape(v['price'])} ({html.escape(v['discount'])})" for k, v in regional_prices.items()])
    
    msg = (f"👾 <b>ИГРА:</b> {safe_name}\n\n"
           f"💰 <b>СКИДКИ:</b>\n{p_block}\n\n"
           f"📝 <b>ОБЗОР:</b>\n{html.escape(ai_text)}\n\n"
           f"🎮 <b>Ссылка:</b> https://store.steampowered.com/app/{game['id']}\n\n"
           f"#{safe_cat} #Steam #Скидки")

    try:
        requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                      files={"photo": ("card.png", photo, "image/png")},
                      data={"chat_id": TG_CHANNEL_ID, "caption": msg, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print(f"[Бот] Ошибка отправки: {e}")

def telegram_scheduler_worker():
    time.sleep(20)
    while True:
        try: run_telegram_autopost_logic()
        except Exception as e: print(f"[Планировщик] Ошибка: {e}")
        time.sleep(7200)

# ==================== HTML ШАБЛОН ====================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>SteamRadar API WebApp</title>
    <link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #dfd4c9; --header-bg: #ffffff; --text-color: #000000;
            --white: #ffffff; --gray: #7a7a7a; --green: #4caf50;
            --border-pixel: 4px solid var(--text-color);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Press Start 2P', monospace; image-rendering: pixelated; }
        body { background-color: var(--bg-color); background-size: 32px 32px; color: var(--text-color); padding-top: 145px; padding-bottom: 80px; }
        header { position: fixed; top: 0; left: 0; width: 100%; height: 115px; background-color: var(--header-bg); border-bottom: var(--border-pixel); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; z-index: 999999; }
        .header-title { font-size: 14px; font-weight: bold; background: var(--white); padding: 4px; border: 2px solid var(--text-color); }
        .container { width: 100%; max-width: 850px; margin: 0 auto; padding: 0 15px; }
        .section-title { font-size: 11px; margin: 30px 0 15px 0; text-align: center; background: #fff; padding: 6px; border: 3px solid #000; }
        .game-card { background-color: #fffbf7; border: var(--border-pixel); display: flex; margin-bottom: 25px; position: relative; box-shadow: 6px 6px 0px rgba(0,0,0,0.15); }
        .game-img-wrapper { width: 35%; min-width: 115px; max-width: 200px; border-right: var(--border-pixel); background: var(--gray); }
        .game-img-wrapper img { width: 100%; height: 100%; object-fit: cover; display: block; }
        .game-info { flex: 1; padding: 12px; display: flex; flex-direction: column; justify-content: space-between; position: relative; min-width: 0; }
        .game-title { font-size: 10px; line-height: 1.4; font-weight: bold; overflow: hidden; text-overflow: ellipsis; padding-right: 20px;}
        .game-details { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
        .badge { font-size: 8px; height: 22px; padding: 0 4px; background: var(--white); border: 2px solid var(--text-color); display: inline-flex; align-items: center; }
        .badge.discount { background: #ffeb3b; }
        .badge.price { background: #ff5722; color: var(--white); }
        .badge.price-free { background: var(--green) !important; color: var(--white) !important; }
        .dots-menu-btn { position: absolute; top: 12px; right: 14px; width: 24px; height: 24px; cursor: pointer; display: flex; flex-direction: column; justify-content: space-between; align-items: center; padding: 3px 0; }
        .dots-menu-btn span { width: 5px; height: 5px; background-color: var(--text-color); }
        .card-context-menu { position: absolute; top: 40px; right: 14px; background: var(--white); border: var(--border-pixel); z-index: 9999; width: 160px; display: none; }
        .card-context-menu.open { display: block; }
        .menu-item { font-size: 9px; padding: 10px; cursor: pointer; border-bottom: 2px solid var(--text-color); }
        .menu-item:last-child { border-bottom: none; }
        .menu-item:hover { background: var(--bg-color); }
        .sub-menu { display: none; background: #fff6ed; border-top: 2px dashed #000; }
        .sub-menu.open { display: block; }
        .sub-menu div { padding: 8px; font-size: 8px; text-align: center; cursor: pointer; border-bottom: 1px dashed #000; }
        .sub-menu div:last-child { border-bottom: none; }
        .load-more-btn { background-color: #fffbf7; border: var(--border-pixel); width: 100%; padding: 12px; text-align: center; font-size: 10px; cursor: pointer; margin: 10px 0 40px 0; display: block; }
    </style>
</head>
<body>
    <header>
        <div class="header-title">STEAM RADAR WEBAPP</div>
        <div style="font-size:8px;">Валюта: {{ current_currency }}</div>
    </header>
    <div class="container">
        {% for cat_key, cat_title in categories_map.items() %}
        <div class="section-title">{{ cat_title }}</div>
        <div id="{{ cat_key }}-container"></div>
        <div id="{{ cat_key }}-btn" class="load-more-btn" onclick="handleLoadMore('{{ cat_key }}')">Загрузить еще</div>
        {% endfor %}
    </div>

    <script>
        const rawData = {{ data_json | safe }};
        const symbols = { "EUR": "€", "USD": "$", "RUB": "₽" };
        const state = {};
        
        Object.keys(rawData).forEach(key => {
            state[key] = { data: rawData[key] || [], index: 0, step: 5, limit: 25 };
        });

        function toggleCardMenu(e, appId) {
            e.stopPropagation();
            document.querySelectorAll('.card-context-menu').forEach(m => { if(m.id !== 'menu-' + appId) m.classList.remove('open'); });
            document.getElementById('menu-' + appId).classList.toggle('open');
        }

        function toggleSubMenu(e, appId) {
            e.stopPropagation();
            document.getElementById('submenu-' + appId).classList.toggle('open');
        }

        function changeGameCurrencyLocal(appId, targetCurr) {
            const card = document.querySelector(`.game-card[data-id="${appId}"]`);
            if(!card) return;
            const priceBadge = card.querySelector('.badge.price');
            if(!priceBadge || priceBadge.innerText === "FREE") return;

            let numPrice = parseFloat(priceBadge.innerText) || 9.99;
            let newPrice = numPrice;
            if(targetCurr === "RUB" && !priceBadge.innerText.includes('₽')) newPrice = numPrice * 95.0;
            if(targetCurr !== "RUB" && priceBadge.innerText.includes('₽')) newPrice = numPrice / 95.0;
            
            priceBadge.innerText = `${newPrice.toFixed(2)} ${symbols[targetCurr]}`;
            document.getElementById('menu-' + appId).classList.remove('open');
        }

        window.addEventListener('click', function() {
            document.querySelectorAll('.card-context-menu').forEach(m => m.classList.remove('open'));
            document.querySelectorAll('.sub-menu').forEach(s => s.classList.remove('open'));
        });

        function createCard(game) {
            const card = document.createElement('div');
            card.className = 'game-card';
            card.dataset.id = game.id;
            let priceClass = game.price === "FREE" ? "price price-free" : "price";
            
            card.innerHTML = `
                <div class="game-img-wrapper"><img src="${game.img}" onerror="this.parentNode.style.background='#7a7a7a'"></div>
                <div class="game-info">
                    <div class="game-title">${game.name}</div>
                    <div class="game-details">
                        <span class="badge discount">${game.discount}</span>
                        <span class="badge ${priceClass}">${game.price}</span>
                    </div>
                    <div class="dots-menu-btn" onclick="toggleCardMenu(event, '${game.id}')"><span></span><span></span><span></span></div>
                    <div class="card-context-menu" id="menu-${game.id}">
                        <div class="menu-item" onclick="window.open('https://store.steampowered.com/app/${game.id}', '_blank')">Открыть Steam</div>
                        <div class="menu-item" onclick="toggleSubMenu(event, '${game.id}')">Изменить валюту ▾</div>
                        <div class="sub-menu" id="submenu-${game.id}">
                            <div onclick="changeGameCurrencyLocal('${game.id}', 'EUR')">EUR (€)</div>
                            <div onclick="changeGameCurrencyLocal('${game.id}', 'USD')">USD ($)</div>
                            <div onclick="changeGameCurrencyLocal('${game.id}', 'RUB')">RUB (₽)</div>
                        </div>
                    </div>
                </div>
            `;
            return card;
        }

        function renderSection(key) {
            const section = state[key];
            if(!section) return;
            const container = document.getElementById(key + '-container');
            const btn = document.getElementById(key + '-btn');

            let rendered = 0;
            while (section.index < section.data.length && section.index < section.limit && rendered < section.step) {
                container.appendChild(createCard(section.data[section.index]));
                section.index++; rendered++;
            }

            const remaining = Math.min(section.limit, section.data.length) - section.index;
            if (remaining > 0 && btn) { btn.style.display = 'block'; btn.innerText = `Показать еще (${remaining})`; } 
            else if(btn) { btn.style.display = 'none'; }
        }

        function handleLoadMore(key) { renderSection(key); }
        window.onload = () => { Object.keys(state).forEach(key => renderSection(key)); };
    </script>
</body>
</html>
"""

# ==================== FLASK МАРШРУТЫ ====================
@app.route("/")
def index():
    global_currency = request.args.get("currency", "EUR")
    if global_currency not in REGIONS: global_currency = "EUR"
        
    if not STEAM_DATA_CACHE.get(global_currency):
        data = get_steam_data(global_currency)
        STEAM_DATA_CACHE[global_currency] = data
    else:
        data = STEAM_DATA_CACHE[global_currency]

    return render_template_string(
        HTML_TEMPLATE, 
        data_json=json.dumps(data), 
        current_currency=global_currency,
        categories_map=CATEGORIES_MAP
    )

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    try:
        update = request.get_json(silent=True)
        if not update or not update.get("message"):
            return "OK", 200
            
        message = update["message"]
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")

        if text.startswith("/start") and chat_id:
            domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL") or "localhost"
            domain = domain.replace("https://", "").replace("http://", "").rstrip('/')
            
            welcome_text = "Сервис поиска скидок Steam. Нажмите на кнопку ниже для перехода в каталог."
            reply_markup = {"inline_keyboard": [[{"text": "Открыть каталог", "web_app": {"url": f"https://{domain}"}}]]}
            
            requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": welcome_text, "reply_markup": reply_markup},
                timeout=10
            )
    except: pass
    return "OK", 200

@app.before_request
def init_telegram_webhook():
    global WEBHOOK_SET
    if not WEBHOOK_SET:
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL")
        if domain:
            domain = domain.replace("https://", "").replace("http://", "").rstrip('/')
            try:
                requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/deleteWebhook", timeout=5)
                requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setWebhook", json={"url": f"https://{domain}/telegram-webhook", "allowed_updates": ["message"]}, timeout=5)
            except: pass
        WEBHOOK_SET = True

if __name__ == "__main__":
    download_pixel_font()
    threading.Thread(target=steam_cache_worker, daemon=True).start()
    threading.Thread(target=telegram_scheduler_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
