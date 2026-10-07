# -*- coding: utf-8 -*-
"""Утилиты безопасного преобразования значений из ответов НСПД.

Назначение файла:
    Мелкие pure-функции приведения «сырых» значений реестровых полей
    (строки, числа, даты в произвольном виде, None) к типам, пригодным
    для записи в таблицу axipy. Используются лямбда-заполнителями
    колонок из categories.py; ни о чём, кроме типов данных, не знают.
"""


def safe_str(val):
    """Любое значение → непустая строка без пробелов по краям (None → "")."""
    if val is None:
        return ""
    return str(val).strip()


def safe_float(val):
    """Число → float; запятая как разделитель дроби tolerated; ошибки → 0.0."""
    if val is None or val == "":
        return 0.0
    try:
        return float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return 0.0


def safe_int(val):
    """Целое из любого представления ("5", "5.0", 5.7) ; ошибки → 0."""
    if val is None or val == "":
        return 0
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return 0


def fmt_date(val):
    """ISO-дата (ГГГГ-ММ-ДД[Thh:mm:ss]) → привычный формат ДД.ММ.ГГГГ.

    Значения, не похожие на ISO-дату (текстовые примечания НСПД),
    возвращаются как есть; пустота/строка "None" → "".
    """
    if not val or val == "None":
        return ""
    s = str(val).strip()
    if len(s) >= 10 and "-" in s:
        parts = s.split("T")[0].split("-")
        if len(parts) == 3:
            return f"{parts[2]}.{parts[1]}.{parts[0]}"
    return s