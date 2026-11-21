import json
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

API_BASE = "https://api.telegram.org/bot{}/{}"
OFFSET_FILE = Path("telegram_offset.json")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def send_message(token, chat_id, text):
    try:
        response = requests.post(
            API_BASE.format(token, "sendMessage"),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        response.raise_for_status()
    except Exception as exc:
        print("Не вдалося надіслати відповідь у Telegram:", exc)


def load_offset():
    try:
        data = json.loads(OFFSET_FILE.read_text(encoding="utf-8"))
        return int(data.get("offset", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def save_offset(offset):
    OFFSET_FILE.write_text(
        json.dumps({"offset": offset}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_user = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()

    if not token or not allowed_user:
        print("Telegram polling пропущено: додайте TELEGRAM_BOT_TOKEN і TELEGRAM_ALLOWED_USER_ID у GitHub Secrets.")
        return

    try:
        allowed_user_id = int(allowed_user)
    except ValueError:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID має бути числовим Telegram user ID.")

    offset = load_offset()
    response = requests.get(
        API_BASE.format(token, "getUpdates"),
        params={"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message"])},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API повернув помилку: {payload}")

    updates = payload.get("result", [])
    print(f"Telegram: отримано повідомлень для перевірки: {len(updates)}")

    for update in updates:
        update_id = int(update["update_id"])
        message = update.get("message") or {}
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        # Усі оновлення підтверджуємо, але команди виконуємо лише від дозволеного користувача.
        if sender.get("id") != allowed_user_id:
            print("Пропущено повідомлення від неавторизованого Telegram user ID.")
            save_offset(update_id + 1)
            continue

        message_text = (message.get("text") or message.get("caption") or "").strip()
        match = URL_RE.search(message_text)
        if not match:
            send_message(token, chat_id, "Надішли мені пряме посилання на новину (http:// або https://).")
            save_offset(update_id + 1)
            continue

        article_url = match.group(0).rstrip(".,!?;:)]}")
        send_message(token, chat_id, "Посилання отримано. Запускаю обробку новини для Review News...")

        env = os.environ.copy()
        env["ARTICLE_URL"] = article_url
        result = subprocess.run(
            [sys.executable, "main.py"],
            env=env,
            capture_output=True,
            text=True,
            timeout=1500,
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        print(output[-12000:])

        if result.returncode == 0:
            created = re.search(r"Створено \(([^)]+)\):\s*(https?://\S+)", output)
            if created:
                status, post_url = created.groups()
                send_message(token, chat_id, f"Новину успішно опубліковано на Review News ({status}):\n{post_url}")
            elif "Вже оброблених пропущено: 1" in output or "вже оброблено раніше" in output.lower():
                send_message(token, chat_id, "Це посилання вже було оброблено раніше. Повторну публікацію не виконано.")
            else:
                send_message(token, chat_id, "Обробка завершилася без підтвердження публікації. Перевір журнал GitHub Actions.")
        else:
            details = (result.stderr or result.stdout or "Невідома помилка").strip()
            details = re.sub(r"\s+", " ", details)[-700:]
            send_message(token, chat_id, f"Не вдалося обробити новину. Перевір GitHub Actions.\n{details}")

        # Не залишаємо проблемне повідомлення в черзі назавжди.
        save_offset(update_id + 1)


if __name__ == "__main__":
    main()
