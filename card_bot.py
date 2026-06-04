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
import io
import random
import subprocess
import pytz
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ═══
TELEGRAM_TOKEN    = os.getenv("CARD_BOT_TOKEN")
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
YOUR_CHAT_ID      = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID      = int(os.getenv("LILU_CHAT_ID", str(os.getenv("YOUR_CHAT_ID", "0"))))
LILU_BOT_TOKEN    = os.getenv("LILU_BOT_TOKEN", "")
DB_PATH           = os.getenv("DB_PATH", "/tmp/freelance.db")
USDT_WALLET       = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
KWORK_URL         = os.getenv("KWORK_URL", "https://kwork.ru/user/artem_sh")
AIDENTIKA_API_KEY = os.getenv("AIDENTIKA_API_KEY", "")
AIDENTIKA_BASE    = "https://api.aidentika.com/api/v1/public"

ANTHROPIC_HAIKU  = "claude-haiku-4-5-20251001"
ANTHROPIC_SONNET = "claude-sonnet-4-6"
ANTHROPIC_URL    = "https://api.anthropic.com/v1/messages"

# ═══ GROQ — РОТАЦИЯ МОДЕЛЕЙ (НОВОЕ) ═══
GROQ_URL    = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
    "mixtral-8x7b-32768",
]
_groq_model_index = 0

# ═══ ВРЕМЯ МСК (НОВОЕ) ═══
def msk_now() -> datetime:
    return datetime.now(pytz.timezone('Europe/Moscow'))

def msk_time_str() -> str:
    return msk_now().strftime("%d.%m.%Y %H:%M МСК")

async def groq_request_smart(messages, max_tokens=800):
    global _groq_model_index
    for attempt in range(len(GROQ_MODELS)):
        model = GROQ_MODELS[_groq_model_index]
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "max_tokens": max_tokens}
                )
                if r.status_code == 429:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"⚠️ Rate limit [{model}] → переключаю, жду {wait:.1f}с")
                    _groq_model_index = (_groq_model_index + 1) % len(GROQ_MODELS)
                    await asyncio.sleep(wait)
                    continue
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            _groq_model_index = (_groq_model_index + 1) % len(GROQ_MODELS)
            await asyncio.sleep(2)
    return "⚠️ Все модели временно недоступны."

user_sessions = {}

# ═══ VECTORIZE — JPG/PNG → SVG (НОВОЕ) ═══

async def vectorize_image(image_path: str) -> str:
    """Конвертирует растровое изображение в SVG через Inkscape"""
    output_path = image_path.rsplit('.', 1)[0] + '.svg'
    try:
        # Метод 1 — современный Inkscape
        result = subprocess.run([
            "inkscape",
            f"--export-filename={output_path}",
            "--export-plain-svg",
            image_path
        ], capture_output=True, timeout=60)
        if result.returncode == 0 and os.path.exists(output_path):
            return output_path
    except Exception as e:
        logger.error(f"Inkscape метод 1: {e}")
    try:
        # Метод 2 — старый синтаксис
        result = subprocess.run([
            "inkscape",
            image_path,
            "--export-plain-svg",
            output_path
        ], capture_output=True, timeout=60)
        if result.returncode == 0 and os.path.exists(output_path):
            return output_path
    except Exception as e:
        logger.error(f"Inkscape метод 2: {e}")
    return None

# ═══ AIDENTIKA API ═══

async def aidentika_upload(image_b64: str) -> str:
    headers = {"Authorization": f"Bearer {AIDENTIKA_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{AIDENTIKA_BASE}/upload", headers=headers,
                              json={"image": {"data": image_b64, "media_type": "image/jpeg"}})
        if r.status_code == 200:
            return r.json().get("upload_id", "")
        return ""

