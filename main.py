import hmac
import csv
import io
import json
import logging
import os
import random
import threading
import time
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional
from urllib.parse import urlparse

import requests
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("max-id-bot")
BASE_URL = "https://kvasmix.ru"

MAX_API_BASE_URL = os.getenv("MAX_API_BASE_URL", "https://platform-api.max.ru")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_TIMEOUT_SECONDS = float(os.getenv("MAX_TIMEOUT_SECONDS", "10"))
MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET")
MAX_API_MAX_RETRIES = int(os.getenv("MAX_API_MAX_RETRIES", "5"))
MAX_DEDUP_TTL_SECONDS = int(os.getenv("MAX_DEDUP_TTL_SECONDS", "3600"))
MAX_WEBHOOK_URL = os.getenv("MAX_WEBHOOK_URL", f"{BASE_URL}/webhook")
BASE_WEBHOOK_UPDATE_TYPES = [
    item.strip()
    for item in os.getenv("MAX_WEBHOOK_UPDATE_TYPES", "message_created,bot_started,message_callback").split(",")
    if item.strip()
]
MAX_WEBHOOK_AUTO_REGISTER = os.getenv("MAX_WEBHOOK_AUTO_REGISTER", "true").lower() in {"1", "true", "yes"}
MAX_STARTUP_SELF_CHECK = os.getenv("MAX_STARTUP_SELF_CHECK", "false").lower() in {"1", "true", "yes"}
MAX_CHANNEL_CHAT_ID = os.getenv("MAX_CHANNEL_CHAT_ID", "-72559954357735")
MAX_CHANNEL_URL = os.getenv("MAX_CHANNEL_URL", f"https://web.max.ru/{MAX_CHANNEL_CHAT_ID}")
MAX_CHANNEL_DEEPLINK = os.getenv("MAX_CHANNEL_DEEPLINK", "https://max.ru/id344309962847_biz")
MAX_WEB_APP = os.getenv("MAX_WEB_APP")
GOOGLE_SHEETS_ENABLED = os.getenv("GOOGLE_SHEETS_ENABLED", "false").lower() in {"1", "true", "yes"}
GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "15nXvYljl4yqNsw_nYLpNzFIo4SLlTQyQDaD2Y77Ll-8")
# Оставлено только для обратной совместимости старых тестов/конфига.
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SCRIPT_URL = os.getenv(
    "GOOGLE_SCRIPT_URL",
    "https://script.google.com/macros/s/AKfycbw81TJmqgmVxMV1NjMzUac7zqDqQialCMTplbpDdqCGgj2iwRbbYl2fYTcz1ee1K-7JQQ/exec",
)
ACTIVE_WEBHOOK_UPDATE_TYPES: list[str] = []
MOSCOW_TZ = timezone(timedelta(hours=3))


def get_channel_id_candidates() -> list[str]:
    """
    Возвращает варианты channel/chat id для запросов в MAX API.
    Практика показывает, что web-ссылка канала может быть со знаком "-", а API
    в некоторых методах ожидает id без знака. Поэтому пробуем оба варианта.
    """
    raw = (MAX_CHANNEL_CHAT_ID or "").strip()
    if not raw:
        return []
    variants = [raw]
    unsigned = raw.lstrip("-")
    if unsigned and unsigned not in variants:
        variants.append(unsigned)
    return variants


def get_channel_api_targets() -> list[str]:
    """
    Кандидаты идентификаторов канала для MAX API:
    - chat_id из конфигурации (со знаком и без);
    - slug/alias из ссылок MAX (`MAX_CHANNEL_DEEPLINK`, `MAX_CHANNEL_URL`), например `id344..._biz`.
    """
    targets: list[str] = []
    for cid in get_channel_id_candidates():
        if cid and cid not in targets:
            targets.append(cid)

    for raw_url in (MAX_CHANNEL_DEEPLINK, MAX_CHANNEL_URL):
        try:
            parsed = urlparse(raw_url)
            slug = parsed.path.strip("/")
            if slug and slug not in targets:
                targets.append(slug)
        except Exception:
            continue
    return targets

def _find_token_recursive(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        token_value = value.get("token")
        if token_value not in (None, ""):
            return str(token_value)
        for nested in value.values():
            found = _find_token_recursive(nested)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_token_recursive(item)
            if found:
                return found
    return None


def _contains_substring_recursive(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_substring_recursive(v, needle) for v in value.values())
    if isinstance(value, list):
        return any(_contains_substring_recursive(v, needle) for v in value)
    if isinstance(value, str):
        return needle in value
    return False


def parse_google_service_account(raw_value: str) -> dict[str, Any]:
    """Legacy helper (unused): parse JSON string into dict."""
    raw = (raw_value or "").strip()
    if not raw:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON пустой")
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1].strip()
    if raw.startswith("{\\"):
        raw = raw.replace('\\"', '"')
    return json.loads(raw)


def normalize_service_account_info(account_info: dict[str, Any]) -> dict[str, Any]:
    """Legacy helper (unused): normalize escaped newlines in private_key."""
    normalized = dict(account_info)
    private_key = normalized.get("private_key")
    if isinstance(private_key, str):
        normalized["private_key"] = private_key.replace("\\n", "\n")
    return normalized


app = FastAPI(title="MAX ID Bot", version="1.2.0")

# Простой in-memory dedup для повторной доставки webhook (at-least-once).
_processed_updates: dict[str, float] = {}
_dedup_lock = threading.Lock()
MONTHLY_REMINDER_USER_ID = "24324984"
MONTHLY_REMINDER_DAY = 29
MONTHLY_REMINDER_TEXT = "Обновить штрихкоды в 1с"
_last_monthly_reminder_date: Optional[date] = None
_monthly_reminder_lock = threading.Lock()
DASHBOARD_ALLOWED_USER_IDS = {"242649311", "24324984"}
DASHBOARD_COMMANDS = {"дашборд", "статистика", "/дашборд", "/статистика"}
QR_UTM_SOURCE_VALUE = "qr_podpiska"
QR_SUBSCRIBE_CALLBACK_DATA = "qr_subscribe_coupon"
_qr_podpiska_users: set[str] = set()
_qr_users_lock = threading.Lock()


def _extract_by_paths(payload: dict[str, Any], paths: list[str]) -> Optional[Any]:
    for path in paths:
        value: Any = payload
        for key in path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                value = None
                break
        if value not in (None, ""):
            return value
    return None


def extract_user_id(payload: dict[str, Any]) -> Optional[str]:
    candidate_paths = [
        "message.sender.user_id",
        "message.sender.id",
        "sender.user_id",
        "sender.id",
        "user_id",
        "user.user_id",
        "user.id",
        "profile.user_id",
        "profile.id",
        "dialog_with_user.user_id",
        "dialog_with_user.id",
    ]
    user_id = _extract_by_paths(payload, candidate_paths)
    return str(user_id) if user_id is not None else None


def extract_chat_id(payload: dict[str, Any]) -> Optional[str]:
    candidate_paths = [
        "message.recipient.chat_id",
        "message.chat_id",
        "chat.chat_id",
        "chat_id",
    ]
    chat_id = _extract_by_paths(payload, candidate_paths)
    return str(chat_id) if chat_id is not None else None


def extract_message_text(payload: dict[str, Any]) -> Optional[str]:
    text_value = _extract_by_paths(
        payload,
        [
            "message.body.text",
            "message.text",
            "body.text",
            "text",
        ],
    )
    return str(text_value) if text_value is not None else None


