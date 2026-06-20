
import os
import sys
import json
import re
import time
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

# ==================== НАСТРОЙКИ (ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ И ДЕФОЛТЫ) ====================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN").strip()
TG_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()

MODEL_NAME = "llama3-8b-8192" 
WEBHOOK_SET = False

STEAM_DATA_CACHE = {
    "EUR": None,
    "USD": None,
    "RUB": None,
    "last_updated": 0
}

HOT_FRESH_GAMES = []

REGIONS = {
    "EUR": {"country": "DE", "symbol": "€", "lang": "en-US,en;q=0.9"},
    "USD": {"country": "US", "symbol": "$", "lang": "en-US,en;q=0.9"},
    "RUB": {"country": "RU", "symbol": "₽", "lang": "ru-RU,ru;q=0.9"}
}

BANNED_GAMES = ["pubg", "counter-strike", "dota", "apex legends", "warframe", "war thunder", "destiny 2", "rainbow six", "free to play", "play for free"]
FONT_PATH = "PressStart2P.ttf"
PLACEHOLDER_IMG = "https://pub-c5e31b5cdafb419a91624d1024ee2702.r2.dev/mock_steam.png"

def is_ignored(name):
    return any(banned in name.lower() for banned in BANNED_GAMES)

def download_pixel_font():
    if not os.path.exists(FONT_PATH):
        url = "https://github.com/google/fonts/raw/main/ofl/pressstart2p/PressStart2P-Regular.ttf"
        try:
            r = requests.get(url, timeout=15)
            with open(FONT_PATH, "wb") as f:
                f.write(r.content)
        except Exception:
            pass

def clean_price(price_str, currency_code):
    if not price_str:
        return 0.0
    txt = price_str.lower()
    if any(word in txt for word in ["free", "бесплатно", "испробовать", "play"]):
        return 0.0
    
    if currency_code == "RUB":
        p_text = price_str.replace("pуб.", "").replace("руб.", "").replace("₽", "").strip()
        p_text = p_text.replace(" ", "").replace("\xa0", "")
        if "," in p_text: p_text = p_text.split(",")[0]
        if "." in p_text: p_text = p_text.split(".")[0]
        p_text = "".join(c for c in p_text if c.isdigit())
        try: return float(p_text) if p_text else 0.0
        except ValueError: return 0.0
    
    p_text = price_str.replace("€", "").replace("$", "").replace("&nbsp;", "").strip()
    p_text = p_text.replace(" ", "").replace(",", ".")
    p_text = "".join(c for c in p_text if c.isdigit() or c == ".")
    try: return float(p_text) if p_text else 0.0
    except ValueError: return 0.0

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
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": cfg["lang"]}
        time.sleep(0.2)
        try:
            r = requests.get(f"https://store.steampowered.com/app/{app_id}/", headers=headers, cookies=cookies, timeout=5)
            if r.status_code == 200:
                s = BeautifulSoup(r.text, "html.parser")
                p_div = s.find("div", class_="discount_final_price") or s.find("div", class_="game_purchase_price")
                d_div = s.find("div", class_="discount_pct")
                disc = d_div.text.strip() if d_div else "0%"
                if p_div:
                    txt = p_div.text.strip()
                    if any(w in txt.lower() for w in ["free", "бесплатно"]):
                        results[code] = {"price": "FREE", "discount": "100%"}
                    else:
                        cleaned = clean_price(txt, code)
                        if code == "RUB":
                            results[code] = {"price": f"{int(cleaned)} {cfg['symbol']}", "discount": disc}
                        else:
                            results[code] = {"price": f"{cleaned:.2f} {cfg['symbol']}", "discount": disc}
                else:
                    results[code] = {"price": "N/A", "discount": "0%"}
            else:
                results[code] = {"price": "N/A", "discount": "0%"}
        except Exception:
            results[code] = {"price": "N/A", "discount": "0%"}
    return results