async def aidentika_generate_card(upload_id: str, product_name: str, features: str, style: str = "classic") -> str:
    headers = {"Authorization": f"Bearer {AIDENTIKA_API_KEY}", "Content-Type": "application/json"}
    payload = {"images": [{"data": upload_id}], "product_name": product_name,
                "user_text": features, "style": style, "aspect_ratio": "3:4"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{AIDENTIKA_BASE}/generate/card", headers=headers, json=payload)
        if r.status_code == 200:
            return str(r.json().get("action_id", ""))
        return ""

async def aidentika_check_status(action_id: str) -> dict:
    headers = {"Authorization": f"Bearer {AIDENTIKA_API_KEY}"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{AIDENTIKA_BASE}/status/{action_id}", headers=headers)
        if r.status_code == 200:
            return r.json()
        return {}

async def aidentika_download(action_id: str) -> bytes:
    headers = {"Authorization": f"Bearer {AIDENTIKA_API_KEY}"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(f"{AIDENTIKA_BASE}/results/{action_id}/download", headers=headers)
        if r.status_code == 200:
            return r.content
        return b""

async def aidentika_wait_and_download(action_id: str, max_wait: int = 120) -> bytes:
    await asyncio.sleep(20)
    waited = 20
    while waited < max_wait:
        status = await aidentika_check_status(action_id)
        if status.get("status") == "completed":
            return await aidentika_download(action_id)
        elif status.get("status") == "failed":
            return b""
        await asyncio.sleep(10)
        waited += 10
    return b""

async def aidentika_balance() -> int:
    headers = {"Authorization": f"Bearer {AIDENTIKA_API_KEY}"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{AIDENTIKA_BASE}/balance", headers=headers)
        if r.status_code == 200:
            return r.json().get("available", 0)
        return -1

# ═══ ANTHROPIC ХЕЛПЕР ═══

async def anthropic_request(messages, system="", model=None, max_tokens=1500, image_b64=None, image_media="image/jpeg") -> str:
    if model is None:
        model = ANTHROPIC_HAIKU
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    if image_b64 and messages:
        last = messages[-1]
        if isinstance(last.get("content"), str):
            messages[-1] = {"role": last["role"], "content": [
                {"type": "image", "source": {"type": "base64", "media_type": image_media, "data": image_b64}},
                {"type": "text", "text": last["content"]}
            ]}
    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        payload["system"] = system
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
        data = r.json()
        if "content" not in data:
            raise Exception(f"Anthropic error: {data}")
        return data["content"][0]["text"]

# ═══ СТИЛИ КАРТОЧЕК ═══

IMAGE_STYLES = {
    "studio":  {"name": "🤍 Студийный",  "desc": "Белый фон, профессиональная съёмка",
                "bg": (255,255,255), "accent": (67,97,238), "text": (20,20,40),
                "badge_bg": (67,97,238), "badge_text": (255,255,255)},
    "dark":    {"name": "🖤 Тёмный",     "desc": "Тёмный фон — премиум стиль",
                "bg": (18,18,30), "accent": (255,165,0), "text": (255,255,255),
                "badge_bg": (255,165,0), "badge_text": (20,20,20)},
    "hype":    {"name": "🔥 Hype",       "desc": "Яркий, молодёжный",
                "bg": (13,13,30), "accent": (255,0,110), "text": (255,255,255),
                "badge_bg": (131,56,236), "badge_text": (255,255,255)},
    "natural": {"name": "🌿 Natural",    "desc": "Природный — для эко-товаров",
                "bg": (240,247,238), "accent": (45,106,79), "text": (30,60,40),
                "badge_bg": (64,145,108), "badge_text": (255,255,255)},
    "warm":    {"name": "🧡 Тёплый",     "desc": "Бежевый — уют и доверие",
                "bg": (253,245,235), "accent": (180,90,30), "text": (60,30,10),
                "badge_bg": (210,120,50), "badge_text": (255,255,255)},
}

PACKAGE_PRICES = {
    "pack5":  {"cards": 5,  "usd": 18,  "rub": 1600, "stars": 180,  "desc": "5 карточек — скидка 10%"},
    "pack10": {"cards": 10, "usd": 30,  "rub": 2700, "stars": 300,  "desc": "10 карточек — скидка 25%"},
    "pack20": {"cards": 20, "usd": 50,  "rub": 4500, "stars": 500,  "desc": "20 карточек — скидка 37%"},
    "pack50": {"cards": 50, "usd": 100, "rub": 9000, "stars": 1000, "desc": "50 карточек — скидка 50%"},
}

CARD_RSS_FEEDS = [
    ("https://www.fl.ru/rss/all.xml", "🇷🇺 FL.ru"),
    ("https://www.fl.ru/rss/all.xml?category=3", "🇷🇺 FL.ru/Тексты"),
    ("https://www.fl.ru/rss/all.xml?category=21", "🇷🇺 FL.ru/Переводы"),
    ("https://problogger.com/jobs/feed/", "🌍 ProBlogger"),
    ("https://weworkremotely.com/remote-jobs.rss", "🌍 WWR"),
]

TG_CARD_CHANNELS = ["wb_help", "ozon_sellers_club", "kopiraiting_ru", "freelance_ru"]

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
    "перевод", "перевести", "редактура", "корректура",
    "product description", "marketplace content", "amazon listing",
    "product listing", "ecommerce", "product copywriting",
    "amazon seo", "etsy listing", "shopify product",
    "content writing", "copywriting", "article writing",
    "blog post", "translation", "proofreading",
    "векторизация", "векторизовать", "jpg в svg", "png в svg",
    "перевести в вектор", "сделать svg", "логотип в svg",
]

CARD_BLACKLIST = [
    "разработка сайта", "программирование", "верстка", "дизайн логотип",
    "видеомонтаж", "анимация", "таргет", "реклама настройка",
    "мобильное приложение", "android", "ios",
    "indesign", "illustrator", "photoshop макет",
    "чертёж", "чертеж", "чертежник", "autocad",
    "написать работу", "курсовая", "дипломная", "реферат",
    "купить и отправить", "купить в городе", "доставить", "курьер",
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
         job['url'], job['source'], job['status'], job['created_at'], job['updated_at']))
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
            return False
    return any(kw in text for kw in CARD_KEYWORDS)

def get_headers():
    return random.choice([
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0", "Accept-Language": "ru-RU,ru;q=0.9"},
        {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15", "Accept-Language": "en-US,en;q=0.9"},
    ])

# ═══ ПРОМПТЫ МАРКЕТПЛЕЙСОВ ═══

MARKETPLACE_PROMPTS = {
    "wb": """Создай продающую карточку для Wildberries. Верни ТОЛЬКО JSON:
{"title": "заголовок до 100 символов с ключевыми словами", "description": "продающее описание 500-800 символов", "characteristics": ["характеристика 1", "характеристика 2", "характеристика 3", "характеристика 4", "характеристика 5"], "keywords": "ключевые слова через запятую 10-15 штук", "seo_tips": "краткий SEO совет", "badges": ["✅ Быстрая доставка", "⭐ Топ продаж", "🎁 Гарантия качества"]}""",
    "ozon": """Создай карточку для Ozon. Верни ТОЛЬКО JSON:
{"title": "название до 200 символов", "description": "подробное описание 1000-2000 символов", "rich_content": [{"heading": "Преимущества", "text": "текст"}], "attributes": ["атрибут 1", "атрибут 2", "атрибут 3", "атрибут 4", "атрибут 5"], "keywords": "ключевые слова", "badges": ["✅ Оригинал", "🚀 Быстро", "💎 Качество"]}""",
    "ym": """Создай карточку для Яндекс Маркет. Верни ТОЛЬКО JSON:
{"title": "точное полное название товара", "description": "описание до 2000 символов", "specs": {"Материал": "значение", "Размер": "значение", "Цвет": "значение", "Вес": "значение"}, "tags": ["тег 1", "тег 2", "тег 3"], "category_tips": "совет по категории", "badges": ["✅ Сертифицировано", "🏆 Бестселлер", "🎯 Выгодно"]}""",
    "amazon": """Create an Amazon product listing. Return ONLY JSON:
{"title": "SEO title max 200 chars", "bullet_points": ["benefit 1", "benefit 2", "benefit 3", "benefit 4", "benefit 5"], "description": "detailed description 2000 chars", "keywords": "backend search terms", "badges": ["✅ Prime Ready", "⭐ Top Rated", "🎁 Gift Ready"]}""",
    "etsy": """Create an Etsy listing. Return ONLY JSON:
{"title": "handmade-focused title max 140 chars", "description": "story-driven description 2000 chars", "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8", "tag9", "tag10"], "materials": ["material1", "material2"], "badges": ["🤝 Handmade", "💚 Eco-friendly", "⭐ Custom Orders"]}""",
}

# ═══ GEMINI ФОТО ═══

async def generate_product_image_gemini(product_name: str, style_key: str = "studio") -> bytes | None:
    style  = IMAGE_STYLES.get(style_key, IMAGE_STYLES["studio"])
    prompt = (f"Create a professional product photo for marketplace listing. "
              f"Product: {product_name}. Style: clean commercial photography, {style['desc'].lower()}. "
              f"Show only the product, no people, no text. High quality e-commerce photo.")
    if not GEMINI_API_KEY:
        return None
    for model in ["gemini-2.5-flash-preview-05-20", "gemini-2.0-flash-preview-image-generation"]:
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                    headers={"Content-Type": "application/json"},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
                )
                if r.status_code == 200:
                    for part in r.json().get("candidates", [{}])[0].get("content", {}).get("parts", []):
                        if "inlineData" in part:
                            img_data = part["inlineData"].get("data", "")
                            if img_data:
                                return base64.b64decode(img_data)
        except Exception as e:
            logger.error(f"Gemini [{model}]: {e}")
    return None

# ═══ PILLOW ИНФОГРАФИКА ═══

def build_infographic(product_name, card_data, marketplace, style_key="studio", product_photo_bytes=None) -> bytes:
    from PIL import Image, ImageDraw
    import textwrap

    style = IMAGE_STYLES.get(style_key, IMAGE_STYLES["studio"])
    W, H  = 900, 1200
    img   = Image.new('RGB', (W, H), color=style['bg'])
    draw  = ImageDraw.Draw(img)
    bg, accent, txt_col = style['bg'], style['accent'], style['text']
    badge_bg, badge_txt = style['badge_bg'], style['badge_text']

    for y in range(180):
        alpha   = int(255 * (1 - y / 180))
        blended = tuple(int(min(accent[i]+40,255)*alpha/255 + bg[i]*(1-alpha/255)) for i in range(3))
        draw.line([(0, y), (W, y)], fill=blended)
    draw.rectangle([0, 0, W, 8], fill=accent)

    mp_labels = {"wb":"WILDBERRIES","ozon":"OZON","ym":"ЯНДЕКС МАРКЕТ","amazon":"AMAZON","etsy":"ETSY"}
    mp_colors = {"wb":(147,0,211),"ozon":(0,91,255),"ym":(255,204,0),"amazon":(255,153,0),"etsy":(235,94,40)}
    mp_color  = mp_colors.get(marketplace, accent)
    mp_txt_col = (255,255,255) if marketplace not in ("ym",) else (20,20,20)
    draw.rectangle([20, 18, 230, 52], fill=mp_color)
    draw.text((125, 35), mp_labels.get(marketplace, "МАРКЕТПЛЕЙС"), anchor="mm", fill=mp_txt_col)

    title = card_data.get("title", product_name)
    for i, line in enumerate(textwrap.wrap(title, width=28)[:3]):
        draw.text((W//2, 70 + i*38), line, anchor="mm", fill=txt_col)

    px0, py0, px1, py1 = 60, 185, W-60, 620
    if product_photo_bytes:
        try:
            prod_img = Image.open(io.BytesIO(product_photo_bytes)).convert("RGBA")
            prod_img.thumbnail((px1-px0, py1-py0), Image.LANCZOS)
            px = px0 + (px1-px0-prod_img.width)//2
            py = py0 + (py1-py0-prod_img.height)//2
            draw.rectangle([px0, py0, px1, py1], fill=(255,255,255) if style_key=="studio" else tuple(min(c+30,255) for c in bg))
            draw.rectangle([px0, py0, px1, py1], outline=accent, width=2)
            img.paste(prod_img, (px, py), prod_img if prod_img.mode=='RGBA' else None)
        except Exception as e:
            logger.error(f"Фото вставка: {e}")
            _placeholder(draw, px0, py0, px1, py1, accent, bg, product_name)
    else:
        _placeholder(draw, px0, py0, px1, py1, accent, bg, product_name)

    badges = card_data.get("badges", ["✅ Доставка", "⭐ Топ продаж", "🎁 Гарантия"])
    by = py1 + 15
    bw = (W-60) // len(badges[:3])
    for i, badge in enumerate(badges[:3]):
        bx0, bx1 = 30 + i*bw, 30 + i*bw + bw - 10
        draw.rectangle([bx0, by, bx1, by+36], fill=badge_bg)
        draw.text(((bx0+bx1)//2, by+18), str(badge)[:22], anchor="mm", fill=badge_txt)

    cy = by + 55
    draw.rectangle([30, cy-5, W-30, cy+2], fill=accent)
    cy += 15
    chars = []
    if marketplace == "wb":    chars = card_data.get("characteristics", [])
    elif marketplace == "ozon": chars = card_data.get("attributes", [])
    elif marketplace == "ym":   chars = [f"{k}: {v}" for k, v in card_data.get("specs", {}).items()]
    elif marketplace == "amazon": chars = card_data.get("bullet_points", [])
    elif marketplace == "etsy": chars = [f"Материал: {m}" for m in card_data.get("materials", [])] + card_data.get("tags", [])[:4]

    for i, char in enumerate(chars[:6]):
        row_bg = tuple(max(c-10,0) if i%2==0 else c for c in bg)
        draw.rectangle([30, cy-4, W-30, cy+28], fill=row_bg)
        draw.rectangle([30, cy+4, 36, cy+20], fill=accent)
        draw.text((50, cy+12), str(char)[:55], anchor="lm", fill=txt_col)
        cy += 38

    kw_y = max(cy+15, 920)
    kw   = card_data.get("keywords", "") or ", ".join(card_data.get("tags", [])[:5])
    if kw:
        draw.rectangle([30, kw_y, W-30, kw_y+35], fill=tuple(max(c-20,0) for c in bg))
        draw.text((W//2, kw_y+17), f"🔍 {str(kw)[:80]}", anchor="mm", fill=accent)

    draw.rectangle([0, H-110, W, H], fill=accent)
    desc = card_data.get("description", "")[:120].replace("\n", " ")
    for i, line in enumerate(__import__('textwrap').wrap(desc, width=60)[:2]):
        draw.text((W//2, H-90+i*28), line, anchor="mm", fill=(255,255,255))
    draw.text((W//2, H-25), "✅ SEO-оптимизировано  •  ✅ Готово для загрузки", anchor="mm", fill=(220,255,220))

    buf = io.BytesIO()
    img.save(buf, format='PNG', quality=95)
    buf.seek(0)
    return buf.read()

def _placeholder(draw, x0, y0, x1, y1, accent, bg, name):
    ph_bg = tuple(min(c+25,255) for c in bg)
    draw.rectangle([x0, y0, x1, y1], fill=ph_bg)
    draw.rectangle([x0, y0, x1, y1], outline=accent, width=2)
    cx, cy = (x0+x1)//2, (y0+y1)//2
    draw.text((cx, cy-40), "📦", anchor="mm", fill=accent)
    draw.text((cx, cy+20), name[:30], anchor="mm", fill=accent)
    draw.text((cx, cy+55), "Отправь фото товара", anchor="mm", fill=tuple(max(c-60,0) for c in accent))

async def generate_product_image(product_name, card_data, marketplace, style_key="studio", photo_bytes=None) -> bytes:
    if photo_bytes:
        return build_infographic(product_name, card_data, marketplace, style_key, photo_bytes)
    gemini_photo = await generate_product_image_gemini(product_name, style_key)
    return build_infographic(product_name, card_data, marketplace, style_key, gemini_photo)

# ═══ ГЕНЕРАЦИЯ КАРТОЧКИ ═══

async def generate_single(product: str, marketplace: str, image_b64: str = None) -> dict:
    prompt = f"ТОВАР: {product}\n\n{MARKETPLACE_PROMPTS[marketplace]}"
    text   = await anthropic_request(
        messages=[{"role": "user", "content": prompt}],
        model=ANTHROPIC_HAIKU, max_tokens=1500, image_b64=image_b64
    )
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    else:
        start, end = text.find('{'), text.rfind('}')
        if start != -1 and end != -1:
            text = text[start:end+1]
    return json.loads(text)

async def generate_card(product: str, marketplace: str, image_b64: str = None) -> dict:
    if marketplace == "all":
        results = {}
        for mp in ["wb", "ozon", "ym"]:
            results[mp] = await generate_single(product, mp, image_b64)
            await asyncio.sleep(0.5)
        return results
    return await generate_single(product, marketplace, image_b64)

async def execute_card_job(job: dict) -> str:
    prompt = (f"Выполни заказ на написание карточек товаров профессионально.\n\n"
              f"ЗАКАЗ: {job['title']}\nОПИСАНИЕ: {job['description'][:800]}\n\n"
              f"Создай готовую карточку товара:\n"
              f"1. Продающий заголовок с ключевыми словами\n"
              f"2. SEO описание 500-800 символов\n"
              f"3. Список характеристик (5-7 пунктов)\n"
              f"4. Ключевые слова (10-15 штук)\n"
              f"5. Совет по оптимизации\n\nОтвечай на языке заказа.")
    return await anthropic_request(messages=[{"role": "user", "content": prompt}],
                                   model=ANTHROPIC_HAIKU, max_tokens=2000)

async def redo_card_job(original: str, fix_instruction: str) -> str:
    return await anthropic_request(
        messages=[{"role": "user", "content":
            f"Исправь карточку товара.\n\nОРИГИНАЛ:\n{original[:2000]}\n\n"
            f"ИНСТРУКЦИЯ: {fix_instruction}\n\nВерни полный исправленный текст."}],
        model=ANTHROPIC_HAIKU, max_tokens=2000
    )

# ═══ АУДИТ КАРТОЧЕК ═══

async def audit_card(card_text: str, marketplace: str = "wb") -> str:
    mp_names = {"wb":"Wildberries","ozon":"Ozon","ym":"Яндекс Маркет","amazon":"Amazon","etsy":"Etsy"}
    mp_name  = mp_names.get(marketplace, marketplace.upper())
    prompt   = (
        f"Ты эксперт по маркетплейсам. Проведи аудит карточки товара для {mp_name}.\n\n"
        f"КАРТОЧКА:\n{card_text[:2000]}\n\n"
        f"Оцени по критериям:\n"
        f"1. Заголовок — ключевые слова, длина\n"
        f"2. Описание — полнота, продающий текст\n"
        f"3. SEO — ключевые слова\n"
        f"4. Характеристики — заполненность\n"
        f"5. Уникальность — отличие от конкурентов\n"
        f"6. Призыв к действию\n"
        f"7. Общая оценка /10\n\n"
        f"Для каждого: ✅ что хорошо | ❌ что плохо | 💡 рекомендация\n"
        f"Говори конкретно, без воды."
    )
    return await anthropic_request(
        messages=[{"role": "user", "content": prompt}],
        model=ANTHROPIC_HAIKU, max_tokens=1500
    )

# ═══ СЕМАНТИКА ═══

async def get_semantics(product: str, marketplace: str = "wb") -> str:
    mp_names = {"wb":"Wildberries","ozon":"Ozon","ym":"Яндекс Маркет"}
    mp_name  = mp_names.get(marketplace, "маркетплейс")
    prompt   = (
        f"Составь семантическое ядро для товара на {mp_name}.\n"
        f"ТОВАР: {product}\n\n"
        f'Верни ТОЛЬКО JSON:\n'
        f'{{"high_freq": ["слово1","слово2","слово3"], '
        f'"mid_freq": ["фраза1","фраза2","фраза3","фраза4","фраза5"], '
        f'"low_freq": ["длинная фраза1","длинная фраза2","длинная фраза3"], '
        f'"negative": ["минус1","минус2"], '
        f'"title_keywords": "лучшие ключи для заголовка", '
        f'"description_keywords": "ключи для описания"}}'
    )
    try:
        text = await anthropic_request(
            messages=[{"role": "user", "content": prompt}],
            model=ANTHROPIC_HAIKU, max_tokens=600
        )
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        data = json.loads(text)
        return (
            f"🔍 *СЕМАНТИКА: {product}* | _{mp_name}_\n\n"
            f"📊 *Высокочастотные (в заголовок):*\n`{', '.join(data.get('high_freq',[]))}`\n\n"
            f"📈 *Среднечастотные (в описание):*\n`{', '.join(data.get('mid_freq',[]))}`\n\n"
            f"🎯 *Низкочастотные (точное вхождение):*\n`{', '.join(data.get('low_freq',[]))}`\n\n"
            f"🚫 *Минус-слова:*\n`{', '.join(data.get('negative',[]))}`\n\n"
            f"📌 *В заголовок:*\n_{data.get('title_keywords','')}_\n\n"
            f"📝 *В описание:*\n_{data.get('description_keywords','')}_"
        )
    except Exception as e:
        logger.error(f"get_semantics: {e}")
        return f"❌ Ошибка генерации семантики: {str(e)[:100]}"

# ═══ UGC-КОНТЕНТ ═══

async def generate_ugc(product: str, ugc_type: str = "review") -> str:
    if ugc_type == "review":
        prompt = (
            f"Напиши 3 варианта живого отзыва на товар для маркетплейса.\n"
            f"Товар: {product}\n\n"
            f"Требования:\n"
            f"• Разная длина: короткий, средний, подробный\n"
            f"• Разный тон: восторженный, нейтральный, практичный\n"
            f"• Естественный язык реального покупателя\n"
            f"• Конкретные детали использования\n"
            f"• Без шаблонных фраз\n\nОформи: *Вариант 1*, *Вариант 2*, *Вариант 3*"
        )
    elif ugc_type == "negative":
        prompt = (
            f"Напиши 3 шаблона ответов продавца на негативные отзывы.\nТовар: {product}\n\n"
            f"Ситуации:\n1. Товар не понравился\n2. Долгая доставка\n3. Брак\n\n"
            f"Тон: вежливый, конкретный. Каждый: признать → объяснить → решение.\n"
            f"Длина: 50-80 слов."
        )
    elif ugc_type == "faq":
        prompt = (
            f"Составь FAQ для карточки товара.\nТовар: {product}\n\n"
            f"8-10 реальных вопросов покупателей с конкретными ответами.\n"
            f"Включи вопросы о: доставке, гарантии, уходе, размере, совместимости.\n"
            f"Формат: **Вопрос?** → Ответ"
        )
    else:
        return "Неизвестный тип UGC"
    return await anthropic_request(
        messages=[{"role": "user", "content": prompt}],
        model=ANTHROPIC_HAIKU, max_tokens=1000
    )

# ═══ ПАРСЕРЫ ═══

async def parse_card_jobs(client) -> list:
    jobs = []
    for url, source in CARD_RSS_FEEDS:
        try:
            headers = get_headers()
            headers['Accept'] = 'application/rss+xml,application/xml,text/xml,*/*'
            r = await client.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            feed = feedparser.parse(r.text)
            if not feed.entries:
                continue
            for e in feed.entries[:10]:
                link = e.get('link', '')
                if not link or is_seen(link):
                    continue
                title    = clean_html(e.get('title', ''))
                desc     = clean_html(e.get('summary', e.get('description', '')))
                budget_m = re.search(r'[\$₽€]\s?[\d\s,]+|[\d\s,]+\s?(?:руб|USD|\$|₽)', desc + title)
                budget   = budget_m.group(0).strip() if budget_m else "Договорная"
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
    return jobs

async def parse_tg_card_channels(client) -> list:
    jobs = []
    for channel in TG_CARD_CHANNELS:
        try:
            r = await client.get(f"https://t.me/s/{channel}", headers=get_headers(), timeout=15)
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
                budget   = budget_m.group(0).strip() if budget_m else "Договорная"
                jobs.append({
                    'id': make_id(post_url), 'title': text[:60] + "...",
                    'description': text[:1000], 'budget': budget,
                    'url': f"https://t.me/{channel}",
                    'source': f'📱 TG @{channel}', 'status': 'found',
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                })
                mark_seen(post_url)
        except Exception as e:
            logger.error(f"❌ TG {channel}: {e}")
    return jobs

async def send_job_to_lilu(bot, job: dict):
    try:
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        source = f"Карточник | {job.get('source', '')}"
        c.execute("UPDATE jobs SET status='pending_lilu', source=?, updated_at=? WHERE id=?",
                  (source[:200], datetime.now().isoformat(), job['id']))
        conn.commit()
        conn.close()
        logger.info(f"📨 Карточник → БД (pending_lilu): {job.get('title','')[:50]}")
    except Exception as e:
        logger.error(f"❌ send_job_to_lilu: {e}")

async def check_card_jobs(bot) -> int:
    count = 0
    async with httpx.AsyncClient() as client:
        jobs = await parse_card_jobs(client)
        jobs += await parse_tg_card_channels(client)
    for job in jobs:
        save_job(job)
        await send_job_to_lilu(bot, job)
        count += 1
        await asyncio.sleep(2)
    logger.info(f"📋 Карточник нашёл и отправил Лиле: {count} заказов")
    return count

# ═══ ФОРМАТИРОВАНИЕ ═══

def format_card(data: dict, marketplace: str) -> str:
    if marketplace == "wb":
        chars = "\n".join([f" • {c}" for c in data.get('characteristics', [])])
        return (f"🟣 *WILDBERRIES*\n\n📌 *Заголовок:*\n`{data.get('title','')}`\n\n"
                f"📝 *Описание:*\n{data.get('description','')}\n\n"
                f"📋 *Характеристики:*\n{chars}\n\n"
                f"🔍 *Ключевые слова:*\n`{data.get('keywords','')}`\n\n"
                f"💡 _{data.get('seo_tips','')}_")
    elif marketplace == "ozon":
        attrs = "\n".join([f" • {a}" for a in data.get('attributes', [])])
        rich  = "".join([f"\n*{s.get('heading','')}*\n{s.get('text','')}\n" for s in data.get('rich_content', [])])
        return (f"🔵 *OZON*\n\n📌 *Название:*\n`{data.get('title','')}`\n\n"
                f"📝 *Описание:*\n{data.get('description','')}\n\n"
                f"🎨 *Rich-контент:*{rich}\n📋 *Атрибуты:*\n{attrs}\n\n"
                f"🔍 `{data.get('keywords','')}`")
    elif marketplace == "ym":
        specs = "\n".join([f" • {k}: {v}" for k, v in data.get('specs', {}).items()])
        tags  = ", ".join(data.get('tags', []))
        return (f"🟡 *ЯНДЕКС МАРКЕТ*\n\n📌 *Название:*\n`{data.get('title','')}`\n\n"
                f"📝 *Описание:*\n{data.get('description','')}\n\n"
                f"⚙️ *Характеристики:*\n{specs}\n\n"
                f"🏷️ `{tags}`\n\n💡 _{data.get('category_tips','')}_")
    elif marketplace == "amazon":
        bullets = "\n".join([f" • {b}" for b in data.get('bullet_points', [])])
        return (f"🟠 *AMAZON*\n\n📌 *Title:*\n`{data.get('title','')}`\n\n"
                f"📝 *Description:*\n{data.get('description','')}\n\n"
                f"✅ *Bullet Points:*\n{bullets}\n\n"
                f"🔍 *Keywords:*\n`{data.get('keywords','')}`")
    elif marketplace == "etsy":
        tags = ", ".join(data.get('tags', []))
        mats = ", ".join(data.get('materials', []))
        return (f"🟢 *ETSY*\n\n📌 *Title:*\n`{data.get('title','')}`\n\n"
                f"📝 *Description:*\n{data.get('description','')}\n\n"
                f"🏷️ *Tags:* `{tags}`\n\n🔧 *Materials:* {mats}")
    return str(data)

# ═══ STARS INVOICE ═══

async def send_stars_invoice(update, context, stars, title, description):
    try:
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title=title, description=description,
            payload=f"card_order_{stars}_{update.effective_user.id}",
            currency="XTR",
            prices=[{"label": title, "amount": stars}],
            provider_token=""
        )
    except Exception as e:
        logger.error(f"Stars invoice: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⭐ Оплата {stars} Stars — напиши нам и выставим счёт вручную."
        )

# ═══ ОТПРАВКА РЕЗУЛЬТАТА ═══

async def send_card_result(message, result, marketplace, product, bot, user_id=None, skip_image=False):
    if marketplace == "all":
        for mp, data in result.items():
            await bot.send_message(chat_id=message.chat_id, text=format_card(data, mp)[:4000], parse_mode='Markdown')
            await asyncio.sleep(0.5)
        if not skip_image:
            mp0, data0 = list(result.keys())[0], list(result.values())[0]
            photo_b    = user_sessions.get(user_id, {}).get("last_photo_bytes") if user_id else None
            img_bytes  = await generate_product_image(product, data0, mp0, "studio", photo_b)
            if img_bytes:
                await bot.send_photo(
                    chat_id=message.chat_id, photo=img_bytes,
                    caption="🖼 *Инфографика готова!*\n\n💡 Выбери стиль:",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(_style_keyboard("studio", product[:15]))
                )
    else:
        await bot.send_message(chat_id=message.chat_id, text=format_card(result, marketplace)[:4000], parse_mode='Markdown')
        if not skip_image:
            photo_b   = user_sessions.get(user_id, {}).get("last_photo_bytes") if user_id else None
            img_bytes = await generate_product_image(product, result, marketplace, "studio", photo_b)
            if img_bytes:
                await bot.send_photo(
                    chat_id=message.chat_id, photo=img_bytes,
                    caption=(f"🖼 *Студийный стиль*\n\n"
                             f"✅ {'На основе вашего фото!' if photo_b else 'Инфографика готова!'}\n\n"
                             f"💡 Выбери другой стиль:"),
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(_style_keyboard("studio", product[:15]))
                )

def _style_keyboard(current_style, product_short):
    rows = []
    for sk, sd in IMAGE_STYLES.items():
        emoji = "✅ " if sk == current_style else ""
        rows.append([InlineKeyboardButton(
            f"{emoji}{sd['name']} — {sd['desc']}",
            callback_data=f"style_{sk}_{product_short}"
        )])
    return rows

def _card_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟣 WB",       callback_data="mp_wb"),
         InlineKeyboardButton("🔵 Ozon",     callback_data="mp_ozon"),
         InlineKeyboardButton("🟡 ЯМ",       callback_data="mp_ym")],
        [InlineKeyboardButton("🟠 Amazon",   callback_data="mp_amazon"),
         InlineKeyboardButton("🟢 Etsy",     callback_data="mp_etsy"),
         InlineKeyboardButton("🎯 Все RU",   callback_data="mp_all")],
        [InlineKeyboardButton("🔍 Аудит",    callback_data="start_audit"),
         InlineKeyboardButton("📊 Семантика",callback_data="start_semantics"),
         InlineKeyboardButton("📝 UGC",      callback_data="start_ugc")],
        [InlineKeyboardButton("🎨 Векторизация", callback_data="start_vectorize"),
         InlineKeyboardButton("💰 Прайс",    callback_data="card_price"),
         InlineKeyboardButton("📊 Статистика",callback_data="card_stats_btn")],
    ])

# ═══ КНОПКИ ═══

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    user_id = update.effective_user.id

    if data == "start_vectorize":
        user_sessions[user_id] = user_sessions.get(user_id, {})
        user_sessions[user_id]['step'] = 'waiting_vectorize'
        await query.edit_message_text(
            "🎨 *ВЕКТОРИЗАЦИЯ JPG/PNG → SVG*\n\n"
            "Отправь файл изображения (JPG или PNG)\n\n"
            "Я конвертирую его в чистый SVG!\n\n"
            "_Используется Inkscape на сервере_",
            parse_mode='Markdown'
        )

    elif data.startswith("mp_"):
        marketplace = data[3:]
        user_sessions[user_id] = {"marketplace": marketplace, "step": "waiting_product"}
        names = {"wb":"🟣 Wildberries","ozon":"🔵 Ozon","ym":"🟡 Яндекс Маркет",
                 "amazon":"🟠 Amazon","etsy":"🟢 Etsy","all":"🎯 Все RU"}
        await query.edit_message_text(
            f"*{names.get(marketplace,'?')}* выбран!\n\n"
            f"📸 Отправь *фото товара* или *название*\n\n"
            f"_Фото товара → инфографика с реальным снимком_",
            parse_mode='Markdown'
        )

    elif data == "start_audit":
        user_sessions[user_id] = user_sessions.get(user_id, {})
        user_sessions[user_id]['step'] = 'waiting_audit'
        user_sessions[user_id]['audit_mp'] = 'wb'
        await query.edit_message_text(
            "🔍 *АУДИТ КАРТОЧКИ*\n\n"
            "Пришли текст своей карточки — скажу что плохо и как улучшить!\n\n"
            "По умолчанию проверяю под WB.",
            parse_mode='Markdown'
        )

    elif data == "start_semantics":
        user_sessions[user_id] = user_sessions.get(user_id, {})
        user_sessions[user_id]['step'] = 'waiting_semantics'
        await query.edit_message_text(
            "📊 *СЕМАНТИКА ТОВАРА*\n\n"
            "Напиши название товара — сгенерирую ключевые слова!\n\n"
            "Пример: _кроссовки мужские летние_",
            parse_mode='Markdown'
        )

    elif data == "start_ugc":
        await query.edit_message_text(
            "📝 *UGC-КОНТЕНТ*\n\nВыбери тип:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Живые отзывы",      callback_data="ugc_review")],
                [InlineKeyboardButton("💬 Ответы на негатив", callback_data="ugc_negative")],
                [InlineKeyboardButton("❓ FAQ для карточки",  callback_data="ugc_faq")],
                [InlineKeyboardButton("◀️ Назад",             callback_data="card_back_main")],
            ])
        )

    elif data.startswith("ugc_"):
        ugc_type = data[4:]
        user_sessions[user_id] = user_sessions.get(user_id, {})
        user_sessions[user_id]['step']     = 'waiting_ugc'
        user_sessions[user_id]['ugc_type'] = ugc_type
        type_names = {"review":"отзывы", "negative":"ответы на негатив", "faq":"FAQ"}
        await query.edit_message_text(
            f"📝 Режим: *{type_names.get(ugc_type, ugc_type)}*\n\nНапиши название товара:",
            parse_mode='Markdown'
        )

    elif data == "show_packages":
        keyboard = []
        for pk, pd in PACKAGE_PRICES.items():
            keyboard.append([InlineKeyboardButton(
                f"📦 {pd['desc']} — {pd['rub']}₽ / ${pd['usd']}",
                callback_data=f"pkg_{pk}"
            )])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="card_back_main")])
        await query.edit_message_text(
            "📦 *ПАКЕТНЫЕ ПРЕДЛОЖЕНИЯ*\n\n"
            "Больше карточек — дешевле каждая!\n\n"
            "🟢 *1 карточка:* 400₽ / $5\n"
            "🔵 *5 карточек:* 1600₽ _(скидка 10%)_\n"
            "🟣 *10 карточек:* 2700₽ _(скидка 25%)_\n"
            "🏆 *20 карточек:* 4500₽ _(скидка 37%)_\n"
            "💎 *50 карточек:* 9000₽ _(скидка 50%)_",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("pkg_"):
        pkg_key = data[4:]
        pkg     = PACKAGE_PRICES.get(pkg_key)
        if pkg:
            await query.edit_message_text(
                f"📦 *{pkg['desc'].upper()}*\n\n"
                f"💎 USDT: ${pkg['usd']}\n"
                f"⭐ Stars: {pkg['stars']}\n"
                f"🇷🇺 Рубли: ₽{pkg['rub']}\n\n"
                f"Карточек: {pkg['cards']} штук\n\nВыбери способ оплаты:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 USDT",  callback_data=f"invoice_{pkg['usd']}")],
                    [InlineKeyboardButton("⭐ Stars", callback_data=f"stars_{pkg['stars']}")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="show_packages")],
                ])
            )

    elif data.startswith("style_") or data.startswith("restyle_"):
        parts     = data.split("_", 2)
        style_key = parts[1]
        session   = user_sessions.get(user_id, {})
        product   = session.get("last_product", "товар")
        mp        = session.get("last_marketplace", "wb")
        card_data = session.get("last_card_data", {})
        photo_b   = session.get("last_photo_bytes")
        try:
            await query.edit_message_text("⏳ Применяю стиль...")
        except:
            pass
        try:
            img_bytes = await generate_product_image(product, card_data, mp, style_key, photo_b)
            await context.bot.send_photo(
                chat_id=update.effective_chat.id, photo=img_bytes,
                caption=(f"🖼 *{IMAGE_STYLES[style_key]['name']}*\n"
                         f"_{IMAGE_STYLES[style_key]['desc']}_\n\n✅ Готово!\n\n💡 Другой стиль:"),
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(_style_keyboard(style_key, product[:15]))
            )
        except Exception as e:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ {str(e)[:100]}")

    elif data.startswith("regen_"):
        parts   = data.split("_", 2)
        mp      = parts[1]
        product = parts[2] if len(parts) > 2 else ""
        await query.edit_message_text("⏳ Генерирую заново...")
        try:
            result = await generate_card(product, mp)
            await send_card_result(query.message, result, mp, product, context.bot, user_id)
        except Exception as e:
            await query.edit_message_text(f"❌ {str(e)[:100]}")

    elif data == "card_price":
        await query.edit_message_text(
            "💰 *ПРАЙС*\n\n"
            "🟢 1 карточка: $5 / 50⭐ / 400₽\n"
            "🔵 5 карточек: $18 / 180⭐ / 1600₽\n"
            "🟣 10 карточек: $30 / 300⭐ / 2700₽\n"
            "🏆 20 карточек: $50 / 500⭐ / 4500₽\n"
            "💎 50 карточек: $100 / 1000⭐ / 9000₽\n\n"
            "🔍 Аудит: от 500₽\n"
            "📊 Семантика: от 300₽\n"
            "📝 UGC: от 300₽\n"
            "🎨 Векторизация JPG→SVG: от 300₽\n\n"
            "🌍 Amazon/Etsy: от $8",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 USDT",           callback_data="pay_usdt")],
                [InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay_stars")],
                [InlineKeyboardButton("🇷🇺 Рубли",         callback_data="pay_rub")],
                [InlineKeyboardButton("◀️ Назад",          callback_data="card_back_main")],
            ])
        )

    elif data == "card_stats_btn":
        stats = get_stats()
        bs    = stats['by_status']
        await query.edit_message_text(
            f"📊 *СТАТИСТИКА*\n_{msk_time_str()}_\n\n"
            f"🔍 Найдено: {bs.get('found',0)}\n"
            f"✅ Принято: {bs.get('accepted',0)}\n"
            f"✨ Выполнено: {bs.get('completed',0)}\n"
            f"💰 Закрыто: {bs.get('done',0)}\n"
            f"⏭ Пропущено: {bs.get('skipped',0)}\n\n"
            f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="card_back_main")]])
        )

    elif data == "card_back_main":
        await query.edit_message_text(
            "🛍️ *КарточникБот v2.1* — выбери действие:",
            parse_mode='Markdown',
            reply_markup=_card_main_keyboard()
        )

    elif data == "pay_usdt":
        await query.edit_message_text(
            "💎 *Оплата в USDT*\n\nВыбери пакет:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1 карточка — $5",    callback_data="invoice_5")],
                [InlineKeyboardButton("5 карточек — $18",   callback_data="invoice_18")],
                [InlineKeyboardButton("10 карточек — $30",  callback_data="invoice_30")],
                [InlineKeyboardButton("20 карточек — $50",  callback_data="invoice_50")],
                [InlineKeyboardButton("50 карточек — $100", callback_data="invoice_100")],
                [InlineKeyboardButton("✏️ Своя сумма",      callback_data="invoice_custom")],
            ])
        )

    elif data == "pay_stars":
        await query.edit_message_text(
            "⭐ *Telegram Stars*\n\n"
            "50⭐ = 1 карточка | 180⭐ = 5 карточек\n"
            "300⭐ = 10 карточек | 500⭐ = 20 карточек",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ 50 Stars",   callback_data="stars_50")],
                [InlineKeyboardButton("⭐ 180 Stars",  callback_data="stars_180")],
                [InlineKeyboardButton("⭐ 300 Stars",  callback_data="stars_300")],
                [InlineKeyboardButton("⭐ 500 Stars",  callback_data="stars_500")],
                [InlineKeyboardButton("⭐ 1000 Stars", callback_data="stars_1000")],
            ])
        )

    elif data == "pay_rub":
        await query.edit_message_text(
            "🇷🇺 *Оплата в рублях*\n\n"
            "Напиши `/order 5 карточек для WB`\n\n"
            "Способы: СБП • ЮMoney • QIWI",
            parse_mode='Markdown'
        )

    elif data.startswith("invoice_"):
        amount_str = data[8:]
        if amount_str == "custom":
            context.user_data['awaiting_custom_amount'] = True
            await query.edit_message_text("✏️ Напиши сумму в USD:", parse_mode='Markdown')
        else:
            amount = float(amount_str)
            descriptions = {5:"1 карточка",18:"5 карточек",30:"10 карточек",50:"20 карточек",100:"50 карточек"}
            desc = descriptions.get(int(amount), f"${amount} пакет")
            msg  = (f"💎 *СЧЁТ*\n\n📋 *{desc}*\n💰 *${amount:.2f} USDT*\n\n"
                    f"━━━━━━━━━━\n📲 *Оплата через @wallet:*\n\n"
                    f"USDT TRC20:\n`{USDT_WALLET}`\n\nСумма: `{amount}` USDT\n\n"
                    f"⚡ После оплаты нажми кнопку:")
            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Оплатил!", callback_data=f"payment_confirm_{amount}"),
                InlineKeyboardButton("❌ Отмена",   callback_data="card_back_main")
            ]]))

    elif data.startswith("payment_confirm_"):
        amount   = float(data[16:])
        user     = update.effective_user
        username = f"@{user.username}" if user.username else user.first_name
        await context.bot.send_message(
            chat_id=YOUR_CHAT_ID,
            text=f"💰 *ОПЛАТА!*\n\n👤 {username}\n💎 ${amount:.2f} USDT\n🕐 {msk_time_str()}\n\n⚡ Проверь кошелёк!",
            parse_mode='Markdown'
        )
        await query.edit_message_text(
            f"✅ *Спасибо!*\n\nОплата ${amount:.2f} USDT зафиксирована.\nНачинаем через 5 мин!\n\n📱 Отправь данные товара",
            parse_mode='Markdown'
        )
        user_sessions[user.id] = {"step": "waiting_product", "marketplace": "all", "paid": True}

    elif data.startswith("stars_"):
        stars     = int(data[6:])
        stars_map = {
            50:   ("1 карточка товара",   "Профессиональная карточка для WB/Ozon/Amazon"),
            180:  ("5 карточек товаров",  "5 карточек — скидка 10%"),
            300:  ("10 карточек товаров", "10 карточек — скидка 25%"),
            500:  ("20 карточек товаров", "20 карточек — скидка 37%"),
            1000: ("50 карточек товаров", "50 карточек — скидка 50%"),
        }
        title, description = stars_map.get(stars, ("Карточки", "Профессиональные карточки"))
        await send_stars_invoice(update, context, stars, title, description)

    elif data.startswith("take_"):
        job_id = data[5:]
        job    = get_job(job_id)
        if not job:
            await query.edit_message_text("❌ Заказ не найден")
            return
        update_job(job_id, 'accepted')
        await query.edit_message_text(f"✅ *Берём!*\n📌 {job['title'][:80]}\n\n⏳ Выполняю...", parse_mode='Markdown')
        try:
            result  = await execute_card_job(job)
            update_job(job_id, 'completed', result)
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Правка",     callback_data=f"redo_{job_id}")
            ]]
            msg = (f"✨ *КАРТОЧКИ ГОТОВЫ!*\n\n📌 *{job['title'][:80]}*\n\n"
                   f"━━━━━━━━━━\n{result[:2500]}\n━━━━━━━━━━\n\n*Лила, проверь — отправляем?*")
            await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=msg, parse_mode='Markdown',
                                           reply_markup=InlineKeyboardMarkup(keyboard))
            if LILU_CHAT_ID and LILU_CHAT_ID != YOUR_CHAT_ID:
                await context.bot.send_message(chat_id=LILU_CHAT_ID, text=msg, parse_mode='Markdown',
                                               reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            await context.bot.send_message(chat_id=YOUR_CHAT_ID, text=f"❌ Ошибка: {str(e)[:200]}")

    elif data == "skip_wishes":
        user_sessions[user_id]['wishes'] = ''
        user_sessions[user_id]['step']   = 'waiting_product'
        await query.edit_message_text("⚡ Генерирую без пожеланий...")
        await generate_and_send_cards(query, context, user_id)

    elif data == "feedback_ok":
        await query.edit_message_text(
            "🎉 *Карточки готовы к публикации!*\n\nЕсли нужна ещё — пришли новое фото.",
            parse_mode='Markdown'
        )
        user_sessions[user_id]['step'] = 'waiting_product'

    elif data == "feedback_edit":
        await query.edit_message_text(
            "✏️ *Что изменить?*\n\nНапиши пожелания:",
            parse_mode='Markdown'
        )
        user_sessions[user_id]['step'] = 'waiting_wishes'

    elif data.startswith("done_"):
        job_id = data[5:]
        job    = get_job(job_id)
        update_job(job_id, 'done')
        if job:
            nums   = re.findall(r'\d+', job.get('budget','0').replace(' ',''))
            amount = float(nums[0]) if nums else 0
            is_rub = '₽' in job.get('budget','') or 'руб' in job.get('budget','').lower()
            save_earning(job_id, amount/90 if is_rub else amount, amount if is_rub else amount*90, job['title'])
        stats = get_stats()
        await query.edit_message_text(
            f"💰 *ЗАКРЫТ!*\n\n✅ Выполнено: {stats['by_status'].get('done',0)}\n"
            f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}",
            parse_mode='Markdown'
        )

    elif data.startswith("redo_"):
        job_id = data[5:]
        job    = get_job(job_id)
        context.user_data['redo_job_id'] = job_id
        context.user_data['redo_result'] = job.get('result','') if job else ''
        await query.edit_message_text("✏️ *Напиши что исправить:*", parse_mode='Markdown')

    elif data.startswith("skip_") and data != "skip_wishes":
        update_job(data[5:], 'skipped')
        await query.edit_message_text("⏭ Пропустили")

