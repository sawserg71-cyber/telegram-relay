from relay import parse_reply_subject, strip_quotes


def test_parse_reply_subject():
    assert parse_reply_subject("Re: [TG 123456789] Иван (@ivan)") == 123456789
    assert parse_reply_subject("[TG 42] Клиент") == 42
    assert parse_reply_subject("Письмо без метки") is None
    assert parse_reply_subject(None) is None


def test_strip_quotes_removes_quoted_lines():
    text = "Вот ответ!\n> Новое сообщение от клиента\n> — Это письмо сформировано релеем."
    assert strip_quotes(text) == "Вот ответ!"
    assert strip_quotes("") == ""
