"""
MVP выдачи результата: отчёт по подобранным вакансиям в Telegram —
список + score (1-10) + пояснение из generate.py (таблица generations).

Никакой отдельной логики оценки здесь нет — это просто рендер уже
сгенерированных LLM-оценок (python generate.py run --resume-id ...) в виде
сообщений Telegram. Если для резюме ещё ничего не сгенерировано — сначала
запустите generate.py.

Зависимостей кроме stdlib нет (urllib) — специально, чтобы не тащить
python-telegram-bot/requests ради одного HTTP-запроса.

Использование:
    python telegram_report.py send --resume-id 3
        -> top-10 сохранённых оценок (по убыванию relevance), в Telegram

    python telegram_report.py send --resume-id 3 --top 5 --min-relevance 6
    python telegram_report.py send --resume-id 3 --chat-id 987654321   # разово в другой чат

    python telegram_report.py test
        -> проверочное сообщение "Watchdog... ой, telegram_report тест ok",
           чтобы проверить TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID до реального отчёта

Перед первым запуском:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID в .env (см. .env.example) — токен от
    @BotFather, chat id — свой (после /start боту: getUpdates) или группы/канала.
"""
import argparse
import html
import json
import sys
import time
import urllib.error
import urllib.request

import config
import postgres_store

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LEN = 4096          # хардлимит Telegram на один sendMessage
SEND_PAUSE_SEC = 0.4            # вежливая пауза между сообщениями (rate limit ~30 msg/sec, но не нагло)


def _api_url(token: str) -> str:
    return TELEGRAM_API_URL.format(token=token)


def send_message(text: str, token: str = None, chat_id: str = None) -> None:
    """Один вызов Telegram Bot API sendMessage. Бросает RuntimeError с текстом
    ошибки от Telegram при неуспехе (неверный токен/chat_id, текст > 4096 и т.п.)."""
    token = token or config.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or config.TELEGRAM_CHAT_ID
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан (см. .env.example)")
    if not chat_id:
        raise RuntimeError("TELEGRAM_CHAT_ID не задан (см. .env.example)")

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        _api_url(token), data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        raise RuntimeError(f"Telegram API вернул ошибку: {body.get('description', body)}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Не удалось достучаться до Telegram API: {e}") from e

    if not body.get("ok"):
        raise RuntimeError(f"Telegram API вернул ошибку: {body.get('description', body)}")


def format_entry(rank: int, r: dict) -> str:
    """Один блок отчёта — вакансия + score + пояснение + варианты cover letter.
    HTML-экранирование обязательно: title/company/url приходят с job-сайтов, а
    parse_mode=HTML у Telegram падает на непарных < > &, если их не эскейпить."""
    title = html.escape(r["title"])
    company = html.escape(r.get("company") or "—")
    source = html.escape(r.get("source") or "")
    reasoning = html.escape(r["reasoning"])
    url = html.escape(r["url"], quote=True)

    lines = [
        f"<b>{rank}. {title}</b> — {company} ({source})",
        f"Score: <b>{r['relevance']}/10</b>",
        reasoning,
    ]
    for i, s in enumerate(r.get("cover_letter_sentences") or [], 1):
        lines.append(f"{i}) {html.escape(s)}")
    lines.append(f'<a href="{url}">Вакансия</a>')
    return "\n".join(lines)


def build_report_chunks(rows: list[dict], header: str) -> list[str]:
    """Режет отчёт на сообщения ≤ MAX_MESSAGE_LEN, никогда не разрывая
    отдельную вакансию посередине — только между вакансиями. Единственное
    исключение: если сама по себе одна вакансия длиннее лимита (гигантское
    description в пояснении не ожидается, но подстраховка не помешает) —
    её текст обрезается с пометкой, а не роняет отправку."""
    entries = [format_entry(i, r) for i, r in enumerate(rows, 1)]

    chunks = []
    current = header
    for entry in entries:
        if len(entry) > MAX_MESSAGE_LEN:
            entry = entry[: MAX_MESSAGE_LEN - 20] + "\n… [обрезано]"
        candidate = current + "\n\n" + entry
        if len(candidate) > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = entry
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def cmd_send(resume_id: int, top: int, min_relevance: int, token: str = None, chat_id: str = None):
    conn = postgres_store.get_connection()
    resume = postgres_store.get_resume(conn, resume_id)
    if resume is None:
        print(f"Резюме id={resume_id} не найдено.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    rows = postgres_store.list_generations(conn, resume_id, limit=top)
    conn.close()

    if min_relevance is not None:
        rows = [r for r in rows if r["relevance"] >= min_relevance]

    if not rows:
        print("Нечего отправлять — нет сохранённых оценок (или все ниже --min-relevance). "
              "Сначала: python generate.py run --resume-id ...")
        return

    header = f"📋 Отчёт по резюме «{html.escape(resume['filename'] or str(resume_id))}»: {len(rows)} вакансий"
    chunks = build_report_chunks(rows, header)

    print(f"Отправляю {len(rows)} вакансий в {len(chunks)} сообщении(ях)...")
    for i, chunk in enumerate(chunks, 1):
        send_message(chunk, token=token, chat_id=chat_id)
        print(f"  {i}/{len(chunks)} отправлено")
        if i < len(chunks):
            time.sleep(SEND_PAUSE_SEC)

    print("Готово.")


def cmd_test(token: str = None, chat_id: str = None):
    send_message("✅ telegram_report.py: связь с ботом настроена.", token=token, chat_id=chat_id)
    print("Тестовое сообщение отправлено.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="отправить отчёт по сохранённым оценкам резюме в Telegram")
    p_send.add_argument("--resume-id", type=int, required=True)
    p_send.add_argument("--top", type=int, default=10, help="сколько вакансий включить (по убыванию relevance)")
    p_send.add_argument("--min-relevance", type=int, help="отсечь вакансии с relevance ниже этого порога")
    p_send.add_argument("--token", help="переопределить TELEGRAM_BOT_TOKEN из .env")
    p_send.add_argument("--chat-id", help="переопределить TELEGRAM_CHAT_ID из .env")

    p_test = sub.add_parser("test", help="отправить проверочное сообщение")
    p_test.add_argument("--token", help="переопределить TELEGRAM_BOT_TOKEN из .env")
    p_test.add_argument("--chat-id", help="переопределить TELEGRAM_CHAT_ID из .env")

    args = parser.parse_args()
    if args.cmd == "send":
        cmd_send(args.resume_id, args.top, args.min_relevance, token=args.token, chat_id=args.chat_id)
    elif args.cmd == "test":
        cmd_test(token=args.token, chat_id=args.chat_id)


if __name__ == "__main__":
    main()
