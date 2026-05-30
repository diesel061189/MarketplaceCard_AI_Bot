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
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("CARD_BOT_TOKEN")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID   = int(os.getenv("YOUR_CHAT_ID", "0"))
LILU_CHAT_ID   = int(os.getenv("LILU_CHAT_ID", "0"))
DB_PATH        = os.getenv("DB_PATH", "/tmp/freelance.db")
USDT_WALLET    = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

user_sessions = {}

import random
HEADERS_LIST = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36", "Accept-Language": "ru-RU,ru;q=0.9"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15", "Accept-Language": "en-US,en;q=0.9"},
]

def get_headers():
    return random.choice(HEADERS_LIST)

# ═══ СТИЛИ КАРТОЧЕК ═══

IMAGE_STYLES = {
    "studio": {
        "name": "🤍 Студийный",
        "desc": "Белый фон, профессиональная съёмка",
        "bg": (255, 255, 255),
        "accent": (67, 97, 238),
        "text": (20, 20, 40),
        "badge_bg": (67, 97, 238),
        "badge_text": (255, 255, 255),
    },
    "dark": {
        "name": "🖤 Тёмный",
        "desc": "Тёмный фон — премиум стиль",
        "bg": (18, 18, 30),
        "accent": (255, 165, 0),
        "text": (255, 255, 255),
        "badge_bg": (255, 165, 0),
        "badge_text": (20, 20, 20),
    },
    "hype": {
        "name": "🔥 Hype",
        "desc": "Яркий, молодёжный",
        "bg": (13, 13, 30),
        "accent": (255, 0, 110),
        "text": (255, 255, 255),
        "badge_bg": (131, 56, 236),
        "badge_text": (255, 255, 255),
    },
    "natural": {
        "name": "🌿 Natural",
        "desc": "Природный — для эко-товаров",
        "bg": (240, 247, 238),
        "accent": (45, 106, 79),
        "text": (30, 60, 40),
        "badge_bg": (64, 145, 108),
        "badge_text": (255, 255, 255),
    },
    "warm": {
        "name": "🧡 Тёплый",
        "desc": "Бежевый — уют и доверие",
        "bg": (253, 245, 235),
        "accent": (180, 90, 30),
        "text": (60, 30, 10),
        "badge_bg": (210, 120, 50),
        "badge_text": (255, 255, 255),
    },
}

# ═══ RSS ИСТОЧНИКИ ═══

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
            return False
    return any(kw in text for kw in CARD_KEYWORDS)

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
            logger.info(f"{source}: {len(feed.entries)} записей")
            for e in feed.entries[:10]:
                link = e.get('link', '')
                if not link or is_seen(link):
                    continue
                title = clean_html(e.get('title', ''))
                desc  = clean_html(e.get('summary', e.get('description', '')))
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
                budget = budget_m.group(0).strip() if budget_m else "Договорная"
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

# ═══ AI ПРОМПТЫ ═══

MARKETPLACE_PROMPTS = {
    "wb": """Создай продающую карточку для Wildberries. Верни ТОЛЬКО JSON:
{"title": "заголовок до 100 символов", "description": "описание 500-1000 символов", "characteristics": ["характеристика 1", "характеристика 2", "характеристика 3", "характеристика 4"], "keywords": "ключевые слова через запятую", "seo_tips": "совет по SEO", "badges": ["значок 1", "значок 2", "значок 3"]}""",
    "ozon": """Создай карточку для Ozon. Верни ТОЛЬКО JSON:
{"title": "название до 200 символов", "description": "описание 1000-3000 символов", "rich_content": [{"heading": "Заголовок", "text": "Текст"}], "attributes": ["атрибут 1", "атрибут 2", "атрибут 3"], "keywords": "ключевые слова", "badges": ["значок 1", "значок 2", "значок 3"]}""",
    "ym": """Создай карточку для Яндекс Маркет. Верни ТОЛЬКО JSON:
{"title": "точное название", "description": "описание до 3000 символов", "specs": {"Параметр1": "Значение1", "Параметр2": "Значение2"}, "tags": ["тег 1", "тег 2"], "category_tips": "совет", "badges": ["значок 1", "значок 2", "значок 3"]}""",
}

# ═══ GEMINI — ГЕНЕРАЦИЯ ФОТО ТОВАРА (когда нет фото) ═══

