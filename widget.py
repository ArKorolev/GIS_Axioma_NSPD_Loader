# -*- coding: utf-8 -*-
"""Пользовательский интерфейс модуля НСПД (док-окно загрузки).

Назначение файла:
    Qt-виджет NspdWidget: поле ввода кадастровых номеров, лог, прогресс,
    кнопки. Сам процесс загрузки вынесен в бизнес-слой и выполняется в
    фоновом QThread (Worker), чтобы не блокировать UI Аксиомы:
    - бизнес-логика пакета — nspd_processor.NspdProcessor;
    - создание таблиц и слоёв — nspd_table.save_results_by_category.
    Взаимодействие поток↔UI только через сигналы Qt (log_message,
    progress, finished) — прямого обращения к виджету из потока нет.
"""

import logging
import re
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from PySide2.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QMessageBox, QProgressBar
)
from PySide2.QtCore import QThread, Signal

from .nspd_processor import NspdProcessor
from .nspd_table import save_results_by_category, find_table_by_category
from .categories import CAT_NAMES, CAT_LAND, CAT_BUILD, CAT_STRUCTURE, CAT_INCOMPLETE

logger = logging.getLogger("NSPD_Loader")

# Формат кадастрового номера: регион:квартал:участок:объект
CAD_NUM_PATTERN = re.compile(r'^\d{2}:\d{2}:\d{6,7}:\d{1,5}$')


class Worker(QThread):
    """Фоновый поток загрузки: тонкая обёртка над NspdProcessor.

    Сигналы (единственный канал связи с UI-потоком):
        progress(int done, int total) — обновление прогресс-бара;
        log_message(str) — готовая строка лога для вывода.

    Процессор вызывается с expected_category=None — категория каждого
    объекта определяется по ответу НСПД автоматически.
    """
    progress = Signal(int, int)
    log_message = Signal(str)

    def __init__(self, numbers):
        super().__init__()
        self.processor = NspdProcessor(numbers, None)

    def cancel(self):
        """Просит процессор остановиться (проверяется между номерами)."""
        self.processor.cancel()

    def run(self):
        """Тело потока: прогон пакета с переводом статусов в текст лога."""
        def on_status(num, status, *args):
            """Форматирует событийный статус процессора в строку лога.

            Специальные статусы ЕЗП (ezp_start/ezp_child_*) приходят из
            LandCategory.expand через коллбек query_nspd; обычные — из
            NspdProcessor. Последним аргументом всегда время ответа (сек).
            """
            if status == "ezp_start":
                msg = f"{num}, запрос состава ЕЗП..."
                self.log_message.emit(msg)
                return

            elapsed = args[-1] if args else 0
            if status == "ok":
                if len(args) > 1 and args[0] > 0:
                    msg = f"{num} — OK, ЕЗП: {args[0]} обособл. ({elapsed:.1f} с)"
                else:
                    msg = f"{num} — OK ({elapsed:.1f} с)"
            elif status == "ezp_child_ok":
                msg = f"{num} — OK ({elapsed:.1f} с)"
            elif status == "ezp_child_not_found":
                msg = f"{num} — не найдено ({elapsed:.1f} с)"
            elif status == "ezp_child_error":
                msg = f"{num} — ошибка сервера ({elapsed:.1f} с)"
            elif status == "not_found":
                msg = f"{num} — не найдено ({elapsed:.1f} с)"
            elif status == "error":
                msg = f"{num} — ошибка сервера ({elapsed:.1f} с)"
            else:
                msg = f"{num} — {status} ({elapsed:.1f} с)"
            self.log_message.emit(msg)

        self.processor.run(
            on_progress=lambda done, total: self.progress.emit(done, total),
            on_status=on_status,
        )



