import os
import json
import logging
import asyncio
import httpx
import base64
import tempfile
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("CARD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YOUR_CHAT_ID = int(os.getenv("YOUR_CHAT_ID", "0"))

# Хранилище сессий пользователей
user_sessions = {}

# ═══ ПРОМПТЫ ДЛЯ КАЖДОГО МАРКЕТПЛЕЙСА ═══
MARKETPLACE_PROMPTS = {
    "wb": {
        "name": "Wildberries",
        "emoji": "🟣",
        "prompt": """Ты эксперт по контенту для Wildberries. Создай продающую карточку товара.

ТРЕБОВАНИЯ WB:
- Заголовок: до 100 символов, главный ключевой запрос в начале
- Описание: 500-1000 символов, ключевые слова, выгоды для покупателя
- Характеристики: список основных параметров
- Ключевые слова: 15-20 поисковых запросов через запятую

ТОВАР: {product}

Верни ТОЛЬКО JSON:
{{
  "title": "заголовок товара",
  "description": "продающее описание",
  "characteristics": ["характеристика 1", "характеристика 2", "характеристика 3"],
  "keywords": "ключевое слово 1, ключевое слово 2, ключевое слово 3",
  "seo_tips": "совет по продвижению на WB"
}}"""
    },
    "ozon": {
        "name": "Ozon",
        "emoji": "🔵",
        "prompt": """Ты эксперт по контенту для Ozon. Создай продающую карточку товара.

ТРЕБОВАНИЯ OZON:
- Название: до 200 символов, SEO-оптимизированное
- Описание: 1000-3000 символов, структурированное, с выгодами
- Rich-контент: заголовки секций и их содержимое
- Атрибуты: ключевые характеристики

ТОВАР: {product}

Верни ТОЛЬКО JSON:
{{
  "title": "название товара",
  "description": "подробное описание",
  "rich_content": [
    {{"heading": "Заголовок секции", "text": "Текст секции"}},
    {{"heading": "Заголовок секции 2", "text": "Текст секции 2"}}
  ],
  "attributes": ["атрибут 1", "атрибут 2", "атрибут 3"],
  "keywords": "ключевые слова через запятую"
}}"""
    },
    "ym": {
        "name": "Яндекс Маркет",
        "emoji": "🟡",
        "prompt": """Ты эксперт по контенту для Яндекс Маркет. Создай карточку товара.

ТРЕБОВАНИЯ ЯМ:
- Название: точное, с характеристиками (бренд, модель, параметры)
- Описание: до 3000 символов, информативное, без воды
- Технические характеристики
- Теги для поиска

ТОВАР: {product}

Верни ТОЛЬКО JSON:
{{
  "title": "точное название",
  "description": "информативное описание",
  "specs": {{"Параметр 1": "Значение 1", "Параметр 2": "Значение 2"}},
  "tags": ["тег 1", "тег 2", "тег 3"],
  "category_tips": "совет по категории"
}}"""
    },
    "all": {
        "name": "Все маркетплейсы",
        "emoji": "🎯",
        "prompt": ""  # Используем все три
    }
}

# ═══ AI ГЕНЕРАЦИЯ ═══
async def generate_card(product_description: str, marketplace: str, image_base64: str = None) -> dict:
    """Генерирует карточку товара для выбранного маркетплейса"""
    
    if marketplace == "all":
        # Генерируем для всех трёх
        results = {}
        for mp in ["wb", "ozon", "ym"]:
            results[mp] = await generate_single_card(product_description, mp, image_base64)
        return results
    else:
        return await generate_single_card(product_description, marketplace, image_base64)

async def generate_single_card(product: str, marketplace: str, image_base64: str = None) -> dict:
    mp_data = MARKETPLACE_PROMPTS[marketplace]
    prompt = mp_data["prompt"].format(product=product)
    
    if image_base64:
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
            {"type": "text", "text": f"Это фото товара. {prompt}"}
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

def format_wb_card(data: dict) -> str:
    chars = "\n".join([f"  • {c}" for c in data.get('characteristics', [])])
    return f"""🟣 *WILDBERRIES*

📌 *Заголовок:*
`{data.get('title', '')}`

📝 *Описание:*
{data.get('description', '')}

📋 *Характеристики:*
{chars}

🔍 *Ключевые слова:*
`{data.get('keywords', '')}`

💡 *SEO совет:*
_{data.get('seo_tips', '')}_"""

def format_ozon_card(data: dict) -> str:
    attrs = "\n".join([f"  • {a}" for a in data.get('attributes', [])])
    rich = ""
    for section in data.get('rich_content', []):
        rich += f"\n*{section.get('heading','')}*\n{section.get('text','')}\n"
    return f"""🔵 *OZON*

📌 *Название:*
`{data.get('title', '')}`

📝 *Описание:*
{data.get('description', '')}

🎨 *Rich-контент:*
{rich}
📋 *Атрибуты:*
{attrs}

🔍 *Ключевые слова:*
`{data.get('keywords', '')}`"""

def format_ym_card(data: dict) -> str:
    specs = "\n".join([f"  • {k}: {v}" for k, v in data.get('specs', {}).items()])
    tags = ", ".join(data.get('tags', []))
    return f"""🟡 *ЯНДЕКС МАРКЕТ*

📌 *Название:*
`{data.get('title', '')}`

📝 *Описание:*
{data.get('description', '')}

⚙️ *Характеристики:*
{specs}

🏷️ *Теги:*
`{tags}`

💡 *Совет по категории:*
_{data.get('category_tips', '')}_"""

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
        "Генерирую продающие карточки товаров для маркетплейсов!\n\n"
        "Выбери маркетплейс:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как использовать:*\n\n"
        "1️⃣ Нажми /start → выбери маркетплейс\n"
        "2️⃣ Отправь описание товара или фото\n"
        "3️⃣ Получи готовую карточку!\n\n"
        "*Форматы описания:*\n"
        "• Просто текст: `Силиконовый чехол для iPhone 15, чёрный`\n"
        "• Подробно: название + характеристики + материал\n"
        "• Фото товара (бот сам определит что на нём)\n\n"
        "*Команды:*\n"
        "/start — выбрать маркетплейс\n"
        "/price — прайс-лист\n"
        "/examples — примеры карточек",
        parse_mode='Markdown'
    )