# ═══ КОМАНДЫ ═══

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🛍️ *КарточникБот v2.1*\n_{msk_time_str()}_\n\n"
        "Генерирую карточки + инфографику для маркетплейсов!\n\n"
        "📸 *Пришли фото товара* — сделаю карточку как у топов\n"
        "📝 *Или напиши название* — сгенерирую сам\n\n"
        "🆕 *Функции:*\n"
        "🔍 /audit — аудит твоей карточки\n"
        "📊 /semantics — ключевые слова для WB/Ozon\n"
        "📝 /ugc — отзывы, ответы на негатив, FAQ\n"
        "🎨 /vectorize — JPG/PNG → SVG\n"
        "📦 /packages — пакеты со скидкой до 50%\n\n"
        "Выбери маркетплейс:",
        parse_mode='Markdown',
        reply_markup=_card_main_keyboard()
    )

async def vectorize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = user_sessions.get(user_id, {})
    user_sessions[user_id]['step'] = 'waiting_vectorize'
    await update.message.reply_text(
        "🎨 *ВЕКТОРИЗАЦИЯ JPG/PNG → SVG*\n\n"
        "Отправь мне файл изображения (JPG или PNG)\n\n"
        "✅ Я конвертирую его в чистый SVG через Inkscape\n"
        "✅ Без встроенных растровых изображений\n"
        "✅ Чистый вектор для печати и маркетплейсов\n\n"
        "_Обычно занимает 10-30 секунд_",
        parse_mode='Markdown'
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = await update.message.reply_text("🔍 Ищу заказы на карточки...")
    count = await check_card_jobs(context.application.bot)
    await msg.edit_text(
        f"✅ Найдено и отправлено Лиле: *{count}* заказов\n\n"
        f"{'Лила анализирует — лучшие придут тебе! 🚀' if count>0 else 'Пока 0 — попробуй /clear и снова'}\n\n"
        f"🕐 {msk_time_str()}",
        parse_mode='Markdown'
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute('DELETE FROM seen_jobs')
    conn.commit()
    conn.close()
    await update.message.reply_text("🗑️ Кэш очищен! Теперь /scan найдёт заново.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    bs    = stats['by_status']
    await update.message.reply_text(
        f"📊 *СТАТИСТИКА КАРТОЧНИКА*\n_{msk_time_str()}_\n\n"
        f"🔍 Найдено: {bs.get('found',0)}\n"
        f"✅ Принято: {bs.get('accepted',0)}\n"
        f"✨ Выполнено: {bs.get('completed',0)}\n"
        f"💰 Закрыто: {bs.get('done',0)}\n"
        f"⏭ Пропущено: {bs.get('skipped',0)}\n\n"
        f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}\n"
        f"📦 Всего выплат: {stats['earn_count']}",
        parse_mode='Markdown'
    )

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 *ПРАЙС — КАРТОЧКИ ТОВАРОВ*\n\n"
        "🟢 *1 карточка:* $5 / 50⭐ / 400₽\n"
        "🔵 *5 карточек:* $18 / 180⭐ / 1600₽ _(скидка 10%)_\n"
        "🟣 *10 карточек:* $30 / 300⭐ / 2700₽ _(скидка 25%)_\n"
        "🏆 *20 карточек:* $50 / 500⭐ / 4500₽ _(скидка 37%)_\n"
        "💎 *50 карточек:* $100 / 1000⭐ / 9000₽ _(скидка 50%)_\n\n"
        "🔍 *Аудит карточки:* от 500₽\n"
        "📊 *Семантика товара:* от 300₽\n"
        "📝 *UGC-контент:* от 300₽\n"
        "🎨 *Векторизация JPG→SVG:* от 300₽\n\n"
        "Способ оплаты:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 USDT",           callback_data="pay_usdt")],
            [InlineKeyboardButton("⭐ Telegram Stars", callback_data="pay_stars")],
            [InlineKeyboardButton("🇷🇺 Рубли",         callback_data="pay_rub")],
        ])
    )

