import requests
import json

from getToken import get_tenant_access_token
from logging_config import setup_logging

logger = setup_logging(__name__)


def send_text_message(chat_id: str, text: str):
    """
    Отправляет текстовое сообщение в указанный чат от имени бота.
    """
    token = get_tenant_access_token()
    if not token:
        logger.error("Failed to get Lark access token")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }

    try:
        resp = requests.post(
            "https://open.larksuite.com/open-apis/im/v1/messages"
            "?receive_id_type=chat_id",
            headers=headers,
            json=payload,
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send Lark message to {chat_id}: {e}")
        return None

    result = resp.json()

    if result.get("code") != 0:
        logger.error(
            f"Lark send error: code={result.get('code')} "
            f"msg={result.get('msg')}"
        )
    else:
        logger.info(f"Message sent to chat {chat_id}")

    return result