class NspdWidget(QWidget):
    """Главный виджет док-окна «Данные НСПД».

    Состав UI:
    - многострочное поле ввода кадастровых номеров (по одному на строку);
    - окно лога (только чтение) + кнопка очистки;
    - кнопка «Загрузить» (во время работы превращается в «Отмена»);
    - прогресс-бар (виден только во время загрузки).

    Логика: валидация и дедупликация ввода (в т.ч. против уже загруженных
    в таблицы номеров — кэш _existing_nums), запуск Worker, приём его
    сигналов, итоговая сводка и сохранение результатов (_on_finished).
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)

        # Поле ввода кадастровых номеров
        layout.addWidget(QLabel("Кадастровые номера (по одному на строку):"))
        self._input = QPlainTextEdit()
        layout.addWidget(self._input)

        # Лог выполнения
        layout.addWidget(QLabel("Лог:"))
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(200)
        layout.addWidget(self._log)

        btn_layout = QHBoxLayout()

        # Кнопка запуска/отмены загрузки
        self._btn = QPushButton("Загрузить")
        self._btn.clicked.connect(self._on_load)
        btn_layout.addWidget(self._btn)

        # Кнопка очистки лога
        self._btn_clear_log = QPushButton("🧹")
        self._btn_clear_log.setToolTip("Очистить лог")
        self._btn_clear_log.setFixedWidth(36)
        self._btn_clear_log.clicked.connect(self._on_clear_log)
        btn_layout.addWidget(self._btn_clear_log)

        layout.addLayout(btn_layout)

        # Прогресс загрузки (скрыт, пока поток не запущен)
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._worker = None             # активный фоновый поток (или None)
        self._existing_nums = set()     # кэш номеров, уже лежащих в таблицах
        self._existing_nums_valid = False  # сбрасывается после каждой загрузки

    def _on_load(self):
        """Клик по основной кнопке: старт пакета либо отмена текущего.

        Ход старта:
        1. Разбор ввода: непустые строки, дедупликация с сохранением порядка.
        2. Валидация формата: неформатные номера — диалог с предложением
           продолжить только с корректными.
        3. Дедупликация против уже загруженных (кэш таблиц) — пропуски в лог.
        4. Запуск Worker с подключёнными сигналами.
        """
        # Если загрузка идёт — клик отменяет
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._btn.setEnabled(False)
            self._btn.setText("Отменяем...")
            return

        text = self._input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Внимание", "Введите кадастровые номера")
            return

        numbers = [line.strip() for line in text.splitlines() if line.strip()]
        # Дедупликация с сохранением порядка
        numbers = list(dict.fromkeys(numbers))

        invalid = [n for n in numbers if not CAD_NUM_PATTERN.match(n)]
        if invalid:
            msg = "Некорректный формат кадастрового номера:\n" + "\n".join(invalid[:5])
            if len(invalid) > 5:
                msg += f"\n...и ещё {len(invalid) - 5}"
            msg += "\n\nПродолжить с корректными номерами?"
            reply = QMessageBox.question(
                self, "Валидация", msg,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if reply == QMessageBox.No:
                return
            numbers = [n for n in numbers if CAD_NUM_PATTERN.match(n)]
            if not numbers:
                QMessageBox.warning(self, "Внимание", "Нет корректных номеров")
                return

        # Вторая дедупликация: против номеров уже в таблицах
        if not self._existing_nums_valid:
            self._update_existing_nums()
        before = len(numbers)
        numbers = [n for n in numbers if n not in self._existing_nums]
        skipped = before - len(numbers)
        if skipped > 0:
            self._log.appendPlainText(
                f"Пропущено {skipped} номеров (уже в таблицах)"
            )
        if not numbers:
            QMessageBox.information(self, "Готово", "Все номера уже загружены")
            return

        self._btn.setText("Отмена")

        self._progress.setVisible(True)
        self._progress.setValue(0)
        self._progress.setMaximum(len(numbers))

        self._worker = Worker(numbers)
        self._worker.log_message.connect(self._on_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_log(self, msg):
        """Слот сигнала log_message: дописывает строку в лог."""
        self._log.appendPlainText(msg)

    def _on_progress(self, done, total):
        """Слот сигнала progress: обновляет прогресс-бар."""
        self._progress.setMaximum(total)
        self._progress.setValue(done)

    def _on_clear_log(self):
        """Очищает окно лога."""
        self._log.clear()

    def _update_existing_nums(self):
        """Пересобирает кэш загруженных номеров из всех таблиц НСПД.

        Нужен, чтобы повторный пакет не запрашивал то, что уже лежит в
        слоях. Вызывается лениво при старте загрузки, если флаг
        _existing_nums_valid сброшен (после каждой успешной загрузки).
        """
        self._existing_nums.clear()
        self._existing_nums_valid = True
        for cat_id in (CAT_LAND, CAT_BUILD, CAT_STRUCTURE, CAT_INCOMPLETE):
            table = find_table_by_category(cat_id)
            if table is None:
                continue
            try:
                for feat in table.items():
                    val = feat.get("Кадастровый_номер", "")
                    if val:
                        self._existing_nums.add(str(val).strip())
            except Exception:
                pass
        logger.info("Кэш номеров обновлён: %d уникальных", len(self._existing_nums))

    def _on_finished(self):
        """Слот завершения Worker'а: сводка, сохранение, восстановление UI.

        Порядок:
        1. Кнопка/прогресс возвращаются в исходное состояние.
        2. В лог печатается сводка (время, успешно, по категориям,
           не найдено, ошибки сервера, обособленные участки ЕЗП).
        3. results_by_category сохраняются через save_results_by_category
           (создание/дополнение таблиц + открытие слоёв на карте).
        4. Информационное окно с итогом по каждой таблице.
        """
        self._progress.setVisible(False)
        self._btn.setEnabled(True)
        self._btn.setText("Загрузить")

        result = self._worker.processor.result

        # Кэш стал неактуален — таблицы изменились
        self._existing_nums_valid = False

        self._log.appendPlainText("")
        self._log.appendPlainText("=" * 25)
        self._log.appendPlainText(f"  Затрачено: {result.total_elapsed:.1f} с")
        self._log.appendPlainText(f"ИТОГ: запрошено {result.total_requested}")
        self._log.appendPlainText(f"  Успешно:      {result.total_success}")
        self._log.appendPlainText(f"  в т.ч. обособленных: {result.total_subunits}")

        for cat_id, results in sorted(result.results_by_category.items()):
            cat_name = CAT_NAMES.get(cat_id, str(cat_id))
            self._log.appendPlainText(f"  {cat_name}: {len(results)}")

        self._log.appendPlainText(f"  Не найдено:   {len(result.errors)}")
        if result.errors:
            self._log.appendPlainText("    " + ", ".join(result.errors))
        self._log.appendPlainText(f"  Ошибки сервера: {len(result.errors_server)}")
        if result.errors_server:
            self._log.appendPlainText("    " + ", ".join(result.errors_server))
        self._log.appendPlainText("=" * 25)
        self._log.appendPlainText("")

        if not result.results_by_category:
            parts = []
            if result.errors:
                parts.append(f"Не найдено: {len(result.errors)}")
            if result.errors_server:
                parts.append(f"Ошибки сервера: {len(result.errors_server)}")
            if not parts:
                parts.append("Объекты не найдены")
            QMessageBox.information(self, "Результат", "\n".join(parts))
            self._worker = None
            return

        output = save_results_by_category(result.results_by_category)

        parts = []
        for cat_id, (table, added, skipped, is_new) in sorted(output.items()):
            cat_name = CAT_NAMES.get(cat_id, str(cat_id))
            if is_new:
                parts.append(f"{cat_name}: создана таблица, {added} объектов")
            else:
                msg = f"{cat_name}: добавлено {added}"
                if skipped:
                    msg += f" (дубли: {skipped})"
                parts.append(msg)

        if result.errors:
            parts.append(f"Не найдено: {len(result.errors)}")
        if result.errors_server:
            parts.append(f"Ошибки сервера: {len(result.errors_server)}")

        QMessageBox.information(self, "Готово", "\n".join(parts))
        self._worker = None

    def cleanup(self):
        """Остановка при закрытии окна/выгрузке плагина: отмена и ожидание.

        Даёт потоку 5 секунд на корректное завершение, чтобы не бросать
        HTTP-запрос на середине и не оставить «висящий» QThread.
        """
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(5000)