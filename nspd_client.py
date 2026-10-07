# -*- coding: utf-8 -*-
"""Клиент поиска объектов на портале НСПД (nspd.gov.ru).

Назначение файла:
    Транспортно-независимая логика запроса объекта по кадастровому
    номеру через API geoportal НСПД. Файл отвечает за:
    - построение URL поиска и цикл повторов (3 попытки);
    - разбор GeoJSON-ответа (data.features → properties/geometry);
    - распознавание категории объекта через реестр categories.py;
    - отсев объектов не того типа (mismatch_cat);
    - раскрытие составных объектов (cat_def.expand — например ЕЗП);
    - классификацию статуса координат (cat_def.classify_coords,
      опирается на общую обработку nspd_geometry.prepare_geometry).
    HTTP-вызовы делегируются nspd_transport.http_get (injectable),
    поэтому модуль тестируется без сети.
"""

import logging
import time
import urllib.parse

from .nspd_transport import http_get
from .categories import (
    CATEGORY_REGISTRY,
    CATEGORY_FILTERS,
    resolve_category,
)

logger = logging.getLogger("NSPD_Loader")


def query_nspd(cad_num, expected_category, _depth=0, on_status=None):
    """Запрашивает объект по кадастровому номеру на НСПД.

    Выполняет до 3 попыток HTTP-запроса с паузами при ошибках сервера.
    Для каждого найденного feature: определяет категорию (по ID или по
    имени через реестр), отбрасывает объекты чужого типа, вычисляет
    статус координат и собирает плоский dict-результат, готовый для
    категорий и таблицы. На верхнем уровне (_depth==0) вызывает
    раскрытие состава (ЕЗП → дочерние участки).

    Args:
        cad_num: кадастровый номер объекта;
        expected_category: ожидаемый ID категории (или None — любой
            распознанный тип);
        _depth: служебная вложенность (1 = дочерний запрос из expand,
            без повторного раскрытия);
        on_status: callback(num, status, *args) для UI-лога
            (ezp_start / ezp_child_ok / ...).

    Returns:
        (list, mismatch_cat) — кортеж:
          - список найденных объектов с подходящей категорией
            (dict: cad_num, category, category_name, geometry,
            options, geom_id, coords_status);
          - mismatch_cat: категория объекта, если она не совпала с
            ожидаемой (для сообщения пользователю).
        Специальные значения первого элемента:
          "BLOCKED" — 403 (портал заблокировал запросы);
          "SERVER_ERROR" — 5xx/таймаут после 3 попыток;
          None — 404 (объект не найден / снят с учёта).
    """
    encoded = urllib.parse.quote(cad_num)
    url = (
        "https://nspd.gov.ru/api/geoportal/v2/search/geoportal"
        f"?query={encoded}&thematicSearchId=1"
    )

    for attempt in range(3):
        try:
            logger.info("Попытка %d/3 для %s", attempt + 1, cad_num)
            code, data = http_get(url)
            logger.info("HTTP статус: %d", code)

            if code == 403:
                # Антибот портала — выше (processor) решит, прерывать ли пакет
                logger.warning("403: доступ заблокирован")
                return "BLOCKED", None
            if code == 404:
                logger.info("404: объект не найден (снят с учёта или отсутствует)")
                return None, None
            if code != 200 or data is None:
                logger.warning("HTTP Error: %d", code)
                time.sleep(2)
                continue

            # GeoJSON FeatureCollection ответов geoportal API
            features = data.get("data", {}).get("features", [])
            logger.info("Найдено features: %d", len(features))

            results = []
            mismatch_cat = None

            for feature in features:
                props = feature.get("properties", {})
                category = props.get("category")
                category_name = props.get("categoryName", "")

                logger.info("category=%s, categoryName=%r", category, category_name)

                # Распознавание через реестр: сначала точный ID,
                # затем fallback по ключевым словам названия
                cat_def, category = resolve_category(category, category_name)
                if cat_def is not None:
                    logger.info("Категория определена: %s", cat_def.CATEGORY_NAME)

                # Проверка совпадения типа: пользователь выбрал, напр., «Здание»,
                # а пришёл «Земельный участок» — запоминаем чужую категорию
                if expected_category is not None:
                    allowed = CATEGORY_FILTERS.get(expected_category, [expected_category])
                    if category is not None and category not in allowed:
                        logger.info(
                            "Тип не совпал: ожидается %s, получено %s",
                            expected_category, category
                        )
                        mismatch_cat = category
                        continue

                options = props.get("options", {})
                geometry = feature.get("geometry", {})
                geom_id = feature.get("id")

                # Классификация координат через категорию
                # (общая обработка геометрии — nspd_geometry.prepare_geometry)
                if cat_def is not None:
                    coords_status = cat_def.classify_coords(geometry, options)
                else:
                    coords_status = "Нет"

                results.append({
                    "cad_num": options.get("cad_num", cad_num),
                    "category": category,
                    "category_name": category_name,
                    "geometry": geometry,
                    "options": options,
                    "geom_id": geom_id,
                    "coords_status": coords_status,
                })

            # Раскрытие (ЕЗП и т.д.) — только на верхнем уровне,
            # чтобы дочерние запросы не запускали рекурсивное раскрытие
            if _depth == 0 and results:
                expanded = []
                for result in results:
                    cat_def = CATEGORY_REGISTRY.get(result.get("category"))
                    if cat_def is not None:
                        expanded.extend(
                            cat_def.expand(result, query_nspd, http_get, on_substatus=on_status)
                        )
                    else:
                        expanded.append(result)
                results = expanded

            if results:
                logger.info("Успешно: %d объектов для %s", len(results), cad_num)
                return results, None
            elif mismatch_cat is not None:
                return [], mismatch_cat
            else:
                logger.info("Ни один feature не подошёл")
                return [], None

        except Exception as e:
            logger.error("Ошибка: %s", e)
            time.sleep(2)

    logger.warning("Объект не найден: %s", cad_num)
    return "SERVER_ERROR", None