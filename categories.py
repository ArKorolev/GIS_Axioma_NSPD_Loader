# -*- coding: utf-8 -*-
"""Категории объектов НСПД (единый модуль категорий модуля).

Назначение файла:
    Хранит ВСЕ определения типов объектов ФГИС ЕГРН, загружаемых модулем:
    идентификаторы категорий НСПД, наборы колонок семантики для таблиц
    axipy, правила распознавания типа и статусов координат, а также
    спецобработку составных объектов (раскрытие ЕЗП в дочерние участки).

Содержит:
- CategoryDef          — базовое определение категории (общие правила);
- LandCategory         — земельный участок (включая раскрытие ЕЗП);
- BuildingCategory     — здание;
- StructureCategory    — сооружение;
- IncompleteCategory   — объект незавершённого строительства (ОНС);
- CATEGORY_REGISTRY / CAT_NAMES / CATEGORY_FILTERS — реестры категорий;
- resolve_category()   — подбор категории по ID или названию.

Добавление нового типа:
1. Добавить класс-наследник CategoryDef в этот файл
2. Зарегистрировать его в CATEGORY_REGISTRY (там же)

Общая обработка геометрии для ВСЕХ категорий (см. nspd_geometry.py):
геометрией объекта считаются ТОЛЬКО Polygon / MultiPolygon
(многоконтурный участок либо полигон с дырами). LineString /
MultiLineString — осевые линии, не являются загружаемой геометрией;
Point / MultiPoint — адресные привязки, используются только когда
собственная геометрия объекта отсутствует.
"""

import json
import logging
import re
import time

from .nspd_utils import safe_str, safe_float, safe_int, fmt_date
from .nspd_transport import http_get
from .nspd_geometry import has_boundary_geometry, prepare_geometry

logger = logging.getLogger("NSPD_Loader")

# registersId реестра «Единое землепользование» в API tab-values-data
REGISTERS_ID_EZP = 36440


class CategoryDef:
    """Базовое определение категории объекта НСПД.

    Класс описывает тип объекта: его ID в НСПД, человекочитаемое имя,
    набор колонок семантики для таблицы и поведение по умолчанию.
    Потомки переопределяют:
    - CATEGORY_ID, CATEGORY_NAME, COLUMNS, FALLBACK_KEYWORDS
      — данные конкретного типа;
    - classify_coords() — если формулировка статуса координат отличается
      (у ЗУ это «Без координат границ», у прочих — «Нет»);
    - expand() — если требуется раскрытие состава (ЕЗП и т.д.).

    Атрибуты:
        CATEGORY_ID: int|None — идентификатор категории в НСПД;
        CATEGORY_NAME: str — отображаемое имя;
        COLUMNS: list[(имя_колонки, тип, lambda options -> значение)] —
            схема семантических полей таблицы; тип: "s" строка,
            "f" float, "i" int; лямбда извлекает значение из options;
        FALLBACK_KEYWORDS: list[str] — подстроки названия категории для
            распознавания, когда numeric category в ответе отсутствует.
    """

    CATEGORY_ID = None
    CATEGORY_NAME = ""
    COLUMNS = []
    FALLBACK_KEYWORDS = []

    @classmethod
    def recognize(cls, category, category_name):
        """Проверяет, принадлежит ли feature этой категории.

        Args:
            category: числовой ID категории из ответа НСПД (или None);
            category_name: categoryName из ответа (или "").

        Returns:
            bool: True, если ID совпал с CATEGORY_ID, либо (при
            отсутствии ID) название содержит один из FALLBACK_KEYWORDS.
        """
        if category is not None:
            return category == cls.CATEGORY_ID
        if category_name:
            # Нормализация: ё -> е (в названиях НСПД встречается оба написания)
            cn = category_name.lower().replace("ё", "е")
            return any(kw in cn for kw in cls.FALLBACK_KEYWORDS)
        return False

    @classmethod
    def expand(cls, result, query_func, http_func, on_substatus=None):
        """Раскрытие результата запроса («составной» объект → список объектов).

        Базовая реализация — без изменений: обычный объект возвращается
        списком из одного элемента. Переопределяется там, где за одним
        номером стоит несколько реальных объектов (ЕЗП у LandCategory).

        Args:
            result: dict-результат из query_nspd;
            query_func: функция поиска (инъекция nspd_client.query_nspd,
                чтобы избежать циклического импорта);
            http_func: HTTP-функция (инъекция nspd_transport.http_get);
            on_substatus: callback(num, status, *args) для UI-лога.

        Returns:
            list[dict] результатов для загрузки в таблицу.
        """
        return [result]

    @classmethod
    def classify_coords(cls, geometry, options):
        """Определяет статус координат объекта (текст в колонку семантики).

        Единая логика для всех категорий: загружаемой геометрией
        являются только Polygon / MultiPolygon (в т.ч. внутри
        GeometryCollection). Point — адресная привязка (подставляется
        вместо отсутствующей геометрии), линии — осевые линии; ни то
        ни другое границами объекта не считается.

        Args:
            geometry: GeoJSON из ответа НСПД;
            options: properties.options (зарезервировано для потомков).

        Returns:
            str: "Есть координаты границ" либо "Нет".
        """
        if not geometry or not isinstance(geometry, dict):
            return "Нет"
        # Общая обработка: prepare_geometry извлекает полигон (или точку)
        loadable = prepare_geometry(geometry)
        if loadable is not None and has_boundary_geometry(loadable):
            return "Есть координаты границ"
        return "Нет"


