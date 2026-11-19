#!/usr/bin/env python3
"""
Telegram-relay: клиенты <-> ваш Telegram-бот -> ваша почта (без VPN).
Два режима:
  relay.py poll    — забирает новые сообщения клиентов через getUpdates и шлёт письма;
  relay.py answers — читает ваши ответы на письма и отправляет их клиентам через бота.

Верификация клиентов: новый пользователь получает кнопку «Подтвердить телефон»
(нативная кнопка Telegram с request_contact). Подтвердившийся попадает в allowed.json.

Защита от повторов:
- poll держит список обработанных update_id в state.json — даже если Telegram
  повторно отдаёт старые апдейты (наблюдалось 19.11), они не уйдут в почту дважды;
- answers держит список обработанных Message-ID писем; письма релея отличает
  служебный заголовок X-Relay-Notice: 1. Тема декодируется из MIME-слов.
"""
import json
import os
import re
import smtplib
import ssl
import sys
import imaplib
import email as email_lib
import datetime
from email.message import EmailMessage
from email.header import decode_header, make_header

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


def header_str(msg, field):
    """Декодирует заголовок из MIME-слов (=?utf-8?B?...?=) в обычный текст."""
    raw = msg.get(field, "")
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


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
    state = load_json(STATE_FILE, {"offset": 0, "tg_done": []})
    allowed = load_json(ALLOWED_FILE, {"users": {}})
    done = set(state.get("tg_done", []))
    allowed_chats = set(filter(None, os.environ.get("TELEGRAM_ALLOWED_CHATS", "").split(",")))
    updates = tg(token, "getUpdates", {"offset": state["offset"], "timeout": 0})
    offset = state["offset"]
    allowed_changed = False
    state_changed = False
    sent = 0
    skipped = 0
    for upd in updates:
        uid = upd.get("update_id")
        offset = max(offset, (uid or 0) + 1)
        if uid is not None and uid in done:
            skipped += 1
            continue  # уже обрабатывали этот апдейт
        msg = upd.get("message")
        if not msg:
            state_changed = True
            continue
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        sender = msg.get("from") or {}
        if chat.get("type") != "private":
            if str(chat_id) not in allowed_chats:
                state_changed = True
                continue
        else:
            uid_sender = str(sender.get("id", ""))
            if uid_sender not in allowed["users"]:
                contact = msg.get("contact") or {}
                if str(contact.get("user_id")) == uid_sender:
                    allowed["users"][uid_sender] = {"phone_tail": (contact.get("phone_number") or "")[-4:]}
                    allowed_changed = True
                    tg(token, "sendMessage", {"chat_id": chat_id,
                        "text": "Телефон подтверждён. Теперь пишите — сообщение дойдёт, я отвечу."})
                    continue
                tg(token, "sendMessage", {"chat_id": chat_id, "text": CONFIRM_TEXT,
                                          "reply_markup": CONFIRM_KEYBOARD})
                state_changed = True
                continue
        text = msg.get("text") or "[сообщение без текста — файл или стикер]"
        send_letter(sender, chat_id, text, app_password)
        sent += 1
        state_changed = True
        if uid is not None:
            done.add(uid)
    state["offset"] = offset
    if done:
        state["tg_done"] = list(done)[-500:]
    state_changed = state_changed or allowed_changed or offset != state.get("offset") or not os.path.exists(STATE_FILE)
    if state_changed:
        save_json(STATE_FILE, state)
        save_json(ALLOWED_FILE, allowed)
    print("poll: отправлено писем: %d, повторов отброшено: %d, подтверждено пользователей: %d" % (sent, skipped, len(allowed["users"])))


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
    state = load_json(STATE_FILE, {"offset": 0, "processed": []})
    processed = set(state.get("processed", []))
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(MAIL_FROM, app_password)
    imap.select("INBOX")
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).strftime("%d-%b-%Y")
    # Отклик ищем среди писем от нас же с "[TG" в теме за последние 7 дней.
    # Повторная отправка исключается списком Message-ID, а не флагами письма.
    crit = '(FROM "%s" SINCE %s)' % (MAIL_FROM, since)
    typ, data = imap.search(None, crit)
    ids = (data[0] or b"").split()
    print("answers: найдено писем от нас за 7 дней: %d" % len(ids))
    sent = 0
    for mid in ids:
        typ, data = imap.fetch(mid, "(RFC822)")
        raw = data[0][1]
        msg, body = fetch_body(raw)
        subj = header_str(msg, "Subject")
        msgid = (msg.get("Message-ID") or "").strip()
        if msg.get("X-Relay-Notice") == "1":
            continue  # собственное письмо релея, не ответ
        chat_id = parse_reply_subject(subj)
        if chat_id is None:
            print("answers: пропущено (нет [TG id]): %r" % subj[:60])
            continue
        if not body:
            print("answers: пропущено (пустое тело): %r" % subj[:60])
            continue
        if msgid and msgid in processed:
            print("answers: пропущено (уже обработано): %r" % subj[:60])
            continue
        tg(token, "sendMessage", {"chat_id": chat_id, "text": body[:4000]})
        if msgid:
            processed.add(msgid)
        sent += 1
        print("answers: отправлено в чат %s: %r" % (chat_id, body[:40]))
    imap.logout()
    if processed:
        state["processed"] = list(processed)[-500:]
        save_json(STATE_FILE, state)
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