def extract_callback_data(payload: dict[str, Any]) -> Optional[str]:
    callback_data = _extract_by_paths(
        payload,
        [
            "callback.payload",
            "callback.data",
            "callback.value",
            "payload",
            "data",
        ],
    )
    return str(callback_data) if callback_data is not None else None


def has_qr_utm_source(payload: dict[str, Any]) -> bool:
    """
    Проверяет наличие utm_source=qr_podpiska в start/deep link payload.
    Поддерживает разные форматы входящих событий.
    """
    candidate_paths = [
        "message.body.text",
        "message.body.payload",
        "message.payload",
        "start.payload",
        "payload",
    ]
    needle = f"utm_source={QR_UTM_SOURCE_VALUE}"
    for path in candidate_paths:
        value = _extract_by_paths(payload, [path])
        if value is not None and needle in str(value):
            return True
    # Фолбэк для нестабильных схем webhook: ищем UTM по всему payload.
    return _contains_substring_recursive(payload, needle)


def mark_user_came_from_qr(user_id: Optional[str]) -> None:
    uid = str(user_id or "").strip()
    if not uid:
        return
    with _qr_users_lock:
        _qr_podpiska_users.add(uid)


def came_from_qr(user_id: Optional[str]) -> bool:
    uid = str(user_id or "").strip()
    if not uid:
        return False
    with _qr_users_lock:
        return uid in _qr_podpiska_users


def normalize_incoming_text(raw_text: str) -> str:
    """
    Нормализует входной текст:
    - trim;
    - lower;
    - убирает упоминание бота в командах вида '/id@my_bot'.
    """
    text = raw_text.strip().lower()
    if text.startswith("/") and "@" in text:
        text = text.split("@", 1)[0]
    return text


def is_start_command(normalized_text: str) -> bool:
    text = (normalized_text or "").strip()
    return text == "start" or text.startswith("/start")


def extract_dedup_key(payload: dict[str, Any]) -> Optional[str]:
    update_type = str(payload.get("update_type") or "unknown")
    mid = _extract_by_paths(payload, ["message.body.mid", "message.mid", "mid"])
    callback_id = _extract_by_paths(payload, ["callback.callback_id", "callback_id"])

    if mid:
        return f"{update_type}:mid:{mid}"
    if callback_id:
        return f"{update_type}:cb:{callback_id}"

    # Фолбэк: если нет mid/callback_id, dedup не применяем.
    return None


def _sleep_backoff(attempt: int, base: float = 0.4, cap: float = 8.0) -> None:
    delay = min(cap, base * (2**attempt))
    delay *= 0.5 + random.random()
    time.sleep(delay)


def should_send_monthly_reminder(now_utc: datetime) -> bool:
    return now_utc.day == MONTHLY_REMINDER_DAY


def send_monthly_reminder_if_needed(now_utc: Optional[datetime] = None) -> bool:
    now = now_utc or datetime.now(timezone.utc)
    if not should_send_monthly_reminder(now):
        return False

    today = now.date()
    with _monthly_reminder_lock:
        global _last_monthly_reminder_date
        if _last_monthly_reminder_date == today:
            return False

        try:
            send_max_message(
                text=MONTHLY_REMINDER_TEXT,
                user_id=MONTHLY_REMINDER_USER_ID,
                chat_id=None,
            )
            _last_monthly_reminder_date = today
            logger.info("Monthly reminder sent to user_id=%s", MONTHLY_REMINDER_USER_ID)
            return True
        except Exception as exc:
            logger.exception("Failed to send monthly reminder: %s", exc)
            return False


def monthly_reminder_worker() -> None:
    while True:
        send_monthly_reminder_if_needed()
        time.sleep(3600)


def _is_duplicate_and_mark(key: str) -> bool:
    now = time.time()
    with _dedup_lock:
        expired = [k for k, ts in _processed_updates.items() if now - ts > MAX_DEDUP_TTL_SECONDS]
        for k in expired:
            _processed_updates.pop(k, None)

        if key in _processed_updates:
            return True

        _processed_updates[key] = now
        return False


def get_coupon_barcode_and_expiry(target_date: Optional[date] = None) -> tuple[str, date]:
    current_date = target_date or datetime.now(timezone.utc).date()
    day = current_date.day
    month_last_day = monthrange(current_date.year, current_date.month)[1]

    if 1 <= day <= 10:
        barcode_value = "7123100000145"
        expiry = current_date.replace(day=10)
    elif 11 <= day <= 20:
        barcode_value = "7123100000152"
        expiry = current_date.replace(day=20)
    else:
        barcode_value = "7123100000169"
        expiry = current_date.replace(day=month_last_day)

    return barcode_value, expiry


def build_coupon_text(expiry_date: date) -> str:
    expiry_str = expiry_date.strftime("%d.%m.%Y")
    return (
        "Спасибо, что подписались 💛\n"
        "Дарим вам дополнительную скидку 5%.\n"
        "🛍 Покажите штрихкод на кассе и покупайте с выгодой\n\n"
        f"⏳ Купон действует до {expiry_str}\n"
        "_⚠ Скидка действует только на товары с белыми ценниками. "
        "Максимальная суммарная скидка - 20%. "
        "Купон доступен к получению один раз для каждого участника._"
    )


def generate_ean13_png_file(barcode_value: str, output_dir: Path) -> Path:
    """
    Генерирует PNG-файл EAN13.
    Используется ленивый импорт, чтобы модуль main.py не падал при импорте без этих зависимостей.
    """
    from barcode import EAN13  # type: ignore[import-not-found]
    from barcode.writer import ImageWriter  # type: ignore[import-not-found]

    filename = output_dir / "coupon_ean13"
    ean = EAN13(barcode_value, writer=ImageWriter())
    saved_path = Path(
        ean.save(
            str(filename),
            options={
                "write_text": False,
            },
        )
    )
    return saved_path


def _extract_upload_url(payload: dict[str, Any]) -> Optional[str]:
    return _extract_by_paths(payload, ["url", "upload_url", "data.url"])


def _extract_attachment_token(payload: dict[str, Any]) -> Optional[str]:
    return _extract_by_paths(payload, ["token", "file.token", "data.token", "attachment.token"]) or _find_token_recursive(
        payload
    )