def get_steam_data(global_currency="EUR"):
    cfg = REGIONS.get(global_currency, REGIONS["EUR"])
    cookies = {"wants_mature_content": "1", "birthtime": "288028801", "last_steam_country": cfg["country"]}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": cfg["lang"]}
    symbol = cfg["symbol"]

    def fetch_url(url, category_name):
        games = []
        try:
            res = requests.get(url, headers=headers, cookies=cookies, timeout=10)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                for row in soup.find_all("a", class_="search_result_row"):
                    name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else "Unknown"
                    if is_ignored(name): continue
                    app_id = extract_game_id(row)
                    if not app_id: continue
                    
                    img_url = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{app_id}/header.jpg"
                    disc_div = row.find("div", class_="discount_pct")
                    price_div = row.find("div", class_="discount_final_price") or row.find("div", class_="search_price")
                    
                    discount = "0%"
                    if disc_div:
                        discount = f"-{abs(int(disc_div.text.replace('%', '').replace('-', '').strip()))}%"
                    
                    price = "FREE"
                    if price_div:
                        txt = price_div.text.strip()
                        if not any(w in txt.lower() for w in ["free", "бесплатно"]):
                            price_val = clean_price(txt, global_currency)
                            price = f"{int(price_val)} {symbol}" if global_currency == "RUB" else f"{price_val:.2f} {symbol}"
                        else:
                            discount = "100%"

                    games.append({"id": app_id, "name": name, "discount": discount, "price": price, "img": img_url, "tags": [category_name], "type": category_name})
        except Exception:
            pass
        return games

    discounts = fetch_url("https://store.steampowered.com/search/?specials=1&ndl=1", "Steam")
    free_single = fetch_url("https://store.steampowered.com/search/?maxprice=free&ndl=1", "Single")
    coop_disc = fetch_url("https://store.steampowered.com/search/?category2=38,9&specials=1&ndl=1", "Co-op")
    coop_free = fetch_url("https://store.steampowered.com/search/?maxprice=free&category2=38,9&ndl=1", "Multiplayer")
    upcoming = fetch_url("https://store.steampowered.com/search/?filter=comingsoon&ndl=1", "Upcoming")

    return {
        "discounts": discounts[:100],
        "free_single": free_single[:20],
        "coop_disc": coop_disc[:20],
        "coop_free": coop_free[:20],
        "upcoming": upcoming[:20]
    }

def steam_cache_worker():
    global HOT_FRESH_GAMES
    while True:
        now = time.time()
        HOT_FRESH_GAMES = [g for g in HOT_FRESH_GAMES if now - g["added_at"] < 86400]
        for currency in ["EUR", "USD", "RUB"]:
            try:
                STEAM_DATA_CACHE[currency] = get_steam_data(currency)
                time.sleep(1)
            except Exception:
                pass
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
    except Exception:
        font_main = font_title = font_sub = ImageFont.load_default()

    draw.rectangle([40, 40, 960, 560], fill="#fffbf7", outline="#000000", width=5)
    header_bg = "#ff5722" if is_ultra_hot else "#ffffff"
    header_txt_color = "#ffffff" if is_ultra_hot else "#000000"
    header_text = "🔥 MEGA DROPS / РАЗДАЧИ И ТОП ИГРЫ 🔥" if is_ultra_hot else "STEAM HIDDEN GEMS RADAR"
    
    draw.rectangle([40, 40, 960, 100], fill=header_bg, outline="#000000", width=5)
    draw.text((65, 58), header_text, fill=header_txt_color, font=font_title)

    try:
        response = requests.get(img_url, timeout=5)
        game_thumb = Image.open(BytesIO(response.content)).convert("RGB")
        game_thumb = game_thumb.resize((360, 200), Image.Resampling.NEAREST)
        img.paste(game_thumb, (70, 140))
        draw.rectangle([67, 137, 433, 343], outline="#000000", width=4)
    except Exception:
        draw.rectangle([70, 140, 430, 340], fill="#7a7a7a", outline="#000000", width=4)

    draw.text((470, 140), "MAIN GAME:", fill="#7a7a7a", font=font_sub)
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

    img_buf = BytesIO()
    img.save(img_buf, format="PNG")
    img_buf.seek(0)
    return img_buf

