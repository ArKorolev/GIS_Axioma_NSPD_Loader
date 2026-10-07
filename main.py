# -*- coding: utf-8 -*-
"""Запуск и остановка модуля НСПД (создание док-окна с интерфейсом).

Назначение файла:
    Связующее звено между плагином (__init__.py) и интерфейсом (widget.py).
    При первом вызове run() создаёт QDockWidget с NspdWidget и прикрепляет
    его справа к главному окну Аксиомы; повторные вызовы скрывают/показывают
    уже созданное окно (синглтон-поведение). stop() уничтожает окно и
    корректно завершает фоновую загрузку.
"""

import logging
import traceback

logger = logging.getLogger("NSPD_Loader")

# Единственный экземпляр док-окна модуля (None — окно не создавалось)
_dock = None


def run():
    """Открывает (или показывает/скрывает) окно модуля «Данные НСПД».

    Returns:
        QDockWidget — окно модуля, либо None при ошибке.
    """
    global _dock
    try:
        from axipy import mainwindow, DockWidgetArea
        from PySide2.QtWidgets import QDockWidget
        from .widget import NspdWidget

        # Окно уже создавалось — просто переключаем видимость (toggle)
        if _dock is not None:
            if _dock.isVisible():
                _dock.setVisible(False)
            else:
                _dock.setVisible(True)
                _dock.raise_()
            return _dock

        # Первое открытие: док-виджет с интерфейсом модуля справа
        widget = NspdWidget()
        dock = QDockWidget("Данные НСПД")
        dock.setWidget(widget)
        dock.setObjectName("NspdLoaderDock")  # имя для сохранения состояния UI

        mainwindow.add_dock_widget(dock, DockWidgetArea.Right)
        dock.setVisible(True)

        _dock = dock
        return dock

    except Exception as exc:
        logger.error("Ошибка запуска: %s", exc)
        traceback.print_exc()
        return None


def stop():
    """Закрывает окно модуля и останавливает фоновую загрузку.

    Вызывается из NspdLoaderPlugin.unload() при выгрузке плагина:
    сначала cleanup() виджета (отмена Worker-потока), затем окно
    отвязывается от родителя и удаляется.
    """
    global _dock
    try:
        if _dock is not None:
            widget = _dock.widget()
            if widget is not None and hasattr(widget, "cleanup"):
                widget.cleanup()
            _dock.setParent(None)
            _dock = None
    except Exception as e:
        logger.error("Ошибка остановки: %s", e)
        traceback.print_exc()