async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args    = context.args
    marketplace = args[0].lower() if args and args[0].lower() in ("wb","ozon","ym","amazon","etsy") else "wb"
    user_sessions[user_id] = user_sessions.get(user_id, {})
    user_sessions[user_id]['step']     = 'waiting_audit'
    user_sessions[user_id]['audit_mp'] = marketplace
    await update.message.reply_text(
        f"🔍 *АУДИТ КАРТОЧКИ* | {marketplace.upper()}\n\n"
        "Пришли текст своей карточки — скажу что плохо и как улучшить!",
        parse_mode='Markdown'
    )

async def semantics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "📊 Использование:\n"
            "`/semantics кроссовки мужские` — для WB\n"
            "`/semantics кроссовки ozon` — для Ozon",
            parse_mode='Markdown'
        )
        return
    args        = context.args
    marketplace = "wb"
    if args[-1].lower() in ("wb","ozon","ym","amazon","etsy"):
        marketplace = args[-1].lower()
        product     = " ".join(args[:-1])
    else:
        product = " ".join(args)
    await update.message.reply_text("📊 Генерирую семантику...")
    result = await get_semantics(product, marketplace)
    await update.message.reply_text(result, parse_mode='Markdown')

async def ugc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "📝 *UGC-контент*\n\n"
            "`/ugc review кроссовки` — живые отзывы\n"
            "`/ugc negative кроссовки` — ответы на негатив\n"
            "`/ugc faq кроссовки` — вопрос-ответ",
            parse_mode='Markdown'
        )
        return
    ugc_type = context.args[0].lower()
    product  = " ".join(context.args[1:]) if len(context.args) > 1 else "товар"
    type_names = {"review":"Отзывы", "negative":"Ответы на негатив", "faq":"FAQ"}
    await update.message.reply_text(f"📝 Генерирую {type_names.get(ugc_type,ugc_type)}...")
    result = await generate_ugc(product, ugc_type)
    await update.message.reply_text(
        f"📝 *{type_names.get(ugc_type,ugc_type).upper()}:*\n\n{result}",
        parse_mode='Markdown'
    )

