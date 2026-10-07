# -*- coding: utf-8 -*-
"""Общая обработка геометрии НСПД: GeoJSON → объекты геометрии axipy.

Назначение файла:
    Единственная точка входа для ВСЕХ категорий по работе с геометрией
    (используется categories.py для классификации статуса координат и
    nspd_table.py для фактической загрузки в таблицу). Реализует правила
    ФГИС ЕГРН/НСПД:

    1. Загружаемой геометрией объекта являются ТОЛЬКО Polygon и
       MultiPolygon — многоконтурный участок либо полигон с дырами
       (внутренними кольцами). Все контуры и дыры сохраняются как
       единый объект.
    2. LineString / MultiLineString — осевые линии (например, оси
       линейных сооружений). Загружаемой геометрией НЕ являются,
       в таблицу не добавляются, границами объекта не считаются.
    3. Point / MultiPoint — адресные привязки. Используются вместо
       геометрии только когда собственная граница (полигон) объекта
       отсутствует.
    4. GeometryCollection — контейнер частей: берётся первый валидный
       полигон; если полигонов нет — первая адресная точка.

    Дополнительно выполняются чистка и защита данных: отбрасывается
    замыкающая точка колец GeoJSON, отбраковываются вырожденные кольца
    (< 3 уникальных вершин), мусорные координаты игнорируются.

    Модуль не импортирует axipy на уровне пакета (только внутри
    make_geometry/_make_point), поэтому его можно тестировать вне ГИС.
"""

import logging
import traceback

logger = logging.getLogger("NSPD_Loader")

# Типы, являющиеся загружаемой геометрией (границами объекта)
POLYGON_TYPES = ("Polygon", "MultiPolygon")
# Типы адресных привязок (используются при отсутствии полигона)
POINT_TYPES = ("Point", "MultiPoint")
# Осевые линии — не являются загружаемой геометрией
LINE_TYPES = ("LineString", "MultiLineString")


def ring_to_points(ring):
    """GeoJSON-кольцо/массив точек → список [(x, y), ...] float-кортежей.

    Универсальный разбор: принимает любое представление списка позиций,
    пропускает битые/неполные точки (len < 2, нечисловые значения).
    Используется и для колец полигонов, и для массивов точек MultiPoint.
    """
    pts = []
    if not isinstance(ring, list):
        return pts
    for p in ring:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                pts.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError):
                continue
    return pts


def _valid_polygon_coords(coords):
    """Проверяет и чистит координаты ОДНОГО полигона GeoJSON.

    Args:
        coords: структура [внешнее_кольцо, *дыры] из GeoJSON.

    Returns:
        Список колец [[(x, y), ...], ...] (первое — внешнее, далее дыры)
        или None, если внешнее кольцо вырождено (< 3 уникальных вершин).
        Дыры с < 3 вершин отбрасываются, но не делают полигон невалидным.
    """
    if not isinstance(coords, list) or not coords:
        return None

    def clean_ring(ring):
        pts = ring_to_points(ring)
        # Кольцо GeoJSON замкнуто: первая точка равна последней — убираем
        if len(pts) >= 4 and pts[0] == pts[-1]:
            pts = pts[:-1]
        # Уникальные вершины в исходном порядке (защита от колец-«линий»)
        uniq = list(dict.fromkeys(pts))
        return uniq

    outer = clean_ring(coords[0])
    if len(outer) < 3:
        return None
    rings = [outer]
    for hole_ring in coords[1:]:
        hole = clean_ring(hole_ring)
        if len(hole) >= 3:
            rings.append(hole)
    return rings


def _polygon_parts(geom_json):
    """Нормализует Polygon/MultiPolygon в список валидных полигонов.

    Returns:
        [[внешнее_кольцо, *дыры], ...] или None, если ни одного
        валидного полигона в геометрии нет (в т.ч. для линий/точек).
    """
    gtype = geom_json.get("type", "")
    coords = geom_json.get("coordinates")
    if coords is None:
        return None

    polys = []
    if gtype == "Polygon":
        poly = _valid_polygon_coords(coords)
        if poly is not None:
            polys.append(poly)
    elif gtype == "MultiPolygon":
        if not isinstance(coords, list):
            return None
        for poly_coords in coords:
            poly = _valid_polygon_coords(poly_coords)
            if poly is not None:
                polys.append(poly)
    else:
        return None

    return polys if polys else None


def has_boundary_geometry(geom_json):
    """True, если GeoJSON содержит загружаемую ГРАНИЧНУЮ геометрию.

    Границей считается только валидный полигон (Polygon/MultiPolygon).
    Точки и линии возвращают False — они границами объекта не являются.
    Используется категориями для статуса «Есть координаты границ».
    """
    if not geom_json or not isinstance(geom_json, dict):
        return False
    return _polygon_parts(geom_json) is not None


def has_address_point(geom_json):
    """True, если GeoJSON является адресной привязкой (Point/MultiPoint).

    Проверяется наличие хотя бы одной разобранной пары координат.
    """
    if not geom_json or not isinstance(geom_json, dict):
        return False
    gtype = geom_json.get("type", "")
    coords = geom_json.get("coordinates")
    if coords is None:
        return False
    if gtype == "Point":
        return bool(ring_to_points([coords]))
    if gtype == "MultiPoint":
        return bool(ring_to_points(coords))
    return False


def has_coords(geom_json):
    """True, если в GeoJSON есть любая полезная геометрия.

    Полигоны (границы) и адресные точки считаются; осевые линии — нет.
    """
    return has_boundary_geometry(geom_json) or has_address_point(geom_json)


def _make_point(points, cs):
    """Список [(x, y), ...] → axipy.Point (одна точка) или MultiPoint."""
    from axipy import Point, MultiPoint
    if len(points) == 1:
        return Point(points[0][0], points[0][1], cs)
    mp = MultiPoint(cs)
    for x, y in points:
        mp.append((x, y))
    return mp