class LandCategory(CategoryDef):
    """Земельный участок (категория НСПД 36368).

    Особенности:
    - статус координат формулируется как «Без координат границ»
      (терминология земельного законодательства);
    - поддерживает раскрытие ЕЗП (единого землепользования): за одним
      кадастровым номером скрываются обособленные земельные участки,
      каждый из которых запрашивается и загружается отдельно;
    - отдельная колонка «Кадастровый_номер_ЕЗП» хранит номер родителя.
    """

    CATEGORY_ID = 36368
    CATEGORY_NAME = "Земельный участок"
    FALLBACK_KEYWORDS = ["земельн"]

    # Схема таблицы ЗУ: (имя колонки, тип, извлечение значения из options).
    # Типы: "s" — строка, "f" — float, "i" — int. Часть полей читается из
    # нескольких ключей через `or` — НСПД кладёт площадь/тип в разные поля
    # в зависимости от вида записи.
    COLUMNS = [
        ("Вид_объекта",              "s", lambda o: "Земельный участок"),
        ("Вид_ЗУ",                   "s", lambda o: safe_str(o.get("land_record_subtype") or o.get("land_record_type") or "Земельный участок")),
        ("Кадастровый_номер",        "s", lambda o: safe_str(o.get("cad_num"))),
        ("Кадастровый_номер_ЕЗП",    "s", lambda o: safe_str(o.get("parent_cad_num"))),
        ("Кадастровый_квартал",      "s", lambda o: safe_str(o.get("quarter_cad_number"))),
        ("Адрес",                    "s", lambda o: safe_str(o.get("readable_address"))),
        ("Площадь",                  "f", lambda o: safe_float(o.get("area") or o.get("specified_area") or o.get("declared_area") or o.get("land_record_area"))),
        ("Статус",                   "s", lambda o: safe_str(o.get("status"))),
        ("Категория_земель",         "s", lambda o: safe_str(o.get("land_record_category_type"))),
        ("ВРИ",                      "s", lambda o: safe_str(o.get("permitted_use_established_by_document"))),
        ("Форма_собственности",      "s", lambda o: safe_str(o.get("ownership_type"))),
        ("Кадастровая_стоимость",    "f", lambda o: safe_float(o.get("cost_value"))),
        ("Удельный_показатель_КС",   "f", lambda o: safe_float(o.get("cost_index"))),
        ("Дата_присвоения",          "s", lambda o: fmt_date(o.get("land_record_reg_date"))),
        ("Декларированная_площадь",  "f", lambda o: safe_float(o.get("declared_area"))),
        ("Уточненная_площадь",       "f", lambda o: safe_float(o.get("specified_area"))),
        ("Площадь_кад_учет",         "f", lambda o: safe_float(o.get("land_record_area"))),
        ("Наличие_координат",        "s", lambda o: safe_str(o.get("coords_status")) or "Без координат границ"),
        ("Основание_КС",             "s", lambda o: safe_str(o.get("determination_couse"))),
        ("Дата_определения_КС",      "s", lambda o: fmt_date(o.get("cost_determination_date"))),
        ("Дата_утверждения_КС",      "s", lambda o: fmt_date(o.get("cost_approvement_date"))),
        ("Дата_применения_КС",       "s", lambda o: fmt_date(o.get("cost_application_date"))),
        ("Дата_регистрации_КС",      "s", lambda o: fmt_date(o.get("cost_registration_date"))),
        ("Ранее_опубликованные",     "s", lambda o: safe_str(o.get("previously_posted"))),
    ]

    @classmethod
    def classify_coords(cls, geometry, options):
        """Статус координат ЗУ: точка — адресная привязка, не границы.

        Возвращает «Есть координаты границ» только при наличии валидного
        полигона; иначе — «Без координат границ» (терминология ЗК РФ:
        участок без описанных границ считается уточняемым).
        """
        if not geometry or not isinstance(geometry, dict):
            return "Без координат границ"
        # Общая обработка: prepare_geometry извлекает полигон (в т.ч.
        # из GeometryCollection); точка/линия границами не считаются
        loadable = prepare_geometry(geometry)
        if loadable is not None and has_boundary_geometry(loadable):
            return "Есть координаты границ"
        return "Без координат границ"

    # ─── Единое землепользование ──────────────────────────

    @classmethod
    def expand(cls, result, query_func, http_func, on_substatus=None):
        """Раскрывает ЕЗП (единое землепользование) в дочерние участки.

        Если объект — не ЕЗП, возвращается без изменений ([result]).
        Иначе: через API tab-values-data получается список кадастровых
        номеров обособленных участков состава, каждый запрашивается
        отдельно (query_func с _depth=1 — без повторного раскрытия) и
        сливается с атрибутами родителя: поля родителя служат базой,
        дочерние перекрывают их; в «Кадастровый_номер_ЕЗП» записывается
        номер родителя (parent_cad_num). Геометрия берётся дочерняя, а
        при её отсутствии — геометрия ЕЗП. Статус координат пересчитывается.

        Returns:
            list[dict] дочерних результатов; при пустом составе — [result].
        """
        if not cls._is_ezp(result):
            return [result]

        geom_id = result.get("geom_id")
        parent_options = dict(result.get("options", {}))
        parent_cad_num = parent_options.get("cad_num", "")

        logger.info("ЕЗП обнаружен: %s, запрос состава...", parent_cad_num)
        if on_substatus:
            on_substatus(parent_cad_num, "ezp_start")

        children = cls._get_ezp_children(geom_id, http_func)

        if not children:
            logger.info("ЕЗП %s: состав не найден, сохраняем как есть", parent_cad_num)
            return [result]

        expanded = []
        for child_num in children:
            t_child = time.monotonic()
            child_results, _ = query_func(child_num, cls.CATEGORY_ID, _depth=1)
            child_elapsed = time.monotonic() - t_child

            if child_results in ("SERVER_ERROR", "BLOCKED"):
                logger.warning("ЕЗП: сервер недоступен для дочернего %s", child_num)
                if on_substatus:
                    on_substatus(child_num, "ezp_child_error", child_elapsed)
                continue
            if not child_results:
                logger.warning("ЕЗП: дочерний участок %s не найден", child_num)
                if on_substatus:
                    on_substatus(child_num, "ezp_child_not_found", child_elapsed)
                continue

            for child in child_results:
                # Слияние атрибутов: база — родитель, дочерий перекрывает
                merged_options = dict(parent_options)
                child_opts = child.get("options", {})
                merged_options.update(child_opts)
                if not merged_options.get("cad_num"):
                    merged_options["cad_num"] = child_num
                merged_options["parent_cad_num"] = parent_cad_num

                # Геометрия дочернего участка; если отсутствует — ЕЗП целиком
                merged_geom = child.get("geometry") or result.get("geometry")
                merged = {
                    "cad_num": merged_options["cad_num"],
                    "category": child.get("category", result.get("category")),
                    "category_name": child.get("category_name", result.get("category_name")),
                    "geometry": merged_geom,
                    "options": merged_options,
                    "geom_id": child.get("geom_id"),
                    "coords_status": cls.classify_coords(merged_geom, merged_options),
                }
                expanded.append(merged)
                logger.info("ЕЗП: добавлен дочерний участок %s", merged_options["cad_num"])

            if on_substatus:
                on_substatus(child_num, "ezp_child_ok", child_elapsed)

        return expanded

    @staticmethod
    def _is_ezp(result):
        """True, если объект является единым землепользованием (ЕЗП).

        Критерии: подтип записи содержит «единое», а registersId совпадает
        с REGISTERS_ID_EZP (или отсутствует — тогда доверяем подтипу).
        """
        options = result.get("options", {})
        subtype = str(options.get("land_record_subtype", "")).lower()
        if "единое" not in subtype:
            return False

        registers_id = options.get("registersId")
        if registers_id is not None:
            try:
                if int(registers_id) == REGISTERS_ID_EZP:
                    return True
            except (ValueError, TypeError):
                pass

        return True

    @staticmethod
    def _get_ezp_children(geom_id, http_func):
        """Список кадастровых номеров обособленных участков состава ЕЗП.

        Запрос к API tab-values-data (tabClass=compositionLand) по id
        геометрии родителя; до 3 попыток. Разбор устойчив к разным формам
        ответа: обход object[].value[], затем value[], затем regex-поиск
        номеров вида NN:NN:NNNNNN:NNN по всему JSON.

        Returns:
            list[str] номеров; [] при блокировке/ошибке/пустом составе.
        """
        url = (
            "https://nspd.gov.ru/api/geoportal/v1/tab-values-data"
            f"?tabClass=compositionLand&objdocId={geom_id}&registersId={REGISTERS_ID_EZP}"
        )

        for attempt in range(3):
            code, data = http_func(url)
            if code == 403:
                logger.warning("ЕЗП: 403, доступ заблокирован")
                return []
            if code != 200 or data is None:
                logger.warning("ЕЗП: HTTP %d (попытка %d)", code, attempt + 1)
                time.sleep(2)
                continue

            cad_nums = []

            if isinstance(data, dict):
                objects = data.get("object", [])
                if isinstance(objects, list):
                    for obj in objects:
                        vals = obj.get("value", [])
                        if isinstance(vals, list):
                            for v in vals:
                                if isinstance(v, str) and ":" in v:
                                    cad_nums.append(v.strip())
                if not cad_nums and isinstance(data.get("value"), list):
                    for v in data["value"]:
                        if isinstance(v, str) and ":" in v:
                            cad_nums.append(v.strip())

            if not cad_nums:
                text = json.dumps(data, ensure_ascii=False)
                cad_nums = re.findall(r'\d{2}:\d{2}:\d{6,7}:\d{1,5}', text)

            if cad_nums:
                logger.info("ЕЗП: найдено %d дочерних участков", len(cad_nums))
                return cad_nums

            logger.info("ЕЗП: дочерние участки не найдены")
            return []

        return []


