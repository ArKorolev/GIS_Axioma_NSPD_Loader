# -*- coding: utf-8 -*-
"""HTTP-транспорт для запросов к порталу НСПД (nspd.gov.ru).

Назначение файла:
    Изолирует сетевой слой модуля: SSL-контекст, заголовки браузера,
    инициализация сессии (получение cookie), задержки между запросами и
    предохранитель от блокировки портала. НЕ знает про категории,
    кадастровые номера и разбор JSON — только «GET url → (код, json)».
    Благодаря этому верхние слои (nspd_client, categories) легко
    тестируются без сети: http_get передаётся им как параметр http_func.
"""

import json
import logging
import ssl
import time
import urllib.error
import urllib.request

logger = logging.getLogger("NSPD_Loader")

# Заголовки, имитирующие обычный браузер: без них НСПД отдаёт 403
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://nspd.gov.ru/map/",
}

# SSL-контекст с отключённой проверкой сертификатов: на стороне НСПД
# периодически возникают проблемы с цепочкой сертификатов, модуль
# ориентирован на работу внутри защищённой корпоративной сети
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_OPENER = None       # переиспользуемый opener (хранит cookie-сессию)
_REQUEST_COUNT = 0   # счётчик запросов для противоабонентских пауз

# Колбэк отмены (ставится бизнес-слоем через set_cancel_check): возвращает
# True, если пользователь нажал «Отмена». Нужен, чтобы длинные паузы
# (60 сек каждые 50 запросов) не блокировали отмену загрузки.
_CANCEL_CHECK = None


def set_cancel_check(func):
    """Регистрирует колбэк is_cancelled() для прерывания пауз в http_get.

    Вызывается из рабочего потока перед началом пакетной загрузки.
    Передача через модульный регистр, а не параметр, сохраняет простую
    сигнатуру http_get(url), которую категории получают как http_func.
    """
    global _CANCEL_CHECK
    _CANCEL_CHECK = func


def _interruptible_sleep(seconds):
    """Сон с дроблением на 1-секундные slices и проверкой отмены.

    Обычный time.sleep(60) не реагирует на «Отмену» пользователя: поток
    спит полную минуту. Здесь каждые 1 сек опрашивается _CANCEL_CHECK;
    при True сон прекращается досрочно (вызывающий цикл сам завершится
    по флагу отмены). Без зарегистрированного колбэка — обычный sleep.
    """
    if _CANCEL_CHECK is None:
        time.sleep(seconds)
        return
    end = time.monotonic() + seconds
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        try:
            if _CANCEL_CHECK():
                return
        except Exception:
            return
        time.sleep(min(1.0, remaining))


def get_opener():
    """Возвращает singleton-opener; при первом вызове инициализирует сессию.

    Инициализация = GET главной страницы nspd.gov.ru: портал выдаёт
    установочные cookie, без которых API-запросы могут получить 403.
    Сбой инициализации не фатален — пишется warning, запросы продолжаются.
    """
    global _OPENER
    if _OPENER is not None:
        return _OPENER

    _OPENER = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_SSL_CTX)
    )
    _OPENER.addheaders = [
        ("User-Agent", _HEADERS["User-Agent"]),
        ("Referer", _HEADERS["Referer"]),
    ]

    try:
        req = urllib.request.Request("https://nspd.gov.ru/")
        _OPENER.open(req, timeout=30)
        logger.info("Сессия инициализирована")
    except Exception as e:
        logger.warning("Не удалось инициализировать сессию: %s", e)

    return _OPENER


def http_get(url, timeout=30):
    """HTTP GET с обязательными паузами и подсчётом запросов.

    Антиблокировка НСПД:
    - задержка 2 сек перед каждым запросом;
    - пауза 60 сек каждые 50 запросов.

    Args:
        url: полный адрес API;
        timeout: таймаут соединения/чтения, сек.

    Returns:
        (status_code, parsed_json_or_None):
        код HTTP и разобранный JSON при успехе;
        (http_код, None) при HTTPError (403/404/5xx);
        (0, None) при сетевой ошибке/битом JSON.
    """
    global _REQUEST_COUNT
    _REQUEST_COUNT += 1

    # Пауза 60 сек каждые 50 запросов (дробная, с проверкой отмены:
    # «Отмена» пользователя прерывает сон в течение секунды)
    if _REQUEST_COUNT % 50 == 0:
        logger.info("Пауза 60 сек после %d запросов", _REQUEST_COUNT)
        _interruptible_sleep(60)

    # Задержка 2 сек между запросами
    _interruptible_sleep(2)

    opener = get_opener()
    try:
        req = urllib.request.Request(url)
        resp = opener.open(req, timeout=timeout)
        code = resp.getcode()
        body = resp.read().decode("utf-8")
        return code, json.loads(body)
    except urllib.error.HTTPError as e:
        # 403/404/500 и т.п. — код возвращается вызывающей стороне,
        # она сама решает: ретраить, пропустить или прерваться
        return e.code, None
    except Exception as e:
        logger.error("HTTP ошибка: %s", e)
        return 0, None