"""
Обратная связь сайт -> парсер: сайт (ua_jobs_web) логирует реальные запросы
пользователей в таблицу search_queries (поиск по ключевым словам, поиск из
чата, факт загрузки резюме — см. src/lib/searchLog.ts). Этот модуль читает
её и находит "провалы" — запросы, на которые семантический поиск нашёл мало
или ничего, и которых ещё нет в keywords.csv.

CLI-режим (ручной разбор, ничего не меняет без --apply):
    python suggest_keywords.py
        -> кандидаты за последние 30 дней (по частоте, без учёта провалов), топ-15

    python suggest_keywords.py --days 14 --top 10
    python suggest_keywords.py --apply
    python suggest_keywords.py --gaps-only        # только запросы с < GAP_THRESHOLD результатов

Функции find_candidates()/apply_candidates() переиспользует auto_refresh.py
для полностью автоматического цикла (без ручного review) — см. его docstring.

Учитываются только source='search' и source='chat' (короткие,
похожие-на-ключевые-слова запросы). source='resume' — это текст резюме
(summary), не короткая фраза, поэтому в кандидаты не идёт, но статистика по
нему печатается отдельно в CLI-режиме — просто чтобы видеть, сколько резюме
загружают.
"""
import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import config

try:
    import psycopg2
except ImportError:
    raise SystemExit("Нужен psycopg2: pip install -r requirements-supabase.txt")

SCRIPT_DIR = Path(__file__).parent
KEYWORDS_CSV = SCRIPT_DIR / "keywords.csv"

DEFAULT_DAYS = 30
DEFAULT_TOP = 15
GAP_THRESHOLD = 3  # среднее число найденных результатов ниже этого — считаем "провалом"


def normalize(text: str) -> str:
    """Нижний регистр, схлопнутые пробелы — для сравнения "одно и то же
    ключевое слово или нет" без ложных различий из-за регистра/пробелов."""
    return re.sub(r"\s+", " ", text.strip().lower())


def load_existing_keywords() -> set[str]:
    if not KEYWORDS_CSV.exists():
        return set()
    with open(KEYWORDS_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {normalize(row["keyword"]) for row in reader if row.get("keyword")}


def fetch_queries(conn, days: int, source: str) -> list[tuple[str, int | None]]:
    """Возвращает список (query, results_count) для заданного источника."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select query, results_count from search_queries
            where source = %s and created_at > now() - (%s || ' days')::interval
            """,
            (source, days),
        )
        return cur.fetchall()


def is_keyword_like(text: str) -> bool:
    """Отсекает откровенно не-ключевые фразы (пустое, слишком длинное —
    похоже на случайный текст, а не на "python розробник")."""
    n = normalize(text)
    return bool(n) and len(n) <= 60 and len(n.split()) <= 6


def already_covered(candidate: str, existing: set[str]) -> bool:
    """Кандидат считается уже покрытым, если он совпадает с существующим
    ключевым словом или является его подстрокой (и наоборот) — грубо, но
    отсекает очевидные дубли вроде "python" при уже имеющемся "python розробник"."""
    for kw in existing:
        if candidate == kw or candidate in kw or kw in candidate:
            return True
    return False


def find_candidates(
    conn, days: int, existing: set[str], gaps_only: bool = False,
) -> list[tuple[str, int, float]]:
    """Возвращает [(keyword, частота, среднее results_count)], отсортировано
    по частоте, без уже покрытых ключевых слов. Если gaps_only — оставляет
    только запросы со средним results_count < GAP_THRESHOLD (это и есть
    реальный сигнал "сайт не смог нормально ответить на этот запрос")."""
    rows = fetch_queries(conn, days, "search") + fetch_queries(conn, days, "chat")

    stats: dict[str, list] = defaultdict(lambda: [0, 0])  # [count, sum(results_count)]
    for text, results_count in rows:
        if not is_keyword_like(text):
            continue
        key = normalize(text)
        stats[key][0] += 1
        stats[key][1] += results_count if results_count is not None else 0

    candidates = []
    for text, (count, total_results) in stats.items():
        if already_covered(text, existing):
            continue
        avg_results = total_results / count if count else 0.0
        if gaps_only and avg_results >= GAP_THRESHOLD:
            continue
        candidates.append((text, count, avg_results))

    candidates.sort(key=lambda t: t[1], reverse=True)
    return candidates


def apply_candidates(candidates: list[tuple[str, int, float]]) -> None:
    with open(KEYWORDS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for text, _, _ in candidates:
            writer.writerow([text])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"окно в днях (по умолчанию {DEFAULT_DAYS})")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"сколько кандидатов показать (по умолчанию {DEFAULT_TOP})")
    parser.add_argument("--apply", action="store_true", help="дописать топ-N кандидатов в keywords.csv")
    parser.add_argument("--gaps-only", action="store_true",
                         help=f"только запросы со средним < {GAP_THRESHOLD} результатов (реальные провалы поиска)")
    args = parser.parse_args()

    if not config.DATABASE_URL:
        raise SystemExit("DATABASE_URL пустой — проверь .env")

    existing = load_existing_keywords()
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        resume_count = len(fetch_queries(conn, args.days, "resume"))
        candidates = find_candidates(conn, args.days, existing, gaps_only=args.gaps_only)
    finally:
        conn.close()

    top_candidates = candidates[: args.top]

    print(f"Уже в keywords.csv: {len(existing)} ключевых слов.")
    print(f"Загрузок резюме за {args.days} дн.: {resume_count}.")
    print(f"Новых кандидатов: {len(candidates)} (показаны топ-{len(top_candidates)}).\n")

    if not top_candidates:
        print("Нет новых кандидатов — либо мало данных, либо всё уже покрыто.")
        return

    for text, count, avg_results in top_candidates:
        print(f"  {count:>3}x  ~{avg_results:.1f} результатов  {text}")

    if args.apply:
        apply_candidates(top_candidates)
        print(f"\nДобавлено {len(top_candidates)} ключевых слов в {KEYWORDS_CSV.name}.")
    else:
        print("\nЗапустите с --apply, чтобы дописать эти ключевые слова в keywords.csv.")


if __name__ == "__main__":
    main()