class BuildingCategory(CategoryDef):
    """Здание (категория НСПД 36369).

    Набор колонок описывает характеристики здания: площадь, этажность,
    назначение, материал стен, годы постройки/ввода, кадастровую
    стоимость и даты её определения/утверждения/применения/регистрации.
    Статус координат — базовый (см. CategoryDef.classify_coords).
    """

    CATEGORY_ID = 36369
    CATEGORY_NAME = "Здание"
    FALLBACK_KEYWORDS = ["здани"]

    # Схема таблицы зданий (см. формат COLUMNS в CategoryDef)
    COLUMNS = [
        ("Вид_объекта",              "s", lambda o: "Здание"),
        ("Кадастровый_номер",        "s", lambda o: safe_str(o.get("cad_num"))),
        ("Кадастровый_квартал",      "s", lambda o: safe_str(o.get("quarter_cad_number"))),
        ("Адрес",                    "s", lambda o: safe_str(o.get("readable_address"))),
        ("Площадь",                  "f", lambda o: safe_float(o.get("build_record_area") or o.get("specified_area") or o.get("declared_area"))),
        ("Статус",                   "s", lambda o: safe_str(o.get("status"))),
        ("Форма_собственности",      "s", lambda o: safe_str(o.get("ownership_type"))),
        ("Кадастровая_стоимость",    "f", lambda o: safe_float(o.get("cost_value"))),
        ("Удельный_показатель_КС",   "f", lambda o: safe_float(o.get("cost_index"))),
        ("Дата_присвоения",          "s", lambda o: fmt_date(o.get("build_record_registration_date"))),
        ("Наименование",             "s", lambda o: safe_str(o.get("building_name"))),
        ("Назначение",               "s", lambda o: safe_str(o.get("purpose"))),
        ("Количество_этажей",        "i", lambda o: safe_int(o.get("floors"))),
        ("Подземные_этажи",          "i", lambda o: safe_int(o.get("underground_floors"))),
        ("Год_постройки",            "s", lambda o: safe_str(o.get("year_built"))),
        ("Год_ввода",                "s", lambda o: safe_str(o.get("year_commisioning"))),
        ("Материал_стен",            "s", lambda o: safe_str(o.get("materials"))),
        ("Основание_КС",             "s", lambda o: safe_str(o.get("determination_couse"))),
        ("Дата_определения_КС",      "s", lambda o: fmt_date(o.get("cost_determination_date"))),
        ("Дата_утверждения_КС",      "s", lambda o: fmt_date(o.get("cost_approval_date"))),
        ("Дата_применения_КС",       "s", lambda o: fmt_date(o.get("cost_application_date"))),
        ("Дата_регистрации_КС",      "s", lambda o: fmt_date(o.get("cost_registration_date"))),
        ("Ранее_опубликованные",     "s", lambda o: safe_str(o.get("previously_posted"))),
        ("Кадастровый_номер_ЗУ",     "s", lambda o: safe_str(o.get("parcel_cad_number"))),
        ("Инвентаризационный_номер", "s", lambda o: safe_str(o.get("inventory_number"))),
        ("Наличие_координат",        "s", lambda o: safe_str(o.get("coords_status")) or "Нет"),
    ]


