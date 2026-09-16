"""
LLM-grounding: для каждой топ-вакансии — запрос к OpenAI с контекстом
[резюме + текст вакансии], чтобы оценить релевантность 1-10, объяснить
почему, и сгенерировать 3 варианта первого предложения cover letter.

Это grounding, а не "выдумывание": модель не оценивает вакансию по названию
или общим представлениям, а видит текстом и резюме, и описание вакансии в
промпте, и обязана опираться только на них (см. SYSTEM_PROMPT).

Отличие от resume_match.py: там — быстрый и дешёвый семантический подбор
(эмбеддинги, косинусная близость) по ВСЕЙ базе вакансий. Здесь — дорогой и
медленный LLM-запрос по уже отобранному топу (semantic match даёт top-N
кандидатов, generate.py их оценивает и объясняет).

Результат сохраняется в таблицу generations (resume_id, vacancy_id) —
повторный запуск для той же пары обновляет запись, а не плодит дубли, если
явно не передан --force.

Использование:
    python generate.py run --resume-id 3
        -> top-15 вакансий (semantic match, mode=full), для каждой запрос
           к OpenAI, печатает и сохраняет результат

    python generate.py run --resume-id 3 --top-k 5 --by-section --model gpt-4o
    python generate.py run --resume-id 3 --city Київ --min-salary 1500 --currency USD
    python generate.py run --resume-id 3 --force
        -> пересчитать даже то, что уже было сгенерировано ранее

    python generate.py show --resume-id 3
        -> печатает уже сохранённые оценки, отсортированные по релевантности

Перед первым запуском: pip install -r requirements-generation.txt (это просто
openai, тот же клиент, что и для EMBEDDING_PROVIDER=openai в embeddings.py),
и OPENAI_API_KEY в .env (см. .env.example).
"""
import argparse
import json
import sys

import config
import postgres_store


# Синхронизировано вручную с SYSTEM_PROMPT в ua_jobs_web/src/lib/generate.ts:
# 1) reasoning раньше писало "кандидат має досвід..." от третьего лица, хотя
#    это ответ самому пользователю про его же резюме — теперь обращение на "ви".
# 2) cover_letter_sentences выходили канцелярским шаблоном ("що робить мене
#    ідеальним кандидатом", "мене дуже зацікавила можливість...") — просим
#    живую речь без штампов и без пересказа пунктов резюме.
# 3) На выходе были только варианты ПЕРВОГО предложения — мало для реального
#    отклика. Пользовательница показала свой реальный пример отклика (живой,
#    конкретный, с перечнем инструментов и чёткой структурой) — теперь
#    генерируем полноценные короткие cover letter (5-8 предложений),
#    используя её пример как ориентир стиля/структуры (STYLE_EXAMPLE ниже).
STYLE_EXAMPLE = (
    "Вітаю!\n"
    "Зацікавила ваша вакансія, оскільки за описом вона дуже близька до того, чим я зараз займаюся — "
    "автоматизацією бізнес-процесів та практичним впровадженням AI-рішень.\n"
    "Працюю з Make, Google Apps Script, API та LLM, будую інтеграції між сервісами та автоматизую "
    "рутинні процеси. Використовую OpenAI/ChatGPT у зв'язці з Google Sheets, Telegram, SendPulse та "
    "іншими системами. Окремо займаюся prompt engineering, побудовою багатокрокових AI-сценаріїв, "
    "обробкою та структуруванням даних.\n"
    "Мені цікаво не просто підключити AI до процесу, а розібрати сам процес, знайти точки для "
    "автоматизації, спроєктувати рішення та довести його до робочого результату.\n"
    "Також маю досвід роботи з великими масивами даних, API-інтеграціями, парсингом, автоматизацією "
    "Google Sheets та побудовою AI-пайплайнів. Зараз поглиблюю знання в напрямках AI agents, RAG та "
    "архітектури LLM-рішень.\n"
    "Буду рада поспілкуватися та показати приклади реалізованих автоматизацій і AI-рішень."
)