def upload_image_and_get_token(file_path: Path) -> str:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    response = requests.post(
        f"{MAX_API_BASE_URL}/uploads",
        params={"type": "image"},
        headers={"Authorization": MAX_BOT_TOKEN},
        timeout=MAX_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Не удалось получить upload URL: {response.status_code}")

    upload_meta = response.json() if response.content else {}
    upload_url = _extract_upload_url(upload_meta)
    if not upload_url:
        raise HTTPException(status_code=502, detail="MAX /uploads не вернул URL для загрузки")

    token_from_meta = _extract_attachment_token(upload_meta)
    with file_path.open("rb") as fh:
        upload_response = requests.post(
            upload_url,
            headers={"Authorization": MAX_BOT_TOKEN},
            files={"data": (file_path.name, fh, "image/png")},
            timeout=MAX_TIMEOUT_SECONDS,
        )
    if upload_response.status_code >= 400:
        # fallback для совместимости с возможной схемой multipart-поля "file"
        with file_path.open("rb") as fh:
            upload_response = requests.post(
                upload_url,
                headers={"Authorization": MAX_BOT_TOKEN},
                files={"file": (file_path.name, fh, "image/png")},
                timeout=MAX_TIMEOUT_SECONDS,
            )
    if upload_response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Ошибка загрузки файла: {upload_response.status_code}")

    try:
        upload_result = upload_response.json() if upload_response.content else {}
    except ValueError:
        upload_result = {}

    token = _extract_attachment_token(upload_result) or token_from_meta
    if not token:
        logger.error(
            "Upload token not found. /uploads response=%s upload response=%s",
            json.dumps(upload_meta, ensure_ascii=False),
            upload_response.text[:500],
        )
        raise HTTPException(status_code=502, detail="Не получен token загруженного изображения")
    return str(token)


def send_max_message(
    text: str,
    user_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")
    if not user_id and not chat_id:
        raise ValueError("Нужен user_id или chat_id для отправки сообщения")

    params: dict[str, str] = {"user_id": user_id} if user_id else {"chat_id": chat_id}  # type: ignore[arg-type]
    url = f"{MAX_API_BASE_URL}/messages"

    last_error: Optional[str] = None
    for attempt in range(MAX_API_MAX_RETRIES):
        try:
            response = requests.post(
                url,
                params=params,
                headers={"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"},
                json={"text": text, **({"attachments": attachments} if attachments else {})},
                timeout=MAX_TIMEOUT_SECONDS,
            )

            if response.status_code in (429, 503):
                last_error = f"retryable status={response.status_code}"
                _sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                logger.error("MAX API error %s: %s", response.status_code, response.text)
                raise HTTPException(status_code=502, detail="Ошибка отправки сообщения через MAX API")

            return response.json() if response.content else {"ok": True}
        except requests.RequestException as exc:
            last_error = str(exc)
            _sleep_backoff(attempt)

    raise HTTPException(status_code=502, detail=f"MAX API недоступен после ретраев: {last_error}")


def log_to_sheets(user_id: int, event: str) -> None:
    if not GOOGLE_SHEETS_ENABLED:
        print("Sheets отключен")
        return
    if not GOOGLE_SCRIPT_URL or GOOGLE_SCRIPT_URL == "ВСТАВЬ_СЮДА_URL":
        print("Ошибка: GOOGLE_SCRIPT_URL не задан")
        return
    uid = str(user_id).strip()
    if not uid:
        print("Ошибка: пустой user_id")
        return

    now_moscow = datetime.now(MOSCOW_TZ)
    payload = {
        "date": now_moscow.strftime("%d.%m.%Y"),
        "time": now_moscow.strftime("%H:%M:%S"),
        "user_id": int(uid),
        "event": event,
    }
    print("📤 Отправка в Google Sheets:", payload)
    try:
        response = requests.post(
            GOOGLE_SCRIPT_URL,
            json=payload,
            timeout=5,
        )
        print("📥 Ответ Google Script:", response.status_code, response.text)
        if response.status_code != 200:
            print("❌ Google Script вернул не 200")
    except Exception as e:
        print("❌ Ошибка отправки в Google Sheets:", e)


def log_coupon_event_to_google_sheet(user_id: Optional[str], event_name: str = "Скидка за подписку") -> None:
    uid = str(user_id or "").strip()
    if not uid:
        return
    print(f"LOG EVENT: user_id={uid}, event={event_name}")
    try:
        log_to_sheets(int(uid), event_name)
    except Exception as exc:
        print(f"Ошибка записи в Google Sheets: {exc}")


def get_google_sheets_config_issues() -> list[str]:
    issues: list[str] = []
    if not GOOGLE_SHEETS_ENABLED:
        return issues

    if not GOOGLE_SCRIPT_URL or GOOGLE_SCRIPT_URL == "ВСТАВЬ_СЮДА_URL":
        issues.append("GOOGLE_SCRIPT_URL is empty")
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        try:
            parse_google_service_account(GOOGLE_SERVICE_ACCOUNT_JSON)
        except Exception as exc:
            issues.append(f"GOOGLE_SERVICE_ACCOUNT_JSON parse error: {exc}")
    return issues


def get_coupon_participation_date(user_id: str) -> Optional[str]:
    """
    Возвращает дату первого участия пользователя в акции из Google Sheets в формате DD.MM.YYYY.
    Ищет строку по колонке `user_id` (или `User ID`) и берёт дату из колонки `Дата` (или `date`).
    """
    # Сценарий чтения из таблицы отключён: запись в Google Sheets выполняется через Apps Script webhook.
    return None


def get_dashboard_url() -> Optional[str]:
    return f"{get_public_base_url()}/max_sub/statistic"


def parse_sheet_date(raw_date: str) -> Optional[date]:
    value = (raw_date or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def parse_sheet_datetime(raw_date: str, raw_time: str) -> Optional[datetime]:
    date_value = (raw_date or "").strip()
    for dt_fmt in ("%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S"):
        try:
            return datetime.strptime(date_value, dt_fmt)
        except ValueError:
            continue

    row_date = parse_sheet_date(date_value)
    if row_date is None:
        return None

    time_value = (raw_time or "").strip() or "00:00:00"
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            row_time = datetime.strptime(time_value, fmt).time()
            return datetime.combine(row_date, row_time)
        except ValueError:
            continue
    return datetime.combine(row_date, datetime.min.time())


def aggregate_dates(dates: list[date | datetime], granularity: str) -> dict[str, int]:
    buckets: dict[str, int] = {}
    for dt in dates:
        dt_value = dt if isinstance(dt, datetime) else datetime.combine(dt, datetime.min.time())
        if granularity == "hour":
            key = dt_value.strftime("%Y-%m-%d %H:00")
        elif granularity == "week":
            iso_year, iso_week, _ = dt_value.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"
        elif granularity == "month":
            key = dt_value.strftime("%Y-%m")
        elif granularity == "day":
            key = dt_value.strftime("%Y-%m-%d")
        else:
            key = dt_value.strftime("%Y-%m-%d")
        buckets[key] = buckets.get(key, 0) + 1
    return dict(sorted(buckets.items(), key=lambda x: x[0]))


def get_coupon_events_dates(start_date: date, end_date: date) -> list[date]:
    if not GOOGLE_SCRIPT_URL or GOOGLE_SCRIPT_URL == "ВСТАВЬ_СЮДА_URL":
        return []

    try:
        resp = requests.get(GOOGLE_SCRIPT_URL, timeout=5)
        data = resp.json()

        if not data.get("ok"):
            return []

        result = []
        for d in data.get("dates", []):
            dt = datetime.strptime(d, "%Y-%m-%d").date()
            if start_date <= dt <= end_date:
                result.append(dt)

        return result

    except Exception as e:
        print("Ошибка чтения из Google Sheets:", e)
        return []


def is_dashboard_user_allowed(user_id: Optional[str]) -> bool:
    uid = str(user_id or "").strip()
    return uid in DASHBOARD_ALLOWED_USER_IDS


def send_coupon(user_id: Optional[str], chat_id: Optional[str]) -> None:
    barcode_value, expiry_date = get_coupon_barcode_and_expiry()
    coupon_text = build_coupon_text(expiry_date)

    try:
        with TemporaryDirectory(prefix="coupon_ean13_") as tmp_dir:
            image_path = generate_ean13_png_file(barcode_value, Path(tmp_dir))
            token = upload_image_and_get_token(image_path)
            send_max_message(
                text=f"\n{coupon_text}",
                user_id=user_id,
                chat_id=chat_id,
                attachments=[{"type": "image", "payload": {"token": token}}],
            )
            log_coupon_event_to_google_sheet(user_id, "Скидка за подписку")
    except Exception as exc:
        logger.exception("Не удалось отправить изображение купона, отправляем fallback без цифрового кода: %s", exc)
        send_max_message(
            text=(
                f"{coupon_text}\n\n"
                "⚠️ Сейчас не удалось прикрепить изображение штрихкода. "
                "Попробуйте запросить купон ещё раз через минуту."
            ),
            user_id=user_id,
            chat_id=chat_id,
        )
        log_coupon_event_to_google_sheet(user_id, "Скидка за подписку")


def _send_coupon_after_subscribe_click(user_id: str) -> None:
    try:
        logger.info("Subscribe click watcher started for user_id=%s", user_id)
        time.sleep(6.0)
        send_coupon(user_id=user_id, chat_id=None)
        logger.info("Subscribe click watcher: coupon sent for user_id=%s after 6 seconds", user_id)
    except Exception as exc:
        logger.exception("Subscribe click watcher failed for user_id=%s: %s", user_id, exc)


def start_subscription_watch(user_id: str) -> bool:
    worker = threading.Thread(target=_send_coupon_after_subscribe_click, args=(user_id,), daemon=True)
    worker.start()
    return True


def get_miniapp_url() -> Optional[str]:
    base_url = get_public_base_url()
    if base_url:
        return f"{base_url}/miniapp"
    webhook_url = get_effective_webhook_url()
    if webhook_url:
        return webhook_url.removesuffix("/webhook") + "/miniapp"
    return None


def get_public_base_url() -> Optional[str]:
    return BASE_URL


def build_miniapp_button_attachments() -> list[dict[str, Any]]:
    miniapp_url = get_miniapp_url()
    web_app_value = (MAX_WEB_APP or "").strip()

    # Для open_app MAX API ожидает поле web_app (snake_case) и значение
    # с username миниприложения (бота), например: "my_bot".
    # Если web_app не задан, отправляем link-кнопку как безопасный фолбэк.
    if web_app_value:
        button: dict[str, Any] = {
            "type": "open_app",
            "text": "Получить купон",
            "web_app": web_app_value,
        }
    elif miniapp_url:
        logger.warning(
            "MAX_WEB_APP не задан: отправляем link-кнопку вместо open_app. "
            "Чтобы miniapp открывался внутри MAX с контекстом пользователя, укажите MAX_WEB_APP=<bot_username>."
        )
        button = {
            "type": "link",
            "text": "Получить купон",
            "url": miniapp_url,
        }
    else:
        return []

    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        button
                    ]
                ]
            },
        }
    ]


