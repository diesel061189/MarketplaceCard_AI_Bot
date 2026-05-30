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

TELEGRAM_TOKEN = os.getenv("CARD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "/tmp/freelance.db")

user_sessions = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ═══ ИСТОЧНИКИ ЗАКАЗОВ НА КАРТОЧКИ ═══
CARD_RSS_FEEDS = [
    ("https://www.fl.ru/rss/all.xml?category=3", "🇷🇺 FL.ru"),
    ("https://www.weblancer.net/jobs/feed/?cat=13", "🇷🇺 Weblancer"),
    ("https://freelance.ru/rss/projects.xml", "🇷🇺 Freelance.ru"),
    ("https://www.guru.com/jobs/rss/?skill=writing", "🟠 Guru.com"),
    ("https://www.peopleperhour.com/jobs/rss?service=writing", "🔵 PPH"),
]

TG_CARD_CHANNELS = [
    "wb_sellers_ru",
    "ozon_sellers",
    "marketplace_freelance",
    "kopiraiting_ru",
]

# Ключевые слова для заказов на карточки
CARD_KEYWORDS = [
    # Русские
    "карточка товара", "карточки товаров", "описание товара", "описание продукта",
    "wildberries", "вайлдберриз", "wb карточка", "ozon карточка", "озон карточка",
    "яндекс маркет", "маркетплейс", "контент для вб", "контент для озон",
    "seo описание", "продающее описание", "копирайтинг товар",
    "наполнение карточек", "написать карточку", "заполнить карточку",
    "характеристики товара", "ключевые слова товар", "rich контент",
    # Английские
    "product description", "marketplace content", "amazon listing",
    "product listing", "ecommerce content", "product copywriting",
    "amazon seo", "etsy listing", "shopify product",
]

CARD_BLACKLIST = [
    "разработка сайта", "программирование", "верстка", "дизайн логотип",
    "видеомонтаж", "анимация", "таргет", "реклама настройка",
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
            r = await client.get(url)
            if r.status_code != 200:
                continue
            feed = feedparser.parse(r.text)
            for e in feed.entries[:8]:
                link = e.get('link', '')
                if not link or is_seen(link):
                    continue
                title = clean_html(e.get('title', ''))
                desc = clean_html(e.get('summary', ''))
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
            logger.info(f"✅ {source}: {len(jobs)} карточных заказов")
        except Exception as e:
            logger.error(f"❌ {source}: {e}")
    return jobs

async def parse_tg_card_channels(client) -> list:
    jobs = []
    for channel in TG_CARD_CHANNELS:
        try:
            r = await client.get(f"https://t.me/s/{channel}", headers=HEADERS)
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

# ═══ AI ═══
MARKETPLACE_PROMPTS = {
    "wb": """Создай продающую карточку для Wildberries. Верни ТОЛЬКО JSON:
{{"title": "заголовок до 100 символов", "description": "описание 500-1000 символов", "characteristics": ["характеристика 1", "характеристика 2"], "keywords": "ключевые слова через запятую", "seo_tips": "совет"}}""",
    "ozon": """Создай карточку для Ozon. Верни ТОЛЬКО JSON:
{{"title": "название до 200 символов", "description": "описание 1000-3000 символов", "rich_content": [{{"heading": "Заголовок", "text": "Текст"}}], "attributes": ["атрибут 1", "атрибут 2"], "keywords": "ключевые слова"}}""",
    "ym": """Создай карточку для Яндекс Маркет. Верни ТОЛЬКО JSON:
{{"title": "точное название", "description": "описание до 3000 символов", "specs": {{"Параметр": "Значение"}}, "tags": ["тег 1", "тег 2"], "category_tips": "совет"}}""",
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
        if "```" in text:
            text = text.split("```")[1].split("```")[0].replace("json","").strip()
        return json.loads(text)

async def execute_card_job(job: dict) -> str:
    """Выполняем заказ на карточку — генерируем для всех маркетплейсов"""
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

# ═══ ФОРМАТИРОВАНИЕ КАРТОЧЕК ═══
def format_card(data: dict, marketplace: str) -> str:
    if marketplace == "wb":
        chars = "\n".join([f"  • {c}" for c in data.get('characteristics', [])])
        return f"🟣 *WILDBERRIES*\n\n📌 *Заголовок:*\n`{data.get('title','')}`\n\n📝 *Описание:*\n{data.get('description','')}\n\n📋 *Характеристики:*\n{chars}\n\n🔍 *Ключевые слова:*\n`{data.get('keywords','')}`\n\n💡 _{data.get('seo_tips','')}_"
    elif marketplace == "ozon":
        attrs = "\n".join([f"  • {a}" for a in data.get('attributes', [])])
        rich = ""
        for s in data.get('rich_content', []):
            rich += f"\n*{s.get('heading','')}*\n{s.get('text','')}\n"
        return f"🔵 *OZON*\n\n📌 *Название:*\n`{data.get('title','')}`\n\n📝 *Описание:*\n{data.get('description','')}\n\n🎨 *Rich-контент:*{rich}\n📋 *Атрибуты:*\n{attrs}\n\n🔍 `{data.get('keywords','')}`"
    else:
        specs = "\n".join([f"  • {k}: {v}" for k, v in data.get('specs', {}).items()])
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

# ═══ КНОПКИ ═══
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # Выбор маркетплейса для генерации
    if data.startswith("mp_"):
        marketplace = data[3:]
        user_sessions[user_id] = {"marketplace": marketplace, "step": "waiting_product"}
        names = {"wb": "🟣 Wildberries", "ozon": "🔵 Ozon", "ym": "🟡 Яндекс Маркет", "all": "🎯 Все"}
        await query.edit_message_text(
            f"{names.get(marketplace,'?')} выбран!\n\n"
            f"Отправь название товара или фото:",
            parse_mode='Markdown'
        )

    # Перегенерировать
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

    # Сменить маркетплейс
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
            USDT_WALLET = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
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
        
        # Уведомляем владельца
        await context.bot.send_message(
            chat_id=YOUR_CHAT_ID,
            text=f"💰 *ОПЛАТА ПОЛУЧЕНА!*\n\n"
                 f"👤 Клиент: {username}\n"
                 f"💎 Сумма: ${amount:.2f} USDT\n"
                 f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
                 f"⚡ Проверь кошелёк и начни работу!",
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
        # Сбрасываем сессию для приёма товара
        user_sessions[user.id] = {"step": "waiting_product", "marketplace": "all", "paid": True}

    elif data.startswith("stars_"):
        stars = int(data[6:])
        stars_map = {
            50: ("1 карточка товара", "Профессиональная карточка для WB, Ozon, Amazon или другого маркетплейса"),
            200: ("5 карточек товаров", "5 профессиональных карточек для любых маркетплейсов"),
            350: ("10 карточек товаров", "10 профессиональных карточек — скидка 30%"),
            1500: ("50 карточек товаров", "50 профессиональных карточек — максимальная скидка 40%"),
        }
        title, description = stars_map.get(stars, ("Карточки товаров", "Профессиональные карточки"))
        await send_stars_invoice(update, context, stars, title, description)

    elif data == "change_mp_":
        keyboard = [
            [InlineKeyboardButton("🟣 WB", callback_data="mp_wb"),
             InlineKeyboardButton("🔵 Ozon", callback_data="mp_ozon")],
            [InlineKeyboardButton("🟡 ЯМ", callback_data="mp_ym"),
             InlineKeyboardButton("🎯 Все", callback_data="mp_all")],
        ]
        await query.edit_message_text("Выбери маркетплейс:", reply_markup=InlineKeyboardMarkup(keyboard))

    # Взять заказ с биржи
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
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=f"✨ *КАРТОЧКИ ГОТОВЫ!*\n\n📌 *{job['title'][:80]}*\n\n━━━━━━━━━━\n{result[:2500]}\n━━━━━━━━━━\n\n*Лила, проверь — отправляем?*",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
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
        await query.edit_message_text("✏️ Напиши что исправить:")

async def send_card_result(message, result, marketplace, product, bot):
    if marketplace == "all":
        for mp, data in result.items():
            text = format_card(data, mp)
            await bot.send_message(chat_id=message.chat_id, text=text[:4000], parse_mode='Markdown')
            await asyncio.sleep(0.5)
        keyboard = [[
            InlineKeyboardButton("🔄 Заново", callback_data=f"regen_all_{product[:50]}"),
            InlineKeyboardButton("🔀 Другой", callback_data="change_mp_")
        ]]
        await bot.send_message(chat_id=message.chat_id, text="✅ *Все карточки готовы!*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        text = format_card(result, marketplace)
        keyboard = [[
            InlineKeyboardButton("🔄 Заново", callback_data=f"regen_{marketplace}_{product[:50]}"),
            InlineKeyboardButton("🔀 Другой", callback_data="change_mp_")
        ]]
        await bot.send_message(chat_id=message.chat_id, text=text[:4000], parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

# ═══ ОБРАБОТЧИК СООБЩЕНИЙ ═══
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
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

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    await update.message.reply_text(
        f"📊 *СТАТИСТИКА КАРТОЧНИКА*\n\n"
        f"🔍 Найдено заказов: {stats['by_status'].get('found',0)}\n"
        f"✅ Принято: {stats['by_status'].get('accepted',0)}\n"
        f"🏁 Выполнено: {stats['by_status'].get('done',0)}\n"
        f"💰 Заработано: ${stats['earn_usd']:.2f} / ₽{stats['earn_rub']:.0f}",
        parse_mode='Markdown'
    )

async def stars_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оплата звёздами Telegram"""
    keyboard = [
        [InlineKeyboardButton("⭐ 50 Stars — 1 карточка", callback_data="stars_50")],
        [InlineKeyboardButton("⭐ 200 Stars — 5 карточек", callback_data="stars_200")],
        [InlineKeyboardButton("⭐ 350 Stars — 10 карточек", callback_data="stars_350")],
        [InlineKeyboardButton("⭐ 1500 Stars — 50 карточек", callback_data="stars_1500")],
    ]
    await update.message.reply_text(
        "⭐ *Оплата Telegram Stars*\n\n"
        "Быстро и безопасно прямо в Telegram!\n\n"
        "50 ⭐ = 1 карточка товара\n"
        "200 ⭐ = 5 карточек (-20%)\n"
        "350 ⭐ = 10 карточек (-30%)\n"
        "1500 ⭐ = 50 карточек (-40%)\n\n"
        "Выбери пакет:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def send_stars_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, stars: int, title: str, description: str):
    """Отправляет счёт на оплату звёздами"""
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=description,
        payload=f"card_{stars}_{update.effective_user.id}",
        currency="XTR",  # Telegram Stars
        prices=[{"label": title, "amount": stars}],
    )

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждаем платёж"""
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатываем успешный платёж звёздами"""
    payment = update.message.successful_payment
    stars = payment.total_amount
    user = update.effective_user
    username = f"@{user.username}" if user.username else user.first_name
    
    # Считаем количество карточек
    cards_map = {50: 1, 200: 5, 350: 10, 1500: 50}
    cards_count = cards_map.get(stars, 1)
    
    # Уведомляем владельца
    await context.bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=f"⭐ *ОПЛАТА ЗВЁЗДАМИ!*\n\n"
             f"👤 Клиент: {username}\n"
             f"⭐ Stars: {stars}\n"
             f"📦 Карточек: {cards_count}\n"
             f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        parse_mode='Markdown'
    )
    
    # Отвечаем клиенту
    await update.message.reply_text(
        f"✅ *Спасибо! Оплата получена!*\n\n"
        f"⭐ {stars} Stars\n"
        f"📦 Карточек: {cards_count}\n\n"
        f"Отправь фото или описание товара — начинаем! 🚀",
        parse_mode='Markdown'
    )
    
    # Готовим сессию
    user_sessions[user.id] = {
        "step": "waiting_product",
        "marketplace": "all",
        "paid": True,
        "cards_left": cards_count
    }

async def invoice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Быстро выставить счёт клиенту"""
    args = context.args
    if args:
        try:
            amount = float(args[0])
            desc = " ".join(args[1:]) if len(args) > 1 else "Product listing service"
            USDT_WALLET = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
            msg = (
                f"💎 *INVOICE*\n\n"
                f"📋 {desc}\n"
                f"💰 *${amount:.2f} USDT*\n\n"
                f"📲 Send to @wallet:\n"
                f"`{USDT_WALLET}`\n"
                f"Network: TRC20\n\n"
                f"After payment: /start"
            )
            await update.message.reply_text(msg, parse_mode='Markdown')
        except:
            await update.message.reply_text("❌ Формат: `/invoice 25 5 карточек WB`", parse_mode='Markdown')
    else:
        await update.message.reply_text(
            "💎 *Выставить счёт*\n\n"
            "Формат: `/invoice [сумма] [описание]`\n\n"
            "Примеры:\n"
            "`/invoice 5 1 Amazon listing`\n"
            "`/invoice 20 5 карточек WB`\n"
            "`/invoice 35 10 Ozon listings`",
            parse_mode='Markdown'
        )

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принять заказ от клиента"""
    args = context.args
    order_text = " ".join(args) if args else "не указан"
    user = update.effective_user
    username = f"@{user.username}" if user.username else user.first_name
    
    # Уведомляем владельца
    await context.bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=f"📦 *НОВЫЙ ЗАКАЗ!*\n\n"
             f"👤 Клиент: {username}\n"
             f"📋 Заказ: {order_text}\n"
             f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
             f"Ответь клиенту и выстави счёт через /invoice",
        parse_mode='Markdown'
    )
    await update.message.reply_text(
        f"✅ Заказ принят!\n\n"
        f"📋 {order_text}\n\n"
        f"Мы свяжемся с вами в течение 5 минут.\n"
        f"We'll contact you within 5 minutes. 🚀",
    )
    msg = await update.message.reply_text("🔍 Ищу заказы на карточки...")
    count = await check_card_jobs(context.application.bot)
    await msg.edit_text(f"✅ Найдено заказов: {count}\n{'Заказы летят! 🚀' if count > 0 else 'Пока тихо, ищу дальше ⏳'}")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Оплатить в рублях", callback_data="pay_rub")],
        [InlineKeyboardButton("💎 Pay in USDT (Telegram Wallet)", callback_data="pay_usdt")],
        [InlineKeyboardButton("⭐ Оплатить Stars", callback_data="pay_stars")],
    ]
    await update.message.reply_text(
        "💰 *ПРАЙС-ЛИСТ*\n\n"
        "🇷🇺 *Российские маркетплейсы:*\n"
        "🟣 Wildberries — от 150 ₽/карточка\n"
        "🔵 Ozon — от 200 ₽/карточка\n"
        "🟡 Яндекс Маркет — от 150 ₽/карточка\n"
        "🎯 Все 3 сразу — от 400 ₽\n\n"
        "🌍 *International marketplaces:*\n"
        "🟠 Amazon — from $5/listing\n"
        "🔴 eBay — from $3/listing\n"
        "🟢 Etsy — from $5/listing\n"
        "🛍️ Shopify — from $5/listing\n"
        "🎯 All platforms — from $15\n\n"
        "📦 *Пакеты / Packages:*\n"
        "• 10 карточек — скидка 10%\n"
        "• 50 карточек — скидка 20%\n"
        "• 100+ карточек — скидка 30%\n\n"
        "✅ 1 карточка = 2 минуты\n"
        "✅ Оплата: рубли или USDT",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def send_invoice_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE, amount_usd: float, description: str):
    """Отправляет счёт на оплату в USDT через Telegram Wallet"""
    USDT_WALLET = os.getenv("USDT_WALLET", "TECM5HuPvi9Z6RNzbHZLtesSkKwHBLJEJc")
    
    msg = (
        f"💎 *Invoice / Счёт на оплату*\n\n"
        f"📋 {description}\n"
        f"💰 Amount: *${amount_usd:.2f} USDT*\n\n"
        f"📲 *Как оплатить:*\n"
        f"1. Открой @wallet в Telegram\n"
        f"2. Нажми Отправить / Send\n"
        f"3. Вставь адрес:\n"
        f"`{USDT_WALLET}`\n"
        f"4. Сумма: `{amount_usd}` USDT\n"
        f"5. Сеть: *TRC20 (Tron)*\n\n"
        f"⚡ После оплаты нажми кнопку ниже"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Оплатил / Paid", callback_data=f"payment_confirm_{amount_usd}"),
        InlineKeyboardButton("❌ Отмена", callback_data="payment_cancel")
    ]]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

# ═══ ГЛАВНЫЙ ПАРСЕР ═══
async def check_card_jobs(bot) -> int:
    logger.info("🛍️ Ищу заказы на карточки...")
    all_jobs = []
    async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
        results = await asyncio.gather(
            parse_card_jobs(client),
            parse_tg_card_channels(client),
            return_exceptions=True
        )
        for r in results:
            if isinstance(r, list):
                all_jobs.extend(r)

    logger.info(f"📦 Найдено заказов на карточки: {len(all_jobs)}")
    sent = 0
    for job in all_jobs[:3]:
        try:
            save_job(job)
            analysis = await analyze_card_job(job)
            if analysis.get('can_do', True):
                await send_job_card(bot, job, analysis)
                sent += 1
                await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    return sent

async def periodic_check(app):
    await asyncio.sleep(60)
    while True:
        try:
            await check_card_jobs(app.bot)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
        await asyncio.sleep(15 * 60)

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CommandHandler("stars", stars_command))
    app.add_handler(CommandHandler("invoice", invoice_command))
    app.add_handler(CommandHandler("order", order_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    from telegram.ext import PreCheckoutQueryHandler
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))

    async def post_init(application):
        asyncio.create_task(periodic_check(application))
    app.post_init = post_init

    logger.info("🛍️ КарточникБот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