def run_telegram_autopost_logic():
    global HOT_FRESH_GAMES
    headers = {"User-Agent": "Mozilla/5.0"}
    cat_freebies = []   
    cat_cool_free = []  
    cat_discounts = []  

    # Парсинг платных скидок и 100% раздач
    try:
        for page in range(1, 4):
            res = requests.get(f"https://store.steampowered.com/search/?specials=1&page={page}", headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, "html.parser")
            for row in soup.find_all("a", class_="search_result_row"):
                name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else ""
                if not name or is_ignored(name): continue
                app_id = extract_game_id(row)
                if not app_id or any(str(g["game"]["id"]) == str(app_id) for g in HOT_FRESH_GAMES): continue
                
                img_url = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{app_id}/header.jpg"
                disc_div = row.find("div", class_="discount_pct")
                price_div = row.find("div", class_="discount_final_price")
                
                disc_text = disc_div.text.strip() if disc_div else "0%"
                price_text = price_div.text.strip() if price_div else "0.00"
                disc_val = abs(int(disc_text.replace('%','').replace('-','').strip())) if '%' in disc_text else 0
                
                game_data = {"id": app_id, "name": name, "price": price_text, "discount": disc_text, "disc_val": disc_val, "img": img_url, "cat": "Скидка"}
                
                if disc_val == 100 or any(w in price_text.lower() for w in ["free", "бесплатно"]):
                    game_data["cat"] = "Халява"
                    game_data["price"] = "FREE"
                    game_data["discount"] = "100%"
                    cat_freebies.append(game_data)
                elif disc_val >= 30:
                    cat_discounts.append(game_data)
    except Exception: pass

    # Парсинг стабильно хороших бесплатных тайтлов
    try:
        res = requests.get("https://store.steampowered.com/search/?maxprice=free&category2=9,38&ndl=1", headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        for row in soup.find_all("a", class_="search_result_row"):
            name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else ""
            if not name or is_ignored(name): continue
            app_id = extract_game_id(row)
            if not app_id or any(str(g["game"]["id"]) == str(app_id) for g in HOT_FRESH_GAMES): continue
            
            img_url = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{app_id}/header.jpg"
            game_data = {"id": app_id, "name": name, "price": "FREE", "discount": "100%", "disc_val": 100, "img": img_url, "cat": "Бесплатно"}
            cat_cool_free.append(game_data)
    except Exception: pass

    selected_games = []
    cat_freebies.sort(key=lambda x: x["disc_val"], reverse=True)
    sel_freebies = cat_freebies[:3]
    selected_games.extend(sel_freebies)
    
    random.shuffle(cat_cool_free)
    sel_cool_free = cat_cool_free[:4]
    selected_games.extend(sel_cool_free)
    
    needed_discounts = 9 - len(selected_games)
    cat_discounts.sort(key=lambda x: x["disc_val"], reverse=True)
    sel_discounts = cat_discounts[:needed_discounts]
    selected_games.extend(sel_discounts)

    if not selected_games: return

    for g in selected_games:
        HOT_FRESH_GAMES.append({"game": g, "added_at": time.time()})

    main_game = selected_games[0]
    is_ultra_hot = any(g["cat"] == "Халява" for g in selected_games)

    ai_descriptions = "Описания от ИИ подготавливаются."
    if GROQ_API_KEY:
        try:
            client = Groq(api_key=GROQ_API_KEY)
            games_list_text = "\n".join([f"- {g['name']} (Категория: {g['cat']})" for g in selected_games])
            prompt_content = (
                f"Ты — геймерский ИИ аналитик под ником zeptg. Напиши для каждой игры строго ОДНО ультра-короткое предложение, "
                f"раскрывая её главную фишку. Будь честен, отсекай скуку. Твой стиль: адаптивный, точечный, сжатый.\n"
                f"Список игр:\n{games_list_text}\n"
                f"Формат вывода строго такой:\n🎮 [Название игры] — [Одно предложение]. Не выделяй текст жирным markdown."
            )
            completion = client.chat.completions.create(
                model=MODEL_NAME, 
                messages=[{"role": "user", "content": prompt_content}], 
                max_tokens=600,
                temperature=0.7,
                timeout=30
            )
            if completion.choices:
                ai_descriptions = completion.choices[0].message.content.strip()
        except Exception: pass

    regional_prices = get_prices_for_all_regions(main_game["id"])
    photo_buffer = generate_game_card_image(main_game["name"], main_game["cat"].upper(), regional_prices, main_game["img"], is_ultra_hot)

    tg_message = "🚨 🔥 <b>МЕГА ПОДБОРКА ИГР ОТ КАНАЛА</b> 🔥 🚨\n\n"
    if sel_freebies:
        tg_message += "🎁 <b>РАЗДАЧА И ХАЛЯВА:</b>\n"
        for g in sel_freebies:
            tg_message += f"🔹 <a href='https://store.steampowered.com/app/{g['id']}'>{html.escape(g['name'])}</a>\n"
        tg_message += "\n"
    if sel_cool_free:
        tg_message += "🕹 <b>АКТУАЛЬНЫЙ БЕСПЛАТНЫЙ КОНТЕНТ:</b>\n"
        for g in sel_cool_free:
            tg_message += f"🔹 <a href='https://store.steampowered.com/app/{g['id']}'>{html.escape(g['name'])}</a>\n"
        tg_message += "\n"
    if sel_discounts:
        tg_message += "💸 <b>ГОРЯЧИЕ СКИДКИ:</b>\n"
        for g in sel_discounts:
            tg_message += f"🔹 <a href='https://store.steampowered.com/app/{g['id']}'>{html.escape(g['name'])}</a> | {g['discount']}\n"
        tg_message += "\n"

    safe_ai_text = html.escape(ai_descriptions)
    tg_message += f"🤖 <b>ГЕЙМ-АНАЛИЗ ОТ zeptg:</b>\n<i>{safe_ai_text}</i>\n\n👇 Нажми на Web App, чтобы увидеть все региональные цены в реальном времени!"

    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
            files={"photo": ("card.png", photo_buffer, "image/png")},
            data={"chat_id": TG_CHANNEL_ID, "caption": tg_message, "parse_mode": "HTML"},
            timeout=15
        )
    except Exception: pass

