# -*- coding: utf-8 -*-
"""Оркестратор загрузки данных с НСПД (бизнес-логика модуля).

Назначение файла:
    Связывает клиент (nspd_client.query_nspd) и сохранение (nspd_table).
    Отвечает за жизненный цикл пакетной загрузки: цикл запросов по
    номерам, повторные раунды для серверных ошибок, прерывание при
    блокировке портала (3 подряд 403), отмену по действию пользователя
    и сборку итоговой сводки. Не зависит от Qt (UI — в widget.py) и от
    HTTP (транспорт — в nspd_transport.py); вызывается из Worker-потока.
"""

import logging
import time

from .nspd_client import query_nspd
from .categories import CAT_NAMES

logger = logging.getLogger("NSPD_Loader")


class ProcessorResult:
    """Итоговая сводка пакетной загрузки (накапливается в NspdProcessor.run).

    Атрибуты:
        results_by_category: {category_id: [result dict]} — успешно
            полученные объекты, сгруппированные по типу категории;
        errors: номера, не найденные на портале (404 / пустой ответ);
        errors_server: номера с серверными ошибками и блокировками (403);
        total_requested: сколько номеров всего запрошено;
        total_elapsed: суммарное время выполнения пакета, сек;
        total_subunits: сколько добавлено обособленных участков ЕЗП
            (сверх первого объекта на запрос).
    """

    def __init__(self):
        self.results_by_category = {}  # {category_id: [result, ...]}
        self.errors = []         # не найдено
        self.errors_server = []  # серверные ошибки / блокировки
        self.total_requested = 0
        self.total_elapsed = 0.0
        self.total_subunits = 0


    @property
    def total_success(self):
        """Число успешно полученных объектов (по всем категориям)."""
        return sum(len(v) for v in self.results_by_category.values())


class NspdProcessor:
    """Управляет циклом запросов и обработкой результатов пакета номеров.

    Не зависит от Qt — может использоваться в любом контексте
    (в модуле запускается внутри Worker-потока из widget.py).

    Поведение:
    - после каждого номера пауза 1 сек (щадящий режим для портала);
    - BLOCKED (403): номер уходит в errors_server; три блокировки подряд
      означают, что портал заблокировал сессию — пакет прерывается;
    - SERVER_ERROR: повторные раунды (до 2, с паузой 5 сек между ними);
    - отмена через cancel() проверяется на каждой итерации обоих циклов.
    """

    def __init__(self, numbers, expected_category):
        """
        Args:
            numbers: список кадастровых номеров (извлечён из ввода UI);
            expected_category: ожидаемая категория (ID) или None —
                принять любой распознанный тип.
        """
        self._numbers = numbers
        self._expected_category = expected_category
        self.result = ProcessorResult()
        self._cancel = False   # флаг отмены (ставится из UI-потока)

    @property
    def expected_category(self):
        """ID ожидаемой категории (для подписи слоя/таблицы)."""
        return self._expected_category

    def cancel(self):
        """Запрашивает досрочную остановку (безопасен из любого потока)."""
        self._cancel = True

    def is_cancelled(self):
        """True, если загрузка была отменена или прервана блокировкой."""
        return self._cancel

    def run(self, on_progress=None, on_status=None):
        """Выполняет пакет: основной цикл + до 2 повторных раундов.

        Ход:
        1. Основной цикл по всем номерам: запрос через query_nspd,
           классификация ответа (см. process_number ниже), прогресс.
        2. До двух повторных раундов для номеров из errors_server
           (НСПД нестабилен: временные 5xx/таймауты часто проходят
           со второй-третьей попытки).

        Args:
            on_progress: callback(done, total) для прогресс-бара;
            on_status: callback(num, status, *args) для строк лога UI.

        Returns:
            ProcessorResult — полная сводка (также self.result).
        """
        total = len(self._numbers)
        self.result.total_requested = total
        done = 0
        blocked_count = 0   # счётчик подряд идущих 403
        t_start = time.monotonic()

        def process_number(num):
            """Запрашивает один номер и относит результат в сводку.

            Классификация ответа query_nspd:
            - "BLOCKED"      → errors_server, +1 к blocked_count
              (при 3 подряд — прерываем весь пакет);
            - "SERVER_ERROR" → errors_server (попадёт в повторные раунды);
            - None / пусто   → errors (не найдено);
            - список         → результаты раскладываются по категориям,
              дополнительные объекты считаются обособленными участками ЕЗП.
            """
            nonlocal done, blocked_count
            t0 = time.monotonic()
            result, mismatch_cat = query_nspd(num, self._expected_category, on_status=on_status)
            elapsed = time.monotonic() - t0
            done += 1
            if on_progress:
                on_progress(done, total)

            if result == "BLOCKED":
                self.result.errors_server.append(num)
                blocked_count += 1
                if on_status:
                    on_status(num, "error", elapsed)
                if blocked_count >= 3:
                    logger.warning(
                        "3 подряд 403 — сервер заблокировал, прерываем"
                    )
                    self._cancel = True
            elif result == "SERVER_ERROR":
                self.result.errors_server.append(num)
                blocked_count = 0
                if on_status:
                    on_status(num, "error", elapsed)
            elif result is None:
                self.result.errors.append(num)
                blocked_count = 0
                if on_status:
                    on_status(num, "not_found", elapsed)
            elif result:
                for r in result:
                    cat = r.get("category")
                    if cat is not None:
                        self.result.results_by_category.setdefault(cat, []).append(r)
                    else:
                        logger.warning("Неизвестная категория для %s", num)
                subunits = len(result) if len(result) > 1 else 0
                self.result.total_subunits += subunits
                blocked_count = 0
                if on_status:
                    on_status(num, "ok", subunits, elapsed)
            else:
                self.result.errors.append(num)
                blocked_count = 0
                if on_status:
                    on_status(num, "not_found", elapsed)

        # Основной цикл
        for num in self._numbers:
            if self._cancel:
                break
            num = num.strip()
            if not num:
                continue
            time.sleep(1)
            process_number(num)

        # Повтор для серверных ошибок (до 2 раундов)
        for retry in range(2):
            if not self.result.errors_server or self._cancel:
                break
            time.sleep(5)
            logger.info(
                "Повтор %d/2 для %d объектов с ошибками сервера",
                retry + 1, len(self.result.errors_server)
            )
            retry_nums = self.result.errors_server[:]
            self.result.errors_server = []
            for num in retry_nums:
                if self._cancel:
                    break
                time.sleep(1)
                process_number(num)

        self.result.total_elapsed = time.monotonic() - t_start
        return self.result