def make_geometry(geom_json, cs):
    """Создаёт объект геометрии axipy из GeoJSON по правилам НСПД.

    Поведение по типам:
    - Polygon/MultiPolygon → axipy.Polygon (с holes) / axipy.MultiPolygon;
      MultiPolygon из одного полигона упрощается до Polygon с дырами;
    - Point/MultiPoint → axipy.Point/MultiPoint (адресная привязка);
      если в геометрии есть и полигон — точка игнорируется;
    - LineString/MultiLineString → None (осевые линии не грузятся);
    - отсутствующая СК (cs=None) → None с предупреждением.

    Args:
        geom_json: словарь GeoJSON (обычно — после prepare_geometry);
        cs: система координат axipy (CoordSystem).

    Returns:
        Объект геометрии axipy или None.
    """
    if not geom_json or not isinstance(geom_json, dict):
        return None
    if cs is None:
        logger.warning("Не задана система координат — геометрия пропущена")
        return None

    gtype = geom_json.get("type", "")
    coords = geom_json.get("coordinates")
    if coords is None:
        return None

    try:
        # ── Полигоны: единственная полноценная геометрия НСПД ──
        if gtype in POLYGON_TYPES:
            polys = _polygon_parts(geom_json)
            if polys is None:
                return None

            from axipy import Polygon, MultiPolygon

            def build_polygon(rings):
                """Кольца [[внешнее], *дыры] → axipy.Polygon с holes."""
                poly = Polygon(rings[0], cs)
                for hole in rings[1:]:
                    poly.holes.append(hole)
                return poly

            # Один полигон (или MultiPolygon из одного) → Polygon с дырами
            if gtype == "Polygon" or len(polys) == 1:
                return build_polygon(polys[0])

            # Несколько полигонов → многоконтурный MultiPolygon
            mpoly = MultiPolygon(cs)
            for rings in polys:
                mpoly.append(build_polygon(rings))
            logger.info("MultiPolygon: %d полигонов", len(polys))
            return mpoly

        # ── Адресные привязки: только при отсутствии полигона ──
        if gtype in POINT_TYPES:
            if has_boundary_geometry(geom_json):
                return None
            points = ring_to_points(coords if gtype == "MultiPoint" else [coords])
            if not points:
                return None
            return _make_point(points, cs)

        # ── Осевые линии: не загружаемая геометрия ──
        if gtype in LINE_TYPES:
            logger.debug("%s — осевая линия, геометрия не создаётся", gtype)
            return None

    except Exception as e:
        logger.error("Ошибка геометрии (%s): %s", gtype, e)
        traceback.print_exc()
        return None
    return None


def prepare_geometry(obj):
    """Возвращает GeoJSON, пригодный для загрузки как геометрия объекта.

    Общая для всех категорий функция-«фильтр» между сырыми данными НСПД
    и make_geometry. Принимает ЛИБО объект результата поиска (из словаря
    берётся поле "geometry"), ЛИБО сам GeoJSON (см. extract_geometry).

    Правила отбора (по приоритету):
    - полигон (Polygon/MultiPolygon) возвращается как есть;
    - GeometryCollection — первый валидный полигон из составных частей,
      при отсутствии полигонов — первая адресная точка;
    - точка (Point/MultiPoint) возвращается только если полигона нет
      (адресная привязка вместо отсутствующей границы);
    - линии и прочие типы → None (загружать нечего).

    Returns:
        dict GeoJSON или None.
    """
    geom_json = extract_geometry(obj)
    if geom_json is None:
        return None

    # GeometryCollection: НСПД может возвращать набор геометрических частей
    if geom_json.get("type") == "GeometryCollection":
        parts = geom_json.get("geometries", [])
        if not isinstance(parts, list):
            return None
        # Сначала ищем полигоны (полноценная граница)
        for part in parts:
            if isinstance(part, dict) and has_boundary_geometry(part):
                return part
        # Затем — адресную точку
        for part in parts:
            if isinstance(part, dict) and has_address_point(part):
                return part
        return None

    if has_boundary_geometry(geom_json):
        return geom_json
    if has_address_point(geom_json):
        return geom_json
    return None


# Полный набор GeoJSON-типов, встречающихся в ответах НСПД
GEOMETRY_TYPES = ("Polygon", "MultiPolygon", "Point", "MultiPoint",
                  "LineString", "MultiLineString", "GeometryCollection")


def is_geojson(d):
    """True, если dict похож на GeoJSON-геометрию (а не на объект НСПД).

    Признаки: известный тип геометрии + наличие "coordinates"
    (для GeometryCollection — непустого списка "geometries").
    Нужен, чтобы отличить переданный напрямую GeoJSON от результата
    поиска, у которого geometry лежит во вложенном поле.
    """
    if not isinstance(d, dict):
        return False
    gtype = d.get("type")
    if gtype not in GEOMETRY_TYPES:
        return False
    if gtype == "GeometryCollection":
        return isinstance(d.get("geometries"), list)
    return "coordinates" in d


def extract_geometry(obj):
    """Извлекает GeoJSON геометрии из объекта НСПД или из самого GeoJSON.

    Supports оба варианта аргумента:
    - результат query_nspd: {"cad_num": ..., "geometry": {...}} → .get("geometry");
    - уже GeoJSON-геометрия (is_geojson) → возвращается как есть.
    """
    if not isinstance(obj, dict):
        return None
    # Сам объект — GeoJSON-геометрия
    if is_geojson(obj):
        return obj
    geom_json = obj.get("geometry")
    if not geom_json or not isinstance(geom_json, dict):
        return None
    return geom_json