# -*- coding: utf-8 -*-
"""Слой сохранения данных НСПД в ГИС: временные таблицы, слои карты.

Назначение файла:
    Превращает результаты загрузки (списки dict от nspd_client) в
    объекты Аксиомы: создаёт/дополняет временные табличные файлы (`.tab`
    через provider_manager.shp), заполняет семантические колонки по
    схеме категории (categories.COLUMNS), строит геометрию через
    nspd_geometry и открывает слои на карте с цветом по типу объекта.
    Используется виджетом (widget.py) после завершения пакета.
"""

import logging
import traceback

from axipy import (
    data_manager,
    provider_manager,
    view_manager,
    Layer,
    CoordSystem,
    Style,
    MapView,
)
from axipy.da import Attribute, Schema, Feature

from .nspd_utils import safe_str
from .nspd_geometry import (
    has_boundary_geometry,
    has_address_point,
    make_geometry,
    prepare_geometry,
)
from .categories import (
    CATEGORY_REGISTRY,
    CAT_NAMES,
    CAT_LAND,
    CAT_BUILD,
    CAT_STRUCTURE,
    CAT_INCOMPLETE,
    resolve_category,
)

logger = logging.getLogger("NSPD_Loader")

# Имена таблиц по категориям — служат ключом поиска уже открытых таблиц
TABLE_NAMES = {
    CAT_LAND: "Земельные_участки_НСПД",
    CAT_BUILD: "Здания_НСПД",
    CAT_STRUCTURE: "Сооружения_НСПД",
    CAT_INCOMPLETE: "ОНС_НСПД",
}

# Стили слоёв по категориям (цвет различает типы объектов на карте).
# MapBasic: Pen (pattern, width, color) Brush (pattern, color)
# Цвет в формате R + G*256 + B*65536
LAYER_STYLES = {
    CAT_LAND:       Style.from_mapinfo("Pen (1, 2, 25600) Brush (1, 25600)"),        # синий
    CAT_BUILD:      Style.from_mapinfo("Pen (1, 2, 139) Brush (1, 139)"),            # тёмно-красный
    CAT_STRUCTURE:  Style.from_mapinfo("Pen (1, 2, 9109504) Brush (1, 9109504)"),    # серый
    CAT_INCOMPLETE: Style.from_mapinfo("Pen (1, 2, 30091) Brush (1, 30091)"),        # оранжевый
}


def _make_layer_style(cat_id):
    """Возвращает стиль заливки для слоя по категории (или None)."""
    return LAYER_STYLES.get(cat_id)


def _is_table_alive(table):
    """Проверяет, что таблица ещё существует в data_manager.

    Обёртка над «мёртвым» C++-объектом: обращение к атрибутам удалённой
    из менеджера таблицы бросает исключение — ловим и считаем мёртвой.
    """
    try:
        _ = table.coordsystem
        return True
    except Exception:
        return False


def find_table_by_category(category):
    """Ищет открытую временную таблицу по ID категории.

    Нужен для дозагрузки: если слой категории уже создан предыдущим
    пакетом, новые записи добавляются в него, а не плодят дубли-таблицы.

    Returns:
        Таблица axipy или None.
    """
    name = TABLE_NAMES.get(category)
    if name is None:
        return None
    found = data_manager.find(name)
    if found is not None and _is_table_alive(found):
        return found
    return None


def save_results_by_category(results_by_category):
    """Сохраняет сводку пакета: создаёт или дополняет таблицы по категориям.

    Для каждой категории результата: если открытая таблица уже есть —
    append_to_table (с пропуском дублей), иначе create_table. Все вновь
    созданные таблицы одним вызовом открываются на карте
    (_open_views_single_map).

    Args:
        results_by_category: {category_id: [result dict]} из ProcessorResult.

    Returns:
        {category_id: (table, added, skipped, is_new)} — отчёт для UI.
    """
    output = {}
    new_tables = []
    for cat_id, results in results_by_category.items():
        existing = find_table_by_category(cat_id)
        if existing is not None:
            added, skipped = append_to_table(results, existing, cat_id)
            output[cat_id] = (existing, added, skipped, False)
        else:
            table, cat, inserted = create_table(
                results, cat_id, name=TABLE_NAMES[cat_id]
            )
            if table is not None:
                output[cat_id] = (table, inserted, 0, True)
                new_tables.append((table, cat_id))
    if new_tables:
        _open_views_single_map(new_tables)
    return output


