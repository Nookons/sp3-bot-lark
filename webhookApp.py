from flask import Flask, request, jsonify
import json
import threading
from collections import OrderedDict
from rich.console import Console
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from getUserName import get_user_name
from donwloadImage import download_image
from pending_photos import handle_incoming_photo, forward_error
from error_parser import parse_error_message
from lark_send import send_text_message
from sendToDataBase import (
    SUPABASE_SERVICE_KEY,
    send_to_data_base,
    count_robot_errors_in_shift,
    shift_stats,
)
from shift import get_current_shift
from shift_report import (
    build_shift_summary,
    send_shift_report,
    start_shift_scheduler,
)
from logging_config import setup_logging


console = Console()
logger = setup_logging(__name__)

app = Flask(__name__)

# ============================================================
# TIMEZONE
# ============================================================

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def now_warsaw() -> datetime:
    """
    Текущее время Europe/Warsaw (лето/зима учитывается
    автоматически).
    """
    return datetime.now(WARSAW_TZ)


# ============================================================
# SETTINGS
# ============================================================

ERROR_THRESHOLD = 3
MESSAGE_MAX_AGE_SECONDS = 120

PARSE_ERROR_HINT = (
    "Can't parse the message. Please use the format:\n"
    "<issue type>: <description>. <robot number>\n\n"
    "Example:\n"
    "Unable to drive: Security module failure. 3780"
)

_SEEN_LIMIT = 2000
_seen_lock = threading.Lock()
_seen_message_ids = OrderedDict()


# ============================================================
# MESSAGE DUPLICATE CHECK
# ============================================================

def _already_processed(message_id: str) -> bool:
    with _seen_lock:
        if message_id in _seen_message_ids:
            return True

        _seen_message_ids[message_id] = True

        if len(_seen_message_ids) > _SEEN_LIMIT:
            _seen_message_ids.popitem(last=False)

        return False


# ============================================================
# MESSAGE AGE CHECK
# ============================================================

def _is_message_too_old(create_time) -> bool:
    """
    create_time от Lark приходит как Unix timestamp
    в миллисекундах.

    Сравнение выполняется в часовом поясе Europe/Warsaw.
    """
    if not create_time:
        return False

    try:
        message_time = datetime.fromtimestamp(
            int(create_time) / 1000,
            tz=WARSAW_TZ,
        )
    except (ValueError, TypeError):
        return False

    current_time = now_warsaw()
    age_seconds = (current_time - message_time).total_seconds()

    return age_seconds > MESSAGE_MAX_AGE_SECONDS