async def generate_product_image_gemini(product_name: str, style_key: str = "studio") -> bytes | None:
    """Генерирует фото товара через Gemini (только если у клиента нет фото)"""
    style = IMAGE_STYLES.get(style_key, IMAGE_STYLES["studio"])

    prompt = (
        f"Create a professional product photo for marketplace listing. "
        f"Product: {product_name}. "
        f"Style: clean commercial photography, {style['desc'].lower()}. "
        f"Show only the product, no people, no hands, no text overlay, no watermarks. "
        f"High quality, sharp focus, ready for e-commerce."
    )

    GEMINI_MODELS = [
        "gemini-2.5-flash-preview-05-20",
        "gemini-2.0-flash-preview-image-generation",
        "gemini-2.5-flash",
    ]

    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY не задан")
        return None

    for model in GEMINI_MODELS:
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}",
                    headers={"Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}
                    }
                )
                logger.info(f"Gemini [{model}]: {r.status_code}")
                if r.status_code == 200:
                    data = r.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        for part in candidates[0].get("content", {}).get("parts", []):
                            if "inlineData" in part:
                                img_data = part["inlineData"].get("data", "")
                                if img_data:
                                    logger.info(f"✅ Gemini [{model}] — фото товара готово!")
                                    return base64.b64decode(img_data)
                    logger.warning(f"Gemini [{model}] — нет картинки в ответе: {str(data)[:200]}")
                else:
                    logger.error(f"Gemini [{model}] ошибка {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.error(f"Gemini [{model}] исключение: {e}")

    return None

# ═══ PILLOW — ПРОФЕССИОНАЛЬНАЯ ИНФОГРАФИКА ═══

def build_infographic(
    product_name: str,
    card_data: dict,
    marketplace: str,
    style_key: str = "studio",
    product_photo_bytes: bytes = None
) -> bytes:
    """
    Строит профессиональную инфографику:
    - Если есть product_photo_bytes — вставляет реальное фото товара
    - Если нет — делает красивый placeholder
    """
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
    import textwrap

    style = IMAGE_STYLES.get(style_key, IMAGE_STYLES["studio"])
    W, H = 900, 1200

    # ─── ФОНОВЫЙ СЛОЙ ───
    img = Image.new('RGB', (W, H), color=style['bg'])
    draw = ImageDraw.Draw(img)

    bg      = style['bg']
    accent  = style['accent']
    txt_col = style['text']
    badge_bg   = style['badge_bg']
    badge_txt  = style['badge_text']

    # Декоративный градиентный блок сверху
    for y in range(180):
        alpha = int(255 * (1 - y / 180))
        r = min(accent[0] + 40, 255)
        g = min(accent[1] + 40, 255)
        b = min(accent[2] + 40, 255)
        blended = (
            int(r * alpha/255 + bg[0] * (1 - alpha/255)),
            int(g * alpha/255 + bg[1] * (1 - alpha/255)),
            int(b * alpha/255 + bg[2] * (1 - alpha/255)),
        )
        draw.line([(0, y), (W, y)], fill=blended)

    # Цветная полоса сверху
    draw.rectangle([0, 0, W, 8], fill=accent)

    # ─── МАРКЕТПЛЕЙС БЕЙДЖ ───
    mp_labels = {"wb": "WILDBERRIES", "ozon": "OZON", "ym": "ЯНДЕКС МАРКЕТ"}
    mp_colors = {
        "wb": (147, 0, 211),
        "ozon": (0, 91, 255),
        "ym": (255, 204, 0),
    }
    mp_color = mp_colors.get(marketplace, accent)
    mp_txt_color = (255, 255, 255) if marketplace != "ym" else (20, 20, 20)
    draw.rectangle([20, 18, 220, 52], fill=mp_color, outline=mp_color)
    draw.text((120, 35), mp_labels.get(marketplace, "МАРКЕТПЛЕЙС"),
              anchor="mm", fill=mp_txt_color)

    # ─── НАЗВАНИЕ ТОВАРА ───
    title = card_data.get("title", product_name)
    title_lines = textwrap.wrap(title, width=28)[:3]
    y_title = 70
    for line in title_lines:
        draw.text((W // 2, y_title), line, anchor="mm", fill=txt_col)
        y_title += 38

    # ─── ЗОНА ФОТО ТОВАРА ───
    photo_y0, photo_y1 = 185, 620
    photo_x0, photo_x1 = 60, W - 60

    if product_photo_bytes:
        try:
            prod_img = Image.open(io.BytesIO(product_photo_bytes)).convert("RGBA")
            # Вписываем в зону сохраняя пропорции
            prod_img.thumbnail((photo_x1 - photo_x0, photo_y1 - photo_y0), Image.LANCZOS)
            # Центрируем
            px = photo_x0 + (photo_x1 - photo_x0 - prod_img.width) // 2
            py = photo_y0 + (photo_y1 - photo_y0 - prod_img.height) // 2
            # Белый фон под фото
            draw.rectangle([photo_x0, photo_y0, photo_x1, photo_y1],
                           fill=(255, 255, 255) if style_key == "studio" else tuple(min(c+30, 255) for c in bg))
            # Тонкая рамка
            draw.rectangle([photo_x0, photo_y0, photo_x1, photo_y1],
                           outline=accent, width=2)
            img.paste(prod_img, (px, py), prod_img if prod_img.mode == 'RGBA' else None)
            logger.info("✅ Фото товара вставлено в карточку")
        except Exception as e:
            logger.error(f"Ошибка вставки фото: {e}")
            _draw_photo_placeholder(draw, photo_x0, photo_y0, photo_x1, photo_y1, accent, bg, product_name)
    else:
        _draw_photo_placeholder(draw, photo_x0, photo_y0, photo_x1, photo_y1, accent, bg, product_name)

    # ─── БЕЙДЖИ (ДО/ПОСЛЕ, ГАРАНТИЯ, и т.д.) ───
    badges = card_data.get("badges", ["✅ Быстрая доставка", "⭐ Топ продаж", "🎁 Гарантия"])
    badge_y = photo_y1 + 15
    bw = (W - 60) // len(badges[:3])
    for i, badge in enumerate(badges[:3]):
        bx0 = 30 + i * bw
        bx1 = bx0 + bw - 10
        draw.rectangle([bx0, badge_y, bx1, badge_y + 36], fill=badge_bg)
        # Скругление имитируем дополнительными прямоугольниками
        draw.text(((bx0 + bx1) // 2, badge_y + 18), str(badge)[:22],
                  anchor="mm", fill=badge_txt)

    # ─── ХАРАКТЕРИСТИКИ ───
    chars_y = badge_y + 55
    draw.rectangle([30, chars_y - 5, W - 30, chars_y + 2], fill=accent)

    chars = []
    if marketplace == "wb":
        chars = card_data.get("characteristics", [])
    elif marketplace == "ozon":
        chars = card_data.get("attributes", [])
    elif marketplace == "ym":
        specs = card_data.get("specs", {})
        chars = [f"{k}: {v}" for k, v in specs.items()]

    chars_y += 15
    for i, char in enumerate(chars[:6]):
        # Чередующийся фон строк
        row_bg = tuple(max(c - 10, 0) if i % 2 == 0 else c for c in bg)
        draw.rectangle([30, chars_y - 4, W - 30, chars_y + 28], fill=row_bg)
        # Маркер
        draw.rectangle([30, chars_y + 4, 36, chars_y + 20], fill=accent)
        draw.text((50, chars_y + 12), str(char)[:55], anchor="lm", fill=txt_col)
        chars_y += 38

    # ─── КЛЮЧЕВЫЕ СЛОВА / SEO ───
    kw_y = max(chars_y + 15, 920)
    kw = card_data.get("keywords", "")
    if kw:
        draw.rectangle([30, kw_y, W - 30, kw_y + 35], fill=tuple(max(c-20,0) for c in bg))
        kw_short = str(kw)[:80]
        draw.text((W // 2, kw_y + 17), f"🔍 {kw_short}", anchor="mm", fill=accent)

    # ─── НИЖНЯЯ ПАНЕЛЬ ───
    draw.rectangle([0, H - 110, W, H], fill=accent)

    # Описание (первые 120 символов)
    desc = card_data.get("description", "")
    desc_short = desc[:120].replace("\n", " ")
    desc_wrapped = textwrap.wrap(desc_short, width=60)[:2]
    for i, line in enumerate(desc_wrapped):
        draw.text((W // 2, H - 90 + i * 28), line, anchor="mm", fill=(255, 255, 255))

    draw.text((W // 2, H - 25), "✅ SEO-оптимизировано  •  ✅ Готово для загрузки",
              anchor="mm", fill=(220, 255, 220))

    # Сохраняем
    buf = io.BytesIO()
    img.save(buf, format='PNG', quality=95)
    buf.seek(0)
    return buf.read()


def _draw_photo_placeholder(draw, x0, y0, x1, y1, accent, bg, product_name):
    """Заглушка когда нет фото товара"""
    ph_bg = tuple(min(c + 25, 255) for c in bg)
    draw.rectangle([x0, y0, x1, y1], fill=ph_bg)
    draw.rectangle([x0, y0, x1, y1], outline=accent, width=2)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    # Большая иконка
    draw.text((cx, cy - 40), "📦", anchor="mm", fill=accent)
    draw.text((cx, cy + 20), product_name[:30], anchor="mm", fill=accent)
    draw.text((cx, cy + 55), "Отправь фото товара для", anchor="mm", fill=tuple(max(c-60,0) for c in accent))
    draw.text((cx, cy + 80), "профессиональной карточки", anchor="mm", fill=tuple(max(c-60,0) for c in accent))

# ═══ ГЛАВНАЯ ФУНКЦИЯ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЯ ═══

async def generate_product_image(
    product_name: str,
    card_data: dict,
    marketplace: str,
    style_key: str = "studio",
    photo_bytes: bytes = None
) -> bytes:
    """
    Если есть photo_bytes (клиент прислал фото) — строим инфографику на его основе.
    Если нет — просим Gemini сгенерировать фото товара, потом строим инфографику.
    """
    if photo_bytes:
        logger.info("🖼 Строим инфографику на основе фото клиента")
        return build_infographic(product_name, card_data, marketplace, style_key, photo_bytes)

    # Нет фото — пробуем Gemini
    logger.info("🤖 Фото нет — генерируем через Gemini")
    gemini_photo = await generate_product_image_gemini(product_name, style_key)

    if gemini_photo:
        logger.info("✅ Gemini дал фото — строим инфографику")
        return build_infographic(product_name, card_data, marketplace, style_key, gemini_photo)

    # Gemini тоже не дал — делаем без фото (placeholder)
    logger.info("⚠️ Без фото — placeholder инфографика")
    return build_infographic(product_name, card_data, marketplace, style_key, None)

# ═══ ГЕНЕРАЦИЯ ТЕКСТА КАРТОЧКИ ═══

MARKETPLACE_PROMPTS = {
    "wb": """Создай продающую карточку для Wildberries. Верни ТОЛЬКО JSON:
{"title": "заголовок до 100 символов", "description": "описание 500-1000 символов", "characteristics": ["характеристика 1", "характеристика 2", "характеристика 3", "характеристика 4", "характеристика 5"], "keywords": "ключевые слова через запятую", "seo_tips": "совет по SEO", "badges": ["значок 1", "значок 2", "значок 3"]}""",
    "ozon": """Создай карточку для Ozon. Верни ТОЛЬКО JSON:
{"title": "название до 200 символов", "description": "описание 1000-3000 символов", "rich_content": [{"heading": "Заголовок", "text": "Текст"}], "attributes": ["атрибут 1", "атрибут 2", "атрибут 3", "атрибут 4", "атрибут 5"], "keywords": "ключевые слова", "badges": ["значок 1", "значок 2", "значок 3"]}""",
    "ym": """Создай карточку для Яндекс Маркет. Верни ТОЛЬКО JSON:
{"title": "точное название", "description": "описание до 3000 символов", "specs": {"Параметр1": "Значение1", "Параметр2": "Значение2", "Параметр3": "Значение3"}, "tags": ["тег 1", "тег 2", "тег 3"], "category_tips": "совет", "badges": ["значок 1", "значок 2", "значок 3"]}""",
}

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

# ═══ ФОРМАТИРОВАНИЕ ТЕКСТА КАРТОЧКИ ═══

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
        rich = ""
        for s in data.get('rich_content', []):
            rich += f"\n*{s.get('heading','')}*\n{s.get('text','')}\n"
        return (f"🔵 *OZON*\n\n📌 *Название:*\n`{data.get('title','')}`\n\n"
                f"📝 *Описание:*\n{data.get('description','')}\n\n"
                f"🎨 *Rich-контент:*{rich}\n📋 *Атрибуты:*\n{attrs}\n\n"
                f"🔍 `{data.get('keywords','')}`")
    else:
        specs = "\n".join([f" • {k}: {v}" for k, v in data.get('specs', {}).items()])
        tags  = ", ".join(data.get('tags', []))
        return (f"🟡 *ЯНДЕКС МАРКЕТ*\n\n📌 *Название:*\n`{data.get('title','')}`\n\n"
                f"📝 *Описание:*\n{data.get('description','')}\n\n"
                f"⚙️ *Характеристики:*\n{specs}\n\n"
                f"🏷️ `{tags}`\n\n💡 _{data.get('category_tips','')}_")

# ═══ ОТПРАВКА ЗАКАЗА С БИРЖИ ═══

async def send_job_card(bot, job: dict, analysis: dict):
    diff_emoji = {"ЛЁГКИЙ": "🟢", "СРЕДНИЙ": "🟡", "СЛОЖНЫЙ": "🔴"}.get(analysis.get('difficulty',''), "⚪")
    msg = (f"🛍️ *ЗАКАЗ НА КАРТОЧКИ*\n\n{job['source']}\n\n"
           f"📌 *{job['title'][:100]}*\n\n"
           f"💰 {job['budget']}\n"
           f"{diff_emoji} {analysis.get('difficulty','?')} · ⏱ {analysis.get('estimated_time','?')}\n\n"
           f"💬 _{analysis.get('reason','')}_\n\n"
           f"📝 *Proposal:*\n{analysis.get('proposal','')[:400]}\n\n"
           f"🔗 [Открыть заказ]({job['url']})")
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
    data  = query.data
    user_id = update.effective_user.id

    # Выбор маркетплейса
    if data.startswith("mp_"):
        marketplace = data[3:]
        user_sessions[user_id] = {"marketplace": marketplace, "step": "waiting_product"}
        names = {"wb": "🟣 Wildberries", "ozon": "🔵 Ozon", "ym": "🟡 Яндекс Маркет", "all": "🎯 Все"}
        await query.edit_message_text(
            f"*{names.get(marketplace,'?')}* выбран!\n\n"
            f"📸 Отправь *фото товара* (лучше) или просто *название*\n\n"
            f"_Если пришлёшь фото — инфографика будет с реальным снимком товара_",
            parse_mode='Markdown'
        )

    # Смена стиля / регенерация инфографики
    elif data.startswith("style_") or data.startswith("restyle_"):
        parts = data.split("_", 2)
        style_key = parts[1]
        session_key = parts[2] if len(parts) > 2 else ""

        session = user_sessions.get(user_id, {})
        product   = session.get("last_product", session_key)
        mp        = session.get("last_marketplace", "wb")
        card_data = session.get("last_card_data", {})
        photo_b   = session.get("last_photo_bytes")

        await query.answer(f"🎨 Применяю стиль {IMAGE_STYLES.get(style_key,{}).get('name','...')}...")

        try:
            await query.edit_message_text("⏳ Перегенерирую в новом стиле...")
        except Exception:
            pass

        try:
            img_bytes = await generate_product_image(product, card_data, mp, style_key, photo_b)
            style_keyboard = _style_keyboard(style_key, product[:15])
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=img_bytes,
                caption=(
                    f"🖼 *{IMAGE_STYLES[style_key]['name']}*\n"
                    f"_{IMAGE_STYLES[style_key]['desc']}_\n\n"
                    f"✅ Готово для загрузки!\n\n💡 Выбери другой стиль:"
                ),
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(style_keyboard)
            )
        except Exception as e:
            logger.error(f"Ошибка стиля: {e}")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Ошибка: {str(e)[:100]}")

    # Регенерация карточки
    elif data.startswith("regen_"):
        parts = data.split("_", 2)
        mp      = parts[1]
        product = parts[2] if len(parts) > 2 else ""
        await query.edit_message_text("⏳ Генерирую заново...")
        try:
            result = await generate_card(product, mp)
            await send_card_result(query.message, result, mp, product, context.bot, user_id)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {str(e)[:100]}")

    # Оплата USDT
    elif data == "pay_usdt":
        keyboard = [
            [InlineKeyboardButton("1 карточка — $5", callback_data="invoice_5")],
            [InlineKeyboardButton("5 карточек — $20", callback_data="invoice_20")],
            [InlineKeyboardButton("10 карточек — $35", callback_data="invoice_35")],
            [InlineKeyboardButton("50 карточек — $150", callback_data="invoice_150")],
            [InlineKeyboardButton("✏️ Своя сумма", callback_data="invoice_custom")],
        ]
        await query.edit_message_text(
            "💎 *Оплата в USDT*\n\nВыбери пакет:",
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard)
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
            "50 ⭐ = 1 карточка\n200 ⭐ = 5 карточек\n"
            "350 ⭐ = 10 карточек\n1500 ⭐ = 50 карточек\n\nВыбери пакет:",
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard)
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
            await query.edit_message_text("✏️ Напиши сумму в USD:\n\nПример: `25`", parse_mode='Markdown')
        else:
            amount = float(amount_str)
            descriptions = {5: "1 product listing", 20: "5 product listings",
                            35: "10 product listings", 150: "50 product listings"}
            desc = descriptions.get(amount, f"${amount} package")
            msg = (
                f"💎 *INVOICE / СЧЁТ*\n\n📋 Service: *{desc}*\n💰 Amount: *${amount:.2f} USDT*\n\n"
                f"━━━━━━━━━━━━━━━━\n📲 *Payment via @wallet:*\n\n"
                f"1️⃣ Open @wallet in Telegram\n2️⃣ Tap Send → Crypto\n"
                f"3️⃣ Choose USDT TRC20\n4️⃣ Paste address:\n`{USDT_WALLET}`\n"
                f"5️⃣ Amount: `{amount}` USDT\n\n━━━━━━━━━━━━━━━━\n"
                f"⚡ After payment tap button below\n🕐 Work starts within 5 minutes"
            )
            keyboard = [[
                InlineKeyboardButton("✅ I paid / Оплатил", callback_data=f"payment_confirm_{amount}"),
                InlineKeyboardButton("❌ Cancel", callback_data="payment_cancel")
            ]]
            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("payment_confirm_"):
        amount   = float(data[16:])
        user     = update.effective_user
        username = f"@{user.username}" if user.username else user.first_name
        await context.bot.send_message(
            chat_id=YOUR_CHAT_ID,
            text=(f"💰 *ОПЛАТА!*\n\n👤 {username}\n💎 ${amount:.2f} USDT\n"
                  f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n⚡ Проверь кошелёк!"),
            parse_mode='Markdown'
        )
        await query.edit_message_text(
            f"✅ *Thank you!*\n\nPayment ${amount:.2f} USDT confirmed.\nWork starts in 5 min!\n\n"
            f"📱 Send your product info / Отправь данные товара",
            parse_mode='Markdown'
        )
        user_sessions[user.id] = {"step": "waiting_product", "marketplace": "all", "paid": True}

    elif data.startswith("stars_"):
        stars = int(data[6:])
        stars_map = {
            50:   ("1 карточка товара", "Профессиональная карточка для WB, Ozon или Amazon"),
            200:  ("5 карточек товаров", "5 профессиональных карточек"),
            350:  ("10 карточек товаров", "10 профессиональных карточек — скидка 30%"),
            1500: ("50 карточек товаров", "50 профессиональных карточек — скидка 40%"),
        }
        title, description = stars_map.get(stars, ("Карточки", "Профессиональные карточки"))
        await send_stars_invoice(update, context, stars, title, description)

    elif data.startswith("take_"):
        job_id = data[5:]
        job = get_job(job_id)
        if not job:
            await query.edit_message_text("❌ Заказ не найден")
            return
        update_job(job_id, 'accepted')
        await query.edit_message_text(
            f"✅ *Берём!*\n📌 {job['title'][:80]}\n\n⏳ Выполняю...", parse_mode='Markdown'
        )
        try:
            result = await execute_card_job(job)
            update_job(job_id, 'completed', result)
            keyboard = [[
                InlineKeyboardButton("👍 ОК, сдаём!", callback_data=f"done_{job_id}"),
                InlineKeyboardButton("✏️ Правка", callback_data=f"redo_{job_id}")
            ]]
            msg = (f"✨ *КАРТОЧКИ ГОТОВЫ!*\n\n📌 *{job['title'][:80]}*\n\n"
                   f"━━━━━━━━━━\n{result[:2500]}\n━━━━━━━━━━\n\n*Лила, проверь — отправляем?*")
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
        job    = get_job(job_id)
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
            f"💰 *ЗАКРЫТ!*\n\n✅ Выполнено: {stats['by_status'].get('done',0)}\n"
            f"💵 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}",
            parse_mode='Markdown'
        )

    elif data.startswith("redo_"):
        job_id = data[5:]
        job    = get_job(job_id)
        context.user_data['redo_job_id']  = job_id
        context.user_data['redo_result']  = job.get('result','') if job else ''
        await query.edit_message_text(
            "✏️ *Напиши что исправить:*\n\nПример: _сократи_, _переведи на английский_",
            parse_mode='Markdown'
        )

# ═══ ВСПОМОГАТЕЛЬНЫЕ ═══

def _style_keyboard(current_style: str, product_short: str) -> list:
    rows = []
    for sk, sd in IMAGE_STYLES.items():
        emoji = "✅ " if sk == current_style else ""
        rows.append([InlineKeyboardButton(
            f"{emoji}{sd['name']} — {sd['desc']}",
            callback_data=f"style_{sk}_{product_short}"
        )])
    return rows

async def send_card_result(message, result, marketplace, product, bot, user_id=None):
    if marketplace == "all":
        for mp, data in result.items():
            text = format_card(data, mp)
            await bot.send_message(chat_id=message.chat_id, text=text[:4000], parse_mode='Markdown')
            await asyncio.sleep(0.5)
        # Генерируем инфографику для первого маркетплейса
        mp0   = list(result.keys())[0]
        data0 = list(result.values())[0]
        title = data0.get("title", product)
        photo_b = user_sessions.get(user_id, {}).get("last_photo_bytes") if user_id else None
        img_bytes = await generate_product_image(product, data0, mp0, "studio", photo_b)
        style_kb = _style_keyboard("studio", product[:15])
        await bot.send_photo(
            chat_id=message.chat_id, photo=img_bytes,
            caption="🖼 *Инфографика готова!*\n\n💡 Выбери стиль:",
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(style_kb)
        )
    else:
        text = format_card(result, marketplace)
        await bot.send_message(chat_id=message.chat_id, text=text[:4000], parse_mode='Markdown')
        title   = result.get("title", product)
        photo_b = user_sessions.get(user_id, {}).get("last_photo_bytes") if user_id else None
        img_bytes = await generate_product_image(product, result, marketplace, "studio", photo_b)
        style_kb  = _style_keyboard("studio", product[:15])
        await bot.send_photo(
            chat_id=message.chat_id, photo=img_bytes,
            caption=(
                f"🖼 *Студийный стиль*\n\n"
                f"✅ {'На основе вашего фото!' if photo_b else 'Инфографика готова!'}\n\n"
                f"💡 Выбери другой стиль:"
            ),
            parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(style_kb)
        )

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
        logger.error(f"Stars invoice ошибка: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⭐ Оплата {stars} Stars — напиши нам и мы выставим счёт вручную."
        )

# ═══ ОБРАБОТЧИК СООБЩЕНИЙ ═══

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Режим правки
    if context.user_data.get('redo_job_id'):
        job_id   = context.user_data['redo_job_id']
        original = context.user_data.get('redo_result', '')
        fix      = update.message.text
        await update.message.reply_text("⏳ Исправляю...")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json={"model": "llama-3.3-70b-versatile",
                          "messages": [{"role": "user", "content":
                              f"Исправь карточку товара.\n\nОРИГИНАЛ:\n{original[:2000]}\n\nИНСТРУКЦИЯ: {fix}\n\nВерни полный исправленный текст."}],
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

    # Не выбран маркетплейс
    if user_id not in user_sessions or user_sessions[user_id].get('step') != 'waiting_product':
        keyboard = [[
            InlineKeyboardButton("🟣 WB",   callback_data="mp_wb"),
            InlineKeyboardButton("🔵 Ozon", callback_data="mp_ozon"),
            InlineKeyboardButton("🟡 ЯМ",   callback_data="mp_ym"),
            InlineKeyboardButton("🎯 Все",  callback_data="mp_all"),
        ]]
        await update.message.reply_text(
            "👇 Сначала выбери маркетплейс:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    marketplace  = user_sessions[user_id]['marketplace']
    image_base64 = None
    photo_bytes  = None
    product      = ""

    # ─── ФОТО ТОВАРА ───
    if update.message.photo:
        await update.message.reply_text(
            "📸 *Фото получено!*\n\n⏳ Генерирую карточку и инфографику с твоим товаром...",
            parse_mode='Markdown'
        )
        photo      = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await photo_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                photo_bytes  = f.read()
                image_base64 = base64.b64encode(photo_bytes).decode()
            os.unlink(tmp.name)
        product = update.message.caption or "товар на фото"

    # ─── ТОЛЬКО ТЕКСТ ───
    elif update.message.text:
        product = update.message.text
        await update.message.reply_text(
            f"⏳ Генерирую карточку для *{product[:40]}*...\n\n"
            f"_Совет: пришли фото товара — инфографика будет красивее!_",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("Отправь текст или фото товара!")
        return

    # Сохраняем в сессию для смены стиля
    session_mp = marketplace if marketplace != "all" else "wb"
    user_sessions[user_id]['last_product']    = product
    user_sessions[user_id]['last_marketplace'] = session_mp
    user_sessions[user_id]['last_photo_bytes'] = photo_bytes

    try:
        result = await generate_card(product, marketplace, image_base64)
        # Сохраняем card_data для смены стиля
        if marketplace == "all":
            user_sessions[user_id]['last_card_data'] = list(result.values())[0]
        else:
            user_sessions[user_id]['last_card_data'] = result

        await send_card_result(update.message, result, marketplace, product, context.bot, user_id)
        user_sessions[user_id]['step'] = 'done'

    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await update.message.reply_text(
            f"❌ Ошибка. Попробуй ещё раз.\n`{str(e)[:100]}`",
            parse_mode='Markdown'
        )

# ═══ КОМАНДЫ ═══

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🟣 Wildberries", callback_data="mp_wb"),
         InlineKeyboardButton("🔵 Ozon",        callback_data="mp_ozon")],
        [InlineKeyboardButton("🟡 Яндекс Маркет", callback_data="mp_ym"),
         InlineKeyboardButton("🎯 Все сразу",   callback_data="mp_all")],
    ]
    await update.message.reply_text(
        "🛍️ *КарточникБот*\n\n"
        "Генерирую карточки товаров + профессиональную инфографику!\n\n"
        "📸 *Пришли фото товара* — сделаю карточку как у конкурентов\n"
        "📝 *Или напиши название* — сгенерирую сам\n\n"
        "Выбери маркетплейс:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = await update.message.reply_text("🔍 Ищу заказы на карточки...")
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
    stats     = get_stats()
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
        [InlineKeyboardButton("💎 Оплата USDT",        callback_data="pay_usdt")],
        [InlineKeyboardButton("⭐ Telegram Stars",      callback_data="pay_stars")],
        [InlineKeyboardButton("🇷🇺 Рубли (СБП/ЮMoney)", callback_data="pay_rub")],
    ]
    await update.message.reply_text(
        "💰 *ПРАЙС — КАРТОЧКИ ТОВАРОВ*\n\n"
        "🛍️ *Wildberries / Ozon / ЯМ*\n\n"
        "🟢 *Эконом* — текст\n"
        " • 1 карточка: $5 / 50⭐ / 400₽\n\n"
        "🔵 *Стандарт* — текст + SEO\n"
        " • 5 карточек: $20 / 200⭐\n\n"
        "🟣 *Бизнес* — текст + SEO + инфографика\n"
        " • 10 карточек: $35 / 350⭐\n\n"
        "🌍 *Amazon / Etsy / eBay*\n"
        " • от $8 за карточку\n\n"
        "Выбери способ оплаты:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ═══ АВТОСКАНИРОВАНИЕ ═══

async def auto_scan(context):
    logger.info("🔄 Автосканирование...")
    try:
        count = await check_card_jobs(context.bot)
        logger.info(f"✅ Найдено {count} заказов")
    except Exception as e:
        logger.error(f"❌ Автосканирование: {e}")

# ═══ ЗАПУСК ═══

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  start_command))
    app.add_handler(CommandHandler("scan",   scan_command))
    app.add_handler(CommandHandler("clear",  clear_command))
    app.add_handler(CommandHandler("stats",  stats_command))
    app.add_handler(CommandHandler("price",  price_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))

    # Автосканирование каждые 30 минут
    app.job_queue.run_repeating(auto_scan, interval=1800, first=60)

    logger.info("🛍️ КарточникБот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