def build_dashboard_button_attachments(user_id: Optional[str]) -> list[dict[str, Any]]:
    dashboard_url = get_dashboard_url()
    if not dashboard_url:
        return []
    uid = str(user_id or "").strip()
    if uid:
        separator = "&" if "?" in dashboard_url else "?"
        dashboard_url = f"{dashboard_url}{separator}user_id={uid}"
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [[{"type": "link", "text": "Открыть дашборд", "url": dashboard_url}]]
            },
        }
    ]


def build_qr_subscribe_button_attachments() -> list[dict[str, Any]]:
    """
    Кнопка для сценария qr_podpiska.
    Нажатие генерирует callback, после чего бот:
    1) отправляет ссылку на канал;
    2) запускает watcher на авто-отправку купона через ~6 секунд.
    """
    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        {
                            "type": "callback",
                            "text": "Подписаться на канал и получить доп. скидку -5%",
                            "payload": QR_SUBSCRIBE_CALLBACK_DATA,
                        }
                    ]
                ]
            },
        }
    ]


def send_dashboard_entry(user_id: Optional[str], chat_id: Optional[str]) -> None:
    send_max_message(
        text="Откройте дашборд статистики купонов по кнопке ниже.",
        user_id=user_id,
        chat_id=chat_id,
        attachments=build_dashboard_button_attachments(user_id=user_id),
    )


def send_qr_subscription_entry(user_id: Optional[str], chat_id: Optional[str]) -> None:
    send_max_message(
        text="Нажмите кнопку ниже, чтобы подписаться на канал и получить купон.",
        user_id=user_id,
        chat_id=chat_id,
        attachments=build_qr_subscribe_button_attachments(),
    )


def send_miniapp_entry(user_id: Optional[str], chat_id: Optional[str]) -> None:
    try:
        send_max_message(
            text="Откройте миниприложение и нажмите «Получить купон».",
            user_id=user_id,
            chat_id=chat_id,
            attachments=build_miniapp_button_attachments(),
        )
    except HTTPException:
        # Фолбэк для нестандартных клиентов/конфигов:
        # если open_app не принялся, отправляем link-кнопку на URL miniapp.
        miniapp_url = get_miniapp_url()
        fallback_attachments: list[dict[str, Any]] = []
        if miniapp_url:
            fallback_attachments = [
                {
                    "type": "inline_keyboard",
                    "payload": {
                        "buttons": [
                            [
                                {
                                    "type": "link",
                                    "text": "Получить купон",
                                    "url": miniapp_url,
                                }
                            ]
                        ]
                    },
                }
            ]
        send_max_message(
            text="Откройте миниприложение и нажмите «Получить купон».",
            user_id=user_id,
            chat_id=chat_id,
            attachments=fallback_attachments,
        )


def get_user_subscription_state(user_id: str) -> str:
    """
    Возвращает одно из значений:
    - subscribed
    - not_subscribed
    - unknown (если MAX API не дал однозначного ответа, например массовые 400/5xx)
    """
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    candidates: list[str] = []
    for channel_target in get_channel_api_targets():
        candidates.extend(
            [
                f"{MAX_API_BASE_URL}/chats/{channel_target}/members/{user_id}",
                f"{MAX_API_BASE_URL}/chats/{channel_target}/members",
                f"{MAX_API_BASE_URL}/chats/{channel_target}/subscribers/{user_id}",
                f"{MAX_API_BASE_URL}/chats/{channel_target}/subscribers",
                f"{MAX_API_BASE_URL}/channels/{channel_target}/members/{user_id}",
                f"{MAX_API_BASE_URL}/channels/{channel_target}/members",
                f"{MAX_API_BASE_URL}/channels/{channel_target}/subscribers/{user_id}",
                f"{MAX_API_BASE_URL}/channels/{channel_target}/subscribers",
            ]
        )
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    had_api_errors = False
    had_success_response = False

    for url in candidates:
        try:
            params = {"user_id": user_id} if url.endswith("/members") or url.endswith("/subscribers") else None
            resp = requests.get(url, params=params, headers=headers, timeout=MAX_TIMEOUT_SECONDS)
            if resp.status_code == 404:
                continue
            if resp.status_code >= 400:
                logger.info("Subscription check endpoint %s returned status=%s", url, resp.status_code)
                had_api_errors = True
                continue
            had_success_response = True

            # Для endpoint вида /members/{user_id} или /subscribers/{user_id} успешный 200 обычно уже означает,
            # что пользователь найден среди участников.
            if not (url.endswith("/members") or url.endswith("/subscribers")):
                return "subscribed"

            payload = resp.json() if resp.content else {}
            if payload and is_subscription_confirmed(payload, user_id):
                return "subscribed"
        except Exception:
            had_api_errors = True
            continue
    if had_success_response:
        return "not_subscribed"
    if had_api_errors:
        return "unknown"
    return "not_subscribed"


def is_user_subscribed_to_channel(user_id: str) -> bool:
    return get_user_subscription_state(user_id) == "subscribed"


