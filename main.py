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

# ==================== НАСТРОЙКИ====================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHANNEL_ID = os.environ.get("TG_CHANNEL_ID")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
MODEL_NAME = "llama-3.3-70b-versatile"


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

BANNED_GAMES = ["pubg", "counter-strike", "dota", "apex legends", "warframe", "war thunder", "destiny 2", "rainbow six"]
FONT_PATH = "PressStart2P.ttf"

def is_ignored(name):
    return any(banned in name.lower() for banned in BANNED_GAMES)

def download_pixel_font():
    if not os.path.exists(FONT_PATH):
        print("[Система] Скачивание пиксельного шрифта...")
        url = "https://github.com/google/fonts/raw/main/ofl/pressstart2p/PressStart2P-Regular.ttf"
        try:
            r = requests.get(url, timeout=15)
            with open(FONT_PATH, "wb") as f:
                f.write(r.content)
            print("[Система] Шрифт успешно загружен.")
        except Exception as e:
            print(f"[Система] Не удалось скачать шрифт: {e}.")

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
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": cfg["lang"]}
        time.sleep(0.4)
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
        except Exception:
            results[code] = {"price": "N/A", "discount": "0%"}
    return results

def get_steam_data(global_currency="EUR"):
    cfg = REGIONS.get(global_currency, REGIONS["EUR"])
    cookies = {"wants_mature_content": "1", "birthtime": "288028801", "last_steam_country": cfg["country"]}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": cfg["lang"]}
    symbol = cfg["symbol"]

    discounted = []
    try:
        res = requests.get("https://store.steampowered.com/search/?specials=1&ndl=1", headers=headers, cookies=cookies, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for row in soup.find_all("a", class_="search_result_row"):
                name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else "Unknown"
                if is_ignored(name): continue
                app_id = extract_game_id(row)
                img_tag = row.find("div", class_="search_capsule").find("img")
                img_url = img_tag["src"] if img_tag and img_tag.has_attr("src") else ""

                disc_div = row.find("div", class_="discount_pct")
                price_div = row.find("div", class_="discount_final_price")
                if not disc_div or not price_div: continue
                
                discount = f"-{abs(int(disc_div.text.replace('%', '').strip()))}%"
                price = f"{clean_price(price_div.text, global_currency):.2f} {symbol}"
                
                discounted.append({"id": app_id, "name": name, "discount": discount, "price": price, "img": img_url, "tags": ["Steam"], "type": "discount"})
    except Exception: pass

    free_single = []
    try:
        res = requests.get("https://store.steampowered.com/search/?maxprice=free&ndl=1", headers=headers, cookies=cookies, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for row in soup.find_all("a", class_="search_result_row"):
                name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else "Unknown"
                if is_ignored(name): continue
                app_id = extract_game_id(row)
                img_url = row.find("div", class_="search_capsule").find("img")["src"] if row.find("div", class_="search_capsule") and row.find("div", class_="search_capsule").find("img") else ""
                free_single.append({"id": app_id, "name": name, "discount": "100%", "price": "FREE", "img": img_url, "tags": ["Single"], "type": "free_single"})
                if len(free_single) >= 14: break
    except Exception: pass

    coop_disc = []
    try:
        res = requests.get("https://store.steampowered.com/search/?category2=38,9&specials=1&ndl=1", headers=headers, cookies=cookies, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for row in soup.find_all("a", class_="search_result_row"):
                name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else "Unknown"
                if is_ignored(name): continue
                app_id = extract_game_id(row)
                img_url = row.find("div", class_="search_capsule").find("img")["src"] if row.find("div", class_="search_capsule") and row.find("div", class_="search_capsule").find("img") else ""

                disc_div = row.find("div", class_="discount_pct")
                price_div = row.find("div", class_="discount_final_price")
                if not disc_div or not price_div: continue
                
                discount = f"-{abs(int(disc_div.text.replace('%', '').strip()))}%"
                price = f"{clean_price(price_div.text, global_currency):.2f} {symbol}"

                coop_disc.append({"id": app_id, "name": name, "discount": discount, "price": price, "img": img_url, "tags": ["Co-op"], "type": "coop_disc"})
                if len(coop_disc) >= 10: break
    except Exception: pass

    coop_free = []
    try:
        res = requests.get("https://store.steampowered.com/search/?maxprice=free&category2=38,9&ndl=1", headers=headers, cookies=cookies, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for row in soup.find_all("a", class_="search_result_row"):
                name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else "Unknown"
                if is_ignored(name): continue
                app_id = extract_game_id(row)
                img_url = row.find("div", class_="search_capsule").find("img")["src"] if row.find("div", class_="search_capsule") and row.find("div", class_="search_capsule").find("img") else ""
                coop_free.append({"id": app_id, "name": name, "discount": "100%", "price": "FREE", "img": img_url, "tags": ["Multiplayer"], "type": "coop_free"})
                if len(coop_free) >= 10: break
    except Exception: pass

    upcoming = []
    try:
        res = requests.get("https://store.steampowered.com/search/?maxprice=free&filter=comingsoon&ndl=1", headers=headers, cookies=cookies, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for row in soup.find_all("a", class_="search_result_row"):
                name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else "Unknown"
                app_id = extract_game_id(row)
                img_url = row.find("div", class_="search_capsule").find("img")["src"] if row.find("div", class_="search_capsule") and row.find("div", class_="search_capsule").find("img") else ""
                date_div = row.find("div", class_="search_released")
                release_date = date_div.text.strip() if date_div and date_div.text.strip() else "TBA"
                upcoming.append({"id": app_id, "name": name, "discount": "FREE", "price": "FREE", "img": img_url, "tags": [release_date], "type": "upcoming"})
                if len(upcoming) >= 14: break
    except Exception: pass

    return {
        "discounts": discounted[:50],
        "free_single": free_single,
        "coop_disc": coop_disc,
        "coop_free": coop_free,
        "upcoming": upcoming
    }

def steam_cache_worker():
    global HOT_FRESH_GAMES
    while True:
        print("[Кэш] Обновление данных Steam по всем регионам...")
        now = time.time()
        HOT_FRESH_GAMES = [g for g in HOT_FRESH_GAMES if now - g["added_at"] < 86400]
        
        for currency in ["EUR", "USD", "RUB"]:
            try:
                STEAM_DATA_CACHE[currency] = get_steam_data(currency)
                time.sleep(2)
            except Exception as e:
                print(f"[Кэш] Ошибка обновления {currency}: {e}")
        STEAM_DATA_CACHE["last_updated"] = time.time()
        print("[Кэш] Обновление завершено. Следующее через 30 минут.")
        time.sleep(1800)

def generate_game_card_image(game_name, category, prices, img_url, is_ultra_hot=False):
    img = Image.new("RGB", (1000, 600), "#dfd4c9")
    draw = ImageDraw.Draw(img)
    
    for x in range(0, 1000, 40):
        draw.line([(x, 0), (x, 600)], fill="#ebdcd0", width=2)
    for y in range(0, 600, 40):
        draw.line([(0, y), (1000, y)], fill="#ebdcd0", width=2)

    try:
        font_main = ImageFont.truetype(FONT_PATH, 16)
        font_title = ImageFont.truetype(FONT_PATH, 20)
        font_sub = ImageFont.truetype(FONT_PATH, 12)
    except Exception:
        font_main = font_title = font_sub = ImageFont.load_default()

    draw.rectangle([40, 40, 960, 560], fill="#fffbf7", outline="#000000", width=5)
    
    header_bg = "#ff5722" if is_ultra_hot else "#ffffff"
    header_txt_color = "#ffffff" if is_ultra_hot else "#000000"
    header_text = "🔥 HOT TEMPORARY FREEBIE! 🔥" if is_ultra_hot else "STEAM HIDDEN GEMS RADAR"
    
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

    img_buf = BytesIO()
    img.save(img_buf, format="PNG")
    img_buf.seek(0)
    return img_buf

def run_telegram_autopost_logic():
    global HOT_FRESH_GAMES
    print("[Бот] Анализ глубоких страниц Steam в поисках Жгучей Халявы и больших скидок...")
    categories = {"Халява / Раздачи": [], "Лучшее дня": [], "Для друзей": []}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    for page in range(1, 4):
        time.sleep(1)
        try:
            res_spec = requests.get(f"https://store.steampowered.com/search/?specials=1&page={page}", headers=headers, timeout=10)
            if res_spec.status_code == 200:
                soup = BeautifulSoup(res_spec.text, "html.parser")
                for row in soup.find_all("a", class_="search_result_row"):
                    name = row.find("span", class_="title").text.strip() if row.find("span", class_="title") else ""
                    if not name or is_ignored(name): continue
                    
                    app_id = extract_game_id(row)
                    img_tag = row.find("div", class_="search_capsule").find("img")
                    img_url = img_tag["src"] if img_tag and img_tag.has_attr("src") else ""
                    disc_div = row.find("div", class_="discount_pct")
                    price_div = row.find("div", class_="discount_final_price")
                    
                    disc_text = disc_div.text.strip() if disc_div else "0%"
                    price_text = price_div.text.strip() if price_div else "0.00"
                    
                    disc_val = abs(int(disc_text.replace('%','').replace('-','').strip())) if '%' in disc_text else 0
                    game_data = {"id": app_id, "name": name, "price": price_text, "discount": disc_text, "disc_val": disc_val, "img": img_url, "tags": ["New!"]}
                    
                    if disc_val == 100 or "free" in price_text.lower() or "бесплатно" in price_text.lower() or disc_val >= 80:
                        categories["Халява / Раздачи"].append(game_data)
                    elif "category2=38" in row.get('href', '') or "category2=9" in row.get('href', ''):
                        categories["Для друзей"].append(game_data)
                    else:
                        categories["Лучшее дня"].append(game_data)
        except Exception as e:
            print(f"[Бот] Ошибка сбора: {e}")

    chosen_game = None
    chosen_category = ""
    is_ultra_hot = False

    if categories["Халява / Раздачи"]:
        categories["Халява / Раздачи"].sort(key=lambda x: x["disc_val"], reverse=True)
        chosen_game = categories["Халява / Раздачи"][0]
        chosen_category = "Халява / Раздачи"
        is_ultra_hot = True
    else:
        all_other = categories["Лучшее дня"] + categories["Для друзей"]
        if all_other:
            all_other.sort(key=lambda x: x["disc_val"], reverse=True)
            chosen_game = random.choice(all_other[:3])
            chosen_category = "Для друзей" if chosen_game in categories["Для друзей"] else "Лучшее дня"

    if not chosen_game:
        print("[Бот] Ничего интересного не найдено.")
        return

    if not any(g["game"]["id"] == chosen_game["id"] for g in HOT_FRESH_GAMES):
        HOT_FRESH_GAMES.append({"game": chosen_game, "added_at": time.time()})

    print(f"[Бот] Выбрана игра для публикации: '{chosen_game['name']}' ({chosen_category})")
    
    regional_prices = get_prices_for_all_regions(chosen_game["id"])
    photo_buffer = generate_game_card_image(chosen_game["name"], chosen_category, regional_prices, chosen_game["img"], is_ultra_hot)

    ai_text = "Интересная игра со скидкой уже доступна в магазине Steam!"
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prices_summary = ", ".join([f"{k}: {v['price']} (скидка {v['discount']})" for k, v in regional_prices.items()])
        
        prompt_content = (
            f"Ты — опытный игровой аналитик. Напиши краткий, емкий обзор для Телеграм-канала о малоизвестной игре '{chosen_game['name']}'.\n"
            f"Данные игры:\n"
            f"- Категория: #{chosen_category.replace(' ', '_')}\n"
            f"- Цены по регионам: {prices_summary}\n\n"
            f"Требования к тексту:\n"
            f"1. Расскажи в 2 коротких предложениях, про что эта игра и в чём её фишка.\n"
            f"2. Добавь 1 предложение с экспертным мнением о выгоде покупки по указанным региональным скидкам.\n"
            f"Пиши на русском языке, живым геймерским стилем, без штампов и воды."
        )
        if is_ultra_hot:
            prompt_content += "\nВНИМАНИЕ: На эту игру сейчас действует максимальная халява/скидка, выдели это особо!"

        completion = client.chat.completions.create(
            model=MODEL_NAME, messages=[{"role": "user", "content": prompt_content}], timeout=20
        )
        if completion.choices:
            ai_text = completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Бот] Ошибка Groq: {e}")

    safe_category = html.escape(chosen_category.replace(' ', '_'))
    safe_game_name = html.escape(chosen_game['name'])
    safe_ai_text = html.escape(ai_text)
    
    prices_block = ""
    for r_code, r_data in regional_prices.items():
        prices_block += f"• <b>{r_code}:</b> {html.escape(r_data['price'])} ({html.escape(r_data['discount'])})\n"

    prefix = "🚨 🔥 <b>ОГРАНИЧЕННАЯ ХАЛЯВА</b> 🔥 🚨\n" if is_ultra_hot else ""
    tg_message = (
        f"{prefix}"
        f"👾 <b>КАТЕГОРИЯ:</b> #{safe_category}\n"
        f"🔥 <b>ИГРА:</b> {safe_game_name}\n\n"
        f"💰 <b>РЕГИОНАЛЬНЫЕ СКИДКИ:</b>\n{prices_block}\n"
        f"📝 <b>ОБЗОР ИИ:</b>\n{safe_ai_text}\n\n"
        f"🎮 <b>Ссылка на Steam:</b> https://store.steampowered.com/app/{chosen_game['id']}"
    )

    try:
        tg_res = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
            files={"photo": ("card.png", photo_buffer, "image/png")},
            data={"chat_id": TG_CHANNEL_ID, "caption": tg_message, "parse_mode": "HTML"},
            timeout=15
        )
        if tg_res.status_code == 200:
            print("[Бот] Пост успешно улетел в Телеграм-канал!")
        else:
            print(f"[Бот] Ошибка ТГ: {tg_res.text}")
    except Exception as e:
        print(f"[Бот] Ошибка отправки: {e}")

def telegram_scheduler_worker():
    time.sleep(8)
    try:
        run_telegram_autopost_logic()
    except Exception as e:
        print(f"[Поток] Ошибка старта: {e}")
        
    while True:
        time.sleep(7200)
        try:
            run_telegram_autopost_logic()
        except Exception as e:
            print(f"[Поток] Ошибка: {e}")

# (HTML_TEMPLATE остается неизменным, как в исходном коде)
HTML_TEMPLATE = """<!DOCTYPE html>
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
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Press Start 2P', monospace;
            image-rendering: pixelated;
        }
        
        @keyframes gridScrollDown {
            0% { background-position: 0 0; }
            100% { background-position: 0 32px; }
        }

        body {
            background-color: var(--bg-color);
            background-image: 
                linear-gradient(rgba(255, 255, 255, 0.25) 2px, transparent 2px),
                linear-gradient(90deg, rgba(255, 255, 255, 0.25) 2px, transparent 2px);
            background-size: 32px 32px;
            animation: gridScrollDown 5s linear infinite;
            color: var(--text-color);
            padding-top: 145px;
            padding-bottom: 80px;
            overflow-x: hidden;
        }
        
        header {
            position: fixed;
            top: 0; left: 0; width: 100%; height: 115px;
            background-color: var(--header-bg);
            background-image: 
                linear-gradient(var(--grid-skin) 2px, transparent 2px),
                linear-gradient(90deg, var(--grid-skin) 2px, transparent 2px);
            background-size: 32px 32px;
            animation: gridScrollDown 5s linear infinite;
            border-bottom: var(--border-pixel);
            display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px;
            z-index: 999999;
        }
        .header-title {
            font-size: 16px; font-weight: bold; text-align: center;
            background: var(--white); padding: 2px 6px; border: 2px solid var(--text-color);
        }
        
        .custom-select-wrapper { position: relative; display: inline-block; user-select: none; }
        .custom-select-trigger { font-size: 11px; background: var(--white); border: var(--border-pixel); padding: 8px 16px; cursor: pointer; }
        .custom-options {
            position: absolute; display: block; top: 100%; left: 50%; width: 150px;
            border: var(--border-pixel); border-top: none; background: var(--white); z-index: 100000;
            transform-origin: top; transform: translateX(-50%) scaleY(0); opacity: 0; pointer-events: none;
            transition: transform 0.22s cubic-bezier(0.175, 0.885, 0.32, 1.2), opacity 0.18s ease;
        }
        .custom-select-wrapper.open .custom-options { transform: translateX(-50%) scaleY(1); opacity: 1; pointer-events: auto; }
        .custom-option { font-size: 10px; padding: 12px; cursor: pointer; background: var(--white); text-align: center; border-bottom: 2px dashed var(--gray); }
        .custom-option:last-child { border-bottom: none; }
        .custom-option:hover { background: var(--bg-color); }

        .container { width: 100%; max-width: 850px; margin: 0 auto; padding: 0 15px; }
        .section-title { font-size: 12px; margin: 40px 0 20px 0; text-align: center; line-height: 1.6; }
        
        /* Особый стиль для блока НОВОЕ! */
        .section-title.fresh-title {
            background-color: var(--green);
            color: var(--white);
            border: var(--border-pixel);
            padding: 10px;
            display: inline-block;
            margin: 40px auto 20px auto;
            left: 50%; transform: translateX(-50%); position: relative;
        }

        .game-card {
            background-color: #fffbf7; border: var(--border-pixel); display: flex; margin-bottom: 25px;
            min-height: 115px; position: relative; box-shadow: 6px 6px 0px rgba(0,0,0,0.15);
            transform: scale(0.85); opacity: 0;
        }
        .game-card.active-card { z-index: 99999 !important; }
        
        .game-img-wrapper { width: 35%; min-width: 115px; max-width: 200px; border-right: var(--border-pixel); background: var(--gray); flex-shrink: 0; }
        .game-img-wrapper img { width: 100%; height: 100%; object-fit: cover; display: block; }
        
        .game-info { flex: 1; padding: 14px; display: flex; flex-direction: column; justify-content: space-between; position: relative; min-width: 0; }
        .game-title { font-size: 11px; line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 30px; font-weight: bold; }
        .game-details { display: flex; align-items: center; justify-content: flex-start; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
        
        .badge {
            font-size: 8px; height: 24px; padding: 0 6px; background: var(--white);
            border: 2px solid var(--text-color); display: inline-flex; align-items: center; justify-content: center; 
        }
        .badge.discount { background: #ffeb3b; }
        .badge.price { background: #ff5722; color: var(--white); }
        .badge.price-free { background: var(--green) !important; color: var(--white) !important; font-weight: bold; }
        .badge.fresh-tag { background: var(--green); color: var(--white); font-weight: bold; }

        .dots-menu-btn {
            position: absolute; top: 12px; right: 14px; width: 24px; height: 24px; cursor: pointer;
            display: flex; flex-direction: column; justify-content: space-between; align-items: center; padding: 3px 0; z-index: 100;
        }
        .dots-menu-btn span { width: 5px; height: 5px; background-color: var(--text-color); }
        
        .card-context-menu {
            position: absolute; top: 42px; right: 14px; background: var(--white); border: var(--border-pixel);
            z-index: 999999; width: 170px; box-shadow: 6px 6px 0px rgba(0,0,0,0.25);
            transform-origin: top; transform: scaleY(0); opacity: 0; pointer-events: none;
            transition: transform 0.22s cubic-bezier(0.175, 0.885, 0.32, 1.2), opacity 0.18s ease;
        }
        .card-context-menu.open { transform: scaleY(1); opacity: 1; pointer-events: auto; }
        .menu-item { font-size: 9px; padding: 12px; cursor: pointer; border-bottom: 2px solid var(--text-color); }
        .menu-item:last-child { border-bottom: none; }
        .menu-item:hover { background: var(--bg-color); }
        
        .sub-menu { display: block; max-height: 0; overflow: hidden; background: #fff6ed; transform-origin: top; transition: max-height 0.22s ease-out; }
        .sub-menu.open { max-height: 150px; }
        .sub-menu div { padding: 10px; font-size: 8px; text-align: center; cursor: pointer; border-bottom: 1px dashed var(--text-color); }
        .sub-menu div:last-child { border-bottom: none; }
        .sub-menu div:hover { background: var(--bg-color); }

        .load-more-btn {
            background-color: #fffbf7; border: var(--border-pixel); width: 100%; padding: 16px;
            text-align: center; font-size: 11px; cursor: pointer; margin: 15px 0 50px 0;
            box-shadow: 4px 4px 0px rgba(0,0,0,0.15); display: block; user-select: none;
            transition: transform 0.1s ease, box-shadow 0.1s ease;
        }
        .load-more-btn:hover { transform: translate(2px, 2px); box-shadow: 2px 2px 0px rgba(0,0,0,0.15); background-color: var(--white); }
        
        .bounce-in-active { animation: smoothIn 0.35s cubic-bezier(0.175, 0.885, 0.32, 1.25) forwards; }
        @keyframes smoothIn {
            0% { transform: scale(0.85); opacity: 0; }
            100% { transform: scale(1); opacity: 1; }
        }
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
        <div class="section-title fresh-title">СВЕЖАЯ ИНФОРМАЦИЯ: НОВОЕ!</div>
        <div id="fresh-container">
            {% for entry in fresh_games %}
            <div class="game-card bounce-in-active" style="opacity: 1; transform: scale(1);">
                <div class="game-img-wrapper">
                    <img src="{{ entry.game.img }}" onerror="this.parentNode.style.background='#7a7a7a'">
                </div>
                <div class="game-info">
                    <div class="game-title" title="{{ entry.game.name }}">{{ entry.game.name }}</div>
                    <div class="game-details">
                        <span class="badge fresh-tag">НОВОЕ!</span>
                        <span class="badge discount">{{ entry.game.discount }}</span>
                        <span class="badge price {% if entry.game.price == 'FREE' %}price-free{% endif %}">{{ entry.game.price }}</span>
                    </div>
                    <div class="dots-menu-btn" onclick="toggleCardMenu(event, 'fresh-{{ entry.game.id }}')">
                        <span></span><span></span><span></span>
                    </div>
                    <div class="card-context-menu" id="menu-fresh-{{ entry.game.id }}">
                        <div class="menu-item" onclick="window.open('https://store.steampowered.com/app//{{ entry.game.id }}', '_blank')">Открыть Steam</div>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        {% endif %}

        <div id="default-game-sections">
            <div class="section-title">Дешёвые игры - скидки (50 игр)</div>
            <div id="discounts-container"></div>
            <div id="discounts-btn" class="load-more-btn bounce-in-active" onclick="handleLoadMore('discounts')">Ещё?</div>

            <div class="section-title">Топ бесплатных сингл игр</div>
            <div id="free_single-container"></div>
            <div id="free_single-btn" class="load-more-btn bounce-in-active" onclick="handleLoadMore('free_single')">Ещё?</div>

            <div class="section-title">Игры со скидками для друзей</div>
            <div id="coop_disc-container"></div>
            <div id="coop_disc-btn" class="load-more-btn bounce-in-active" onclick="handleLoadMore('coop_disc')">Ещё?</div>

            <div class="section-title">Бесплатные игры с другом</div>
            <div id="coop_free-container"></div>
            <div id="coop_free-btn" class="load-more-btn bounce-in-active" onclick="handleLoadMore('coop_free')">Ещё?</div>

            <div class="section-title">Можно получить бесплатно скоро</div>
            <div id="upcoming-container"></div>
            <div id="upcoming-btn" class="load-more-btn bounce-in-active" onclick="handleLoadMore('upcoming')">Ещё?</div>
        </div>
    </div>

    <script>
        const rawData = {{ data_json | safe }};
        const currentGlobalCurrency = "{{ current_currency }}";
        
        const state = {
            discounts: { data: rawData.discounts || [], index: 0, step: 3, limit: 50 },
            free_single: { data: rawData.free_single || [], index: 0, step: 3, limit: 14 },
            coop_disc: { data: rawData.coop_disc || [], index: 0, step: 3, limit: 10 },
            coop_free: { data: rawData.coop_free || [], index: 0, step: 3, limit: 10 },
            upcoming: { data: rawData.upcoming || [], index: 0, step: 3, limit: 14 }
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
            const urlParams = new URLSearchParams(window.location.search);
            urlParams.set('override_' + appId, targetCurr);
            if(!urlParams.has('currency')) urlParams.set('currency', currentGlobalCurrency);
            window.location.href = window.location.pathname + '?' + urlParams.toString();
        }

        window.addEventListener('click', function() {
            document.getElementById('currency-wrapper').classList.remove('open');
            document.querySelectorAll('.card-context-menu').forEach(m => m.classList.remove('open'));
            document.querySelectorAll('.sub-menu').forEach(s => s.classList.remove('open'));
            document.querySelectorAll('.game-card').forEach(c => c.classList.remove('active-card'));
        });

        function createCard(game) {
            const card = document.createElement('div');
            card.className = 'game-card';
            card.dataset.id = game.id;
            
            let tagsHtml = '';
            if(Array.isArray(game.tags)) {
                game.tags.slice(0, 2).forEach(t => { tagsHtml += `<span class="badge">${t}</span>`; });
            }

            let priceClass = game.price === "FREE" ? "price price-free" : "price";

            card.innerHTML = `
                <div class="game-img-wrapper">
                    <img src="${game.img}" alt="img" onerror="this.parentNode.style.background='#7a7a7a'">
                </div>
                <div class="game-info">
                    <div class="game-title" title="${game.name}">${game.name}</div>
                    <div class="game-details">
                        <span class="badge discount">${game.discount}</span>
                        <span class="badge ${priceClass}">${game.price}</span>
                        ${tagsHtml}
                    </div>
                    <div class="dots-menu-btn" onclick="toggleCardMenu(event, '${game.id}')">
                        <span></span><span></span><span></span>
                    </div>
                    <div class="card-context-menu" id="menu-${game.id}">
                        <div class="menu-item" onclick="window.open('https://store.steampowered.com/app/${game.id}', '_blank')">Открыть Steam</div>
                        <div class="menu-item" onclick="toggleSubMenu(event, '${game.id}')">Регион цены ▾</div>
                        <div class="sub-menu" id="submenu-${game.id}">
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
                setTimeout(() => {
                    const card = createCard(game);
                    container.appendChild(card);
                    observer.observe(card);
                }, idx * 60); 
            });

            const remaining = Math.min(section.limit, section.data.length) - section.index;
            if (remaining > 0) {
                btn.style.display = 'block';
                btn.innerText = `Ещё? (Осталось: ${remaining})`;
                btn.classList.add('bounce-in-active'); 
            } else {
                btn.style.display = 'none';
            }
        }

        function handleLoadMore(key) {
            const btn = document.getElementById(key + '-btn');
            btn.classList.remove('bounce-in-active');
            void btn.offsetWidth; 
            renderSection(key);
        }

        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if(entry.isIntersecting) {
                    entry.target.classList.add('bounce-in-active');
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: 0.05 });

        window.onload = () => { Object.keys(state).forEach(key => renderSection(key)); };
    </script>
</body>
</html>""" # Скопируй сюда полностью строковый HTML шаблон из твоего вопроса

@app.route("/")
def index():
    global_currency = request.args.get("currency", "EUR")
    if global_currency not in REGIONS:
        global_currency = "EUR"
        
    if STEAM_DATA_CACHE[global_currency] is None:
        data = get_steam_data(global_currency)
        STEAM_DATA_CACHE[global_currency] = data
    else:
        data = STEAM_DATA_CACHE[global_currency]

    return render_template_string(
        HTML_TEMPLATE, 
        data_json=json.dumps(data), 
        current_currency=global_currency,
        fresh_games=HOT_FRESH_GAMES
    )
# ==================== ОБРАБОТКА КОМАНДЫ /START (WEBHOOK) ====================
@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    try:
        # Проверяем, что запрос пришел именно в формате JSON
        update = request.get_json(silent=True)
        if not update or "message" not in update:
            return "OK", 200
        
        message = update["message"]
        text = message.get("text", "")
        chat_id = message["chat"]["id"]

        # Если пользователь пишет /start
        if text.startswith("/start"):
            host_url = request.host_url.rstrip('/')
            
            welcome_text = (
                "Привет! Я бот- скрытых скидок и халявы в Steam.\n\n"
                "чтобы открыть наше приложение и посмотреть весь список игр!"
            )
            
            reply_markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "🎮 Открыть Web App",
                            "web_app": {"url": host_url}
                        }
                    ]
                ]
            }
            
            requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": welcome_text,
                    "reply_markup": reply_markup
                },
                timeout=10
            )
            
    except Exception as e:
        print(f"[Webhook] Ошибка обработки апдейта: {e}")
        
    return "OK", 200

def set_telegram_webhook():
    """Автоматическая привязка вебхука к Railway при запуске"""
    time.sleep(5) # Даем Flask загрузиться
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_domain:
        # Теперь путь фиксированный — /telegram-webhook
        webhook_url = f"https://{railway_domain}/telegram-webhook"
        print(f"[Webhook] Попытка установить вебхук на: {webhook_url}")
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setWebhook",
                json={"url": webhook_url},
                timeout=10
            )
            print(f"[Webhook] Ответ Telegram: {res.json()}")
        except Exception as e:
            print(f"[Webhook] Не удалось установить вебхук: {e}")
    else:
        print("[Webhook] Предупреждение: Переменная RAILWAY_PUBLIC_DOMAIN не найдена.")
# ============================================================================

if __name__ == "__main__":
    # ==================== ОБРАБОТКА КОМАНДЫ /START (WEBHOOK) ====================
# ============================================================================

    download_pixel_font()
    
    threading.Thread(target=steam_cache_worker, daemon=True).start()
    threading.Thread(target=telegram_scheduler_worker, daemon=True).start()
    
    
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