def _build_schema(columns, cs):
    """Строит Schema axipy по описанию колонок категории.

    Маппинг типов: "s" → строка 254, "f" → float, "i" → int.
    Система координат передаётся в Schema; если версия API её не
    принимает (TypeError) — схема строится без неё.
    """
    attrs = []
    for name, typ, _ in columns:
        if typ == "s":
            attrs.append(Attribute.string(name, 254))
        elif typ == "f":
            attrs.append(Attribute.float(name))
        elif typ == "i":
            attrs.append(Attribute.integer(name))
    try:
        schema = Schema(*attrs, coordsystem=cs)
    except TypeError:
        schema = Schema(*attrs)
    return schema


def _resolve_category(flat, expected_category=None):
    """Определяет категорию набора объектов через реестр категорий.

    Приоритет: явное ожидание (expected_category) → category первого
    объекта (через resolve_category с fallback по имени) → ЗУ по умолчанию.
    """
    if expected_category is not None:
        return expected_category

    first = flat[0]
    category = first.get("category")
    category_name = safe_str(first.get("category_name", ""))
    _, cat_id = resolve_category(category, category_name)
    return cat_id if cat_id is not None else CAT_LAND


def _get_existing_cad_nums(table, column_name="Кадастровый_номер"):
    """Множество существующих кадастровых номеров таблицы (защита от дублей)."""
    existing = set()
    try:
        for feat in table.items():
            val = feat.get(column_name, "")
            if val:
                existing.add(str(val).strip())
    except Exception as e:
        logger.warning("Не удалось прочитать существующие номера: %s", e)
    return existing


def _geometry_kind(geom_json):
    """Тип геометрии для логов: polygon / point (адресная привязка) / None."""
    if has_boundary_geometry(geom_json):
        return "polygon"
    if has_address_point(geom_json):
        return "point (адресная привязка)"
    return None


def _build_feature(obj, columns, cs):
    """Создаёт объект Feature из данных НСПД.

    Геометрия формируется общей обработкой (nspd_geometry):
    - Polygon/MultiPolygon — граница объекта;
    - Point/MultiPoint — адресная привязка, только если границы нет;
    - линии в таблицу не добавляются.
    Объекты без геометрии записываются как атрибутивные строки.

    Значения колонок вычисляются лямбдами схемы категории из options;
    сюда же подкладываются coords_status и гарантированный cad_num.
    Ошибки отдельного заполнителя не роняют запись — значение становится "".
    Если создание Feature с геометрией падает — строится атрибутивная
    запись (данные важнее формы).
    """
    row_options = dict(obj.get("options", {}))
    # Передаём coords_status в row_options для лямбд колонок
    row_options["coords_status"] = obj.get("coords_status", "")
    # Гарантируем cad_num — для некоторых категорий НСПД не кладёт его в options
    if "cad_num" not in row_options:
        row_options["cad_num"] = obj.get("cad_num", "")

    data = {}
    for name, typ, fill_fn in columns:
        try:
            val = fill_fn(row_options)
        except Exception:
            val = ""
        if typ == "f":
            try:
                val = float(val) if val != "" else 0.0
            except (ValueError, TypeError):
                val = 0.0
        elif typ == "i":
            try:
                val = int(float(val)) if val != "" else 0
            except (ValueError, TypeError):
                val = 0
        else:
            val = str(val) if val is not None else ""
        data[name] = val

    geom = None
    loadable = prepare_geometry(obj)
    if loadable is not None and cs is not None:
        geom = make_geometry(loadable, cs)
        if geom is not None:
            logger.info(
                "Геометрия OK (%s) для %s",
                _geometry_kind(loadable), obj.get("cad_num", "?")
            )

    try:
        feat = Feature(data, geometry=geom)
    except Exception as e:
        logger.warning("Feature с геометрией не создан (%s), пробуем без", e)
        feat = Feature(data)
    return feat


def _get_coordsystem():
    """СК для новых таблиц: Web Mercator (EPSG:3857), fallback — WGS84.

    Координаты НСПД приходят в географических градусах; 3857 выбрана
    как стандартная СК веб-карт, при недоступности — 4326, при обоих
    сбоях None (тогда грузятся только атрибуты).
    """
    try:
        return CoordSystem.from_epsg(3857)
    except Exception:
        try:
            return CoordSystem.from_epsg(4326)
        except Exception:
            return None


def _flatten(results):
    """Приводит результат запроса к плоскому списку dict-объектов.

    Принимает список объектов, список списков или одиночный dict —
    унифицирует вход для create_table/append_to_table.
    """
    flat = []
    if isinstance(results, list):
        for item in results:
            if isinstance(item, list):
                flat.extend(item)
            elif isinstance(item, dict):
                flat.append(item)
    elif isinstance(results, dict):
        flat = [results]
    return flat