class StructureCategory(CategoryDef):
    """Сооружение (категория НСПД 36383).

    Отличается от здания набором габаритных характеристик: площадь,
    протяжённость, объём, высота (линейные сооружения могут приходить
    с осевыми линиями — они не загружаются, см. nspd_geometry).
    Часть полей имеет двойственные ключи (address_readable_address /
    readable_address и т.п.) из-за непоследовательности API НСПД.
    """

    CATEGORY_ID = 36383
    CATEGORY_NAME = "Сооружение"
    FALLBACK_KEYWORDS = ["сооруж"]

    # Схема таблицы сооружений (см. формат COLUMNS в CategoryDef)
    COLUMNS = [
        ("Вид_объекта",              "s", lambda o: "Сооружение"),
        ("Кадастровый_номер",        "s", lambda o: safe_str(o.get("cad_num"))),
        ("Кадастровый_квартал",      "s", lambda o: safe_str(o.get("quarter_cad_number"))),
        ("Адрес",                    "s", lambda o: safe_str(o.get("address_readable_address") or o.get("readable_address"))),
        ("Площадь",                  "f", lambda o: safe_float(o.get("params_area"))),
        ("Протяженность",            "f", lambda o: safe_float(o.get("params_extension"))),
        ("Объем",                    "f", lambda o: safe_float(o.get("params_volume"))),
        ("Высота",                   "f", lambda o: safe_float(o.get("params_height"))),
        ("Статус",                   "s", lambda o: safe_str(o.get("object_previously_posted") or o.get("status"))),
        ("Форма_собственности",      "s", lambda o: safe_str(o.get("ownership_type"))),
        ("Кадастровая_стоимость",    "f", lambda o: safe_float(o.get("cost_value"))),
        ("Удельный_показатель_КС",   "f", lambda o: safe_float(o.get("cost_index"))),
        ("Дата_присвоения",          "s", lambda o: fmt_date(o.get("registration_date"))),
        ("Наименование",             "s", lambda o: safe_str(o.get("params_name"))),
        ("Назначение",               "s", lambda o: safe_str(o.get("params_purpose"))),
        ("Количество_этажей",        "i", lambda o: safe_int(o.get("params_floors") or o.get("floors"))),
        ("Основание_КС",             "s", lambda o: safe_str(o.get("determination_couse"))),
        ("Дата_определения_КС",      "s", lambda o: fmt_date(o.get("cost_determination_date"))),
        ("Дата_утверждения_КС",      "s", lambda o: fmt_date(o.get("cost_approval_date"))),
        ("Дата_применения_КС",       "s", lambda o: fmt_date(o.get("cost_application_date"))),
        ("Дата_регистрации_КС",      "s", lambda o: fmt_date(o.get("cost_registration_date"))),
        ("Ранее_опубликованные",     "s", lambda o: safe_str(o.get("object_previously_posted"))),
        ("Кадастровый_номер_ЗУ",     "s", lambda o: safe_str(o.get("parcel_cad_number"))),
        ("Материал",                 "s", lambda o: safe_str(o.get("material"))),
        ("Год_ввода",                "s", lambda o: safe_str(o.get("year_commisioning"))),
        ("Наличие_координат",        "s", lambda o: safe_str(o.get("coords_status")) or "Нет"),
    ]