async def packages_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for pk, pd in PACKAGE_PRICES.items():
        keyboard.append([InlineKeyboardButton(
            f"📦 {pd['desc']} — {pd['rub']}₽ / ${pd['usd']}",
            callback_data=f"pkg_{pk}"
        )])
    await update.message.reply_text(
        "📦 *ПАКЕТНЫЕ ПРЕДЛОЖЕНИЯ*\n\n"
        "🟢 1 шт: 400₽ | 🔵 5 шт: 1600₽ | 🟣 10 шт: 2700₽\n"
        "🏆 20 шт: 4500₽ | 💎 50 шт: 9000₽\n\nВыбери пакет:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ═══ ГЕНЕРАЦИЯ КАРТОЧЕК ═══

async def generate_and_send_cards(update, context, user_id: int):
    session      = user_sessions.get(user_id, {})
    product      = session.get('last_product', 'товар')
    marketplace  = session.get('last_marketplace', 'wb')
    photo_bytes  = session.get('last_photo_bytes')
    image_base64 = session.get('last_image_base64')
    wishes       = session.get('wishes', '')

    await update.message.reply_text(
        f"🎨 *Генерирую карточки...*\n\n"
        f"{'💬 Учитываю: _' + wishes[:60] + '_' if wishes else '⚡ Без пожеланий'}\n\n"
        f"⏳ Обычно 60-90 секунд",
        parse_mode='Markdown'
    )

    try:
        if AIDENTIKA_API_KEY and image_base64:
            upload_id = await aidentika_upload(image_base64)
            if not upload_id:
                raise Exception("Не удалось загрузить фото")

            text_result = await generate_card(product, marketplace, image_base64)
            card_data   = list(text_result.values())[0] if marketplace == "all" else text_result
            base_features = "\n".join(card_data.get("characteristics", card_data.get("attributes", []))[:5])
            features_text = f"{base_features}\n{wishes}" if wishes else base_features

            action_classic, action_premium = await asyncio.gather(
                aidentika_generate_card(upload_id, product[:100], features_text, "classic"),
                aidentika_generate_card(upload_id, product[:100], features_text, "premium")
            )

            async def empty_bytes(): return b""

            img_classic, img_premium = await asyncio.gather(
                aidentika_wait_and_download(action_classic) if action_classic else empty_bytes(),
                aidentika_wait_and_download(action_premium) if action_premium else empty_bytes()
            )

            sent_any = False
            if img_classic:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=io.BytesIO(img_classic),
                    caption=f"🎨 *Вариант 1 — Классический*\n\n_{product[:60]}_",
                    parse_mode='Markdown'
                )
                sent_any = True

            if img_premium:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=io.BytesIO(img_premium),
                    caption=f"✨ *Вариант 2 — Премиум*\n\n_{product[:60]}_",
                    parse_mode='Markdown'
                )
                sent_any = True

            if sent_any:
                await send_card_result(update.message, text_result, marketplace, product,
                                       context.bot, user_id, skip_image=True)
                balance     = await aidentika_balance()
                balance_msg = f"\n\n⚠️ Осталось {balance} искр — пополни!" if 0 <= balance < 8 else ""
                keyboard    = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Всё отлично!", callback_data="feedback_ok"),
                    InlineKeyboardButton("✏️ Хочу изменить", callback_data="feedback_edit")
                ]])
                await update.message.reply_text(
                    f"👆 *Два варианта готовы!*\n\nВыбери который нравится{balance_msg}",
                    parse_mode='Markdown', reply_markup=keyboard
                )
                user_sessions[user_id]['step'] = 'waiting_feedback'
                return

        result = await generate_card(product, marketplace, image_base64)
        await send_card_result(update.message, result, marketplace, product, context.bot, user_id)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Всё отлично!", callback_data="feedback_ok"),
            InlineKeyboardButton("✏️ Хочу изменить", callback_data="feedback_edit")
        ]])
        await update.message.reply_text("📝 *Карточка готова!*\n\nВсё устраивает?",
                                        parse_mode='Markdown', reply_markup=keyboard)
        user_sessions[user_id]['step'] = 'waiting_feedback'

    except Exception as e:
        logger.error(f"generate_and_send_cards: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")

