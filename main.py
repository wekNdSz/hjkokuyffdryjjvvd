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
HTML_TEMPLATE = """...""" # Скопируй сюда полностью строковый HTML шаблон из твоего вопроса

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

if __name__ == "__main__":
    download_pixel_font()
    
    threading.Thread(target=steam_cache_worker, daemon=True).start()
    threading.Thread(target=telegram_scheduler_worker, daemon=True).start()
    
    
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
