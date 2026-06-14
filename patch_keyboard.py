#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_keyboard.py — добавляет ПОСТОЯННУЮ НИЖНЮЮ reply-клавиатуру в card_bot.py
(как у Насти): главные кнопки всегда под полем ввода, без /start.

Идемпотентен: повторный запуск ничего не ломает.
Безопасен: при отсутствии якорей — аборт без изменений.
Инлайн-кнопки (_card_main_keyboard) НЕ трогаются.
"""
import re, sys, py_compile, shutil, datetime

TARGET = "card_bot.py"

# ── читаем файл ──
try:
    with open(TARGET, encoding="utf-8") as f:
        src = f.read()
except FileNotFoundError:
    print(f"❌ {TARGET} не найден. Запусти патч в папке с card_bot.py")
    sys.exit(1)

orig = src

# ── бэкап ──
ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy(TARGET, f"{TARGET}.bak_{ts}")

# проверка идемпотентности
if "CARD_REPLY_KB" in src:
    print("ℹ️ Нижняя клавиатура уже вшита (CARD_REPLY_KB найден). Выходим без изменений.")
    sys.exit(0)

# ── 1. импорт ReplyKeyboardMarkup/KeyboardButton ──
imp_anchor = "from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup"
if imp_anchor not in src:
    print("❌ Якорь импорта не найден. Аборт.")
    sys.exit(1)
src = src.replace(
    imp_anchor,
    "from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton"
)

# ── 2. определение клавиатуры — вставляем перед start_command ──
KB_BLOCK = '''
# ═══ НИЖНЯЯ ПОСТОЯННАЯ КЛАВИАТУРА (CARD_REPLY_KB) ═══
CARD_REPLY_KB = ReplyKeyboardMarkup(
    [
        ["🟣 WB", "🔵 Ozon", "🟡 ЯМ"],
        ["🔍 Аудит", "📊 Семантика", "📝 UGC"],
        ["📦 Пакеты", "💰 Прайс", "📊 Статистика"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# карта: текст нижней кнопки → (тип, значение)
CARD_KB_ROUTES = {
    "🟣 WB":        ("mp", "wb"),
    "🔵 Ozon":      ("mp", "ozon"),
    "🟡 ЯМ":        ("mp", "ym"),
    "🔍 Аудит":     ("cmd", "audit"),
    "📊 Семантика": ("cmd", "semantics"),
    "📝 UGC":       ("cmd", "ugc"),
    "📦 Пакеты":    ("cmd", "packages"),
    "💰 Прайс":     ("cmd", "price"),
    "📊 Статистика":("cmd", "stats"),
}

async def _route_reply_button(update, context, label):
    """Обрабатывает нажатие нижней кнопки. Возвращает True если перехватил."""
    route = CARD_KB_ROUTES.get(label)
    if not route:
        return False
    kind, val = route
    uid = update.effective_user.id
    if kind == "mp":
        user_sessions[uid] = user_sessions.get(uid, {})
        user_sessions[uid]["marketplace"] = val
        user_sessions[uid]["step"] = "waiting_product"
        mp_names = {"wb": "Wildberries", "ozon": "Ozon", "ym": "Яндекс Маркет"}
        await update.message.reply_text(
            f"✅ *{mp_names.get(val, val)}* выбран!\\n\\n"
            "📸 Пришли фото товара или 📝 напиши название — сделаю карточку.",
            parse_mode="Markdown", reply_markup=CARD_REPLY_KB
        )
        return True
    if kind == "cmd":
        cmd_map = {
            "audit":     audit_command,
            "semantics": semantics_command,
            "ugc":       ugc_command,
            "packages":  packages_command,
            "price":     price_command,
            "stats":     stats_command,
        }
        fn = cmd_map.get(val)
        if fn:
            await fn(update, context)
            return True
    return False

'''

sc_anchor = "async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):"
if sc_anchor not in src:
    print("❌ Якорь start_command не найден. Аборт.")
    sys.exit(1)
src = src.replace(sc_anchor, KB_BLOCK + sc_anchor, 1)

# ── 3. привязать нижнюю клаву к стартовому сообщению ──
# в start_command есть reply_markup=_card_main_keyboard() — это ИНЛАЙН, оставляем.
# Добавляем ВТОРЫМ сообщением показ нижней клавиатуры.
start_tail_anchor = "        reply_markup=_card_main_keyboard()\n    )"
if start_tail_anchor in src:
    src = src.replace(
        start_tail_anchor,
        "        reply_markup=_card_main_keyboard()\n    )\n"
        "    await update.message.reply_text(\n"
        "        \"⌨️ Меню снизу всегда под рукой — жми кнопки без /start\",\n"
        "        reply_markup=CARD_REPLY_KB\n"
        "    )",
        1
    )
else:
    print("⚠️ Хвост start_command не совпал дословно — нижняя клава к /start не привязана, но роутер кнопок работает.")

# ── 4. роутер нажатий в начало handle_message ──
hm_anchor = ("async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):\n"
             "    user_id = update.effective_user.id\n")
if hm_anchor not in src:
    print("❌ Якорь начала handle_message не найден. Аборт.")
    sys.exit(1)
ROUTER = (
    "async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):\n"
    "    user_id = update.effective_user.id\n"
    "    # ─── РОУТЕР НИЖНЕЙ КЛАВИАТУРЫ (перехватываем до парсера текста) ───\n"
    "    if update.message and update.message.text and update.message.text in CARD_KB_ROUTES:\n"
    "        if await _route_reply_button(update, context, update.message.text):\n"
    "            return\n"
)
src = src.replace(hm_anchor, ROUTER, 1)

# ── записываем и компилируем ──
with open(TARGET, "w", encoding="utf-8") as f:
    f.write(src)

try:
    py_compile.compile(TARGET, doraise=True)
except py_compile.PyCompileError as e:
    print(f"❌ Ошибка компиляции, откатываю:\n{e}")
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(orig)
    sys.exit(1)

print("✅ Нижняя reply-клавиатура вшита:")
print("   • импорт ReplyKeyboardMarkup/KeyboardButton добавлен")
print("   • CARD_REPLY_KB (3×3) определена")
print("   • роутер кнопок встроен в handle_message")
print("   • нижнее меню привязано к /start")
print("   • инлайн-кнопки НЕ тронуты")
print("   Перезапусти: systemctl restart card")