class IncompleteCategory(CategoryDef):
    """Объект незавершённого строительства — ОНС (категория НСПД 36384).

    Специфические колонки: степень готовности (%) и площадь застройки.
    Многие поля читаются из нескольких возможных ключей options,
    т.к. НСПД отдаёт данные ОНС в разнородных структурах.
    """

    CATEGORY_ID = 36384
    CATEGORY_NAME = "ОНС"
    FALLBACK_KEYWORDS = ["незавершен"]

    # Схема таблицы ОНС (см. формат COLUMNS в CategoryDef)
    COLUMNS = [
        ("Вид_объекта",              "s", lambda o: safe_str(o.get("object_under_construction_record_record_type_value")) or "ОНС"),
        ("Кадастровый_номер",        "s", lambda o: safe_str(o.get("cad_num"))),
        ("Кадастровый_квартал",      "s", lambda o: safe_str(o.get("quarter_cad_number"))),
        ("Адрес",                    "s", lambda o: safe_str(o.get("readable_address") or o.get("address_readable_address"))),
        ("Площадь",                  "f", lambda o: safe_float(o.get("area") or o.get("params_area"))),
        ("Площадь_застройки",        "f", lambda o: safe_float(o.get("built_up_area"))),
        ("Статус",                   "s", lambda o: safe_str(o.get("common_data_status") or o.get("status"))),
        ("Форма_собственности",      "s", lambda o: safe_str(o.get("ownership_type"))),
        ("Кадастровая_стоимость",    "f", lambda o: safe_float(o.get("cost_value"))),
        ("Удельный_показатель_КС",   "f", lambda o: safe_float(o.get("cost_index"))),
        ("Дата_присвоения",          "s", lambda o: fmt_date(o.get("registration_date") or o.get("land_record_reg_date"))),
        ("Наименование",             "s", lambda o: safe_str(o.get("object_under_construction_record_name"))),
        ("Назначение",               "s", lambda o: safe_str(o.get("object_under_construction_record_name") or o.get("assignment_type") or o.get("name_type"))),
        ("Количество_этажей",        "i", lambda o: safe_int(o.get("params_floors") or o.get("floors"))),
        ("Основание_КС",             "s", lambda o: safe_str(o.get("determination_couse"))),
        ("Дата_определения_КС",      "s", lambda o: fmt_date(o.get("cost_determination_date"))),
        ("Дата_утверждения_КС",      "s", lambda o: fmt_date(o.get("cost_approval_date"))),
        ("Дата_применения_КС",       "s", lambda o: fmt_date(o.get("cost_application_date"))),
        ("Дата_регистрации_КС",      "s", lambda o: fmt_date(o.get("cost_registration_date"))),
        ("Ранее_опубликованные",     "s", lambda o: safe_str(o.get("previously_posted") or o.get("object_previously_posted"))),
        ("Кадастровый_номер_ЗУ",     "s", lambda o: safe_str(o.get("parcel_cad_number"))),
        ("Материал",                 "s", lambda o: safe_str(o.get("materials") or o.get("material"))),
        ("Год_ввода",                "s", lambda o: safe_str(o.get("year_commisioning"))),
        ("Степень_готовности",       "f", lambda o: safe_float(o.get("degree_readiness"))),
        ("Наличие_координат",        "s", lambda o: safe_str(o.get("coords_status")) or "Нет"),
    ]