SYSTEM_PROMPT = (
    "Ти — асистент з пошуку роботи, який звертається напряму до користувача (кандидата), а не оцінює "
    "стороннього кандидата. Тобі дають текст його резюме і текст вакансії. Оціни, наскільки вакансія "
    "релевантна, СПИРАЮЧИСЬ ЛИШЕ на текст резюме та вакансії нижче — нічого не вигадуй про досвід "
    "користувача чи вимоги вакансії, якого там немає.\n\n"
    "Відповідай ЛИШЕ JSON-об'єктом з ключами:\n"
    '  "relevance": ціле число від 1 до 10 (10 — ідеальний збіг вимог і досвіду),\n'
    '  "reasoning": 2-4 речення — звертайся до користувача на "ви" ("у вас є досвід...", "вам бракує...", '
    'а НЕ "кандидат має досвід..."), з конкретними збігами чи розбіжностями (навички, роки досвіду, '
    "рівень позиції, домен) — без загальних фраз типу «непогано корелює»,\n"
    '  "cover_letter_sentences": масив РІВНО з 3 рядків — три ПОВНОЦІННІ короткі варіанти cover letter '
    "(5-8 речень кожен, з абзацами через \\n\\n), готові до відправки роботодавцю як є. Орієнтуйся на "
    "стиль, структуру і тон прикладу нижче (ЦЕ ЛИШЕ ОРІЄНТИР СТИЛЮ — не копіюй фрази чи факти з нього, "
    "весь зміст бери ЛИШЕ з реального резюме користувача, яке буде надано):\n\n"
    f'"""\n{STYLE_EXAMPLE}\n"""\n\n'
    "Типова структура (адаптуй під конкретне резюме й вакансію, не пиши формально-по-пунктах): "
    "(1) вітання + чому саме ця вакансія відгукнулась, зв'язок із тим, чим людина реально займається; "
    "(2) конкретні інструменти/технології/навички з резюме, що напряму стосуються вакансії — "
    "перелічуй реальні назви (мови, фреймворки, сервіси), а не загальні слова; "
    "(3) одна фраза про підхід до роботи чи цінності, ЯКЩО це видно з резюме (напр. фокус на результаті, "
    "розбір процесу перед автоматизацією, увага до деталей) — без вигадування, якщо в резюме такого "
    "натяку немає, пропусти цей пункт; "
    "(4) додатковий релевантний досвід чи те, що людина зараз вивчає/розвиває, якщо є в резюме; "
    "(5) коротке запрошення поспілкуватися / показати приклади робіт. "
    "Жива мова від першої особи («я», «мій»), БЕЗ штампів на кшталт «ідеальний кандидат», «мене дуже "
    "зацікавила можливість», «я захоплююсь X», без переказу пунктів резюме як списку досягнень. "
    "Три варіанти мають реально відрізнятись акцентом: (1) на конкретному результаті/проєкті з резюме; "
    "(2) на технічному стеку, що збігається з вимогами вакансії; (3) на домені/продукті компанії з "
    "вакансії, який реально резонує з досвідом користувача. Пиши тією ж мовою, що і текст вакансії."
)


def build_user_prompt(resume_text: str, vacancy: dict) -> str:
    return (
        f"### Резюме кандидата\n{resume_text.strip()}\n\n"
        f"### Вакансія\n"
        f"Посада: {vacancy['title']}\n"
        f"Компанія: {vacancy.get('company') or '—'}\n"
        f"Опис:\n{(vacancy.get('description') or '').strip()}\n"
    )


def call_llm(resume_text: str, vacancy: dict, model: str) -> dict:
    from openai import OpenAI  # лениво — не обязателен, если модуль не используется

    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан (см. .env.example)")
    client = OpenAI(api_key=config.OPENAI_API_KEY)

    resp = client.chat.completions.create(
        model=model,
        temperature=0.4,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(resume_text, vacancy)},
        ],
    )
    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM вернула не-JSON: {raw[:200]!r}") from e

    if "relevance" not in data:
        raise ValueError(f"В ответе LLM нет поля relevance: {data!r}")
    relevance = max(1, min(10, int(data["relevance"])))

    sentences = data.get("cover_letter_sentences") or []
    if not isinstance(sentences, list) or not sentences:
        raise ValueError(f"LLM не вернула cover_letter_sentences: {data!r}")
    sentences = [str(s).strip() for s in sentences[:3]]

    return {
        "relevance": relevance,
        "reasoning": str(data.get("reasoning") or "").strip(),
        "cover_letter_sentences": sentences,
    }