async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 *ПРАЙС-ЛИСТ*\n\n"
        "🟣 Wildberries — от 150 руб/карточка\n"
        "🔵 Ozon — от 200 руб/карточка\n"
        "🟡 Яндекс Маркет — от 150 руб/карточка\n"
        "🎯 Все 3 маркетплейса — от 400 руб\n\n"
        "📦 *Пакеты:*\n"
        "• 10 карточек — скидка 10%\n"
        "• 50 карточек — скидка 20%\n"
        "• 100+ карточек — скидка 30%\n\n"
        "✅ Срок: 1 карточка за 2 минуты\n"
        "✅ Оплата после получения",
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("mp_"):
        marketplace = data[3:]
        user_sessions[user_id] = {"marketplace": marketplace, "step": "waiting_product"}
        mp_data = MARKETPLACE_PROMPTS[marketplace]
        
        await query.edit_message_text(
            f"{mp_data['emoji']} *{mp_data['name']}* выбран!\n\n"
            f"Отправь мне:\n"
            f"• Название и описание товара\n"
            f"• Или фото товара\n\n"
            f"Пример: `Силиконовый чехол iPhone 15 Pro, чёрный матовый, защита от падений`",
            parse_mode='Markdown'
        )

    elif data.startswith("regen_"):
        # Перегенерировать
        parts = data.split("_", 2)
        marketplace = parts[1]
        product = parts[2] if len(parts) > 2 else ""
        
        if product and user_id in user_sessions:
            await query.edit_message_text("⏳ Генерирую заново...")
            try:
                result = await generate_card(product, marketplace)
                await send_card_result(query.message, result, marketplace, product, context.bot, user_id)
            except Exception as e:
                await query.edit_message_text(f"❌ Ошибка: {str(e)[:100]}")

    elif data.startswith("change_mp_"):
        # Сменить маркетплейс
        keyboard = [
            [InlineKeyboardButton("🟣 Wildberries", callback_data="mp_wb"),
             InlineKeyboardButton("🔵 Ozon", callback_data="mp_ozon")],
            [InlineKeyboardButton("🟡 Яндекс Маркет", callback_data="mp_ym"),
             InlineKeyboardButton("🎯 Все сразу", callback_data="mp_all")],
        ]
        await query.edit_message_text(
            "Выбери маркетплейс:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def send_card_result(message, result, marketplace, product, bot, user_id):
    """Отправляет результат генерации"""
    
    if marketplace == "all":
        # Отправляем три карточки
        for mp, data in result.items():
            if mp == "wb":
                text = format_wb_card(data)
            elif mp == "ozon":
                text = format_ozon_card(data)
            else:
                text = format_ym_card(data)
            
            await bot.send_message(
                chat_id=message.chat_id,
                text=text[:4000],
                parse_mode='Markdown'
            )
            await asyncio.sleep(0.5)
        
        # Кнопки после всех карточек
        keyboard = [[
            InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"regen_all_{product[:50]}"),
            InlineKeyboardButton("🔀 Другой маркетплейс", callback_data="change_mp_")
        ]]
        await bot.send_message(
            chat_id=message.chat_id,
            text="✅ *Все карточки готовы!*\n\nСкопируй нужное и используй на маркетплейсе.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        if marketplace == "wb":
            text = format_wb_card(result)
        elif marketplace == "ozon":
            text = format_ozon_card(result)
        else:
            text = format_ym_card(result)
        
        keyboard = [[
            InlineKeyboardButton("🔄 Перегенерировать", callback_data=f"regen_{marketplace}_{product[:50]}"),
            InlineKeyboardButton("🔀 Другой маркетплейс", callback_data="change_mp_")
        ]]
        
        await bot.send_message(
            chat_id=message.chat_id,
            text=text[:4000],
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Если не выбран маркетплейс
    if user_id not in user_sessions or user_sessions[user_id].get('step') != 'waiting_product':
        keyboard = [
            [InlineKeyboardButton("🟣 WB", callback_data="mp_wb"),
             InlineKeyboardButton("🔵 Ozon", callback_data="mp_ozon"),
             InlineKeyboardButton("🟡 ЯМ", callback_data="mp_ym"),
             InlineKeyboardButton("🎯 Все", callback_data="mp_all")],
        ]
        await update.message.reply_text(
            "Сначала выбери маркетплейс 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    marketplace = user_sessions[user_id]['marketplace']
    mp_data = MARKETPLACE_PROMPTS[marketplace]
    
    # Обрабатываем фото или текст
    image_base64 = None
    product_description = ""
    
    if update.message.photo:
        await update.message.reply_text(f"📸 Фото получено! {mp_data['emoji']} Генерирую карточку для {mp_data['name']}...")
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await photo_file.download_to_drive(tmp.name)
            with open(tmp.name, "rb") as f:
                image_base64 = base64.b64encode(f.read()).decode()
            os.unlink(tmp.name)
        product_description = update.message.caption or "товар на фото"
        
    elif update.message.text:
        product_description = update.message.text
        await update.message.reply_text(
            f"{mp_data['emoji']} Генерирую карточку для *{mp_data['name']}*...\n\n⏳ Подожди 10-15 секунд",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("Отправь текст или фото товара!")
        return
    
    try:
        result = await generate_card(product_description, marketplace, image_base64)
        await send_card_result(update.message, result, marketplace, product_description, context.bot, user_id)
        
        # Уведомляем владельца о новом использовании
        if user_id != YOUR_CHAT_ID:
            await context.bot.send_message(
                chat_id=YOUR_CHAT_ID,
                text=f"💡 *Новый клиент использует КарточникБот!*\n\nТовар: {product_description[:100]}\nМаркетплейс: {mp_data['name']}",
                parse_mode='Markdown'
            )
        
        # Сбрасываем сессию
        user_sessions[user_id] = {"step": "done"}
        
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        await update.message.reply_text(
            f"❌ Ошибка генерации. Попробуй ещё раз или опиши товар подробнее.\n\n`{str(e)[:100]}`",
            parse_mode='Markdown'
        )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("price", price_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    
    logger.info("🛍️ КарточникБот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
