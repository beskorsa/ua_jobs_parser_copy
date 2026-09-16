"""
Хранилище вакансий.

По умолчанию — локальный SQLite-файл (vacancies.db), без внешних зависимостей.
Схема специально сделана 1-в-1 совместимой с будущей таблицей в Supabase/Postgres —
см. schema_postgres.sql. Когда будет готов Supabase-проект:
  1. выполнить schema_postgres.sql в нём (добавляет расширение pgvector и колонку embedding);
  2. выгрузить текущий SQLite в CSV/INSERT-ы и залить в Postgres, либо просто начать парсить заново;
  3. переключить эту прослойку на psycopg2 (сигнатуры функций ниже менять не придётся —
     upsert_vacancies / mark_inactive / fetch_active останутся теми же).

Генерация эмбеддингов (bge-small локально или text-embedding-3-small) сюда сознательно
не добавлена — это следующий шаг, под него уже есть колонка embedding (NULL пока не используется).
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from models import Vacancy

DB_PATH = Path(__file__).parent / "vacancies.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    external_id     TEXT,
    keyword         TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT,
    description     TEXT,
    url             TEXT NOT NULL UNIQUE,
    published_at    TEXT,
    city            TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    salary_raw      TEXT,
    work_mode       TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    embedding       BLOB
);
CREATE INDEX IF NOT EXISTS idx_vacancies_source_keyword ON vacancies(source, keyword);
CREATE INDEX IF NOT EXISTS idx_vacancies_active ON vacancies(is_active);
"""

# CREATE TABLE IF NOT EXISTS не добавит колонки в уже существующий
# vacancies.db (старые базы создавались до city/salary_*) — поэтому миграция
# отдельным шагом, ALTER TABLE ADD COLUMN идемпотентно (ловим "duplicate
# column" и молча пропускаем, если колонка уже есть).
_MIGRATIONS = (
    "ALTER TABLE vacancies ADD COLUMN city TEXT",
    "ALTER TABLE vacancies ADD COLUMN salary_min REAL",
    "ALTER TABLE vacancies ADD COLUMN salary_max REAL",
    "ALTER TABLE vacancies ADD COLUMN salary_currency TEXT",
    "ALTER TABLE vacancies ADD COLUMN salary_raw TEXT",
    "ALTER TABLE vacancies ADD COLUMN work_mode TEXT",
)


@contextmanager
def connect():
    # timeout + busy_timeout — источники теперь парсятся параллельно (см.
    # main.py), каждый в своём потоке со своим коротким connect()/close(),
    # и могут упереться в SQLite-запись друг друга одновременно. WAL уже
    # разрешает параллельное чтение+запись, но при коллизии на самой записи
    # sqlite3 по умолчанию сразу кидает "database is locked" — с таймаутом
    # он подождёт и повторит, вместо падения потока.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def upsert_vacancies(vacancies: Iterable[Vacancy]) -> int:
    """Insert new vacancies / refresh last_seen_at & is_active=1 for ones seen again."""
    count = 0
    with connect() as conn:
        for v in vacancies:
            conn.execute(
                """
                INSERT INTO vacancies (source, external_id, keyword, title, company,
                                        description, url, published_at, city,
                                        salary_min, salary_max, salary_currency, salary_raw,
                                        work_mode, first_seen_at, last_seen_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,
                    company=excluded.company,
                    description=excluded.description,
                    published_at=excluded.published_at,
                    city=excluded.city,
                    salary_min=excluded.salary_min,
                    salary_max=excluded.salary_max,
                    salary_currency=excluded.salary_currency,
                    salary_raw=excluded.salary_raw,
                    work_mode=excluded.work_mode,
                    last_seen_at=excluded.last_seen_at,
                    is_active=1
                """,
                (
                    v.source, v.external_id, v.keyword, v.title, v.company,
                    v.description, v.url, v.published_at, v.city,
                    v.salary_min, v.salary_max, v.salary_currency, v.salary_raw,
                    v.work_mode, v.scraped_at, v.scraped_at,
                ),
            )
            count += 1
    return count


def mark_inactive(source: str, keyword: str, seen_urls: set[str]):
    """
    Любая вакансия, которая раньше встречалась для этой пары (source, keyword),
    но не попала в текущую выдачу поиска — значит с сайта пропала (закрыта/архивирована).
    Помечаем is_active=0, из базы не удаляем.
    """
    with connect() as conn:
        cur = conn.execute(
            "SELECT url FROM vacancies WHERE source=? AND keyword=? AND is_active=1",
            (source, keyword),
        )
        stored = {row[0] for row in cur.fetchall()}
        stale = stored - seen_urls
        if stale:
            conn.executemany(
                "UPDATE vacancies SET is_active=0 WHERE url=?",
                [(u,) for u in stale],
            )
    return len(stale)


def fetch_active(source: str | None = None, keyword: str | None = None):
    query = "SELECT source, keyword, title, company, url, published_at, last_seen_at FROM vacancies WHERE is_active=1"
    params = []
    if source:
        query += " AND source=?"
        params.append(source)
    if keyword:
        query += " AND keyword=?"
        params.append(keyword)
    with connect() as conn:
        cur = conn.execute(query, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
