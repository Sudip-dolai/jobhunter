import requests

from app import config


def send_telegram(chat_id: str | None, text: str) -> bool:
    if not (config.TELEGRAM_BOT_TOKEN and chat_id):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        return r.ok
    except requests.RequestException:
        return False
