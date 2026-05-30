import os
import json
import logging
import asyncio
import httpx
import base64
import sqlite3
import tempfile
import feedparser
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger(__name__).setLevel(logging.INFO)

TELEGRAM_TOKEN = os.getenv("CARD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID = int(os.getenv("LILU_CHAT_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/tmp/freelance.db")
USDT_WALLET = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

user_sessions = {}

import random

HEADERS_LIST = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15", "Accept-Language": "en-US,en;q=0.9"},
    {"User-Agent": "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)", "Accept-Language": "en-US,en;q=0.9"},
]
HEADERS = HEADERS_LIST[0]

def get_headers():
    return random.choice(HEADERS_LIST)

CARD_RSS_FEEDS = [
    ("https://www.fl.ru/rss/all.xml", "🇷🇺 FL.ru"),
    ("https://www.fl.ru/rss/all.xml?category=3", "🇷🇺 FL.ru/Тексты"),
    ("https://www.fl.ru/rss/all.xml?category=21", "🇷🇺 FL.ru/Переводы"),
    ("https://problogger.com/jobs/feed/", "🌍 ProBlogger"),
    ("https://weworkremotely.com/remote-jobs.rss", "🌍 WWR"),
]

TG_CARD_CHANNELS = [
    "wb_help",
    "ozon_sellers_club",
    "kopiraiting_ru",
    "freelance_ru",
]

CARD_KEYWORDS = [
    "карточка товара", "карточки товаров", "описание товара", "описание продукта",
    "wildberries", "вайлдберриз", "wb ", " вб ", "ozon", "озон",
    "яндекс маркет", "маркетплейс", "маркетплейсов",
    "rich контент", "инфографика товар",
    "наполнение карточек", "написать карточку", "заполнить карточку",
    "seo описание", "продающее описание",
    "написать текст", "написать статью", "написать описание",
    "копирайтинг", "копирайтер", "контент для",
    "текст для сайта", "тексты для", "наполнение сайта",
    "продающий текст", "рекламный текст",
    "статья", "пост для", "посты для",
    "перевод", "перевести",
    "редактура", "корректура",
    "product description", "marketplace content", "amazon listing",
    "product listing", "ecommerce", "product copywriting",
    "amazon seo", "etsy listing", "shopify product",
    "content writing", "copywriting", "article writing",
    "blog post", "translation", "proofreading",
]

CARD_BLACKLIST = [
    "разработка сайта", "программирование", "верстка", "дизайн логотип",
    "видеомонтаж", "анимация", "таргет", "реклама настройка",
    "мобильное приложение", "android", "ios",
    "допечатная", "раскладка элементов", "фотозона", "широкоформатная печать",
    "indesign", "illustrator", "photoshop макет",
    "чертёж", "чертеж", "чертежник", "конструктор", "autocad",
    "solidworks", "компас", "проектирование",
    "написать работу", "курсовая", "дипломная", "реферат",
    "контрольная работа", "решить задачи по",
    "купить и отправить", "купить в городе", "забрать и привезти",
    "доставить", "курьер", "съездить", "поехать",
    "отправить посылку", "пвз", "cdek", "сдэк",
    "купить книги", "купить товар", "найти и купить",
]