# ============================================================
# WEBHOOK
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    if not data:
        return "", 200

    # --------------------------------------------------------
    # Lark URL verification
    # --------------------------------------------------------

    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    # --------------------------------------------------------
    # Event type
    # --------------------------------------------------------

    if data.get("header", {}).get("event_type") != "im.message.receive_v1":
        return "", 200

    # --------------------------------------------------------
    # Event data
    # --------------------------------------------------------

    event = data["event"]
    message = event["message"]
    sender = event["sender"]

    chat_id = message.get("chat_id")
    message_id = message["message_id"]

    # --------------------------------------------------------
    # Duplicate message protection
    # --------------------------------------------------------

    if _already_processed(message_id):
        logger.warning(f"Duplicate delivery {message_id}, skipping")
        return "", 200

    # --------------------------------------------------------
    # Message age
    # --------------------------------------------------------

    create_time = message.get("create_time")

    if _is_message_too_old(create_time):
        logger.warning(
            f"Message {message_id} is older than "
            f"{MESSAGE_MAX_AGE_SECONDS}s, skipping"
        )
        return "", 200

    # --------------------------------------------------------
    # User
    # --------------------------------------------------------

    user_id = sender["sender_id"].get("user_id")
    user_name = get_user_name(user_id, console)

    logger.info(
        f"Incoming message | user={user_name} (id={user_id}) | "
        f"chat_type={message['chat_type']} | "
        f"type={message['message_type']} | "
        f"message_id={message_id}"
    )

    # --------------------------------------------------------
    # Message content
    # --------------------------------------------------------

    content = json.loads(message["content"])

    # ========================================================
    # TEXT MESSAGE
    # ========================================================

    if message["message_type"] == "text":

        text = content.get("text", "")

        parsed = parse_error_message(text)

        if not parsed:
            logger.warning(
                f"Failed to parse message text {message_id}: "
                f"'{text}' (chat_id={chat_id})"
            )

            send_text_message(chat_id, PARSE_ERROR_HINT)

            return "", 200

        # ----------------------------------------------------
        # Current shift
        # ----------------------------------------------------

        shift_date, shift_name = get_current_shift()

        # ----------------------------------------------------
        # Save to Supabase (exceptions + exceptions_glpc)
        # ----------------------------------------------------

        data_obj = {
            "employee": user_name,
            "robot": parsed["robot"],
            "error_text": parsed["error_text"],
        }

        saved = send_to_data_base(parsed, data_obj, chat_id)

        if not saved:
            # Причина уже сообщена в чат внутри send_to_data_base.
            logger.warning(
                f"Exception not saved, skip forwarding: "
                f"robot={parsed['robot']}"
            )
            return "", 200

        # ----------------------------------------------------
        # Count robot errors for this shift (from Supabase)
        # ----------------------------------------------------

        count = count_robot_errors_in_shift(
            parsed["robot"], shift_date, shift_name
        )

        now = now_warsaw()
        pretty = now.strftime("%d.%m.%Y %H:%M:%S")

        logger.info(
            f"New error | robot={parsed['robot']} | "
            f"type={parsed['error_type']} | "
            f"employee={user_name} | "
            f"shift={shift_date}/{shift_name} | "
            f"shift_count={count}"
        )

        # ----------------------------------------------------
        # Forward ready message to another chat
        # ----------------------------------------------------

        table_lines = [
            ("👤 Employee", user_name),
            ("🤖 Robot", parsed["robot"]),
            ("⚠️ Time", pretty),
            ("📝 Details", parsed["error_text"]),
            ("📊 Shift issues", str(count)),
        ]

        forward_error(parsed, table_lines)

        # ----------------------------------------------------
        # Alert after threshold
        # ----------------------------------------------------

        if count >= ERROR_THRESHOLD:

            alert = (
                f"⚠️ Robot {parsed['robot']} "
                f"has {count} exceptions this shift. "
                f"It should be sent to maintenance!"
            )

            send_text_message(chat_id, alert)

            logger.warning(
                f"Robot {parsed['robot']} reached the error threshold "
                f"for this shift: {count} >= {ERROR_THRESHOLD}"
            )

    # ========================================================
    # IMAGE MESSAGE
    # ========================================================

    elif message["message_type"] == "image":

        image_key = content.get("image_key")

        if image_key:

            filename = download_image(image_key, message_id, console)

            if filename:
                logger.info(f"Image saved: {filename}")
                handle_incoming_photo(filename, console)
            else:
                logger.error(
                    f"Failed to download image "
                    f"(image_key={image_key}, message_id={message_id})"
                )

    return "", 200


# ============================================================
# SHIFT STATISTICS
# ============================================================

@app.route("/shift_stats", methods=["GET"])
def shift_stats_endpoint():

    shift_date = request.args.get("date")
    shift_name = request.args.get("shift")

    if not shift_date or not shift_name:
        shift_date, shift_name = get_current_shift()

    total, by_robot, by_type = shift_stats(shift_date, shift_name)

    return jsonify({
        "shift_date": shift_date,
        "shift_name": shift_name,
        "total_errors": total,
        "by_robot": by_robot,
        "by_error_type": by_type,
    })


# ============================================================
# SHIFT REPORT (то же сообщение, что уходит в конце смены)
# ============================================================

@app.route("/shift_report", methods=["GET"])
def shift_report_endpoint():
    """
    Предпросмотр отчёта за смену и (опционально) отправка его
    в целевую группу.

    /shift_report?date=2026-09-16&shift=night
    /shift_report?date=2026-09-16&shift=night&send=1
    """
    shift_date = request.args.get("date")
    shift_name = request.args.get("shift")

    if not shift_date or not shift_name:
        shift_date, shift_name = get_current_shift()

    text = build_shift_summary(shift_date, shift_name)

    do_send = str(request.args.get("send", "")).lower() in (
        "1", "true", "yes", "send",
    )

    if do_send:
        send_shift_report(shift_date, shift_name)

    return jsonify({
        "shift_date": shift_date,
        "shift_name": shift_name,
        "sent": do_send,
        "text": text,
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health", methods=["GET"])
def health_endpoint():
    return jsonify({
        "status": "ok",
        "time": now_warsaw().strftime("%d.%m.%Y %H:%M:%S"),
        "timezone": str(WARSAW_TZ),
        "supabase_configured": bool(SUPABASE_SERVICE_KEY),
    })


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    logger.info("Webhook server started")
    logger.info(f"Timezone: {WARSAW_TZ}")
    logger.info(
        f"Current time: {now_warsaw().strftime('%d.%m.%Y %H:%M:%S')}"
    )
    logger.info(
        f"Supabase service key configured: "
        f"{bool(SUPABASE_SERVICE_KEY)}"
    )

    # Отчёт за смену: в конце каждой смены шлёт метрики в группу.
    start_shift_scheduler()

    port = int(os.environ.get("PORT", 7777))

    app.run(host="0.0.0.0", port=port)
