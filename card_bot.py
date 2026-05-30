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

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Ищу заказы на карточки...")
    count = await check_card_jobs(context.application.bot)
    await msg.edit_text(f"✅ Найдено заказов: {count}\n{'Заказы летят! 🚀' if count > 0 else 'Пока тихо, ищу дальше ⏳'}")

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 *ПРАЙС-ЛИСТ*\n\n"
        "🟣 Wildberries — от 150 руб/карточка\n"
        "🔵 Ozon — от 200 руб/карточка\n"
        "🟡 Яндекс Маркет — от 150 руб/карточка\n"
        "🎯 Все 3 — от 400 руб\n\n"
        "📦 *Пакеты:*\n"
        "• 10 карточек — скидка 10%\n"
        "• 50 карточек — скидка 20%\n"
        "• 100+ карточек — скидка 30%\n\n"
        "✅ 1 карточка = 2 минуты\n"
        "✅ Оплата после получения",
        parse_mode='Markdown'
    )

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
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))

    async def post_init(application):
        asyncio.create_task(periodic_check(application))
    app.post_init = post_init

    logger.info("🛍️ КарточникБот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