# ═══ БАЗА ДАННЫХ ═══

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, title TEXT, description TEXT,
        budget TEXT, url TEXT, source TEXT,
        status TEXT DEFAULT 'found', result TEXT,
        created_at TEXT, updated_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS seen_jobs (url TEXT PRIMARY KEY, seen_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS earnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT, amount_usd REAL, amount_rub REAL,
        date TEXT, description TEXT
    )''')
    conn.commit()
    conn.close()

def save_job(job):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO jobs
        (id, title, description, budget, url, source, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (job['id'], job['title'], job['description'], job['budget'],
         job['url'], job['source'], job['status'],
         job['created_at'], job['updated_at']))
    conn.commit()
    conn.close()

def update_job(job_id, status, result=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if result:
        c.execute('UPDATE jobs SET status=?, result=?, updated_at=? WHERE id=?',
                  (status, result, datetime.now().isoformat(), job_id))
    else:
        c.execute('UPDATE jobs SET status=?, updated_at=? WHERE id=?',
                  (status, datetime.now().isoformat(), job_id))
    conn.commit()
    conn.close()

def get_job(job_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id,title,description,budget,url,source,status,result FROM jobs WHERE id=?', (job_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(zip(['id','title','description','budget','url','source','status','result'], row))
    return None

def is_seen(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT 1 FROM seen_jobs WHERE url=?', (url,))
    r = c.fetchone()
    conn.close()
    return r is not None

def mark_seen(url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO seen_jobs (url, seen_at) VALUES (?, ?)',
              (url, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def save_earning(job_id, amount_usd, amount_rub, description):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO earnings (job_id, amount_usd, amount_rub, date, description) VALUES (?, ?, ?, ?, ?)',
              (job_id, amount_usd, amount_rub, datetime.now().isoformat(), description))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status')
    by_status = dict(c.fetchall())
    c.execute('SELECT COALESCE(SUM(amount_usd),0), COALESCE(SUM(amount_rub),0), COUNT(*) FROM earnings')
    earn = c.fetchone()
    conn.close()
    return {'by_status': by_status, 'earn_usd': earn[0], 'earn_rub': earn[1], 'earn_count': earn[2]}

def clean_html(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;|&amp;|&lt;|&gt;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def make_id(url):
    return str(abs(hash(url)) % (10**12))

def is_card_job(title, desc):
    text = (title + " " + desc).lower()
    for bad in CARD_BLACKLIST:
        if bad in text:
            logger.debug(f"❌ Чёрный список '{bad}': {title[:50]}")
            return False
    result = any(kw in text for kw in CARD_KEYWORDS)
    if not result:
        logger.info(f"⚠️ Не прошёл фильтр: {title[:60]}")
    return result

# ═══ ПАРСЕРЫ ═══

async def parse_card_jobs(client) -> list:
    jobs = []
    for url, source in CARD_RSS_FEEDS:
        try:
            headers = get_headers()
            headers['Accept'] = 'application/rss+xml,application/xml,text/xml,*/*'
            r = await client.get(url, headers=headers, timeout=15)
            logger.info(f"🛍️ {source}: {r.status_code}")
            if r.status_code != 200:
                continue
            feed = feedparser.parse(r.text)
            if not feed.entries:
                logger.info(f"{source}: пустой фид")
                continue
            logger.info(f"{source}: {len(feed.entries)} записей")
            for e in feed.entries[:10]:
                link = e.get('link', '')
                if not link or is_seen(link):
                    continue
                title = clean_html(e.get('title', ''))
                desc = clean_html(e.get('summary', e.get('description', '')))
                budget_m = re.search(r'[\$₽€]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|\$|₽)', desc + title)
                budget = budget_m.group(0).strip() if budget_m else "Договорная"
                if is_card_job(title, desc):
                    jobs.append({
                        'id': make_id(link), 'title': title[:200],
                        'description': desc[:1200], 'budget': budget,
                        'url': link, 'source': f'🛍️ {source}',
                        'status': 'found',
                        'created_at': datetime.now().isoformat(),
                        'updated_at': datetime.now().isoformat()
                    })
                    mark_seen(link)
        except Exception as e:
            logger.error(f"❌ {source}: {e}")
    logger.info(f"🛍️ Карточных заказов всего: {len(jobs)}")
    return jobs

async def parse_tg_card_channels(client) -> list:
    jobs = []
    for channel in TG_CARD_CHANNELS:
        try:
            headers = get_headers()
            r = await client.get(f"https://t.me/s/{channel}", headers=headers, timeout=15)
            logger.info(f"📱 TG @{channel}: {r.status_code}")
            if r.status_code != 200:
                continue
            posts = re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', r.text, re.DOTALL)
            for post_html in posts[:5]:
                text = clean_html(post_html)
                if len(text) < 40 or not is_card_job(text, ""):
                    continue
                post_url = f"https://t.me/{channel}/p_{abs(hash(text)) % 100000}"
                if is_seen(post_url):
                    continue
                budget_m = re.search(r'[\$₽]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|\$|₽)', text)
                budget = budget_m.group(0).strip() if budget_m else "Договорная"
                jobs.append({
                    'id': make_id(post_url),
                    'title': text[:60] + "...",
                    'description': text[:1000], 'budget': budget,
                    'url': f"https://t.me/{channel}",
                    'source': f'📱 TG @{channel}',
                    'status': 'found',
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
                mark_seen(post_url)
        except Exception as e:
            logger.error(f"❌ TG {channel}: {e}")
    return jobs

# ═══ AI ПРОМПТЫ ═══

MARKETPLACE_PROMPTS = {
    "wb": """Создай продающую карточку для Wildberries. Верни ТОЛЬКО JSON:
{"title": "заголовок до 100 символов", "description": "описание 500-1000 символов", "characteristics": ["характеристика 1", "характеристика 2"], "keywords": "ключевые слова через запятую", "seo_tips": "совет"}""",
    "ozon": """Создай карточку для Ozon. Верни ТОЛЬКО JSON:
{"title": "название до 200 символов", "description": "описание 1000-3000 символов", "rich_content": [{"heading": "Заголовок", "text": "Текст"}], "attributes": ["атрибут 1", "атрибут 2"], "keywords": "ключевые слова"}""",
    "ym": """Создай карточку для Яндекс Маркет. Верни ТОЛЬКО JSON:
{"title": "точное название", "description": "описание до 3000 символов", "specs": {"Параметр": "Значение"}, "tags": ["тег 1", "тег 2"], "category_tips": "совет"}""",
}

# ═══ СТИЛИ ИЗОБРАЖЕНИЙ ═══

IMAGE_STYLES = {
    "studio": {
        "name": "🤍 Студийный",
        "desc": "Белый фон, профессиональная съёмка",
        "prompt": "professional product photo, pure white background, studio lighting, sharp focus, commercial photography, 4k quality",
        "negative": "text, watermark, people, hands, shadow, dark background, blurry"
    },
    "lifestyle": {
        "name": "🌆 Lifestyle",
        "desc": "Товар в жизни — высокая кликабельность",
        "prompt": "lifestyle product photo, beautiful interior background, natural light, cozy atmosphere, instagram style, editorial photography",
        "negative": "text, watermark, blurry, ugly, deformed"
    },
    "hype": {
        "name": "🔥 Hype",
        "desc": "Яркий, молодёжный, цепляющий взгляд",
        "prompt": "hype product photo, vibrant neon background, dynamic lighting, bold colors, streetwear aesthetic, trendy, eye-catching",
        "negative": "text, watermark, boring, dull, white background"
    },
    "natural": {
        "name": "🌿 Natural",
        "desc": "Природный фон — для эко-товаров",
        "prompt": "product photo on natural background, wood texture, green plants, eco friendly aesthetic, soft natural lighting, organic feel",
        "negative": "text, watermark, artificial, neon, dark"
    },
    "closeup": {
        "name": "📱 Макро",
        "desc": "Крупный план — детали и текстура",
        "prompt": "extreme close-up product photo, macro photography, sharp details, texture visible, bokeh background, professional macro lens",
        "negative": "text, watermark, full body shot, distant, blurry subject"
    }
}

# ═══ ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ (ИСПРАВЛЕНО) ═══

async def generate_product_image(product_name: str, style_key: str = "studio") -> bytes:
    """Генерирует фото товара через Gemini, при ошибке — через Pillow"""
    style = IMAGE_STYLES.get(style_key, IMAGE_STYLES["studio"])
    full_prompt = (
        f"Create a professional marketplace product photo of: {product_name}. "
        f"Style: {style['prompt']}. "
        f"High quality, commercial photography, ready for marketplace listing. "
        f"Do NOT include: {style['negative']}."
    )

    # Актуальные модели Gemini для генерации изображений (2026)
    GEMINI_MODELS = [
        "gemini-2.5-flash-preview-05-20",        # ✅ актуальная flash
        "gemini-2.0-flash-preview-image-generation",  # запасная
        "gemini-2.5-flash",                       # ещё один вариант
    ]

    if GEMINI_API_KEY:
        for model in GEMINI_MODELS:
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    payload = {
                        "contents": [{
                            "parts": [{"text": full_prompt}]
                        }],
                        "generationConfig": {
                            "responseModalities": ["TEXT", "IMAGE"]
                        }
                    }
                    r = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                        headers={"Content-Type": "application/json"},
                        json=payload
                    )
                    logger.info(f"Gemini [{model}] статус: {r.status_code}")
                    if r.status_code == 200:
                        data = r.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            for part in parts:
                                if "inlineData" in part:
                                    img_data = part["inlineData"].get("data", "")
                                    if img_data:
                                        logger.info(f"✅ Gemini [{model}] — изображение получено!")
                                        return base64.b64decode(img_data)
                        logger.warning(f"Gemini [{model}] — ответ 200 но нет картинки: {str(data)[:300]}")
                    else:
                        logger.error(f"Gemini [{model}] ошибка: {r.status_code} — {r.text[:300]}")
            except Exception as e:
                logger.error(f"Gemini [{model}] исключение: {e}")

    # Резерв — Pillow инфографика
    logger.info("🎨 Gemini недоступен — генерирую через Pillow")
    return generate_pillow_card(product_name, style_key)


def generate_pillow_card(product_name: str, style_key: str = "studio") -> bytes:
    """Генерирует красивую инфографику через Pillow"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import io

        styles_colors = {
            "studio":    ("#FFFFFF", "#1a1a2e", "#4361ee"),
            "hype":      ("#0d0d0d", "#ff006e", "#8338ec"),
            "lifestyle": ("#f8f4f0", "#2d3436", "#e17055"),
            "natural":   ("#f0f7ee", "#2d6a4f", "#40916c"),
            "closeup":   ("#1a1a2e", "#ffffff", "#4cc9f0"),
        }
        bg_color, text_color, accent = styles_colors.get(style_key, styles_colors["studio"])

        W, H = 800, 800
        img = Image.new('RGB', (W, H), color=bg_color)
        draw = ImageDraw.Draw(img)

        acc_r = int(accent[1:3], 16)
        acc_g = int(accent[3:5], 16)
        acc_b = int(accent[5:7], 16)
        accent_rgb = (acc_r, acc_g, acc_b)

        txt_r = int(text_color[1:3], 16) if text_color.startswith('#') else 30
        txt_g = int(text_color[3:5], 16) if text_color.startswith('#') else 30
        txt_b = int(text_color[5:7], 16) if text_color.startswith('#') else 30
        text_rgb = (txt_r, txt_g, txt_b)

        # Верхняя полоса
        draw.rectangle([0, 0, W, 10], fill=accent_rgb)

        # Акцентный прямоугольник фона для фото (центр)
        draw.rectangle([80, 80, W-80, H-200], fill=(
            min(acc_r+180, 255),
            min(acc_g+180, 255),
            min(acc_b+180, 255)
        ))
        draw.rectangle([80, 80, W-80, H-200], outline=accent_rgb, width=3)

        # Большой текст товара в центре карточки
        style_info = IMAGE_STYLES.get(style_key, IMAGE_STYLES["studio"])
        center_text = product_name.upper()

        # Разбиваем на строки по 20 символов
        words = center_text.split()
        lines = []
        line = ""
        for word in words:
            if len(line + " " + word) <= 20:
                line = (line + " " + word).strip()
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)

        # Рисуем название товара
        y_start = 200
        for i, ln in enumerate(lines[:4]):
            draw.text((W//2, y_start + i*60), ln, anchor="mm", fill=accent_rgb)

        # Иконка маркетплейса (условная)
        draw.text((W//2, 420), "🛒", anchor="mm", fill=text_rgb)

        # Бейджи снизу блока
        badge_y = H - 185
        badges = ["✅ SEO", "✅ Ключевые слова", "✅ Rich-контент"]
        for i, badge in enumerate(badges):
            bx = 120 + i * 200
            draw.rectangle([bx-60, badge_y-18, bx+140, badge_y+18], fill=accent_rgb)
            draw.text((bx+40, badge_y), badge, anchor="mm", fill=(255,255,255))

        # Нижняя панель
        draw.rectangle([0, H-160, W, H], fill=accent_rgb)

        # Стиль
        draw.text((W//2, H-130), style_info["name"], anchor="mm", fill=(255,255,255))
        draw.text((W//2, H-95), style_info["desc"], anchor="mm", fill=(220,220,220))

        # Маркетплейсы
        draw.text((W//2, H-55), "Wildberries  •  Ozon  •  Яндекс Маркет", anchor="mm", fill=(255,255,255))
        draw.text((W//2, H-25), "Готово для загрузки на маркетплейс!", anchor="mm", fill=(200,255,200))

        buf = io.BytesIO()
        img.save(buf, format='PNG', quality=95)
        buf.seek(0)
        return buf.read()

    except Exception as e:
        logger.error(f"Pillow ошибка: {e}")
        return None

# ═══ ГЕНЕРАЦИЯ КАРТОЧЕК ═══

async def generate_card(product: str, marketplace: str, image_base64: str = None) -> dict:
    if marketplace == "all":
        results = {}
        for mp in ["wb", "ozon", "ym"]:
            results[mp] = await generate_single(product, mp, image_base64)
        return results
    return await generate_single(product, marketplace, image_base64)

async def generate_single(product: str, marketplace: str, image_base64: str = None) -> dict:
    prompt = f"ТОВАР: {product}\n\n{MARKETPLACE_PROMPTS[marketplace]}"
    if image_base64:
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
            {"type": "text", "text": f"Это фото товара.\n\n{MARKETPLACE_PROMPTS[marketplace]}"}
        ]}]
        model = "meta-llama/llama-4-scout-17b-16e-instruct"
    else:
        messages = [{"role": "user", "content": prompt}]
        model = "llama-3.3-70b-versatile"

    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": 1500, "temperature": 0.8}
        )
        text = r.json()["choices"][0]["message"]["content"].strip()

    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    else:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1:
            text = text[start:end+1]
    return json.loads(text)

async def execute_card_job(job: dict) -> str:
    prompt = f"""Выполни заказ на написание карточек товаров профессионально.

ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:800]}

Создай карточку товара для Wildberries и Ozon:
- Продающий заголовок
- SEO описание с ключевыми словами
- Характеристики
- Ключевые слова для поиска

Отвечай на русском языке."""

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 2000}
        )
        return r.json()["choices"][0]["message"]["content"].strip()

async def analyze_card_job(job: dict) -> dict:
    prompt = f"""Оцени заказ на написание карточек товаров. Ответь ТОЛЬКО JSON:

ЗАКАЗ: {job['title']}
ОПИСАНИЕ: {job['description'][:400]}
БЮДЖЕТ: {job['budget']}

{{"can_do": true, "difficulty": "ЛЁГКИЙ", "reason": "одно предложение", "proposal": "proposal на языке заказа 3 предложения", "estimated_time": "1 час"}}"""

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 400}
        )
        text = r.json()["choices"][0]["message"]["content"].strip()
        if "```" in text:
            text = text.split("```")[1].split("```")[0].replace("json","").strip()
        return json.loads(text)

# ═══ ФОРМАТИРОВАНИЕ ═══

def format_card(data: dict, marketplace: str) -> str:
    if marketplace == "wb":
        chars = "\n".join([f" • {c}" for c in data.get('characteristics', [])])
        return f"🟣 *WILDBERRIES*\n\n📌 *Заголовок:*\n`{data.get('title','')}`\n\n📝 *Описание:*\n{data.get('description','')}\n\n📋 *Характеристики:*\n{chars}\n\n🔍 *Ключевые слова:*\n`{data.get('keywords','')}`\n\n💡 _{data.get('seo_tips','')}_"
    elif marketplace == "ozon":
        attrs = "\n".join([f" • {a}" for a in data.get('attributes', [])])
        rich = ""
        for s in data.get('rich_content', []):
            rich += f"\n*{s.get('heading','')}*\n{s.get('text','')}\n"
        return f"🔵 *OZON*\n\n📌 *Название:*\n`{data.get('title','')}`\n\n📝 *Описание:*\n{data.get('description','')}\n\n🎨 *Rich-контент:*{rich}\n📋 *Атрибуты:*\n{attrs}\n\n🔍 `{data.get('keywords','')}`"
    else:
        specs = "\n".join([f" • {k}: {v}" for k, v in data.get('specs', {}).items()])
        tags = ", ".join(data.get('tags', []))
        return f"🟡 *ЯНДЕКС МАРКЕТ*\n\n📌 *Название:*\n`{data.get('title','')}`\n\n📝 *Описание:*\n{data.get('description','')}\n\n⚙️ *Характеристики:*\n{specs}\n\n🏷️ `{tags}`\n\n💡 _{data.get('category_tips','')}_"

# ═══ ОТПРАВКА ЗАКАЗА ═══

async def send_job_card(bot, job: dict, analysis: dict):
    diff_emoji = {"ЛЁГКИЙ": "🟢", "СРЕДНИЙ": "🟡", "СЛОЖНЫЙ": "🔴"}.get(analysis.get('difficulty',''), "⚪")
    msg = f"""🛍️ *ЗАКАЗ НА КАРТОЧКИ*

{job['source']}

📌 *{job['title'][:100]}*

💰 {job['budget']}
{diff_emoji} {analysis.get('difficulty','?')} · ⏱ {analysis.get('estimated_time','?')}

💬 _{analysis.get('reason','')}_

📝 *Proposal:*
{analysis.get('proposal','')[:400]}

🔗 [Открыть заказ]({job['url']})"""

    keyboard = [[
        InlineKeyboardButton("✅ Берём!", callback_data=f"take_{job['id']}"),
        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{job['id']}")
    ]]
    await bot.send_message(
        chat_id=YOUR_CHAT_ID, text=msg,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )

# ═══ ПРОВЕРКА ЗАКАЗОВ ═══

async def check_card_jobs(bot) -> int:
    count = 0
    async with httpx.AsyncClient() as client:
        jobs = await parse_card_jobs(client)
        jobs += await parse_tg_card_channels(client)
    for job in jobs:
        save_job(job)
        try:
            analysis = await analyze_card_job(job)
            if analysis.get('can_do', False):
                await send_job_card(bot, job, analysis)
                count += 1
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Ошибка анализа: {e}")
    return count

# ═══ КНОПКИ ═══

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("mp_"):
        marketplace = data[3:]
        user_sessions[user_id] = {"marketplace": marketplace, "step": "waiting_product"}
        names = {"wb": "🟣 Wildberries", "ozon": "🔵 Ozon", "ym": "🟡 Яндекс Маркет", "all": "🎯 Все"}
        await query.edit_message_text(
            f"{names.get(marketplace,'?')} выбран!\n\nОтправь название товара или фото:",
            parse_mode='Markdown'
        )

    elif data.startswith("gen_img_") or data.startswith("regen_img_"):
        parts = data.split("_", 3)
        style_key = parts[2] if len(parts) > 2 else "studio"
        product = parts[3] if len(parts) > 3 else ""
        style = IMAGE_STYLES.get(style_key, IMAGE_STYLES["studio"])
        await query.answer(f"🎨 Генерирую в стиле {style['name']}...")

        style_keyboard = []
        for sk, sd in IMAGE_STYLES.items():
            emoji = "✅" if sk == style_key else ""
            style_keyboard.append([InlineKeyboardButton(
                f"{emoji} {sd['name']}",
                callback_data=f"gen_img_{sk}_{product[:20]}"
            )])
        style_keyboard.append([InlineKeyboardButton(
            "🔄 Сгенерировать заново",
            callback_data=f"regen_img_{style_key}_{product[:20]}"
        )])

        try:
            await query.edit_message_text("⏳ Генерирую изображение, подожди...")
        except Exception:
            pass

        try:
            img_bytes = await generate_product_image(product, style_key)
            if img_bytes:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=img_bytes,
                    caption=(
                        f"🖼 *{style['name']}*\n"
                        f"_{style['desc']}_\n\n"
                        f"✅ Готово для загрузки на маркетплейс!\n\n"
                        f"💡 Выбери другой стиль:"
                    ),
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(style_keyboard)
                )
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ Не удалось сгенерировать изображение. Попробуй ещё раз.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"gen_img_{style_key}_{product[:20]}")
                    ]])
                )
        except Exception as e:
            logger.error(f"Ошибка генерации изображения: {e}")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ Ошибка: {str(e)[:100]}"
            )

    elif data.startswith("regen_"):
        parts = data.split("_", 2)
        mp = parts[1]
        product = parts[2] if len(parts) > 2 else ""
        await query.edit_message_text("⏳ Генерирую заново...")
        try:
            result = await generate_card(product, mp)
            await send_card_result(query.message, result, mp, product, context.bot)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {str(e)[:100]}")

    elif data == "pay_usdt":
        keyboard = [
            [InlineKeyboardButton("1 карточка / 1 listing — $5", callback_data="invoice_5")],
            [InlineKeyboardButton("5 карточек / 5 listings — $20", callback_data="invoice_20")],
            [InlineKeyboardButton("10 карточек / 10 listings — $35", callback_data="invoice_35")],
            [InlineKeyboardButton("50 карточек / 50 listings — $150", callback_data="invoice_150")],
            [InlineKeyboardButton("✏️ Своя сумма / Custom amount", callback_data="invoice_custom")],
        ]
        await query.edit_message_text(
            "💎 *Оплата в USDT*\n\nВыбери пакет:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "pay_stars":
        keyboard = [
            [InlineKeyboardButton("⭐ 50 Stars — 1 карточка", callback_data="stars_50")],
            [InlineKeyboardButton("⭐ 200 Stars — 5 карточек", callback_data="stars_200")],
            [InlineKeyboardButton("⭐ 350 Stars — 10 карточек", callback_data="stars_350")],
            [InlineKeyboardButton("⭐ 1500 Stars — 50 карточек", callback_data="stars_1500")],
        ]
        await query.edit_message_text(
            "⭐ *Telegram Stars*\n\n"
            "50 ⭐ = 1 карточка\n"
            "200 ⭐ = 5 карточек\n"
            "350 ⭐ = 10 карточек\n"
            "1500 ⭐ = 50 карточек\n\n"
            "Выбери пакет:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "pay_rub":
        await query.edit_message_text(
            "🇷🇺 *Оплата в рублях*\n\n"
            "Напиши что тебе нужно и я выставлю счёт:\n\n"
            "Пример: `/order 5 карточек для WB`\n\n"
            "Способы оплаты:\n"
            "• СБП / Перевод по номеру\n"
            "• ЮMoney\n"
            "• QIWI",
            parse_mode='Markdown'
        )

    elif data.startswith("invoice_"):
        amount_str = data[8:]
        if amount_str == "custom":
            context.user_data['awaiting_custom_amount'] = True
            await query.edit_message_text(
                "✏️ Напиши сумму в USD:\n\nПример: `25`",
                parse_mode='Markdown'
            )
        else:
            amount = float(amount_str)
            descriptions = {
                5: "1 product listing",
                20: "5 product listings",
                35: "10 product listings",
                150: "50 product listings"
            }
            desc = descriptions.get(amount, f"${amount} package")
            msg = (
                f"💎 *INVOICE / СЧЁТ*\n\n"
                f"📋 Service: *{desc}*\n"
                f"💰 Amount: *${amount:.2f} USDT*\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"📲 *Payment via Telegram @wallet:*\n\n"
                f"1️⃣ Open @wallet in Telegram\n"
                f"2️⃣ Tap Send → Crypto\n"
                f"3️⃣ Choose USDT TRC20\n"
                f"4️⃣ Paste address:\n"
                f"`{USDT_WALLET}`\n"
                f"5️⃣ Amount: `{amount}` USDT\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"⚡ After payment tap button below\n"
                f"🕐 Work starts within 5 minutes"
            )
            keyboard = [[
                InlineKeyboardButton("✅ I paid / Оплатил", callback_data=f"payment_confirm_{amount}"),
                InlineKeyboardButton("❌ Cancel", callback_data="payment_cancel")
            ]]
            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("payment_confirm_"):
        amount = float(data[16:])
        user = update.effective_user
        username = f"@{user.username}" if user.username else user.first_name
        await context.bot.send_message(
            chat_id=YOUR_CHAT_ID,
            text=(
                f"💰 *ОПЛАТА ПОЛУЧЕНА!*\n\n"
                f"👤 Клиент: {username}\n"
                f"💎 Сумма: ${amount:.2f} USDT\n"
                f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                f"⚡ Проверь кошелёк и начни работу!"
            ),
            parse_mode='Markdown'
        )
        await query.edit_message_text(
            f"✅ *Thank you! / Спасибо!*\n\n"
            f"Payment of ${amount:.2f} USDT confirmed.\n"
            f"Work starts within 5 minutes!\n\n"
            f"Оплата ${amount:.2f} USDT подтверждена.\n"
            f"Начинаем работу в течение 5 минут!\n\n"
            f"📱 Send your product info / Отправь данные товара",
            parse_mode='Markdown'
        )
        user_sessions[user.id] = {"step": "waiting_product", "marketplace": "all", "paid": True}

    elif data.startswith("stars_"):
        stars = int(data[6:])
        stars_map = {
            50:   ("1 карточка товара", "Профессиональная карточка для WB, Ozon, Amazon или другого маркетплейса"),
            200:  ("5 карточек товаров", "5 профессиональных карточек для любых маркетплейсов"),
            350:  ("10 карточек товаров", "10 профессиональных карточек — скидка 30%"),
            1500: ("50 карточек товаров", "50 профессиональных карточек — максимальная скидка 40%"),
        }
        title, description = stars_map.get(stars, ("Карточки товаров", "Профессиональные карточки"))
        await send_stars_invoice(update, context, stars, title, description)

    elif data.startswith("take_"):
        job_id = data[5:]
        job = get_job(job_id)
        if not job:
            await query.edit_message_text("❌ Заказ не найден")
            return
        update_job(job_id, 'accepted')
        await query.edit_message_text(
            f"✅ *Берём заказ на карточки!*\n📌 {job['title'][:80]}\n\n⏳ Выполняю...",
            parse_mode='Markdown'
        )
        try:
            result = await execute_card_job(job)
            update_job(job_id, 'completed', result)
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Правка", callback_data=f"redo_{job_id}")
            ]]
            msg = (
                f"✨ *КАРТОЧКИ ГОТОВЫ!*\n\n"
                f"📌 *{job['title'][:80]}*\n\n"
                f"━━━━━━━━━━\n{result[:2500]}\n━━━━━━━━━━\n\n"
                f"*Лила, проверь — отправляем?*"
            )
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID, text=msg,
                parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard)
            )
            if LILU_CHAT_ID and LILU_CHAT_ID != YOUR_CHAT_ID:
                await context.bot.send_message(
                    chat_id=LILU_CHAT_ID, text=msg,
                    parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard)
                )
        except Exception as e:
            await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=f"❌ Ошибка: {str(e)[:200]}")

    elif data.startswith("skip_"):
        update_job(data[5:], 'skipped')
        await query.edit_message_text("⏭ Пропустили")

    elif data.startswith("done_"):
        job_id = data[5:]
        job = get_job(job_id)
        update_job(job_id, 'done')
        nums = re.findall(r'\d+', job.get('budget','0').replace(' ',''))
        amount = float(nums[0]) if nums else 0
        is_rub = '₽' in job.get('budget','') or 'руб' in job.get('budget','').lower()
        if is_rub:
            save_earning(job_id, amount/90, amount, job['title'])
        else:
            save_earning(job_id, amount, amount*90, job['title'])
        stats = get_stats()
        await query.edit_message_text(
            f"💰 *ЗАКАЗ ЗАКРЫТ!*\n\n✅ Выполнено: {stats['by_status'].get('done',0)}\n💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n\nБухгалтер записал 📊",
            parse_mode='Markdown'
        )

    elif data.startswith("redo_"):
        job_id = data[5:]
        job = get_job(job_id)
        context.user_data['redo_job_id'] = job_id
        context.user_data['redo_result'] = job.get('result','') if job else ''
        await query.edit_message_text(
            "✏️ *Напиши что исправить:*\n\nНапример: _сократи_, _переведи на английский_, _добавь ключевые слова_",
            parse_mode='Markdown'
        )

async def send_card_result(message, result, marketplace, product, bot):
    if marketplace == "all":
        for mp, data in result.items():
            text = format_card(data, mp)
            await bot.send_message(chat_id=message.chat_id, text=text[:4000], parse_mode='Markdown')
            await asyncio.sleep(0.5)
        keyboard = [[
            InlineKeyboardButton("🔄 Заново", callback_data=f"regen_all_{product[:20]}"),
            InlineKeyboardButton("🔀 Другой", callback_data="change_mp_")
        ]]
        await bot.send_message(
            chat_id=message.chat_id, text="✅ *Все карточки готовы!*",
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        text = format_card(result, marketplace)
        keyboard = [[
            InlineKeyboardButton("🔄 Заново", callback_data=f"regen_{marketplace}_{product[:20]}"),
            InlineKeyboardButton("🔀 Другой", callback_data="change_mp_")
        ]]
        await bot.send_message(
            chat_id=message.chat_id, text=text[:4000],
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ═══ STARS INVOICE ═══

async def send_stars_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, stars: int, title: str, description: str):
    try:
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title=title,
            description=description,
            payload=f"card_order_{stars}_{update.effective_user.id}",
            currency="XTR",
            prices=[{"label": title, "amount": stars}],
            provider_token=""
        )
    except Exception as e:
        logger.error(f"Stars invoice ошибка: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⭐ Оплата {stars} Stars\n\nОтправь Stars напрямую в боте через кнопку ниже.",
        )

# ═══ ОБРАБОТЧИК СООБЩЕНИЙ ═══

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Обработка правки
    if context.user_data.get('redo_job_id'):
        job_id = context.user_data['redo_job_id']
        original = context.user_data.get('redo_result', '')
        fix = update.message.text
        job = get_job(job_id)
        await update.message.reply_text("⏳ Исправляю...")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": "llama-3.3-70b-versatile",
                          "messages": [{"role": "user", "content":
                              f"Исправь текст карточки товара согласно инструкции.\n\nОРИГИНАЛ:\n{original[:2000]}\n\nИНСТРУКЦИЯ: {fix}\n\nВерни исправленный текст полностью."}],
                          "max_tokens": 2000}
                )
                new_result = r.json()["choices"][0]["message"]["content"].strip()
            update_job(job_id, 'completed', new_result)
            context.user_data['redo_result'] = new_result
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Ещё правка", callback_data=f"redo_{job_id}")
            ]]
            msg = f"✨ *ИСПРАВЛЕНО!*\n\n━━━━━━━━━━\n{new_result[:2500]}\n━━━━━━━━━━\n\n*Лила, проверь — отправляем?*"
            await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
            if LILU_CHAT_ID and LILU_CHAT_ID != YOUR_CHAT_ID:
                await context.bot.send_message(chat_id=LILU_CHAT_ID, text=msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
            context.user_data.pop('redo_job_id', None)
            context.user_data.pop('redo_result', None)
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")
        return

    if user_id not in user_sessions or user_sessions[user_id].get('step') != 'waiting_product':
        keyboard = [[
            InlineKeyboardButton("🟣 WB", callback_data="mp_wb"),
            InlineKeyboardButton("🔵 Ozon", callback_data="mp_ozon"),
            InlineKeyboardButton("🟡 ЯМ", callback_data="mp_ym"),
            InlineKeyboardButton("🎯 Все", callback_data="mp_all"),
        ]]
        await update.message.reply_text("Сначала выбери маркетплейс 👇", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    marketplace = user_sessions[user_id]['marketplace']
    image_base64 = None
    product = ""

    if update.message.photo:
        await update.message.reply_text("📸 Генерирую карточку по фото...")
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await photo_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                image_base64 = base64.b64encode(f.read()).decode()
            os.unlink(tmp.name)
        product = update.message.caption or "товар на фото"
    elif update.message.text:
        product = update.message.text
        await update.message.reply_text("⏳ Генерирую карточку...")
    else:
        await update.message.reply_text("Отправь текст или фото товара!")
        return

    try:
        result = await generate_card(product, marketplace, image_base64)
        await send_card_result(update.message, result, marketplace, product, context.bot)

        if marketplace == "all":
            title = list(result.values())[0].get("title", product)
        else:
            title = result.get("title", product)

        keyboard = []
        for style_key, style_data in IMAGE_STYLES.items():
            keyboard.append([InlineKeyboardButton(
                f"{style_data['name']} — {style_data['desc']}",
                callback_data=f"gen_img_{style_key}_{title[:20]}"
            )])

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🎨 *Выбери стиль фото для маркетплейса:*",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        user_sessions[user_id] = {"step": "done"}

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await update.message.reply_text(f"❌ Ошибка. Попробуй ещё раз.\n`{str(e)[:100]}`", parse_mode='Markdown')

# ═══ КОМАНДЫ ═══

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🟣 Wildberries", callback_data="mp_wb"),
         InlineKeyboardButton("🔵 Ozon", callback_data="mp_ozon")],
        [InlineKeyboardButton("🟡 Яндекс Маркет", callback_data="mp_ym"),
         InlineKeyboardButton("🎯 Все сразу", callback_data="mp_all")],
    ]
    await update.message.reply_text(
        "🛍️ *КарточникБот*\n\n"
        "Генерирую карточки товаров + ищу заказы на биржах!\n\n"
        "Выбери маркетплейс для генерации:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Ищу заказы на карточки...")
    count = await check_card_jobs(context.application.bot)
    await msg.edit_text(
        f"✅ Найдено: {count}\n"
        f"{'Заказы летят! 🚀' if count > 0 else 'Пока 0 — попробуй /clear и снова /scan'}"
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM seen_jobs')
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Кэш очищен! Теперь /scan найдёт заказы заново.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    by_status = stats['by_status']
    text = (
        f"📊 *СТАТИСТИКА КАРТОЧНИКА*\n\n"
        f"🔍 Найдено: {by_status.get('found', 0)}\n"
        f"✅ Принято: {by_status.get('accepted', 0)}\n"
        f"✨ Выполнено: {by_status.get('completed', 0)}\n"
        f"💰 Закрыто: {by_status.get('done', 0)}\n"
        f"⏭ Пропущено: {by_status.get('skipped', 0)}\n\n"
        f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
        f"📦 Всего заказов: {stats['earn_count']}"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💎 Оплата USDT", callback_data="pay_usdt")],
        [InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay_stars")],
        [InlineKeyboardButton("🇷🇺 Рубли (СБП/ЮMoney)", callback_data="pay_rub")],
    ]
    await update.message.reply_text(
        "💰 *ПРАЙС — КАРТОЧКИ ТОВАРОВ*\n\n"
        "🛍️ *Wildberries / Ozon / ЯМ*\n\n"
        "🟢 *Эконом* — только текст\n"
        " • 1 карточка: $5 / 50⭐ / 400₽\n\n"
        "🔵 *Стандарт* — текст + SEO\n"
        " • 5 карточек: $20 / 200⭐\n\n"
        "🟣 *Бизнес* — текст + SEO + фото\n"
        " • 10 карточек: $35 / 350⭐\n\n"
        "🌍 *Amazon / Etsy / eBay*\n"
        " • от $8 за карточку\n\n"
        "Выбери способ оплаты:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ═══ АВТОСКАНИРОВАНИЕ ═══

async def auto_scan(context):
    logger.info("🔄 Автосканирование заказов...")
    try:
        count = await check_card_jobs(context.bot)
        logger.info(f"✅ Автосканирование: найдено {count} заказов")
    except Exception as e:
        logger.error(f"❌ Автосканирование ошибка: {e}")

# ═══ ЗАПУСК ═══

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))

    # Автосканирование каждые 30 минут
    app.job_queue.run_repeating(auto_scan, interval=1800, first=60)

    logger.info("🛍️ КарточникБот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