def contains_user_id(value: Any, user_id: str) -> bool:
    target = str(user_id)

    if isinstance(value, dict):
        direct_candidate = value.get("user_id")
        if direct_candidate is not None and str(direct_candidate) == target:
            return True
        direct_id = value.get("id")
        if direct_id is not None and str(direct_id) == target:
            return True

        user_obj = value.get("user")
        if user_obj is not None and contains_user_id(user_obj, user_id):
            return True

        for nested in value.values():
            if contains_user_id(nested, user_id):
                return True
        return False

    if isinstance(value, list):
        for item in value:
            if contains_user_id(item, user_id):
                return True
        return False

    return False


def is_subscription_confirmed(payload: Any, user_id: str) -> bool:
    if contains_user_id(payload, user_id):
        return True

    if isinstance(payload, dict):
        for flag_key in ("subscribed", "is_subscribed", "is_member", "member", "joined", "in_chat"):
            flag = payload.get(flag_key)
            if isinstance(flag, bool) and flag:
                return True
            if isinstance(flag, str) and flag.lower() in {"true", "yes", "member", "joined", "subscribed"}:
                return True
        for status_key in ("status", "membership", "role"):
            status_val = payload.get(status_key)
            if isinstance(status_val, str) and status_val.lower() in {"member", "subscriber", "joined", "admin", "owner"}:
                return True
    return False


def get_channel_title() -> str:
    if not MAX_BOT_TOKEN:
        return f"chat_id {MAX_CHANNEL_CHAT_ID}"

    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    candidates: list[str] = []
    for channel_id in get_channel_api_targets():
        candidates.extend(
            [
                f"{MAX_API_BASE_URL}/chats/{channel_id}",
                f"{MAX_API_BASE_URL}/channels/{channel_id}",
            ]
        )
    for url in candidates:
        try:
            resp = requests.get(url, headers=headers, timeout=MAX_TIMEOUT_SECONDS)
            if resp.status_code >= 400:
                continue
            payload = resp.json() if resp.content else {}
            if isinstance(payload, dict):
                title = payload.get("title") or payload.get("name") or payload.get("chat_title")
                if isinstance(title, str) and title.strip():
                    return title.strip()
        except Exception:
            continue
    return f"chat_id {MAX_CHANNEL_CHAT_ID}"


