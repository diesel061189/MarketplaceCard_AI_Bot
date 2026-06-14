# -*- coding: utf-8 -*-
"""
card_qc.py — ОТК и валидация карточек под правила маркетплейсов.
=================================================================
Самодостаточный модуль БЕЗ сети и БЕЗ ИИ — чистая детерминированная проверка.
Подключается в card_bot.py: import card_qc -> card_qc.validate_card(card, mp).

Правила сверены с актуальными требованиями площадок (июнь 2026):
  WB   — наименование ≤ 60 символов; запрещены № , / , \\ , имя категории/ИП.
  Ozon — запрещены «копия/реплика/аналог/оригинal/1:1/по мотивам» и символы ®©™[]=\\«»;
         аннотация ≤ 6000.
  ЯМ   — описание ≤ 2000.
Числа вынесены в MARKETPLACE_RULES — правит одним местом, когда площадки меняют лимиты.
"""

# ─────────────────────────── ПРАВИЛА ПЛОЩАДОК ───────────────────────────
MARKETPLACE_RULES = {
    "wb": {
        "name": "Wildberries",
        "title_max": 60,
        "title_rec": 40,
        "desc_min": 300, "desc_max": 5000,
        "list_field": "characteristics", "list_min": 5,
        "need_keywords": True,
        "title_forbidden_sub": ["№", "/", "\\"],
        "forbidden_words": [],
    },
    "ozon": {
        "name": "Ozon",
        "title_max": 200,
        "title_rec": 150,
        "desc_min": 300, "desc_max": 6000,
        "list_field": "attributes", "list_min": 5,
        "need_keywords": True,
        "title_forbidden_sub": ["®", "©", "™", "[", "]", "=", "\\", "«", "»"],
        "forbidden_words": ["аналог", "подделка", "копия", "copy", "реплика",
                            "1:1", "по мотивам", "оригинал", "original", "подлинный"],
    },
    "ym": {
        "name": "Яндекс Маркет",
        "title_max": 150,
        "title_rec": 100,
        "desc_min": 200, "desc_max": 2000,
        "list_field": "specs", "list_min": 3,   # specs — это dict
        "need_keywords": False,
        "title_forbidden_sub": [],
        "forbidden_words": [],
    },
    "amazon": {
        "name": "Amazon",
        "title_max": 200,
        "title_rec": 150,
        "desc_min": 300, "desc_max": 2000,
        "list_field": "bullet_points", "list_min": 5,
        "need_keywords": True,
        "title_forbidden_sub": [],
        "forbidden_words": [],
    },
    "etsy": {
        "name": "Etsy",
        "title_max": 140,
        "title_rec": 120,
        "desc_min": 300, "desc_max": 2000,
        "list_field": "tags", "list_min": 5,
        "need_keywords": False,
        "title_forbidden_sub": [],
        "forbidden_words": [],
    },
}

# Громкие непроверяемые заявления — площадки за них режут/занижают. Это ПРЕДУПРЕЖДЕНИЯ.
SUPERLATIVES = ["лучший", "лучшая", "лучшее", "самый", "самая", "самое",
                "№1", "номер один", "100% гарант", "гарантия 100"]


def title_limit(marketplace: str) -> int:
    return MARKETPLACE_RULES.get(marketplace, {}).get("title_max", 200)


def _list_count(card: dict, field: str) -> int:
    v = card.get(field)
    if isinstance(v, dict):
        return len(v)
    if isinstance(v, list):
        return len([x for x in v if str(x).strip()])
    return 0


