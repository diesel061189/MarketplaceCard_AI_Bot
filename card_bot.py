# ═══════════════════════════════════════════════════════
# ДОПОЛНЕНИЯ ДЛЯ card_bot.py → Карточник v3
# Добавить в конец файла перед if __name__ == "__main__"
# ═══════════════════════════════════════════════════════

import subprocess
import tempfile
import os

# ─── GROQ ХЕЛПЕР (добавить в начало файла) ───
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

async def groq_request_card(messages, system="", max_tokens=1500):
    """Groq вместо Anthropic для текстовых задач — бесплатно"""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": msgs, "max_tokens": max_tokens}
        )
        data = r.json()
        if "choices" not in data:
            raise Exception(f"Groq error: {data}")
        return data["choices"][0]["message"]["content"].strip()

# ─── ВЕКТОРИЗАЦИЯ JPG/PNG → SVG ───

async def vectorize_image_inkscape(image_bytes: bytes, filename: str = "image.png") -> bytes | None:
    """
    Конвертирует растровое изображение в SVG через Inkscape.
    Возвращает SVG байты или None при ошибке.
    """
    suffix = ".png" if filename.endswith(".png") else ".jpg"
    
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_in:
        tmp_in.write(image_bytes)
        input_path = tmp_in.name
    
    output_path = input_path.replace(suffix, ".svg")
    
    try:
        result = subprocess.run(
            [
                "inkscape", input_path,
                "--export-plain-svg", output_path,
                "--export-area-drawing",
                "--export-type=svg"
            ],
            capture_output=True,
            timeout=60
        )
        
        if result.returncode == 0 and os.path.exists(output_path):
            with open(output_path, 'rb') as f:
                svg_data = f.read()
            logger.info(f"✅ Векторизация готова: {len(svg_data)} байт")
            return svg_data
        else:
            logger.error(f"Inkscape ошибка: {result.stderr.decode()[:200]}")
            return None
    except subprocess.TimeoutExpired:
        logger.error("Inkscape timeout")
        return None
    except FileNotFoundError:
        logger.error("Inkscape не найден на сервере")
        return None
    except Exception as e:
        logger.error(f"vectorize_image_inkscape: {e}")
        return None
    finally:
        if os.path.exists(input_path):
            os.unlink(input_path)
        if os.path.exists(output_path):
            os.unlink(output_path)

# ─── КОМАНДА /vectorize ───

async def vectorize_command(update, context):
    """Команда для векторизации — ожидает фото"""
    user_id = update.effective_user.id
    user_sessions[user_id] = user_sessions.get(user_id, {})
    user_sessions[user_id]['step'] = 'waiting_vectorize'
    
    await update.message.reply_text(
        "🎨 *Векторизация JPG/PNG → SVG*\n\n"
        "Пришли изображение — конвертирую в чистый SVG вектор!\n\n"
        "✅ Подходит для:\n"
        "• Логотипов\n"
        "• Иконок\n"
        "• Простых иллюстраций\n\n"
        "⚠️ Сложные фото (много деталей) — результат может быть грубым.\n"
        "Лучше всего работает с чёткими контурами на белом фоне.",
        parse_mode='Markdown'
    )

# ─── АУДИТ КАРТОЧКИ — GROQ ВЕРСИЯ ───

async def audit_card_groq(card_text: str, marketplace: str = "wb") -> str:
    """Аудит карточки через Groq — бесплатно"""
    mp_names = {"wb":"Wildberries","ozon":"Ozon","ym":"Яндекс Маркет","amazon":"Amazon","etsy":"Etsy"}
    mp_name  = mp_names.get(marketplace, marketplace.upper())
    prompt   = (
        f"Ты эксперт по маркетплейсам. Проведи аудит карточки для {mp_name}.\n\n"
        f"КАРТОЧКА:\n{card_text[:2000]}\n\n"
        f"Оцени:\n"
        f"1. Заголовок — ключевые слова, длина ✅/❌ + 💡\n"
        f"2. Описание — полнота, продающий текст ✅/❌ + 💡\n"
        f"3. SEO — ключевые слова ✅/❌ + 💡\n"
        f"4. Характеристики — заполненность ✅/❌ + 💡\n"
        f"5. Уникальность ✅/❌ + 💡\n"
        f"6. Общая оценка /10\n\n"
        f"Конкретно, без воды."
    )
    return await groq_request_card(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000
    )

# ─── СЕМАНТИКА — GROQ ВЕРСИЯ ───