def check_max_auth() -> dict[str, Any]:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    response = requests.get(
        f"{MAX_API_BASE_URL}/me",
        headers={"Authorization": MAX_BOT_TOKEN},
        timeout=MAX_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        logger.error("MAX /me auth check failed %s: %s", response.status_code, response.text)
        raise HTTPException(
            status_code=502,
            detail=f"Проверка MAX API (/me) не прошла: status={response.status_code}",
        )

    return response.json() if response.content else {"ok": True}



def register_webhook_subscription() -> dict[str, Any]:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")
    webhook_url = get_effective_webhook_url()
    if not webhook_url:
        raise RuntimeError(
            "Webhook URL не определён. Задайте MAX_WEBHOOK_URL."
        )

    def _register_with_types(update_types: list[str]) -> requests.Response:
        payload: dict[str, Any] = {"url": webhook_url, "update_types": update_types}
        if MAX_WEBHOOK_SECRET:
            payload["secret"] = MAX_WEBHOOK_SECRET
        return requests.post(
            f"{MAX_API_BASE_URL}/subscriptions",
            headers={"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"},
            json=payload,
            timeout=MAX_TIMEOUT_SECONDS,
        )

    effective_update_types = get_effective_update_types()
    response = _register_with_types(effective_update_types)
    if response.status_code < 400:
        ACTIVE_WEBHOOK_UPDATE_TYPES.clear()
        ACTIVE_WEBHOOK_UPDATE_TYPES.extend(effective_update_types)
        return response.json() if response.content else {"ok": True}

    logger.error("MAX /subscriptions register failed %s: %s", response.status_code, response.text)
    raise HTTPException(
        status_code=502,
        detail=f"Не удалось зарегистрировать webhook в MAX: status={response.status_code}",
    )


def get_effective_webhook_url() -> Optional[str]:
    """
    Возвращает webhook URL в приоритете:
    1) MAX_WEBHOOK_URL
    2) BASE_URL -> <base>/webhook
    """
    if MAX_WEBHOOK_URL:
        return MAX_WEBHOOK_URL
    return f"{BASE_URL}/webhook"


def get_effective_update_types() -> list[str]:
    return BASE_WEBHOOK_UPDATE_TYPES


def auto_register_webhook_on_startup() -> None:
    effective_webhook_url = get_effective_webhook_url()
    webhook_url_source = "MAX_WEBHOOK_URL" if MAX_WEBHOOK_URL else "BASE_URL"
    logger.info(
        "Startup config: token_set=%s webhook_url_source=%s configured_webhook_url=%s effective_webhook_url=%s auto_register=%s update_types=%s secret_set=%s self_check=%s",
        bool(MAX_BOT_TOKEN),
        webhook_url_source,
        MAX_WEBHOOK_URL or "<empty>",
        effective_webhook_url or "<empty>",
        MAX_WEBHOOK_AUTO_REGISTER,
        ",".join(get_effective_update_types()) or "<empty>",
        bool(MAX_WEBHOOK_SECRET),
        MAX_STARTUP_SELF_CHECK,
    )
    if not effective_webhook_url:
        logger.warning(
            "Webhook URL не задан. Укажите MAX_WEBHOOK_URL."
        )

    if MAX_STARTUP_SELF_CHECK:
        try:
            me = check_max_auth()
            logger.info("Startup MAX /me check OK: %s", json.dumps(me, ensure_ascii=False))
        except Exception as exc:
            logger.exception("Startup MAX /me check failed: %s", exc)

    if not MAX_WEBHOOK_AUTO_REGISTER:
        logger.info("Webhook auto-registration skipped: MAX_WEBHOOK_AUTO_REGISTER=false")
        return

    try:
        result = register_webhook_subscription()
        logger.info("Webhook registration success on startup: %s", result)
    except Exception as exc:
        logger.exception("Webhook auto-registration failed on startup: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    auto_register_webhook_on_startup()
    threading.Thread(target=monthly_reminder_worker, daemon=True).start()
    send_monthly_reminder_if_needed()
    yield


app.router.lifespan_context = lifespan


@app.post("/setup/subscription")
def setup_subscription() -> JSONResponse:
    result = register_webhook_subscription()
    return JSONResponse({"ok": True, "subscription": result})


@app.get("/subscribe")
def subscribe_get() -> JSONResponse:
    """Удобный endpoint для ручной проверки из браузера/Railway (GET)."""
    result = register_webhook_subscription()
    return JSONResponse({"ok": True, "subscription": result, "hint": "Webhook subscription registered via GET /subscribe"})


@app.post("/subscribe")
def subscribe_post() -> JSONResponse:
    """Алиас на setup endpoint: регистрация webhook через POST /subscribe."""
    result = register_webhook_subscription()
    return JSONResponse({"ok": True, "subscription": result, "hint": "Webhook subscription registered via POST /subscribe"})

def process_update(payload: dict[str, Any]) -> None:
    update_type = str(payload.get("update_type") or "")
    if update_type and update_type not in {"message_created", "bot_started", "message_callback"}:
        logger.info("Skip unsupported update_type=%s", update_type)
        return

    dedup_key = extract_dedup_key(payload)
    if dedup_key and _is_duplicate_and_mark(dedup_key):
        logger.info("Skip duplicate update: %s", dedup_key)
        return

    user_id = extract_user_id(payload)
    chat_id = extract_chat_id(payload)
    message_text = normalize_incoming_text(extract_message_text(payload) or "")
    callback_data = normalize_incoming_text(extract_callback_data(payload) or "")

    # Отслеживаем источник запуска бота через UTM, чтобы применить специальный QR-сценарий.
    if has_qr_utm_source(payload):
        mark_user_came_from_qr(user_id)

    try:
        if callback_data == QR_SUBSCRIBE_CALLBACK_DATA:
            if not user_id:
                logger.warning("QR callback without user_id")
                return
            started = start_subscription_watch(str(user_id))
            if started:
                send_max_message(
                    text=(
                        f"Подпишитесь на канал: {MAX_CHANNEL_DEEPLINK}\n"
                        "После перехода купон будет отправлен автоматически примерно через 6 секунд."
                    ),
                    user_id=user_id,
                    chat_id=chat_id,
                )
            else:
                send_max_message(
                    text="Проверка уже запущена. Купон придёт автоматически через несколько секунд.",
                    user_id=user_id,
                    chat_id=chat_id,
                )
            return

        if is_dashboard_user_allowed(user_id) and message_text in DASHBOARD_COMMANDS:
            send_dashboard_entry(user_id=user_id, chat_id=chat_id)
            return
        if (is_start_command(message_text) or update_type == "bot_started") and came_from_qr(user_id):
            send_qr_subscription_entry(user_id=user_id, chat_id=chat_id)
            return
        if message_text in {"test", "тест", "/test", "/hello", "/start", "+"}:
            send_miniapp_entry(user_id=user_id, chat_id=chat_id)
            return
        if message_text in {"купон", "/купон", "coupon", "/coupon"}:
            send_max_message(
                text="Откройте миниприложение через кнопку «Открыть» в боте, чтобы получить купон.",
                user_id=user_id,
                chat_id=chat_id,
            )
            return
        if message_text in {"id", "айди", "/id"}:
            send_max_message(
                text="Функция отправки user_id отключена.",
                user_id=user_id,
                chat_id=chat_id,
            )
            return

        if not user_id:
            logger.warning("Не удалось извлечь user_id из события")
            return
    except Exception as exc:
        logger.exception("Ошибка обработки события: %s", exc)


def render_miniapp_html() -> str:
    return f"""
<!doctype html>
<html lang="ru">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Купон MAX ID Bot</title>
    <script src="https://st.max.ru/js/max-web-app.js"></script>
    <style>
      body {{
        margin: 0;
        font-family: Inter, system-ui, sans-serif;
        background: linear-gradient(180deg, #f4f8ff 0%, #eef7ff 100%);
        color: #1f2937;
      }}
      .wrap {{
        max-width: 520px;
        margin: 0 auto;
        padding: 20px 14px 28px;
      }}
      .card {{
        background: #fff;
        border-radius: 18px;
        box-shadow: 0 8px 24px rgba(29, 78, 216, 0.1);
        padding: 18px;
      }}
      h2 {{
        margin: 0 0 10px;
        font-size: 22px;
      }}
      p {{
        margin: 0 0 12px;
        line-height: 1.45;
      }}
      input {{
        width: 100%;
        box-sizing: border-box;
        border: 1px solid #dbe4ff;
        border-radius: 12px;
        padding: 12px;
        font-size: 16px;
      }}
      .row {{
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-top: 12px;
      }}
      button, .btn-link {{
        border: 0;
        border-radius: 12px;
        padding: 11px 14px;
        font-size: 15px;
        cursor: pointer;
        text-decoration: none;
        display: inline-flex;
        align-items: center;
        justify-content: center;
      }}
      .btn-primary {{
        background: #2563eb;
        color: #fff;
      }}
      .btn-secondary {{
        background: #eef2ff;
        color: #1e40af;
      }}
      .btn-disabled {{
        background: #e5e7eb;
        color: #9ca3af;
        cursor: not-allowed;
        pointer-events: none;
      }}
      .participation-note {{
        margin-top: 10px;
        font-size: 14px;
        color: #b45309;
      }}
      .status {{
        margin-top: 12px;
        padding: 10px;
        border-radius: 10px;
        background: #f9fafb;
        font-size: 14px;
      }}
      .uid {{
        font-size: 14px;
        color: #475569;
        margin-bottom: 8px;
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card">
        <h2>🎁 Купон на скидку</h2>
        <p>Проверьте подписку и получите купон.</p>
        <div id="uidLabel" class="uid">user_id: определяем...</div>
        <div class="row">
          <a id="subscribeBtn" href="{MAX_CHANNEL_DEEPLINK}" data-web-url="{MAX_CHANNEL_URL}" class="btn-link btn-primary">Подпишись на канал и получи доп.скидку -5%</a>
        </div>
        <div id="participationNote" class="participation-note"></div>
        <div id="status" class="status">Статус: нажмите «Подписаться на канал».</div>
      </div>
    </div>

    <script>
      const subscribeBtn = document.getElementById('subscribeBtn');
      const statusEl = document.getElementById('status');
      const uidLabel = document.getElementById('uidLabel');
      const participationNoteEl = document.getElementById('participationNote');
      if (window.WebApp?.ready) {{
        window.WebApp.ready();
      }}
      const getFromInitData = () => {{
        try {{
          const initData = new URLSearchParams(window.location.search).get('initData');
          if (!initData) return '';
          const params = new URLSearchParams(initData);
          const userRaw = params.get('user');
          if (!userRaw) return '';
          const userObj = JSON.parse(userRaw);
          return (userObj.user_id || userObj.id || '').toString();
        }} catch (_e) {{
          return '';
        }}
      }};
      const getDetectedUserId = () => {{
        return (
          window.WebApp?.initDataUnsafe?.user?.user_id ||
          window.WebApp?.initDataUnsafe?.user?.id ||
          new URLSearchParams(window.location.search).get('user_id') ||
          getFromInitData() ||
          ''
        ).toString();
      }};
      const detectedUserId = getDetectedUserId();
      let alreadyParticipated = false;
      uidLabel.textContent = detectedUserId
        ? `user_id: ${{detectedUserId}}`
        : 'user_id: не определён (откройте miniapp кнопкой из чата с ботом)';

      const setSubscribeDisabled = (value) => {{
        alreadyParticipated = Boolean(value);
        if (alreadyParticipated) {{
          subscribeBtn.classList.remove('btn-primary');
          subscribeBtn.classList.add('btn-disabled');
          subscribeBtn.setAttribute('aria-disabled', 'true');
          subscribeBtn.setAttribute('tabindex', '-1');
        }} else {{
          subscribeBtn.classList.remove('btn-disabled');
          subscribeBtn.classList.add('btn-primary');
          subscribeBtn.removeAttribute('aria-disabled');
          subscribeBtn.removeAttribute('tabindex');
        }}
      }};

      const loadParticipationState = async () => {{
        if (!detectedUserId) return;
        try {{
          const res = await fetch(`/miniapp/participation?user_id=${{encodeURIComponent(detectedUserId)}}`);
          const data = await res.json();
          if (!res.ok || !data.ok) return;
          if (data.already_participated) {{
            setSubscribeDisabled(true);
            const participationDate = data.participation_date || 'неизвестная дата';
            participationNoteEl.textContent =
              `Участие в акции «Скидка за подписку» возможно только один раз. Вы уже принимали участие ${{participationDate}}.`;
            statusEl.textContent = 'Повторное участие в акции недоступно.';
          }}
        }} catch (_e) {{
          // Ничего не делаем: оставляем интерфейс рабочим даже при временных сбоях сети.
        }}
      }};
      loadParticipationState();

      subscribeBtn.onclick = async (e) => {{
        e.preventDefault();
        if (alreadyParticipated) {{
          return;
        }}
        if (!detectedUserId) {{
          statusEl.textContent = 'Не удалось определить user_id. Откройте миниприложение из чата MAX.';
          return;
        }}
        const deepLink = subscribeBtn.getAttribute('href') || '{MAX_CHANNEL_DEEPLINK}';
        const webUrl = subscribeBtn.getAttribute('data-web-url') || '{MAX_CHANNEL_URL}';
        statusEl.textContent = 'Открываем канал. Купон придёт в личный чат примерно через 6 секунд...';
        try {{
          const res = await fetch('/miniapp/start-subscribe-watch', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ user_id: detectedUserId }})
          }});
          const data = await res.json();
          if (res.ok && data.ok) {{
            statusEl.textContent = 'Купон будет отправлен автоматически примерно через 6 секунд ✅';
          }} else {{
            statusEl.textContent = 'Не удалось запустить авто-проверку подписки.';
          }}
        }} catch (_e) {{
          statusEl.textContent = 'Не удалось запустить авто-проверку подписки.';
        }}
        try {{
          // 1) Пробуем нативный метод MAX WebApp (если доступен).
          if (window.WebApp?.openLink) {{
              window.WebApp.openLink(deepLink);
            return;
          }}
        }} catch (_e) {{
          // Переходим к следующей попытке
        }}

        try {{
          // 2) Фолбэк: прямой переход по deep-link.
          window.location.assign(deepLink);
          return;
        }} catch (_e) {{
          // Финальный fallback ниже
        }}

        // 3) Если deep-link не сработал (редкий случай), открываем web-ссылку.
        window.open(webUrl, '_blank', 'noopener,noreferrer');
      }};

    </script>
  </body>
</html>
"""


def render_dashboard_html() -> str:
    return """
<!doctype html>
<html lang="ru">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Дашборд купонов</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
      body { font-family: Inter, system-ui, sans-serif; margin: 0; background: #f8fafc; color: #0f172a; }
      .wrap { max-width: 980px; margin: 0 auto; padding: 18px; }
      .card { background: #fff; border-radius: 14px; padding: 16px; box-shadow: 0 6px 20px rgba(2, 6, 23, 0.08); }
      .controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-bottom: 12px; }
      label { display: block; font-size: 13px; color: #334155; margin-bottom: 4px; }
      select, input, button { width: 100%; padding: 10px; border-radius: 10px; border: 1px solid #cbd5e1; font-size: 14px; box-sizing: border-box; }
      button { background: #2563eb; color: white; border: none; cursor: pointer; }
      .meta { margin-top: 8px; font-size: 13px; color: #475569; }
      .err { color: #b91c1c; margin-top: 8px; font-size: 14px; }
      .hidden { display: none; }
      #fallbackChart { margin-top: 12px; }
      .fallback-row { display: grid; grid-template-columns: 1fr auto; gap: 10px; padding: 6px 0; border-bottom: 1px solid #e2e8f0; font-size: 14px; }
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card">
        <h2>📊 Статистика отправленных купонов</h2>
        <div class="controls">
          <div>
            <label for="period">Период</label>
            <select id="period">
              <option value="yesterday">Вчера</option>
              <option value="today" selected>Сегодня</option>
              <option value="week">Неделя</option>
              <option value="month">Месяц</option>
              <option value="quarter">Квартал</option>
              <option value="custom">Ручной выбор периода</option>
            </select>
          </div>
          <div>
            <label for="granularity">Детализация</label>
            <select id="granularity">
              <option value="day" selected>По дням</option>
              <option value="hour">По часам</option>
              <option value="week">По неделям</option>
              <option value="month">По месяцам</option>
            </select>
          </div>
          <div id="fromWrap" class="hidden">
            <label for="dateFrom">Дата с</label>
            <input id="dateFrom" type="date" />
          </div>
          <div id="toWrap" class="hidden">
            <label for="dateTo">Дата по</label>
            <input id="dateTo" type="date" />
          </div>
          <div>
            <label>&nbsp;</label>
            <button id="applyBtn">Показать</button>
          </div>
        </div>
        <canvas id="statsChart" height="120"></canvas>
        <div id="fallbackChart" class="hidden"></div>
        <div id="meta" class="meta"></div>
        <div id="error" class="err"></div>
      </div>
    </div>
    <script>
      const periodEl = document.getElementById('period');
      const granularityEl = document.getElementById('granularity');
      const fromWrap = document.getElementById('fromWrap');
      const toWrap = document.getElementById('toWrap');
      const dateFromEl = document.getElementById('dateFrom');
      const dateToEl = document.getElementById('dateTo');
      const applyBtn = document.getElementById('applyBtn');
      const metaEl = document.getElementById('meta');
      const errorEl = document.getElementById('error');
      const ctx = document.getElementById('statsChart');
      const fallbackChartEl = document.getElementById('fallbackChart');
      const dashboardUserId = new URLSearchParams(window.location.search).get('user_id') || '';
      let chart;

      const updateCustomVisibility = () => {
        const isCustom = periodEl.value === 'custom';
        fromWrap.classList.toggle('hidden', !isCustom);
        toWrap.classList.toggle('hidden', !isCustom);
      };

      const renderChart = (labels, values) => {
        const safeLabels = labels.length ? labels : ['Нет данных'];
        const safeValues = values.length ? values : [0];

        if (typeof Chart === 'undefined') {
          ctx.classList.add('hidden');
          fallbackChartEl.classList.remove('hidden');
          fallbackChartEl.innerHTML = safeLabels.map((label, idx) =>
            `<div class="fallback-row"><span>${label}</span><strong>${safeValues[idx] ?? 0}</strong></div>`
          ).join('');
          return;
        }

        ctx.classList.remove('hidden');
        fallbackChartEl.classList.add('hidden');
        if (chart) chart.destroy();
        chart = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: safeLabels,
            datasets: [{ label: 'Отправленные купоны', data: safeValues, backgroundColor: '#2563eb' }]
          },
          options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
          }
        });
      };

      const loadStats = async () => {
        errorEl.textContent = '';
        if (!dashboardUserId) {
          errorEl.textContent = 'Отсутствует user_id для доступа к дашборду.';
          return;
        }
        const params = new URLSearchParams({
          user_id: dashboardUserId,
          period: periodEl.value,
          granularity: granularityEl.value
        });
        if (periodEl.value === 'custom') {
          if (!dateFromEl.value || !dateToEl.value) {
            errorEl.textContent = 'Для ручного периода заполните обе даты.';
            return;
          }
          params.set('date_from', dateFromEl.value);
          params.set('date_to', dateToEl.value);
        }
        const res = await fetch(`/dashboard/data?${params.toString()}`);
        const data = await res.json();
        if (!res.ok || !data.ok) {
          errorEl.textContent = data.detail || 'Не удалось загрузить статистику.';
          return;
        }
        renderChart(data.labels, data.values);
        metaEl.textContent = `Период: ${data.period_start} — ${data.period_end}. Всего купонов: ${data.total}.`;
      };

      periodEl.addEventListener('change', updateCustomVisibility);
      applyBtn.addEventListener('click', (event) => {
        event.preventDefault();
        loadStats();
      });
      updateCustomVisibility();
      loadStats();
    </script>
  </body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return render_miniapp_html()


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/max_sub/statistic", response_class=HTMLResponse)
def dashboard_page(user_id: str) -> str:
    if not is_dashboard_user_allowed(user_id):
        raise HTTPException(status_code=403, detail="Доступ к дашборду запрещен")
    return render_dashboard_html()


def resolve_period_dates(period: str, date_from: Optional[str], date_to: Optional[str], now_utc: datetime) -> tuple[date, date]:
    today = now_utc.date()
    if period == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=6), today
    if period == "month":
        return today - timedelta(days=29), today
    if period == "quarter":
        return today - timedelta(days=89), today
    if period == "custom":
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="Для custom периода укажите date_from и date_to")
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
        if start > end:
            raise HTTPException(status_code=400, detail="date_from не может быть позже date_to")
        return start, end
    raise HTTPException(status_code=400, detail="Неверный период")


@app.get("/dashboard/data")
def dashboard_data(
    user_id: str,
    period: str = "today",
    granularity: str = "day",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> JSONResponse:
    if not is_dashboard_user_allowed(user_id):
        raise HTTPException(status_code=403, detail="Доступ к дашборду запрещен")
    if granularity not in {"hour", "day", "week", "month"}:
        raise HTTPException(status_code=400, detail="granularity должен быть hour, day, week или month")

    start_date, end_date = resolve_period_dates(period, date_from, date_to, datetime.now(MOSCOW_TZ))
    try:
        event_dates = get_coupon_events_dates(start_date=start_date, end_date=end_date)
        buckets = aggregate_dates(event_dates, granularity)
        logger.info("Dashboard loaded rows=%s buckets=%s granularity=%s", len(event_dates), len(buckets), granularity)
    except Exception as exc:
        logger.exception("Dashboard data error: %s", exc)
        raise HTTPException(status_code=500, detail="Не удалось загрузить данные дашборда") from exc

    labels = list(buckets.keys())
    values = [buckets[k] for k in labels]
    return JSONResponse(
        {
            "ok": True,
            "period": period,
            "granularity": granularity,
            "period_start": start_date.isoformat(),
            "period_end": end_date.isoformat(),
            "labels": labels,
            "values": values,
            "total": sum(values),
        }
    )


@app.get("/webhook")
def webhook_get_hint() -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"ok": True, "message": "Webhook endpoint is alive. Send POST requests from MAX to /webhook."},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/max")
def health_max() -> JSONResponse:
    me = check_max_auth()
    return JSONResponse({"status": "ok", "max_auth": True, "me": me})


@app.get("/health/config")
def health_config() -> JSONResponse:
    effective_webhook_url = get_effective_webhook_url()
    issues: list[str] = []
    if not MAX_BOT_TOKEN:
        issues.append("MAX_BOT_TOKEN is empty")
    if not effective_webhook_url:
        issues.append("webhook url is empty: set MAX_WEBHOOK_URL")
    issues.extend(get_google_sheets_config_issues())

    return JSONResponse(
        {
            "status": "ok",
            "config": {
                "max_api_base_url": MAX_API_BASE_URL,
                "token_set": bool(MAX_BOT_TOKEN),
                "webhook_url": MAX_WEBHOOK_URL,
                "public_base_url": get_public_base_url(),
                "effective_webhook_url": effective_webhook_url,
                "webhook_secret_set": bool(MAX_WEBHOOK_SECRET),
                "webhook_auto_register": MAX_WEBHOOK_AUTO_REGISTER,
                "webhook_update_types": get_effective_update_types(),
                "active_webhook_update_types": ACTIVE_WEBHOOK_UPDATE_TYPES,
                "startup_self_check": MAX_STARTUP_SELF_CHECK,
                "google_sheets_enabled": GOOGLE_SHEETS_ENABLED,
                "google_sheets_spreadsheet_id_set": bool(GOOGLE_SHEETS_SPREADSHEET_ID),
                "google_script_url_set": bool(GOOGLE_SCRIPT_URL and GOOGLE_SCRIPT_URL != "ВСТАВЬ_СЮДА_URL"),
                "issues": issues,
            },
        }
    )


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_max_bot_api_secret: Optional[str] = Header(default=None),
) -> JSONResponse:
    if MAX_WEBHOOK_SECRET:
        if not x_max_bot_api_secret or not hmac.compare_digest(x_max_bot_api_secret, MAX_WEBHOOK_SECRET):
            logger.warning("Webhook secret mismatch")
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        payload = await request.json()
    except Exception as exc:
        logger.exception("Некорректный JSON в webhook: %s", exc)
        raise HTTPException(status_code=400, detail="Некорректный JSON") from exc

    logger.info("Incoming MAX event: %s", json.dumps(payload, ensure_ascii=False))
    print("INCOMING:", payload)

    # Быстрый ответ по "рабочему варианту":
    # если webhook пришёл в формате message.from.id, сразу отправляем подтверждение.
    try:
        direct_user_id = str(payload.get("message", {}).get("from", {}).get("id") or "").strip()
        if direct_user_id and MAX_BOT_TOKEN:
            requests.post(
                f"{MAX_API_BASE_URL}/messages",
                headers={"Authorization": MAX_BOT_TOKEN},
                json={
                    "user_id": direct_user_id,
                    "text": "Я получил сообщение!",
                },
                timeout=MAX_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        print("ERROR:", exc)

    background_tasks.add_task(process_update, payload)
    return JSONResponse({"ok": True, "accepted": True})


@app.get("/miniapp", response_class=HTMLResponse)
def miniapp_page() -> str:
    return render_miniapp_html()


@app.get("/miniapp/status")
def miniapp_status(user_id: str) -> JSONResponse:
    subscription_state = get_user_subscription_state(user_id=user_id)
    subscribed = subscription_state == "subscribed"
    channel_title = get_channel_title()
    if subscription_state == "subscribed":
        message = f'Вы подписаны на канал "{channel_title}"'
    elif subscription_state == "unknown":
        message = f'Не удалось однозначно проверить подписку на канал "{channel_title}". Попробуйте ещё раз.'
    else:
        message = f'Подписка на канал "{channel_title}" не найдена'
    if not subscribed:
        logger.info("Subscription check is false for user_id=%s channel_chat_id=%s", user_id, MAX_CHANNEL_CHAT_ID)
    return JSONResponse(
        {
            "ok": True,
            "user_id": user_id,
            "subscribed": subscribed,
            "subscription_state": subscription_state,
            "channel_chat_id": MAX_CHANNEL_CHAT_ID,
            "channel_title": channel_title,
            "message": message,
        }
    )


@app.get("/miniapp/participation")
def miniapp_participation(user_id: str) -> JSONResponse:
    participation_date = get_coupon_participation_date(user_id=user_id)
    already_participated = participation_date is not None and str(user_id) != "24324984"
    return JSONResponse(
        {
            "ok": True,
            "user_id": user_id,
            "already_participated": already_participated,
            "participation_date": participation_date,
        }
    )


@app.post("/miniapp/start-subscribe-watch")
async def miniapp_start_subscribe_watch(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = str(body.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id обязателен")
    started = start_subscription_watch(user_id)
    return JSONResponse({"ok": True, "started": started, "user_id": user_id})


@app.post("/miniapp/get-coupon")
async def miniapp_get_coupon(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = str(body.get("user_id") or "")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id обязателен")
    send_coupon(user_id=user_id, chat_id=None)
    return JSONResponse({"ok": True, "sent": True})


def run() -> None:
    """
    Запуск Uvicorn с логами в stdout.
    Это нужно для платформ, где stderr автоматически помечается как error.
    """
    log_level = os.getenv("LOG_LEVEL", "INFO").lower()
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": log_level.upper(), "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": log_level.upper(), "propagate": False},
            "uvicorn.access": {"handlers": ["default"], "level": log_level.upper(), "propagate": False},
        },
    }
    uvicorn.run(app, host=host, port=port, log_level=log_level, log_config=log_config)


if __name__ == "__main__":
    run()