# ═══ ОБРАБОТЧИК СООБЩЕНИЙ ═══

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Режим правки заказа
    if context.user_data.get('redo_job_id'):
        job_id   = context.user_data['redo_job_id']
        original = context.user_data.get('redo_result', '')
        fix      = update.message.text
        await update.message.reply_text("⏳ Исправляю...")
        try:
            new_result = await redo_card_job(original, fix)
            update_job(job_id, 'completed', new_result)
            context.user_data['redo_result'] = new_result
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Ещё правка", callback_data=f"redo_{job_id}")
            ]]
            msg = f"✨ *ИСПРАВЛЕНО!*\n\n{new_result[:2500]}\n\n*Лила, проверь — отправляем?*"
            await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
            if LILU_CHAT_ID and LILU_CHAT_ID != YOUR_CHAT_ID:
                await context.bot.send_message(chat_id=LILU_CHAT_ID, text=msg, parse_mode='Markdown',
                                               reply_markup=InlineKeyboardMarkup(keyboard))
            context.user_data.pop('redo_job_id', None)
            context.user_data.pop('redo_result', None)
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")
        return

    session = user_sessions.get(user_id, {})
    step    = session.get('step', '')

    # ─── ВЕКТОРИЗАЦИЯ (НОВОЕ) ───
    if step == 'waiting_vectorize':
        file_bytes = None
        file_ext   = 'jpg'

        if update.message.photo:
            photo      = update.message.photo[-1]
            photo_file = await context.bot.get_file(photo.file_id)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                await photo_file.download_to_drive(tmp.name)
                file_path = tmp.name
                file_ext  = 'jpg'

        elif update.message.document:
            doc = update.message.document
            if not any(doc.mime_type in mt for mt in ['image/jpeg','image/png','image/jpg']):
                await update.message.reply_text("❌ Отправь JPG или PNG файл!")
                return
            doc_file = await context.bot.get_file(doc.file_id)
            ext      = doc.file_name.rsplit('.', 1)[-1].lower() if doc.file_name else 'jpg'
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                await doc_file.download_to_drive(tmp.name)
                file_path = tmp.name
                file_ext  = ext
        else:
            await update.message.reply_text(
                "📎 Отправь JPG или PNG файл!\n\n"
                "Можно как фото или как документ (лучше качество)."
            )
            return

        await update.message.reply_text("⏳ Конвертирую в SVG через Inkscape...")

        try:
            svg_path = await vectorize_image(file_path)
            if svg_path and os.path.exists(svg_path):
                with open(svg_path, 'rb') as f:
                    await update.message.reply_document(
                        document=f,
                        filename=f"vector_{user_id}.svg",
                        caption=(
                            "✅ *Векторизация завершена!*\n\n"
                            "📄 Чистый SVG файл готов\n"
                            "✅ Без встроенных растровых изображений\n"
                            "✅ Подходит для маркетплейсов и печати\n\n"
                            f"🕐 {msk_time_str()}"
                        ),
                        parse_mode='Markdown'
                    )
                os.unlink(svg_path)
            else:
                await update.message.reply_text(
                    "❌ Ошибка конвертации\n\n"
                    "Попробуй другой файл или более чёткое изображение."
                )
        except Exception as e:
            logger.error(f"vectorize error: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")
        finally:
            try:
                os.unlink(file_path)
            except:
                pass

        user_sessions[user_id]['step'] = ''
        await update.message.reply_text(
            "🎨 Ещё векторизация? Отправь следующий файл или выбери другое действие:",
            reply_markup=_card_main_keyboard()
        )
        return

    # ─── Режим аудита ───
    if step == 'waiting_audit':
        audit_mp  = session.get('audit_mp', 'wb')
        card_text = update.message.text or ""
        if not card_text:
            await update.message.reply_text("Пришли текст карточки!")
            return
        await update.message.reply_text("🔍 Анализирую карточку...")
        audit_result = await audit_card(card_text, audit_mp)
        await update.message.reply_text(f"🔍 *АУДИТ КАРТОЧКИ*\n\n{audit_result}", parse_mode='Markdown')
        user_sessions[user_id]['step'] = 'waiting_product'
        return

    # ─── Режим семантики ───
    if step == 'waiting_semantics':
        text_input = update.message.text or ""
        marketplace = "wb"
        parts = text_input.split()
        if parts and parts[-1].lower() in ("wb","ozon","ym","amazon","etsy"):
            marketplace = parts[-1].lower()
            product     = " ".join(parts[:-1])
        else:
            product = text_input
        await update.message.reply_text("📊 Генерирую семантику...")
        result = await get_semantics(product, marketplace)
        await update.message.reply_text(result, parse_mode='Markdown')
        user_sessions[user_id]['step'] = 'waiting_product'
        return

    # ─── Режим UGC ───
    if step == 'waiting_ugc':
        ugc_type = session.get('ugc_type', 'review')
        product  = update.message.text or "товар"
        type_names = {"review":"Отзывы","negative":"Ответы на негатив","faq":"FAQ"}
        await update.message.reply_text(f"📝 Генерирую {type_names.get(ugc_type,ugc_type)}...")
        result = await generate_ugc(product, ugc_type)
        await update.message.reply_text(
            f"📝 *{type_names.get(ugc_type,ugc_type).upper()}:*\n\n{result}",
            parse_mode='Markdown'
        )
        user_sessions[user_id]['step'] = 'waiting_product'
        return

    # ─── Нет активного шага ───
    if user_id not in user_sessions or step not in ('waiting_product','waiting_wishes','waiting_feedback'):
        await update.message.reply_text(
            "🛍️ *КарточникБот v2.1*\n\n"
            "📸 Пришли фото товара или напиши название\n\n"
            "👇 Выбери маркетплейс:",
            parse_mode='Markdown',
            reply_markup=_card_main_keyboard()
        )
        return

    # ─── Пожелания ───
    if step == 'waiting_wishes':
        user_sessions[user_id]['wishes'] = update.message.text or ""
        user_sessions[user_id]['step']   = 'waiting_product'
        await generate_and_send_cards(update, context, user_id)
        return

    # ─── Обратная связь ───
    if step == 'waiting_feedback':
        feedback = update.message.text.lower() if update.message.text else ""
        if any(w in feedback for w in ['нет','измени','правка','переделай','не нравится','плохо']):
            user_sessions[user_id]['step'] = 'waiting_wishes'
            await update.message.reply_text(
                "✏️ *Что изменить?*\n\nНапиши пожелания:",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "🎉 *Карточка готова к публикации!*\n\nЕсли нужна ещё — пришли новое фото.",
                parse_mode='Markdown', reply_markup=_card_main_keyboard()
            )
            user_sessions[user_id]['step'] = 'waiting_product'
        return

    # ─── Основной шаг ───
    marketplace  = session.get('marketplace', 'wb')
    image_base64 = None
    photo_bytes  = None
    product      = ""

    if update.message.photo:
        photo      = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await photo_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                photo_bytes  = f.read()
                image_base64 = base64.b64encode(photo_bytes).decode()
            os.unlink(tmp.name)
        product = update.message.caption or "товар на фото"
    elif update.message.text:
        product = update.message.text
    else:
        await update.message.reply_text("Отправь текст или фото товара!")
        return

    session_mp = marketplace if marketplace != "all" else "wb"
    user_sessions[user_id].update({
        'last_product':      product,
        'last_marketplace':  session_mp,
        'last_photo_bytes':  photo_bytes,
        'last_image_base64': image_base64,
        'wishes':            ''
    })

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Пропустить — генерировать сразу", callback_data="skip_wishes")
    ]])
    await update.message.reply_text(
        f"📦 *Товар:* _{product[:60]}_\n\n"
        f"💬 *Есть пожелания к карточке?*\n\n"
        f"Например: _тёмный фон_, _премиум стиль_\n\n"
        f"Или нажми кнопку чтобы генерировать сразу:",
        parse_mode='Markdown',
        reply_markup=keyboard
    )
    user_sessions[user_id]['step'] = 'waiting_wishes'