# ─────────────────────────── ВАЛИДАТОР ───────────────────────────
def validate_card(card: dict, marketplace: str) -> dict:
    """Проверяет карточку (dict от generate_single) под правила площадки.
       Возвращает: {marketplace, name, ok, score, errors[], warnings[], passed[]}.
       errors  — блокеры (карточку вернут с модерации / клиент не зальёт).
       warnings — некритично, но стоит улучшить.
       ok = (ошибок нет); score = 0..100."""
    rules = MARKETPLACE_RULES.get(marketplace)
    if not rules:
        return {"marketplace": marketplace, "name": marketplace, "ok": True,
                "score": 100, "errors": [], "warnings": [],
                "passed": ["правил для площадки нет — пропускаю"]}

    errors, warnings, passed = [], [], []

    if not isinstance(card, dict):
        return {"marketplace": marketplace, "name": rules["name"], "ok": False,
                "score": 0, "errors": ["карточка не разобралась в JSON"],
                "warnings": [], "passed": []}

    # ── заголовок ──
    title = str(card.get("title", "")).strip()
    if not title:
        errors.append("нет заголовка")
    else:
        tl = len(title)
        if tl > rules["title_max"]:
            errors.append(f"заголовок {tl} симв. — лимит {rules['title_max']} (обрежется на модерации)")
        else:
            passed.append(f"заголовок {tl}/{rules['title_max']} симв.")
            if tl > rules["title_rec"]:
                warnings.append(f"заголовок длиннее рекомендованных {rules['title_rec']} симв.")
        for sub in rules["title_forbidden_sub"]:
            if sub in title:
                errors.append(f"в заголовке запрещённый символ «{sub}»")

    # ── описание ──
    desc = str(card.get("description", "")).strip()
    if not desc:
        errors.append("нет описания")
    else:
        dl = len(desc)
        if dl > rules["desc_max"]:
            errors.append(f"описание {dl} симв. — лимит {rules['desc_max']}")
        elif dl < rules["desc_min"]:
            warnings.append(f"описание короткое ({dl} симв., желательно от {rules['desc_min']})")
        else:
            passed.append(f"описание {dl} симв.")

    # ── список характеристик/атрибутов/specs/bullets ──
    field = rules["list_field"]
    cnt = _list_count(card, field)
    if cnt == 0:
        errors.append(f"пустой блок «{field}»")
    elif cnt < rules["list_min"]:
        warnings.append(f"мало пунктов в «{field}»: {cnt}, желательно от {rules['list_min']}")
    else:
        passed.append(f"«{field}»: {cnt} пунктов")

    # ── ключевые слова ──
    if rules["need_keywords"]:
        kw = card.get("keywords") or card.get("tags")
        if not kw:
            warnings.append("нет ключевых слов (SEO просядет)")
        else:
            passed.append("ключевые слова есть")

    # ── запрещённые слова площадки (бан за копии и т.п.) ──
    haystack = (title + " " + desc).lower()
    for w in rules["forbidden_words"]:
        if w.lower() in haystack:
            errors.append(f"запрещённое на {rules['name']} слово: «{w}»")

    # ── громкие заявления (предупреждение) ──
    for s in SUPERLATIVES:
        if s.lower() in haystack:
            warnings.append(f"непроверяемое заявление «{s}» — площадка может занизить")
            break

    score = max(0, 100 - 30 * len(errors) - 7 * len(warnings))
    ok = len(errors) == 0
    return {"marketplace": marketplace, "name": rules["name"], "ok": ok,
            "score": score, "errors": errors, "warnings": warnings, "passed": passed}


# ─────────────────────────── ОТЧЁТ ДЛЯ TELEGRAM ───────────────────────────
def build_qc_text(report: dict) -> str:
    head = "✅" if report["ok"] else "⛔"
    L = [f"{head} *ОТК {report['name']}* — {report['score']}/100"]
    if report["errors"]:
        L.append("\n*Блокеры (исправить):*")
        L += [f"  ⛔ {e}" for e in report["errors"]]
    if report["warnings"]:
        L.append("\n*Замечания:*")
        L += [f"  ⚠️ {w}" for w in report["warnings"]]
    if report["ok"] and not report["warnings"]:
        L.append("Карточка чистая — готова к загрузке.")
    return "\n".join(L)


if __name__ == "__main__":
    good = {"title": "Наушники беспроводные TWS с шумоподавлением",
            "description": "x" * 600, "keywords": "наушники, tws, bluetooth",
            "characteristics": ["a", "b", "c", "d", "e"]}
    print(build_qc_text(validate_card(good, "wb")))