async def get_semantics_groq(product: str, marketplace: str = "wb") -> str:
    """Семантика через Groq — бесплатно"""
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
        text = await groq_request_card(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        data = json.loads(text)
        return (
            f"🔍 *СЕМАНТИКА: {product}* | _{mp_name}_\n\n"
            f"📊 *Высокочастотные:*\n`{', '.join(data.get('high_freq',[]))}`\n\n"
            f"📈 *Среднечастотные:*\n`{', '.join(data.get('mid_freq',[]))}`\n\n"
            f"🎯 *Низкочастотные:*\n`{', '.join(data.get('low_freq',[]))}`\n\n"
            f"🚫 *Минус-слова:*\n`{', '.join(data.get('negative',[]))}`\n\n"
            f"📌 *В заголовок:*\n_{data.get('title_keywords','')}_\n\n"
            f"📝 *В описание:*\n_{data.get('description_keywords','')}_"
        )
    except Exception as e:
        logger.error(f"semantics_groq: {e}")
        return f"❌ Ошибка: {str(e)[:100]}"

# ─── UGC — GROQ ВЕРСИЯ ───

async def generate_ugc_groq(product: str, ugc_type: str = "review") -> str:
    """UGC контент через Groq — бесплатно"""
    if ugc_type == "review":
        prompt = (
            f"Напиши 3 живых отзыва на товар для маркетплейса.\n"
            f"Товар: {product}\n\n"
            f"• Короткий (2-3 предложения)\n"
            f"• Средний (4-5 предложений)\n"
            f"• Подробный (6-7 предложений)\n\n"
            f"Разный тон: восторженный, нейтральный, практичный.\n"
            f"Естественный язык реального покупателя.\n"
            f"Формат: *Вариант 1*, *Вариант 2*, *Вариант 3*"
        )
    elif ugc_type == "negative":
        prompt = (
            f"Напиши 3 ответа продавца на негативные отзывы.\nТовар: {product}\n\n"
            f"1. Товар не понравился\n2. Долгая доставка\n3. Дефект/брак\n\n"
            f"Тон: вежливый, конкретный. Признать→объяснить→решение. 50-80 слов каждый."
        )
    elif ugc_type == "faq":
        prompt = (
            f"Составь FAQ для карточки товара.\nТовар: {product}\n\n"
            f"8 реальных вопросов покупателей с конкретными ответами.\n"
            f"Вопросы о: доставке, гарантии, уходе, размере, совместимости.\n"
            f"Формат: **Вопрос?** → Ответ"
        )
    else:
        return "Неизвестный тип"
    
    return await groq_request_card(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800
    )

# ═══════════════════════════════════════════════════════
# КАК ПРИМЕНИТЬ:
#
# 1. В начало card_bot.py добавить:
#    GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
#    GROQ_MODEL = "llama-3.3-70b-versatile"
#
# 2. Добавить функции выше в конец файла
#
# 3. В handle_message добавить обработку waiting_vectorize:
#    if session.get('step') == 'waiting_vectorize' and update.message.photo:
#        photo = update.message.photo[-1]
#        photo_file = await context.bot.get_file(photo.file_id)
#        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
#            await photo_file.download_to_drive(tmp.name)
#            with open(tmp.name, "rb") as f:
#                image_bytes = f.read()
#            os.unlink(tmp.name)
#        await update.message.reply_text("⏳ Векторизирую... 30-60 секунд")
#        svg_data = await vectorize_image_inkscape(image_bytes, "image.jpg")
#        if svg_data:
#            with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp_svg:
#                tmp_svg.write(svg_data)
#                svg_path = tmp_svg.name
#            with open(svg_path, "rb") as f:
#                await update.message.reply_document(
#                    document=f, filename="vector.svg",
#                    caption="🎨 *SVG вектор готов!*\n\nСкачай и используй в любом редакторе.",
#                    parse_mode='Markdown'
#                )
#            os.unlink(svg_path)
#        else:
#            await update.message.reply_text("❌ Не удалось векторизовать. Попробуй фото с чёткими контурами на белом фоне.")
#        user_sessions[user_id]['step'] = 'waiting_product'
#        return
#
# 4. В main() добавить:
#    app.add_handler(CommandHandler("vectorize", vectorize_command))
#
# 5. Заменить вызовы audit_card() на audit_card_groq()
#    Заменить get_semantics() на get_semantics_groq()
#    Заменить generate_ugc() на generate_ugc_groq()
# ═══════════════════════════════════════════════════════
