#!/usr/bin/env python3
"""
Telegram-relay: клиенты <-> ваш Telegram-бот -> ваша почта (без VPN).
Два режима:
  relay.py poll    — забирает новые сообщения клиентов через getUpdates и шлёт письма;
  relay.py answers — читает ваши ответы на письма и отправляет их клиентам через бота.

Верификация клиентов: новый пользователь получает кнопку «Подтвердить телефон»
(нативная кнопка Telegram с request_contact). Подтвердившийся попадает в allowed.json.

Как distinguishes свои письма от ответов: письма релея несут служебный заголовок
X-Relay-Notice: 1. Обратный ход ищет письма FROM=вам, SUBJECT содержит "[TG",
БЕЗ флага \Flagged (звёздочки). Отправив ответ клиенту, релей ставит письму
звёздочку — это метка "обработано". Читать письма в почте можно свободно.
"""
import json
import os
import re
import smtplib
import ssl
import sys
import imaplib
import email as email_lib
from email.message import EmailMessage

import requests

API = "https://api.telegram.org/bot{token}/{method}"
STATE_FILE = "state.json"
ALLOWED_FILE = "allowed.json"

MAIL_FROM = os.environ.get("MAIL_FROM", "sawserg@yandex.ru")
MAIL_TO = os.environ.get("MAIL_TO", MAIL_FROM)
SMTP_HOST = "smtp.yandex.ru"
IMAP_HOST = "imap.yandex.ru"

CONFIRM_TEXT = ("Здравствуйте! Чтобы избежать спама, подтвердите, что вы реальный человек: "
                "нажмите кнопку ниже и отправьте свой контакт. После этого я получу ваше сообщение и отвечу.")
CONFIRM_KEYBOARD = {"keyboard": [[{"text": "Подтвердить телефон", "request_contact": True}]],
                    "resize_keyboard": True, "one_time_keyboard": True}


def tg(token, method, payload=None):
    r = requests.post(API.format(token=token, method=method), json=payload or {}, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram API %s: %s" % (method, data))
    return data.get("result")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def parse_reply_subject(subject):
    """Извлекает chat_id из темы письма вида 'Re: [TG 123456789] Иван (@ivan)'."""
    m = re.search(r"\[TG\s*(\d+)\]", subject or "")
    return int(m.group(1)) if m else None


def strip_quotes(text):
    """Убирает строки цитирования и служебный футер релея, оставляя только ответ человека."""
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith(">") or s == "— Это письмо сформировано релеем.":
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def send_letter(sender, chat_id, text, app_password):
    msg = EmailMessage()
    who = sender.get("first_name", "")
    username = sender.get("username") or ""
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Subject"] = ("[TG %s] %s %s" % (chat_id, who, ("@" + username) if username else "")).strip()
    msg["X-Relay-Notice"] = "1"
    msg.set_content(
        "Новое сообщение от клиента в Telegram.\n\n"
        "Имя: %s\n"
        "Chat ID: %s\n"
        "Текст:\n"
        "%s\n\n"
        "— Это письмо сформировано релеем. Ответьте на него — ответ уйдёт клиенту через бота." % (who or "-", chat_id, text)
    )
    with smtplib.SMTP_SSL(SMTP_HOST, 465, context=ssl.create_default_context()) as smtp:
        smtp.login(MAIL_FROM, app_password)
        smtp.send_message(msg)


def poll(token, app_password):
    state = load_json(STATE_FILE, {"offset": 0})
    allowed = load_json(ALLOWED_FILE, {"users": {}})
    allowed_chats = set(filter(None, os.environ.get("TELEGRAM_ALLOWED_CHATS", "").split(",")))
    updates = tg(token, "getUpdates", {"offset": state["offset"], "timeout": 0})
    offset = state["offset"]
    allowed_changed = False
    sent = 0
    for upd in updates:
        offset = max(offset, upd.get("update_id", 0) + 1)
        msg = upd.get("message")
        if not msg:
            continue
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        sender = msg.get("from") or {}
        if chat.get("type") != "private":
            if str(chat_id) not in allowed_chats:
                continue
        else:
            uid = str(sender.get("id", ""))
            if uid not in allowed["users"]:
                contact = msg.get("contact") or {}
                if str(contact.get("user_id")) == uid:
                    allowed["users"][uid] = {"phone_tail": (contact.get("phone_number") or "")[-4:]}
                    allowed_changed = True
                    tg(token, "sendMessage", {"chat_id": chat_id,
                        "text": "Телефон подтверждён. Теперь пишите — сообщение дойдёт, я отвечу."})
                    continue
                tg(token, "sendMessage", {"chat_id": chat_id, "text": CONFIRM_TEXT,
                                          "reply_markup": CONFIRM_KEYBOARD})
                continue
        text = msg.get("text") or "[сообщение без текста — файл или стикер]"
        send_letter(sender, chat_id, text, app_password)
        sent += 1
    state["offset"] = offset
    state_changed = offset != state["offset"] or allowed_changed or not os.path.exists(STATE_FILE)
    if state_changed:
        save_json(STATE_FILE, state)
        save_json(ALLOWED_FILE, allowed)
    print("poll: отправлено писем: %d, подтверждено пользователей: %d" % (sent, len(allowed["users"])))


def fetch_body(raw):
    msg = email_lib.message_from_bytes(raw)
    body, charset_ok = None, False
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            body = payload.decode(part.get_content_charset() or "utf-8", "replace")
            charset_ok = True
            break
    if not charset_ok:
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                body = re.sub(r"<br\s*/?>", "\n", html)
                body = re.sub(r"<[^>]+>", "", body)
                break
    return msg, strip_quotes(body or "")


def answers(token, app_password):
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(MAIL_FROM, app_password)
    imap.select("INBOX")
    # Отклик ищем среди писем от нас же с меткой [TG в теме и БЕЗ звёздочки.
    # Звёздочка (\Flagged) после успешной отправки = "обработано релеем".
    typ, data = imap.search(None, '(FROM "%s" UNFLAGGED SUBJECT "[TG")' % MAIL_FROM)
    ids = (data[0] or b"").split()
    sent = 0
    for mid in ids:
        typ, data = imap.fetch(mid, "(RFC822)")
        raw = data[0][1]
        msg, body = fetch_body(raw)
        if msg.get("X-Relay-Notice") == "1":
            continue  # собственное письмо релея, не ответ
        chat_id = parse_reply_subject(msg.get("Subject", ""))
        if chat_id is None or not body:
            continue
        body = body[:4000]
        tg(token, "sendMessage", {"chat_id": chat_id, "text": body})
        imap.store(mid, "+FLAGS", "\\Flagged")
        sent += 1
    imap.logout()
    print("answers: отправлено ответов: %d" % sent)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("poll", "answers"):
        print("Использование: relay.py poll|answers")
        return 2
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    app_password = os.environ.get("MAIL_APP_PASSWORD")
    if not token:
        print("Нет секрета TELEGRAM_BOT_TOKEN")
        return 1
    if not app_password:
        print("Нет секрета MAIL_APP_PASSWORD")
        return 1
    if sys.argv[1] == "poll":
        poll(token, app_password)
    else:
        answers(token, app_password)
    return 0


if __name__ == "__main__":
    sys.exit(main())
