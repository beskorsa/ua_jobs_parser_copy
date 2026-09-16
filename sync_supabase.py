"""
Переливает вакансии из локального SQLite (наполняется main.py) в Supabase/Postgres:
raw-запись в vacancies + чанкованные эмбеддинги описания в vacancy_chunks.

Использование:
    python sync_supabase.py                     # синкнуть все вакансии из vacancies.db
    python sync_supabase.py --active-only        # только is_active=1
    python sync_supabase.py --limit 50           # для быстрого теста
    python sync_supabase.py --search "python розробник віддалено"
                                                   # семантический поиск по уже
                                                   # засинканным эмбеддингам, без
                                                   # повторной заливки

Перед первым запуском:
    cp .env.example .env   # и заполнить DATABASE_URL (+ OPENAI_API_KEY, если EMBEDDING_PROVIDER=openai)
    pip install -r requirements.txt -r requirements-embeddings.txt
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

import config
import db
import postgres_store
from embeddings import chunk_text, get_embedder
from main import DEFAULT_KEYWORDS_FILE, DEFAULT_WEEKLY_KEYWORDS_FILE, load_keywords, load_weekly_keywords

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _safe_iso_date(value: str | None) -> str | None:
    """published_at в Postgres — колонка типа `date`, а не text (как в SQLite):
    любое значение, не похожее на YYYY-MM-DD (сырой текст вроде "2 дні тому",
    который парсер по каким-то причинам не распознал), роняет INSERT. Раз уж
    scrapers/base.py и так должен отдавать либо ISO-дату, либо None — это
    просто подстраховка на случай будущих форматов дат, которые парсер ещё не
    умеет разбирать, чтобы одна кривая дата не валила весь синк вакансии.
    """
    if not value or not _ISO_DATE_RE.match(value):
        return None
    return value


def read_sqlite_vacancies(active_only: bool, limit: int | None) -> list[dict]:
    db_path = Path(config.SQLITE_PATH)
    if not db_path.exists():
        print(f"Не найден {db_path} — сначала запустите main.py, чтобы собрать вакансии.", file=sys.stderr)
        sys.exit(1)

    # db.init_db() идемпотентно докатывает миграцию city/salary_* колонок —
    # нужно, если vacancies.db создавался до их появления в схеме (иначе
    # SELECT ниже упадёт с "no such column").
    db.init_db()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = ("SELECT source, external_id, keyword, title, company, description, url, published_at, "
              "city, salary_min, salary_max, salary_currency, salary_raw, work_mode, last_seen_at, is_active "
              "FROM vacancies")
    if active_only:
        query += " WHERE is_active=1"
    query += " ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    conn.close()
    return rows


BATCH_SIZE = 40


def _vacancy_text(row: dict) -> str:
    return f"{row['title']}\n{row['company'] or ''}\n{row['description'] or ''}".strip()


def sync(active_only: bool, limit: int | None, batch_size: int = BATCH_SIZE):
    rows = read_sqlite_vacancies(active_only, limit)
    if not rows:
        print("Нечего синкать — 0 вакансий в SQLite по заданным условиям.")
        return

    print(f"Найдено {len(rows)} вакансий в SQLite. Подключаюсь к Postgres...")
    conn = postgres_store.get_connection()
    postgres_store.ensure_schema(conn)

    # Полный список настроенных ключевых слов (keywords.csv + ВСЕ дни
    # keywords_weekly.csv, не только сегодняшний) — нужен веб-части, чтобы
    # не помечать "вне зоны охвата" ключи, которые скрейпятся, но пока дают
    # 0 результатов. Раньше эта функция была определена, но не вызывалась —
    # scraper_keywords не синкалась вообще.
    all_keywords = load_keywords(DEFAULT_KEYWORDS_FILE) + load_weekly_keywords(DEFAULT_WEEKLY_KEYWORDS_FILE, None)
    postgres_store.sync_scraper_keywords(conn, all_keywords)

    print(f"Инициализирую эмбеддер ({config.EMBEDDING_PROVIDER})... "
          f"{'(первый запуск скачает модель, может занять пару минут)' if config.EMBEDDING_PROVIDER == 'local' else ''}")
    embedder = get_embedder()

    ok, failed = 0, 0
    for batch_start in range(0, len(rows), batch_size):
        batch = rows[batch_start:batch_start + batch_size]

        # Раньше эмбеддинг вызывался ОТДЕЛЬНО на каждую вакансию — 3556
        # вакансий = 3556 сетевых round-trip'ов к OpenAI (или к локальной
        # модели построчно). Здесь чанки всего батча эмбедятся ОДНИМ вызовом
        # (сам embedder внутри разобьёт на под-батчи по 96 текстов, если
        # нужно) — тот же результат, но на порядок меньше сетевых запросов.
        per_row_chunks = []
        flat_chunks: list[str] = []
        chunk_ranges: list[tuple[int, int]] = []
        for row in batch:
            chunks = chunk_text(_vacancy_text(row))
            start = len(flat_chunks)
            flat_chunks.extend(chunks)
            chunk_ranges.append((start, len(flat_chunks)))
            per_row_chunks.append(chunks)

        try:
            batch_vectors = embedder.embed(flat_chunks) if flat_chunks else []
        except Exception as e:
            # Батч целиком не заэмбедился (сетевой сбой, лимит и т.п.) —
            # не роняем весь батч, а эмбедим каждую вакансию по отдельности
            # ниже (медленнее, но только для этого одного батча).
            print(f"  ошибка эмбеддинга батча ({batch_start + 1}-{batch_start + len(batch)}): {e} "
                  f"— эмбежу вакансии этого батча поштучно", file=sys.stderr)
            batch_vectors = None

        for row, chunks, (start, end) in zip(batch, per_row_chunks, chunk_ranges):
            # SAVEPOINT — чтобы ошибка на ОДНОЙ вакансии откатывала только её
            # изменения, а не весь батч (у нас теперь один commit на батч,
            # а не на строку — без savepoint полный conn.rollback() из-за
            # одной плохой записи стёр бы уже обработанные вакансии этого же
            # батча, которые ещё не закоммичены).
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT row_sp")
            try:
                if batch_vectors is not None:
                    vectors = batch_vectors[start:end]
                elif chunks:
                    vectors = embedder.embed(chunks)
                else:
                    vectors = []

                vacancy_id = postgres_store.upsert_vacancy(conn, {
                    "source": row["source"],
                    "external_id": row["external_id"],
                    "keyword": row["keyword"],
                    "title": row["title"],
                    "company": row["company"],
                    "description": row["description"],
                    "url": row["url"],
                    "published_at": _safe_iso_date(row["published_at"]),
                    "city": row["city"],
                    "salary_min": row["salary_min"],
                    "salary_max": row["salary_max"],
                    "salary_currency": row["salary_currency"],
                    "salary_raw": row["salary_raw"],
                    "work_mode": row["work_mode"],
                    "scraped_at": row["last_seen_at"],
                }, commit=False)

                if chunks:
                    postgres_store.replace_chunks(conn, vacancy_id, list(zip(chunks, vectors)), commit=False)

                with conn.cursor() as cur:
                    cur.execute("RELEASE SAVEPOINT row_sp")
                ok += 1
            except Exception as e:
                failed += 1
                print(f"  ошибка на '{row['title'][:50]}' ({row['url']}): {e}", file=sys.stderr)
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT row_sp")

        conn.commit()
        print(f"  {min(batch_start + len(batch), len(rows))}/{len(rows)} обработано")

    # деактивируем в Postgres то, что уже неактивно в SQLite (по каждой паре source+keyword)
    pairs = {(r["source"], r["keyword"]) for r in rows if not active_only}
    for source, keyword in pairs:
        seen_urls = {r["url"] for r in rows if r["source"] == source and r["keyword"] == keyword and r["is_active"]}
        deactivated = postgres_store.mark_inactive(conn, source, keyword, seen_urls)
        if deactivated:
            print(f"  [{source}/{keyword}] помечено неактивными в Postgres: {deactivated}")

    # Не просто деактивируем, а и физически убираем старое из базы — иначе
    # неактивные записи копятся навсегда (mark_inactive лишь выставляет
    # флаг), и рано или поздно начнут путать ручные SQL-запросы/статистику.
    removed = postgres_store.delete_inactive(conn)
    if removed:
        print(f"  удалено неактивных записей из Postgres: {removed}")

    conn.close()
    print(f"\nГотово. Успешно: {ok}, с ошибками: {failed}.")


def search(query: str, top_k: int = 10, city: str = None, min_salary: float = None,
           salary_currency: str = None, published_after: str = None):
    conn = postgres_store.get_connection()
    embedder = get_embedder()
    query_vec = embedder.embed_query(query) if hasattr(embedder, "embed_query") else embedder.embed([query])[0]
    results = postgres_store.semantic_search(
        conn, query_vec, top_k=top_k, city=city, min_salary=min_salary,
        salary_currency=salary_currency, published_after=published_after,
    )
    conn.close()

    if not results:
        print("Ничего не найдено (база пустая, ещё не засинкана, или ничего не подошло под фильтры).")
        return
    for r in results:
        salary = ""
        if r.get("salary_min") or r.get("salary_max"):
            salary = f" | {r.get('salary_min') or '?'}–{r.get('salary_max') or '?'} {r.get('salary_currency') or ''}"
        city_s = f" | {r['city']}" if r.get("city") else ""
        mode_s = f" | {r['work_mode']}" if r.get("work_mode") else ""
        print(f"[{r['distance']:.3f}] {r['title']} — {r['company']} ({r['source']}){city_s}{mode_s}{salary}")
        print(f"  {r['url']}")
        print(f"  ...{r['matched_chunk'][:160]}...")
        print()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--active-only", action="store_true", help="синкать только is_active=1 из SQLite")
    parser.add_argument("--limit", type=int, help="ограничить число вакансий (для теста)")
    parser.add_argument("--search", metavar="QUERY", help="вместо синка — семантический поиск по уже засинканным данным")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--city", help="фильтр: подстрока города (ILIKE)")
    parser.add_argument("--min-salary", type=float, help="фильтр: минимальная salary_max вакансии")
    parser.add_argument("--currency", help="фильтр: валюта зарплаты (UAH/USD/EUR), применяется вместе с --min-salary")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="фильтр: вакансии опубликованы не раньше этой даты")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                         help=f"вакансий за один пакетный вызов эмбеддера/коммит (по умолчанию {BATCH_SIZE})")
    args = parser.parse_args()

    if args.search:
        search(args.search, args.top_k, city=args.city, min_salary=args.min_salary,
               salary_currency=args.currency, published_after=args.since)
    else:
        sync(args.active_only, args.limit, args.batch_size)


if __name__ == "__main__":
    main()
