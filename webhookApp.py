from flask import Flask, request, jsonify
import json
import threading
from collections import OrderedDict
from rich.console import Console
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from sendToDataBase import API_BASE_URL
from sendToDataBase import get_data

from getUserName import get_user_name
from donwloadImage import download_image
from database import (
    init_db,
    save_error,
    count_robot_errors_in_shift,
    shift_stats,
)
from shift import get_current_shift
from pending_photos import handle_incoming_photo, forward_error
from error_parser import parse_error_message
from lark_send import send_text_message
from sendToDataBase import send_to_data_base
from logging_config import setup_logging


console = Console()
logger = setup_logging(__name__)

app = Flask(__name__)

init_db()

# ============================================================
# TIMEZONE
# ============================================================

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def now_warsaw() -> datetime:
    return datetime.now(WARSAW_TZ)


# ============================================================
# SETTINGS
# ============================================================

ERROR_THRESHOLD = 3
MESSAGE_MAX_AGE_SECONDS = 120

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

    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    if data.get("header", {}).get("event_type") != "im.message.receive_v1":
        return "", 200

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

        parsed = parse_error_message(text, chat_id)

        if not parsed:
            logger.warning(
                f"Failed to parse message text {message_id}: "
                f"'{text}' (chat_id={chat_id})"
            )

            send_text_message(
                chat_id,
                "Can't parse the text from message, please try again",
            )

            return "", 200

        robot_data_response = get_data(
            f"{API_BASE_URL}/robots/get_robots_by_number",
            params={
                "robot_number": int(parsed["robot"]),
                "warehouse": "SMALL-P3",
                "limit": 1,
            },
        )

        if not robot_data_response:
            logger.warning(
                f"Robot #{parsed['robot']} not found in API, "
                f"continuing with 'Unknown' warehouse "
                f"(chat_id={chat_id})"
            )
            warehouse = "Unknown"
        else:
            robot_data = robot_data_response[0]
            warehouse = robot_data.get("sub_warehouse") or "Unknown"

        shift_date, shift_name = get_current_shift()

        save_error(
            robot=parsed["robot"],
            error_type=parsed["error_type"],
            error_text=parsed["error_text"],
            raw_text=text,
            chat_id=chat_id,
            shift_date=shift_date,
            shift_name=shift_name,
        )

        count = count_robot_errors_in_shift(
            parsed["robot"], shift_date, shift_name
        )

        now = now_warsaw()
        pretty = now.strftime("%d.%m.%Y %H:%M:%S")

        logger.info(
            f"New error | robot={parsed['robot']} | "
            f"type={parsed['error_type']} | "
            f"employee={user_name} | "
            f"warehouse={warehouse} | "
            f"shift_count={count}"
        )

        table_lines = [
            ("👤 Employee", user_name),
            ("🤖 Robot", parsed["robot"]),
            ("⚠️ Time", pretty),
            ("📝 Details", parsed["error_text"]),
            ("🏭 Warehouse", warehouse),
            ("📊 Shift issues", str(count)),
        ]

        data_obj = {
            "employee": user_name,
            "robot": parsed["robot"],
            "error_text": parsed["error_text"],
        }

        forward_error(parsed, table_lines)
        send_to_data_base(parsed, data_obj, chat_id)

        if count >= ERROR_THRESHOLD:
            alert = (
                f"⚠️ Robot {parsed['robot']} "
                f"have {count} exceptions"
                f". Must be send to maintenance!"
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
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    logger.info("Webhook server started")
    logger.info(f"Timezone: {WARSAW_TZ}")
    logger.info(
        f"Current time: {now_warsaw().strftime('%d.%m.%Y %H:%M:%S')}"
    )

    port = int(os.environ.get("PORT", 7777))

    app.run(host="0.0.0.0", port=port)