def cmd_run(resume_id: int, top_k: int, by_section: bool, model: str, force: bool,
            city: str = None, min_salary: float = None, currency: str = None, since: str = None):
    conn = postgres_store.get_connection()
    postgres_store.ensure_schema(conn)

    resume = postgres_store.get_resume(conn, resume_id)
    if resume is None:
        print(f"Резюме id={resume_id} не найдено.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    matches = postgres_store.match_vacancies_for_resume(
        conn, resume_id, top_k=top_k, mode="sections" if by_section else "full",
        city=city, min_salary=min_salary, salary_currency=currency, published_after=since,
    )
    if not matches:
        print("Нет подходящих вакансий (semantic match вернул пусто) — сначала "
              "python sync_supabase.py и/или проверьте фильтры.")
        conn.close()
        return

    print(f"Топ вакансий: {len(matches)}. Модель: {model}. "
          f"Резюме id={resume_id} ({resume['filename']}).\n")

    done, skipped, failed = 0, 0, 0
    for i, m in enumerate(matches, 1):
        if not force:
            existing = postgres_store.get_generation(conn, resume_id, m["id"])
            if existing:
                print(f"{i}. [уже есть, relevance={existing['relevance']}] "
                      f"{m['title']} — {m['company']}")
                skipped += 1
                continue

        vacancy = postgres_store.get_vacancy(conn, m["id"])
        if vacancy is None:
            print(f"{i}. вакансия id={m['id']} пропала из базы, пропускаю", file=sys.stderr)
            continue

        print(f"{i}. {vacancy['title']} — {vacancy['company']} ({vacancy['source']})... ",
              end="", flush=True)
        try:
            result = call_llm(resume["raw_text"], vacancy, model)
        except Exception as e:
            print(f"ошибка: {e}", file=sys.stderr)
            failed += 1
            continue

        postgres_store.upsert_generation(
            conn, resume_id, m["id"], model,
            result["relevance"], result["reasoning"], result["cover_letter_sentences"],
        )
        print(f"relevance={result['relevance']}")
        print(f"   {result['reasoning']}")
        for j, s in enumerate(result["cover_letter_sentences"], 1):
            print(f"   [{j}] {s}")
        print(f"   {vacancy['url']}")
        done += 1

    conn.close()
    print(f"\nГотово. Сгенерировано: {done}, пропущено (уже было): {skipped}, ошибок: {failed}.")


def cmd_show(resume_id: int, top: int):
    conn = postgres_store.get_connection()
    rows = postgres_store.list_generations(conn, resume_id, limit=top)
    conn.close()
    if not rows:
        print("Пока ничего не сгенерировано — python generate.py run --resume-id ...")
        return
    for i, r in enumerate(rows, 1):
        print(f"{i}. [{r['relevance']}/10] {r['title']} — {r['company']} ({r['source']})")
        print(f"   {r['reasoning']}")
        for j, s in enumerate(r["cover_letter_sentences"], 1):
            print(f"   [{j}] {s}")
        print(f"   {r['url']}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="сгенерировать оценки + cover letter для топ-вакансий резюме")
    p_run.add_argument("--resume-id", type=int, required=True)
    p_run.add_argument("--top-k", type=int, default=15)
    p_run.add_argument("--by-section", action="store_true", help="подбор топа взвешенно по секциям резюме")
    p_run.add_argument("--model", default=config.OPENAI_CHAT_MODEL)
    p_run.add_argument("--force", action="store_true", help="пересчитать, даже если уже есть сохранённый результат")
    p_run.add_argument("--city", help="фильтр: подстрока города (ILIKE)")
    p_run.add_argument("--min-salary", type=float, help="фильтр: минимальная salary_max вакансии")
    p_run.add_argument("--currency", help="фильтр: валюта зарплаты (UAH/USD/EUR), вместе с --min-salary")
    p_run.add_argument("--since", metavar="YYYY-MM-DD", help="фильтр: вакансии опубликованы не раньше этой даты")

    p_show = sub.add_parser("show", help="показать уже сохранённые оценки для резюме")
    p_show.add_argument("--resume-id", type=int, required=True)
    p_show.add_argument("--top", type=int, default=20)

    args = parser.parse_args()
    if args.cmd == "run":
        cmd_run(args.resume_id, args.top_k, args.by_section, args.model, args.force,
                 city=args.city, min_salary=args.min_salary, currency=args.currency, since=args.since)
    elif args.cmd == "show":
        cmd_show(args.resume_id, args.top)


if __name__ == "__main__":
    main()