def create_table(results, expected_category=None, name=None):
    """Создаёт новую временную таблицу и наполняет её данными НСПД.

    Ход: определение категории → схема колонок категории → SHP-временная
    таблица (provider_manager.shp.open_temporary) с именем `name` →
    регистрация в data_manager → построчная вставка _build_feature.

    Args:
        results: объекты (см. _flatten);
        expected_category: ID категории или None (автоопределение);
        name: имя таблицы (из TABLE_NAMES), либо автоимя.

    Returns:
        (table, category, inserted_count) или (None, None, 0) при ошибке.
    """
    flat = _flatten(results)
    if not flat:
        logger.info("Нет данных")
        return None, None, 0

    category = _resolve_category(flat, expected_category)
    cat_def = CATEGORY_REGISTRY.get(category)
    columns = cat_def.COLUMNS if cat_def else []
    cat_name = CAT_NAMES.get(category, "Объект")
    logger.info("Тип: %s (category=%s)", cat_name, category)

    cs = _get_coordsystem()
    schema = _build_schema(columns, cs)
    logger.info("Schema: %d колонок", len(columns))

    try:
        table = provider_manager.shp.open_temporary(schema)
        if name is not None:
            table.name = name
        logger.info("Временная таблица создана")
    except Exception as e:
        logger.error("Не удалось создать временную таблицу: %s", e)
        return None, None, 0

    try:
        data_manager.add(table)
    except Exception:
        pass

    inserted = 0
    for obj in flat:
        feat = _build_feature(obj, columns, cs)
        try:
            table.insert(feat)
            inserted += 1
            logger.info("Записано: %s", obj.get("cad_num", "?"))
        except Exception as e:
            logger.error("Ошибка insert: %s", e)

    return table, category, inserted


def append_to_table(results, table, expected_category=None):
    """Добавляет записи в существующую таблицу, пропуская дубли.

    Дедупликация по «Кадастровый_номер»: номера, уже присутствующие в
    таблице (и ранее добавленные этим вызовом), не вставляются повторно.
    Геометрия строится в СК существующей таблицы.

    Returns:
        (added_count, skipped_duplicates).
    """
    flat = _flatten(results)
    if not flat:
        return 0, 0

    category = _resolve_category(flat, expected_category)
    cat_def = CATEGORY_REGISTRY.get(category)
    columns = cat_def.COLUMNS if cat_def else []
    cs = table.coordsystem if hasattr(table, "coordsystem") else _get_coordsystem()

    existing_nums = _get_existing_cad_nums(table)
    logger.info("Существующих записей в таблице: %d", len(existing_nums))

    added = 0
    skipped = 0
    for obj in flat:
        cad_num = safe_str(obj.get("options", {}).get("cad_num", obj.get("cad_num", "")))
        if cad_num and cad_num in existing_nums:
            logger.info("Дубль пропущен: %s", cad_num)
            skipped += 1
            continue
        feat = _build_feature(obj, columns, cs)
        try:
            table.insert(feat)
            added += 1
            if cad_num:
                existing_nums.add(cad_num)
            logger.info("Добавлен: %s", cad_num or "?")
        except Exception as e:
            logger.error("Ошибка insert: %s", e)

    return added, skipped


def _open_views_single_map(new_tables):
    """Открывает вновь созданные таблицы слоями на карте.

    Выбор окна карты: если активной карты нет или она в плоской проекции
    (NonEarth — например локальная СК проекта), данные НСПД (глобальная
    СК) недопустимо подмешивать в неё — создаётся новая карта. Иначе
    слои добавляются в активную карту. Для каждого слоя применяется
    цвет категории; чисто атрибутивные таблицы пропускаются.
    """
    if not new_tables:
        return

    # Проверяем CRS активного окна
    use_new_map = False
    active_view = view_manager.active
    if active_view is None or not isinstance(active_view, MapView):
        # Нет активной карты — нужна новая
        use_new_map = True
    else:
        try:
            cs = active_view.coordsystem
            if cs is not None and cs.non_earth:
                use_new_map = True
                logger.info(
                    "Активная карта — NonEarth, данные будут открыты в новой карте"
                )
        except Exception as e:
            logger.warning("Не удалось получить CRS активной карты: %s", e)
            use_new_map = True

    for table, cat_id in new_tables:
        try:
            if not table.is_spatial:
                continue
            layer = Layer.create(table)
            style = _make_layer_style(cat_id)
            if style is not None:
                layer.overrideStyle = style

            if use_new_map:
                view_manager.create_mapview(layer)
                logger.info(
                    "Слой добавлен в новую карту: %s",
                    TABLE_NAMES.get(cat_id, "?")
                )
            else:
                view_manager.add_to_current_mapview(layer)
                logger.info(
                    "Слой добавлен на активную карту: %s",
                    TABLE_NAMES.get(cat_id, "?")
                )
        except Exception as e:
            logger.error("Слой не создан: %s", e)
            traceback.print_exc()