# ═══ АВТОСКАНИРОВАНИЕ ═══

async def auto_scan_loop(bot):
    await asyncio.sleep(90)
    while True:
        logger.info("🔄 Карточник: автосканирование...")
        try:
            count = await check_card_jobs(bot)
            if count > 0 and YOUR_CHAT_ID:
                await bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=f"🛍️ *Карточник нашёл {count} заказов* — отправил Лиле!\n🕐 {msk_time_str()}",
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"❌ Автосканирование: {e}")
        await asyncio.sleep(900)  # 15 минут

# ═══ ЗАПУСК ═══

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",     start_command))
    app.add_handler(CommandHandler("scan",      scan_command))
    app.add_handler(CommandHandler("clear",     clear_command))
    app.add_handler(CommandHandler("stats",     stats_command))
    app.add_handler(CommandHandler("price",     price_command))
    app.add_handler(CommandHandler("audit",     audit_command))
    app.add_handler(CommandHandler("semantics", semantics_command))
    app.add_handler(CommandHandler("ugc",       ugc_command))
    app.add_handler(CommandHandler("packages",  packages_command))
    app.add_handler(CommandHandler("vectorize", vectorize_command))  # НОВОЕ
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))  # НОВОЕ для файлов

    async def post_init(application):
        asyncio.create_task(auto_scan_loop(application.bot))
        try:
            if YOUR_CHAT_ID:
                await application.bot.send_message(
                    chat_id=YOUR_CHAT_ID,
                    text=(
                        f"🛍️ *Карточник v2.1 запущен!*\n\n"
                        f"🕐 {msk_time_str()}\n\n"
                        f"✅ Groq ротация 4 моделей\n"
                        f"✅ Rate limit защита\n"
                        f"✅ Время МСК везде\n"
                        f"✅ /vectorize — JPG/PNG → SVG (Inkscape)\n"
                        f"✅ Скан каждые 15 мин\n"
                        f"✅ Файлы через Document handler\n\n"
                        f"/vectorize /audit /semantics /ugc"
                    ),
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f"post_init: {e}")

    app.post_init = post_init
    logger.info("🛍️ Карточник v2.1 запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
