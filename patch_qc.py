# -*- coding: utf-8 -*-
"""
patch_qc.py — вшивает ОТК-цикл (card_qc) в card_bot.py.
Запуск НА VPS:  cd /opt/bots/MarketplaceCard_AI_Bot && python3 patch_qc.py
Идемпотентно: повторный запуск ничего не сломает (видит, что уже применено).
Безопасно: если якорь не найден — НИЧЕГО не пишет, печатает ошибку.
"""
import sys

PATH = "card_bot.py"
src = open(PATH, encoding="utf-8").read()

changed = False

# ── 1) импорт модуля ОТК ──
if "import card_qc" in src:
    print("• импорт card_qc уже есть — пропускаю")
else:
    anchor = "import pytz\n"
    if src.count(anchor) != 1:
        sys.exit(f"❌ не найден уникальный якорь импорта ('import pytz'): {src.count(anchor)} шт. Патч отменён.")
    src = src.replace(anchor, "import pytz\nimport card_qc  # ОТК + валидация карточек под правила площадок\n")
    changed = True
    print("• импорт card_qc добавлен")

# ── 2) ОТК-цикл вокруг генерации ──
if "_generate_validated" in src:
    print("• ОТК-цикл уже вшит — пропускаю")
else:
    anchor = (
        'async def generate_card(product: str, marketplace: str, image_b64: str = None) -> dict:\n'
        '    if marketplace == "all":\n'
        '        results = {}\n'
        '        for mp in ["wb", "ozon", "ym"]:\n'
        '            results[mp] = await generate_single(product, mp, image_b64)\n'
        '            await asyncio.sleep(0.5)\n'
        '        return results\n'
        '    return await generate_single(product, marketplace, image_b64)'
    )
    if src.count(anchor) != 1:
        sys.exit(f"❌ не найден уникальный якорь generate_card: {src.count(anchor)} шт. Патч отменён.")

    replacement = (
        'async def _generate_validated(product: str, marketplace: str, image_b64: str = None,\n'
        '                              max_attempts: int = 3) -> dict:\n'
        '    """ОТК-цикл: генерим -> валидируем под правила площадки -> при ошибках\n'
        '       перегенерим с подсказкой. Отчёт ОТК кладём в card[\'_qc\']."""\n'
        '    problems, best = [], None\n'
        '    for attempt in range(max_attempts):\n'
        '        hint = ""\n'
        '        if problems:\n'
        '            hint = ("\\n\\nВ ПРЕДЫДУЩЕЙ ВЕРСИИ БЫЛИ ОШИБКИ — ИСПРАВЬ ИХ:\\n- "\n'
        '                    + "\\n- ".join(problems)\n'
        '                    + f"\\n(заголовок строго не длиннее {card_qc.title_limit(marketplace)} символов)")\n'
        '        try:\n'
        '            card = await generate_single(product + hint, marketplace, image_b64)\n'
        '        except Exception as e:\n'
        '            logger.error(f"generate_single [{marketplace}] попытка {attempt+1}: {e}")\n'
        '            continue\n'
        '        report = card_qc.validate_card(card, marketplace)\n'
        '        card["_qc"] = report\n'
        '        best = card\n'
        '        if report["ok"]:\n'
        '            if attempt:\n'
        '                logger.info(f"✅ {marketplace}: ОТК пройден с {attempt+1}-й попытки")\n'
        '            return card\n'
        '        problems = report["errors"]\n'
        '        logger.info(f"⚠️ {marketplace}: ОТК не пройден (попытка {attempt+1}): {problems}")\n'
        '    logger.warning(f"❗ {marketplace}: отдаю лучшее с пометками ОТК после {max_attempts} попыток")\n'
        '    return best or {"title": product, "description": "", "_qc": card_qc.validate_card({}, marketplace)}\n'
        '\n'
        '\n'
        'async def generate_card(product: str, marketplace: str, image_b64: str = None) -> dict:\n'
        '    if marketplace == "all":\n'
        '        results = {}\n'
        '        for mp in ["wb", "ozon", "ym"]:\n'
        '            results[mp] = await _generate_validated(product, mp, image_b64)\n'
        '            await asyncio.sleep(0.5)\n'
        '        return results\n'
        '    return await _generate_validated(product, marketplace, image_b64)'
    )
    src = src.replace(anchor, replacement)
    changed = True
    print("• ОТК-цикл вшит в generate_card")

if changed:
    open(PATH, "w", encoding="utf-8").write(src)
    import py_compile
    py_compile.compile(PATH, doraise=True)
    print("\n✅ Патч применён, card_bot.py компилируется. Перезапусти: systemctl restart card")
else:
    print("\nℹ️ Ничего не менялось — всё уже на месте.")