# ═══════════════════════════ Реестр категорий ═══════════════════════════

# Константы категорий (числовые ID НСПД) — используются всеми модулями
CAT_LAND = LandCategory.CATEGORY_ID
CAT_BUILD = BuildingCategory.CATEGORY_ID
CAT_STRUCTURE = StructureCategory.CATEGORY_ID
CAT_INCOMPLETE = IncompleteCategory.CATEGORY_ID

# Человекочитаемые названия: category_id → имя для логов/сообщений
CAT_NAMES = {
    CAT_LAND: LandCategory.CATEGORY_NAME,
    CAT_BUILD: BuildingCategory.CATEGORY_NAME,
    CAT_STRUCTURE: StructureCategory.CATEGORY_NAME,
    CAT_INCOMPLETE: IncompleteCategory.CATEGORY_NAME,
}

# Фильтры допустимых типов: ID ожидаемой категории → список подходящих
# category_id в ответах НСПД (сейчас 1:1; задел для групп, где одному
# выбору пользователя соответствуют несколько категорий)
CATEGORY_FILTERS = {
    CAT_LAND: [CAT_LAND],
    CAT_BUILD: [CAT_BUILD],
    CAT_STRUCTURE: [CAT_STRUCTURE],
    CAT_INCOMPLETE: [CAT_INCOMPLETE],
}

# Реестр: categoryId → класс категории (главный словарь типов модуля;
# используется nspd_client для expand и nspd_table для колонок/названий)
CATEGORY_REGISTRY = {
    LandCategory.CATEGORY_ID: LandCategory,
    BuildingCategory.CATEGORY_ID: BuildingCategory,
    StructureCategory.CATEGORY_ID: StructureCategory,
    IncompleteCategory.CATEGORY_ID: IncompleteCategory,
}


def resolve_category(category=None, category_name="", expected=None):
    """Подбирает определение категории по ответу НСПД.

    Приоритет распознавания:
    1. expected — если задан и числовой category отсутствует
       (доверяем выбору пользователя);
    2. category — точное совпадение ID с CATEGORY_REGISTRY;
    3. category_name — перебор классов через recognize() по ключевым
       словам названия (fallback, когда ID не пришёл).

    Returns:
        (cat_def|None, category_id|None).
    """
    if expected is not None and category is None:
        return CATEGORY_REGISTRY.get(expected), expected
    if category is not None:
        return CATEGORY_REGISTRY.get(category), category
    if category_name:
        for cd in CATEGORY_REGISTRY.values():
            if cd.recognize(None, category_name):
                return cd, cd.CATEGORY_ID
    return None, None