def telegram_scheduler_worker():
    time.sleep(10)
    try: run_telegram_autopost_logic()
    except Exception: pass
    while True:
        time.sleep(10800)
        try: run_telegram_autopost_logic()
        except Exception: pass

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>SteamGamesList-api</title>
    <link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #dfd4c9; 
            --header-bg: #ffffff; 
            --grid-skin: #dfd4c9;
            --text-color: #000000;
            --white: #ffffff;
            --gray: #7a7a7a;
            --green: #4caf50;
            --border-pixel: 4px solid var(--text-color);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Press Start 2P', monospace; image-rendering: pixelated; }
        @keyframes gridScrollDown { 0% { background-position: 0 0; } 100% { background-position: 0 32px; } }
        body {
            background-color: var(--bg-color);
            background-image: linear-gradient(rgba(255, 255, 255, 0.25) 2px, transparent 2px), linear-gradient(90deg, rgba(255, 255, 255, 0.25) 2px, transparent 2px);
            background-size: 32px 32px; animation: gridScrollDown 5s linear infinite; color: var(--text-color);
            padding-top: 145px; padding-bottom: 80px; overflow-x: hidden;
        }
        header {
            position: fixed; top: 0; left: 0; width: 100%; height: 115px; background-color: var(--header-bg);
            background-image: linear-gradient(var(--grid-skin) 2px, transparent 2px), linear-gradient(90deg, var(--grid-skin) 2px, transparent 2px);
            background-size: 32px 32px; animation: gridScrollDown 5s linear infinite; border-bottom: var(--border-pixel);
            display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; z-index: 1000;
        }
        .header-title { font-size: 16px; font-weight: bold; text-align: center; background: var(--white); padding: 2px 6px; border: 2px solid var(--text-color); }
        .custom-select-wrapper { position: relative; display: inline-block; user-select: none; }
        .custom-select-trigger { font-size: 11px; background: var(--white); border: var(--border-pixel); padding: 8px 16px; cursor: pointer; }
        .custom-options {
            position: absolute; display: block; top: 100%; left: 50%; width: 150px; border: var(--border-pixel); border-top: none; background: var(--white);
            transform-origin: top; transform: translateX(-50%) scaleY(0); opacity: 0; pointer-events: none; transition: all 0.2s; z-index: 2000;
        }
        .custom-select-wrapper.open .custom-options { transform: translateX(-50%) scaleY(1); opacity: 1; pointer-events: auto; }
        .custom-option { font-size: 10px; padding: 12px; cursor: pointer; text-align: center; border-bottom: 2px dashed var(--gray); }
        .custom-option:hover { background: var(--bg-color); }
        .container { width: 100%; max-width: 850px; margin: 0 auto; padding: 0 15px; }
        .section-title { font-size: 12px; margin: 40px 0 20px 0; text-align: center; line-height: 1.6; }
        .section-title.fresh-title { background-color: var(--green); color: var(--white); border: var(--border-pixel); padding: 10px; display: inline-block; margin: 40px auto 20px auto; left: 50%; transform: translateX(-50%); position: relative; }
        .game-card { background-color: #fffbf7; border: var(--border-pixel); display: flex; margin-bottom: 25px; min-height: 115px; position: relative; box-shadow: 6px 6px 0px rgba(0,0,0,0.15); transform: scale(1); opacity: 1; }
        .game-card.active-card { z-index: 999 !important; }
        .game-img-wrapper { width: 35%; min-width: 115px; max-width: 200px; border-right: var(--border-pixel); background: var(--gray); flex-shrink: 0; position: relative; }
        .game-img-wrapper img { width: 100%; height: 100%; object-fit: cover; display: block; }
        .game-info { flex: 1; padding: 14px; display: flex; flex-direction: column; justify-content: space-between; position: relative; min-width: 0; }
        .game-title { font-size: 11px; line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 30px; font-weight: bold; }
        .game-details { display: flex; align-items: center; justify-content: flex-start; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
        .badge { font-size: 8px; height: 24px; padding: 0 6px; background: var(--white); border: 2px solid var(--text-color); display: inline-flex; align-items: center; justify-content: center; }
        .badge.discount { background: #ffeb3b; }
        .badge.price { background: #ff5722; color: var(--white); }
        .badge.price-free { background: var(--green) !important; color: var(--white) !important; font-weight: bold; }
        .badge.fresh-tag { background: var(--green); color: var(--white); font-weight: bold; }
        .dots-menu-btn { position: absolute; top: 12px; right: 14px; width: 24px; height: 24px; cursor: pointer; display: flex; flex-direction: column; justify-content: space-between; align-items: center; padding: 3px 0; z-index: 10; }
        .dots-menu-btn span { width: 5px; height: 5px; background-color: var(--text-color); }
        .card-context-menu { position: absolute; top: 42px; right: 14px; background: var(--white); border: var(--border-pixel); z-index: 1000; width: 170px; box-shadow: 6px 6px 0px rgba(0,0,0,0.25); transform-origin: top; transform: scaleY(0); opacity: 0; pointer-events: none; transition: all 0.2s; }
        .card-context-menu.open { transform: scaleY(1); opacity: 1; pointer-events: auto; }
        .menu-item { font-size: 9px; padding: 12px; cursor: pointer; border-bottom: 2px solid var(--text-color); }
        .menu-item:hover { background: var(--bg-color); }
        .sub-menu { display: none; background: #fff6ed; border-top: 2px solid var(--text-color); }
        .sub-menu.open { display: block; }
        .sub-menu div { padding: 10px; font-size: 8px; text-align: center; cursor: pointer; border-bottom: 1px dashed var(--text-color); }
        .sub-menu div:hover { background: var(--bg-color); }
        .load-more-btn { background-color: #fffbf7; border: var(--border-pixel); width: 100%; padding: 16px; text-align: center; font-size: 11px; cursor: pointer; margin: 15px 0 50px 0; box-shadow: 4px 4px 0px rgba(0,0,0,0.15); display: block; user-select: none; }
        .bounce-in-active { animation: smoothIn 0.35s cubic-bezier(0.175, 0.885, 0.32, 1.25) forwards; }
        @keyframes smoothIn { 0% { transform: scale(0.85); opacity: 0; } 100% { transform: scale(1); opacity: 1; } }
    </style>
</head>
<body>
    <header>
        <div class="header-title">SteamGamesList-api</div>
        <div class="custom-select-wrapper" id="currency-wrapper">
            <div class="custom-select-trigger" onclick="toggleSelect(event)">
                <span id="selected-val">{% if current_currency == 'EUR' %}EUR ▾{% elif current_currency == 'USD' %}USD ▾{% else %}RUB ▾{% endif %}</span>
            </div>
            <div class="custom-options">
                <div class="custom-option" onclick="selectGlobalCurrency('EUR')">EUR</div>
                <div class="custom-option" onclick="selectGlobalCurrency('USD')">USD</div>
                <div class="custom-option" onclick="selectGlobalCurrency('RUB')">RUB</div>
            </div>
        </div>
    </header>

    <div class="container">
        {% if fresh_games %}
        <div class="section-title fresh-title">НОВОЕ ИЗ КАНАЛА!</div>
        <div id="fresh-container">
            {% for entry in fresh_games %}
            <div class="game-card bounce-in-active" data-id="{{ entry.game.id }}">
                <div class="game-img-wrapper">
                    <img src="{{ entry.game.img }}" onerror="this.src='https://pub-c5e31b5cdafb419a91624d1024ee2702.r2.dev/mock_steam.png'">
                </div>
                <div class="game-info">
                    <div class="game-title" title="{{ entry.game.name }}">{{ entry.game.name }}</div>
                    <div class="game-details">
                        <span class="badge fresh-tag">НОВОЕ!</span>
                        <span class="badge discount">{{ entry.game.discount }}</span>
                        <span class="badge price {% if entry.game.price == 'FREE' %}price-free{% endif %}" id="price-{{ entry.game.id }}">{{ entry.game.price }}</span>
                    </div>
                    <div class="dots-menu-btn" onclick="toggleCardMenu(event, '{{ entry.game.id }}')">
                        <span></span><span></span><span></span>
                    </div>
                    <div class="card-context-menu" id="menu-{{ entry.game.id }}">
                        <div class="menu-item" onclick="window.open('https://store.steampowered.com/app/{{ entry.game.id }}', '_blank')">Открыть Steam</div>
                        <div class="menu-item" onclick="toggleSubMenu(event, '{{ entry.game.id }}')">Регион цены ▾</div>
                        <div class="sub-menu" id="submenu-{{ entry.game.id }}">
                            <div onclick="changeGameCurrency('{{ entry.game.id }}', 'EUR')">EUR (€)</div>
                            <div onclick="changeGameCurrency('{{ entry.game.id }}', 'USD')">USD ($)</div>
                            <div onclick="changeGameCurrency('{{ entry.game.id }}', 'RUB')">RUB (₽)</div>
                        </div>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        <div id="default-game-sections">
            <div class="section-title">Скидки</div>
            <div id="discounts-container"></div>
            <div id="discounts-btn" class="load-more-btn" onclick="handleLoadMore('discounts')">Ещё?</div>

            <div class="section-title">Топ бесплатных сингл игр</div>
            <div id="free_single-container"></div>
            <div id="free_single-btn" class="load-more-btn" onclick="handleLoadMore('free_single')">Ещё?</div>

            <div class="section-title">Игры со скидками для друзей</div>
            <div id="coop_disc-container"></div>
            <div id="coop_disc-btn" class="load-more-btn" onclick="handleLoadMore('coop_disc')">Ещё?</div>

            <div class="section-title">Бесплатные кооп игры</div>
            <div id="coop_free-container"></div>
            <div id="coop_free-btn" class="load-more-btn" onclick="handleLoadMore('coop_free')">Ещё?</div>

            <div class="section-title">Ожидаемые новинки</div>
            <div id="upcoming-container"></div>
            <div id="upcoming-btn" class="load-more-btn" onclick="handleLoadMore('upcoming')">Ещё?</div>
        </div>
    </div>

    <script>
        const rawData = {{ data_json | safe }};
        const state = {
            discounts: { data: rawData.discounts || [], index: 0, step: 5, limit: 100 },
            free_single: { data: rawData.free_single || [], index: 0, step: 5, limit: 20 },
            coop_disc: { data: rawData.coop_disc || [], index: 0, step: 5, limit: 20 },
            coop_free: { data: rawData.coop_free || [], index: 0, step: 5, limit: 20 },
            upcoming: { data: rawData.upcoming || [], index: 0, step: 5, limit: 20 }
        };

        function toggleSelect(e) { e.stopPropagation(); document.getElementById('currency-wrapper').classList.toggle('open'); }
        function selectGlobalCurrency(val) { window.location.href = "/?currency=" + val; }

        function toggleCardMenu(e, appId) {
            e.stopPropagation();
            const targetMenu = document.getElementById('menu-' + appId);
            const targetCard = targetMenu.closest('.game-card');
            const isOpen = targetMenu.classList.contains('open');
            document.querySelectorAll('.card-context-menu').forEach(m => m.classList.remove('open'));
            document.querySelectorAll('.game-card').forEach(c => c.classList.remove('active-card'));
            if (!isOpen && targetMenu) {
                targetMenu.classList.add('open');
                targetCard.classList.add('active-card');
            }
        }

        function toggleSubMenu(e, appId) { e.stopPropagation(); document.getElementById('submenu-' + appId).classList.toggle('open'); }

        function changeGameCurrency(appId, targetCurr) {
            const priceBadge = document.getElementById(`price-${appId}`);
            if (!priceBadge) return;
            const oldPrice = priceBadge.innerText;
            priceBadge.innerText = '⏳';
            fetch(`/api/prices/${appId}`)
                .then(res => res.json())
                .then(data => {
                    if(data[targetCurr] && data[targetCurr].price !== "N/A") {
                        priceBadge.innerText = data[targetCurr].price;
                        if(data[targetCurr].price === 'FREE') { priceBadge.classList.add('price-free'); } 
                        else { priceBadge.classList.remove('price-free'); }
                    } else { priceBadge.innerText = oldPrice; }
                }).catch(() => { priceBadge.innerText = oldPrice; });
        }

        window.addEventListener('click', function() {
            document.getElementById('currency-wrapper').remove('open');
            document.querySelectorAll('.card-context-menu').forEach(m => m.classList.remove('open'));
        });

        function createCard(game, uniqueId) {
            const card = document.createElement('div');
            card.className = 'game-card bounce-in-active';
            let tagsHtml = '';
            if(Array.isArray(game.tags)) { game.tags.slice(0, 2).forEach(t => { tagsHtml += `<span class="badge">${t}</span>`; }); }
            let priceClass = game.price === "FREE" ? "price price-free" : "price";

            card.innerHTML = `
                <div class="game-img-wrapper">
                    <img src="${game.img}" onerror="this.src='https://pub-c5e31b5cdafb419a91624d1024ee2702.r2.dev/mock_steam.png'">
                </div>
                <div class="game-info">
                    <div class="game-title" title="${game.name}">${game.name}</div>
                    <div class="game-details">
                        <span class="badge discount">${game.discount}</span>
                        <span class="badge ${priceClass}" id="price-${uniqueId}">${game.price}</span>
                        ${tagsHtml}
                    </div>
                    <div class="dots-menu-btn" onclick="toggleCardMenu(event, '${uniqueId}')">
                        <span></span><span></span><span></span>
                    </div>
                    <div class="card-context-menu" id="menu-${uniqueId}">
                        <div class="menu-item" onclick="window.open('https://store.steampowered.com/app/${game.id}', '_blank')">Открыть Steam</div>
                        <div class="menu-item" onclick="toggleSubMenu(event, '${uniqueId}')">Регион цены ▾</div>
                        <div class="sub-menu" id="submenu-${uniqueId}">
                            <div onclick="changeGameCurrency('${game.id}', 'EUR')">EUR (€)</div>
                            <div onclick="changeGameCurrency('${game.id}', 'USD')">USD ($)</div>
                            <div onclick="changeGameCurrency('${game.id}', 'RUB')">RUB (₽)</div>
                        </div>
                    </div>
                </div>
            `;
            return card;
        }

        function renderSection(key) {
            const section = state[key];
            const container = document.getElementById(key + '-container');
            const btn = document.getElementById(key + '-btn');
            let itemsToRender = [];
            let rendered = 0;
            while (section.index < section.data.length && section.index < section.limit && rendered < section.step) {
                itemsToRender.push(section.data[section.index]);
                section.index++;
                rendered++;
            }
            itemsToRender.forEach((game, idx) => {
                const uniqueId = game.id + '-' + Math.random().toString(36).substr(2, 5);
                const card = createCard(game, uniqueId);
                container.appendChild(card);
            });
            const remaining = Math.min(section.limit, section.data.length) - section.index;
            btn.style.display = remaining > 0 ? 'block' : 'none';
            if (remaining > 0) btn.innerText = `Ещё? (Осталось: ${remaining})`;
        }

        function handleLoadMore(key) { renderSection(key); }
        window.onload = () => { Object.keys(state).forEach(key => renderSection(key)); };
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    global_currency = request.args.get("currency", "EUR")
    if global_currency not in REGIONS: global_currency = "EUR"
    if STEAM_DATA_CACHE[global_currency] is None:
        data = get_steam_data(global_currency)
        STEAM_DATA_CACHE[global_currency] = data
    else:
        data = STEAM_DATA_CACHE[global_currency]
    return render_template_string(HTML_TEMPLATE, data_json=json.dumps(data), current_currency=global_currency, fresh_games=HOT_FRESH_GAMES)

@app.route("/api/prices/<app_id>")
def get_prices_api(app_id):
    try: return json.dumps(get_prices_for_all_regions(app_id))
    except Exception as e: return json.dumps({"error": str(e)}), 500

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    try:
        update = request.get_json(silent=True)
        if not update or "message" not in update: return "OK", 200
        message = update["message"]
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")

        if text.startswith("/start") and chat_id:
            domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL") or "localhost"
            host_url = f"https://{domain.replace('https://', '').replace('http://', '').rstrip('/')}"
            welcome_text = "Привет! Я бот скрытых скидок и халявы в Steam. Нажми кнопку ниже, чтобы открыть Web App!"
            reply_markup = {"inline_keyboard": [[{"text": "Открыть Web App", "web_app": {"url": host_url}}]]}
            requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": welcome_text, "reply_markup": reply_markup}, timeout=10)
    except Exception: pass
    return "OK", 200

@app.before_request
def init_telegram_webhook():
    global WEBHOOK_SET
    if not WEBHOOK_SET:
        railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("RAILWAY_STATIC_URL")
        if railway_domain:
            webhook_url = f"https://{railway_domain.replace('https://', '').replace('http://', '').rstrip('/')}/telegram-webhook"
            try:
                requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/deleteWebhook", timeout=10)
                requests.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setWebhook", json={"url": webhook_url, "allowed_updates": ["message"]}, timeout=10)
            except Exception: pass
        WEBHOOK_SET = True

if __name__ == "__main__":
    download_pixel_font()
    threading.Thread(target=steam_cache_worker, daemon=True).start()
    threading.Thread(target=telegram_scheduler_worker, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
