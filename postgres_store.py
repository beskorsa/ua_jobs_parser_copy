"""
Слой хранения в Supabase/Postgres + pgvector:
  - vacancies / vacancy_chunks — вакансии и эмбеддинги их описаний (см. sync_supabase.py)
  - resumes / resume_sections  — резюме соискателя и эмбеддинги отдельно,
    в СВОЕЙ таблице, а не вперемешку с вакансиями (см. resume_match.py)

Схема создаётся автоматически при первом вызове ensure_schema() — руками
катить schema_postgres.sql не обязательно (но он тоже актуален, для тех,
кто предпочитает применять миграции вручную через Supabase SQL Editor).
"""
import psycopg2
import psycopg2.extras

import config


def get_connection():
    if not config.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL не задан. Скопируйте .env.example в .env и укажите "
            "строку подключения из Supabase (Project Settings → Database → Connection string)."
        )
    return psycopg2.connect(config.DATABASE_URL)


def ensure_schema(conn, dim: int = None):
    dim = dim or config.EMBEDDING_DIM
    with conn.cursor() as cur:
        # schema extensions — не public: розширення в public-схемі PostgREST
        # автоматично виставляє в публічний API (Supabase Security Advisor,
        # "Extension in Public"). "extensions" вже є в дефолтному search_path
        # Supabase, тож тип vector/оператори (<=>, <->) лишаються доступні
        # без жодних змін у запитах. IF NOT EXISTS перевіряє тільки ім'я
        # розширення, не схему — на вже існуючій базі, де vector стоїть у
        # extensions, цей рядок нічого не робить.
        cur.execute("create extension if not exists vector with schema extensions;")
        cur.execute("""
            create table if not exists vacancies (
                id              bigint generated always as identity primary key,
                source          text not null,
                external_id     text,
                keyword         text not null,
                title           text not null,
                company         text,
                description     text,
                url             text not null unique,
                published_at    date,
                city            text,
                salary_min      numeric,
                salary_max      numeric,
                salary_currency text,
                salary_raw      text,
                work_mode       text,
                first_seen_at   timestamptz not null default now(),
                last_seen_at    timestamptz not null default now(),
                is_active       boolean not null default true
            );
        """)
        # На случай апгрейда существующей Supabase-базы, созданной до появления
        # этих полей — create table if not exists их не добавит, дописываем миграцией.
        for col, col_type in (
            ("city", "text"), ("salary_min", "numeric"), ("salary_max", "numeric"),
            ("salary_currency", "text"), ("salary_raw", "text"), ("work_mode", "text"),
        ):
            cur.execute(f"alter table vacancies add column if not exists {col} {col_type};")

        cur.execute("create index if not exists idx_vacancies_source_keyword on vacancies (source, keyword);")
        cur.execute("create index if not exists idx_vacancies_active on vacancies (is_active);")
        cur.execute("create index if not exists idx_vacancies_city on vacancies (city);")
        cur.execute("create index if not exists idx_vacancies_salary on vacancies (salary_max);")
        cur.execute(f"""
            create table if not exists vacancy_chunks (
                id           bigint generated always as identity primary key,
                vacancy_id   bigint not null references vacancies(id) on delete cascade,
                chunk_index  int not null,
                content      text not null,
                embedding    vector({dim}),
                unique (vacancy_id, chunk_index)
            );
        """)
        # ivfflat требует, чтобы в таблице уже были строки для обучения кластеров —
        # на пустой таблице просто пропускаем создание индекса, добавьте его позже
        # руками (`create index ... using ivfflat ...`) когда наберётся данных.

        # Резюме — отдельно от vacancies/vacancy_chunks: разный жизненный цикл
        # (одно резюме на пользователя, не "источник вакансии"), разный смысл
        # is_active, никогда не должны попасть в semantic_search() по вакансиям.
        cur.execute("""
            create table if not exists resumes (
                id            bigint generated always as identity primary key,
                filename      text,
                raw_text      text not null,
                uploaded_at   timestamptz not null default now()
            );
        """)
        cur.execute(f"""
            create table if not exists resume_sections (
                id           bigint generated always as identity primary key,
                resume_id    bigint not null references resumes(id) on delete cascade,
                section      text not null,   -- 'full' | 'skills' | 'experience' | 'education' | 'summary' | 'projects' | 'languages'
                content      text not null,
                embedding    vector({dim}),
                unique (resume_id, section)
            );
        """)

        # Результаты LLM-grounding'а (generate.py): для пары (резюме, вакансия) —
        # оценка релевантности 1-10, обоснование и черновики cover letter,
        # сгенерированные с опорой на реальные тексты резюме+вакансии, а не
        # на семантическую близость эмбеддингов (та даёт distance, не оценку "почему").
        cur.execute("""
            create table if not exists generations (
                id                     bigint generated always as identity primary key,
                resume_id              bigint not null references resumes(id) on delete cascade,
                vacancy_id             bigint not null references vacancies(id) on delete cascade,
                model                  text not null,
                relevance              smallint not null check (relevance between 1 and 10),
                reasoning              text not null,
                cover_letter_sentences jsonb not null,
                created_at             timestamptz not null default now(),
                unique (resume_id, vacancy_id)
            );
        """)
        cur.execute("create index if not exists idx_generations_resume on generations (resume_id);")

        # Полный список настроенных ключевых слов парсера (keywords.csv + keywords_weekly.csv,
        # все дни недели) — не только те, что реально дали результаты в vacancies.keyword.
        # Нужен веб-приложению (isQueryWithinScrapedScope), чтобы не помечать "вне зоны охвата"
        # нишевые ключевые слова, которые скрейпятся, но пока дают 0 результатов.
        cur.execute("""
            create table if not exists scraper_keywords (
                keyword    text primary key,
                updated_at timestamptz not null default now()
            );
        """)
    conn.commit()


def sync_scraper_keywords(conn, keywords: list[str]) -> None:
    """Перезаписывает scraper_keywords полным текущим списком настроенных ключевых слов.
    Вызывается на каждом прогоне sync_supabase.py, чтобы таблица не расходилась
    с keywords.csv/keywords_weekly.csv."""
    with conn.cursor() as cur:
        cur.execute("delete from scraper_keywords;")
        for kw in dict.fromkeys(k.strip() for k in keywords if k and k.strip()):
            cur.execute(
                "insert into scraper_keywords (keyword) values (%s) on conflict (keyword) do nothing;",
                (kw,),
            )
    conn.commit()


def upsert_vacancy(conn, v: dict, commit: bool = True) -> int:
    """v: {source, external_id, keyword, title, company, description, url, published_at,
    city, salary_min, salary_max, salary_currency, salary_raw, scraped_at}.
    Отсутствующие необязательные ключи (city, salary_*) можно не передавать — .get() ниже
    подставит None. Возвращает id записи в Postgres.

    commit=False — для батчевой заливки (см. sync_supabase.py): коммит раз на
    батч из N вакансий, а не на каждую отдельно, экономит N-1 round-trip'ів
    до Postgres. Вызывающий сам отвечает за conn.commit()/rollback()."""
    v = {
        "city": None, "salary_min": None, "salary_max": None,
        "salary_currency": None, "salary_raw": None, "work_mode": None,
        **v,
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into vacancies (source, external_id, keyword, title, company,
                                    description, url, published_at, city,
                                    salary_min, salary_max, salary_currency, salary_raw,
                                    work_mode, first_seen_at, last_seen_at, is_active)
            values (%(source)s, %(external_id)s, %(keyword)s, %(title)s, %(company)s,
                    %(description)s, %(url)s, %(published_at)s, %(city)s,
                    %(salary_min)s, %(salary_max)s, %(salary_currency)s, %(salary_raw)s,
                    %(work_mode)s, %(scraped_at)s, %(scraped_at)s, true)
            on conflict (url) do update set
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
                is_active=true
            returning id
            """,
            v,
        )
        vacancy_id = cur.fetchone()[0]
    if commit:
        conn.commit()
    return vacancy_id


def replace_chunks(
    conn, vacancy_id: int, chunks_with_embeddings: list[tuple[str, list[float]]], commit: bool = True
):
    """Удаляет старые чанки этой вакансии и записывает новые. Вызывать при
    каждом ре-эмбеддинге, чтобы не копить дубли/устаревшие версии текста.

    commit=False — см. upsert_vacancy выше, тот же смысл (батчевая заливка)."""
    with conn.cursor() as cur:
        cur.execute("delete from vacancy_chunks where vacancy_id = %s", (vacancy_id,))
        for idx, (content, vec) in enumerate(chunks_with_embeddings):
            cur.execute(
                """
                insert into vacancy_chunks (vacancy_id, chunk_index, content, embedding)
                values (%s, %s, %s, %s)
                """,
                (vacancy_id, idx, content, _vec_to_pgvector(vec)),
            )
    if commit:
        conn.commit()


def mark_inactive(conn, source: str, keyword: str, seen_urls: set[str]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "select url from vacancies where source=%s and keyword=%s and is_active=true",
            (source, keyword),
        )
        stored = {row[0] for row in cur.fetchall()}
        stale = stored - seen_urls
        if stale:
            cur.execute(
                "update vacancies set is_active=false where url = any(%s)",
                (list(stale),),
            )
    conn.commit()
    return len(stale)


def delete_inactive(conn) -> int:
    """Физически удаляет вакансии, помечені is_active=false (mark_inactive
    только проставляет флаг). vacancy_chunks/generations уносятся каскадно
    (on delete cascade), отдельно чистить их не треба."""
    with conn.cursor() as cur:
        cur.execute("delete from vacancies where is_active=false")
        deleted = cur.rowcount
    conn.commit()
    return deleted


def fetch_active(conn, source: str = None, keyword: str = None) -> list[dict]:
    query = "select source, keyword, title, company, url, published_at, last_seen_at from vacancies where is_active=true"
    params = []
    if source:
        query += " and source=%s"
        params.append(source)
    if keyword:
        query += " and keyword=%s"
        params.append(keyword)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]


def _build_metadata_filters(city: str = None, min_salary: float = None,
                             salary_currency: str = None, published_after: str = None) -> tuple[str, dict]:
    """
    Общий кусок WHERE для фильтрации по metadata — переиспользуется в
    semantic_search() и (через неё) в match_vacancies_for_resume().

    city            — подстрока, регистронезависимо ("Київ" найдёт "м. Київ",
                       "Київська область" и т.п. — это ILIKE %term%, не точное совпадение).
    min_salary      — вакансия проходит, если salary_max >= min_salary. Вакансии
                       без указанной зарплаты (salary_max IS NULL) фильтром
                       исключаются — раз зарплата неизвестна, нельзя утверждать,
                       что она устраивает.
    salary_currency — если задан вместе с min_salary, сравнение идёт ТОЛЬКО среди
                       вакансий в этой валюте (сравнивать 2000 USD и 50000 UAH
                       напрямую как числа было бы неправильно — конвертации курса
                       здесь нет). Если не задан — сравнение идёт по числу как
                       есть, невзирая на валюту; используйте это с осторожностью
                       для смешанных по валюте наборов вакансий.
    published_after — ISO-дата (YYYY-MM-DD), только вакансии не старше неё.
    """
    clauses = []
    params = {}
    if city:
        clauses.append("v.city ilike %(city)s")
        params["city"] = f"%{city}%"
    if min_salary is not None:
        clauses.append("v.salary_max is not null and v.salary_max >= %(min_salary)s")
        params["min_salary"] = min_salary
        if salary_currency:
            clauses.append("v.salary_currency = %(salary_currency)s")
            params["salary_currency"] = salary_currency
    if published_after:
        clauses.append("v.published_at is not null and v.published_at >= %(published_after)s")
        params["published_after"] = published_after
    sql = ("".join(f" and {c}" for c in clauses))
    return sql, params


def semantic_search(
    conn, query_embedding: list[float], top_k: int = 10, active_only: bool = True,
    city: str = None, min_salary: float = None, salary_currency: str = None,
    published_after: str = None,
) -> list[dict]:
    """Косинусный поиск по чанкам (pgvector `<=>` — cosine distance, меньше = ближе),
    возвращает по одной (лучшей) строке на вакансию. См. _build_metadata_filters()
    для семантики city/min_salary/salary_currency/published_after."""
    filter_sql, filter_params = _build_metadata_filters(city, min_salary, salary_currency, published_after)

    sql = """
        select id, source, title, company, url, published_at, city,
               salary_min, salary_max, salary_currency, work_mode, matched_chunk, distance
        from (
            select distinct on (v.id)
                v.id, v.source, v.title, v.company, v.url, v.published_at, v.city,
                v.salary_min, v.salary_max, v.salary_currency, v.work_mode,
                c.content as matched_chunk,
                c.embedding <=> %(q)s as distance
            from vacancy_chunks c
            join vacancies v on v.id = c.vacancy_id
            where 1=1
    """
    if active_only:
        sql += " and v.is_active = true"
    sql += filter_sql
    # ВАЖНО: distinct on (v.id) + "order by v.id, distance" даёт по одной
    # (ближайшей) строке на вакансию — но сортирует получившийся набор по id,
    # а не по расстоянию. LIMIT нельзя вешать на этот внутренний запрос: он
    # обрежет по возрастанию id, а не по релевантности. Сортировка по
    # distance и LIMIT — только во внешнем запросе, после дедупликации.
    sql += """
            order by v.id, distance asc
        ) matched
        order by distance asc
        limit %(limit)s
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, {"q": _vec_to_pgvector(query_embedding), "limit": top_k, **filter_params})
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Резюме: хранение + подбор вакансий
# ---------------------------------------------------------------------------

def upsert_resume(conn, filename: str, raw_text: str) -> int:
    """Резюме — не "вакансия", upsert по url тут не подходит: одно и то же
    резюме можно перезалить (обновилось) — делаем это явной операцией:
    каждый вызов создаёт новую запись. Кто хочет только одно активное резюме
    на пользователя — пусть удаляет старое (delete_resume) перед заливкой нового."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into resumes (filename, raw_text) values (%s, %s) returning id",
            (filename, raw_text),
        )
        resume_id = cur.fetchone()[0]
    conn.commit()
    return resume_id


def replace_resume_sections(conn, resume_id: int, sections_with_embeddings: dict[str, tuple[str, list[float]]]):
    """sections_with_embeddings: {"full": (текст, вектор), "skills": (текст, вектор), ...}"""
    with conn.cursor() as cur:
        cur.execute("delete from resume_sections where resume_id = %s", (resume_id,))
        for section, (content, vec) in sections_with_embeddings.items():
            cur.execute(
                """
                insert into resume_sections (resume_id, section, content, embedding)
                values (%s, %s, %s, %s)
                """,
                (resume_id, section, content, _vec_to_pgvector(vec)),
            )
    conn.commit()


def delete_resume(conn, resume_id: int):
    with conn.cursor() as cur:
        cur.execute("delete from resumes where id = %s", (resume_id,))
    conn.commit()


def list_resumes(conn) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("select id, filename, uploaded_at from resumes order by uploaded_at desc")
        return [dict(r) for r in cur.fetchall()]


def get_resume(conn, resume_id: int) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "select id, filename, raw_text, uploaded_at from resumes where id = %s",
            (resume_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_vacancy(conn, vacancy_id: int) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select id, source, title, company, description, url, published_at, city,
                   salary_min, salary_max, salary_currency, work_mode
            from vacancies where id = %s
            """,
            (vacancy_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_resume_sections(conn, resume_id: int) -> dict[str, list[float]]:
    """{"full": вектор, "skills": вектор, ...} — без текста, для матчинга."""
    with conn.cursor() as cur:
        cur.execute(
            "select section, embedding from resume_sections where resume_id = %s",
            (resume_id,),
        )
        return {row[0]: _pgvector_to_vec(row[1]) for row in cur.fetchall()}


# skills весит больше всего — по опыту ближе всего к тому, что реально ищут
# при подборе вакансии; веса нормализуются по факту присутствующих секций,
# так что если в резюме нет "projects"/"languages" — это не проблема.
DEFAULT_SECTION_WEIGHTS = {
    "skills": 0.45,
    "experience": 0.30,
    "education": 0.10,
    "summary": 0.10,
    "projects": 0.05,
}


def match_vacancies_for_resume(
    conn,
    resume_id: int,
    top_k: int = 20,
    mode: str = "full",
    section_weights: dict[str, float] = None,
    active_only: bool = True,
    city: str = None,
    min_salary: float = None,
    salary_currency: str = None,
    published_after: str = None,
) -> list[dict]:
    """
    mode="full"     — один эмбеддинг всего резюме -> semantic_search() как обычно.
    mode="sections" — отдельные эмбеддинги skills/experience/... взвешенно
                      комбинируются в один скор на вакансию (см. DEFAULT_SECTION_WEIGHTS).
                      Секций, которых в резюме нет, просто не участвуют — веса
                      остальных нормализуются так, чтобы сумма была равна 1.

    city/min_salary/salary_currency/published_after — те же фильтры по metadata,
    что и в semantic_search() (см. _build_metadata_filters), применяются ДО
    ранжирования по косинусной близости — то есть отфильтрованные вакансии не
    отбирают место в top_k у подходящих по metadata.
    """
    search_kwargs = dict(active_only=active_only, city=city, min_salary=min_salary,
                          salary_currency=salary_currency, published_after=published_after)
    with conn.cursor() as cur:
        cur.execute(
            "select section, embedding from resume_sections where resume_id = %s",
            (resume_id,),
        )
        rows = cur.fetchall()
    # psycopg2 не знает тип vector нативно — колонка приходит как текстовый
    # литерал "[0.1,0.2,...]", разбираем его обратно в список float здесь же,
    # чтобы дальше по коду (semantic_search и т.д.) везде были обычные list[float].
    sections = {row[0]: _pgvector_to_vec(row[1]) for row in rows}
    if not sections:
        raise ValueError(f"У резюме id={resume_id} нет ни одной секции с эмбеддингом")

    # Защита от вырожденных векторов (нулевая норма) — cosine distance на них
    # даёт NaN в pgvector (деление на ноль), а один NaN в сумме портит скор
    # ВСЕХ вакансий в mode="sections". У нормальных моделей эмбеддингов такое
    # не должно происходить (текст всегда даёт ненулевой вектор), но на всякий
    # случай просто исключаем такую секцию из матчинга, а не падаем/портим счёт.
    def _is_degenerate(vec: list[float]) -> bool:
        return not vec or sum(x * x for x in vec) < 1e-12

    degenerate = [k for k, v in sections.items() if _is_degenerate(v)]
    for k in degenerate:
        del sections[k]
    if not sections:
        raise ValueError(f"У резюме id={resume_id} все эмбеддинги вырожденные (нулевые) — переиндексируйте резюме")

    if mode == "full" or (mode == "sections" and set(sections) == {"full"}):
        if "full" not in sections:
            raise ValueError("Нет эмбеддинга 'full' для резюме, а mode='full'")
        return semantic_search(conn, sections["full"], top_k=top_k, **search_kwargs)

    if mode != "sections":
        raise ValueError(f"Неизвестный mode={mode!r}, ожидается 'full' или 'sections'")

    weights = dict(section_weights or DEFAULT_SECTION_WEIGHTS)
    present = {k: w for k, w in weights.items() if k in sections and k != "full"}
    if not present:
        # в резюме распознались только нетипичные секции — откатываемся на full
        return semantic_search(conn, sections["full"], top_k=top_k, **search_kwargs)
    total_w = sum(present.values())
    present = {k: w / total_w for k, w in present.items()}

    # для каждой секции — топ-N кандидатов с расстоянием, затем комбинируем в Python.
    # candidate_pool больше top_k: разные секции могут указывать на разные вакансии,
    # объединение кандидатов должно быть шире финального списка.
    candidate_pool = max(top_k * 5, 100)
    scores: dict[int, dict] = {}
    for section, weight in present.items():
        rows = semantic_search(conn, sections[section], top_k=candidate_pool, **search_kwargs)
        for r in rows:
            vid = r["id"]
            entry = scores.setdefault(vid, {
                "id": vid, "source": r["source"], "title": r["title"], "company": r["company"],
                "url": r["url"], "published_at": r["published_at"], "matched_chunk": r["matched_chunk"],
                "city": r.get("city"), "salary_min": r.get("salary_min"),
                "salary_max": r.get("salary_max"), "salary_currency": r.get("salary_currency"),
                "work_mode": r.get("work_mode"),
                "_weighted_sum": 0.0, "_weight_seen": 0.0, "_by_section": {},
            })
            entry["_weighted_sum"] += weight * r["distance"]
            entry["_weight_seen"] += weight
            entry["_by_section"][section] = round(r["distance"], 4)
            # держим сниппет из секции с наибольшим весом, где вакансия встретилась
            if weight == max(present[s] for s in entry["_by_section"]):
                entry["matched_chunk"] = r["matched_chunk"]

    results = []
    for entry in scores.values():
        # вакансия, найденная не по всем секциям — это нормально (например,
        # совпала по skills, но не попала в топ-N по experience); штрафуем
        # недостающий вес нейтральной дистанцией 1.0 (macos: ортогональные вектора),
        # чтобы такие вакансии не обгоняли те, что совпали по всем секциям без причины.
        missing_w = 1.0 - entry["_weight_seen"]
        score = entry["_weighted_sum"] + missing_w * 1.0
        results.append({
            "id": entry["id"], "source": entry["source"], "title": entry["title"],
            "company": entry["company"], "url": entry["url"], "published_at": entry["published_at"],
            "city": entry["city"], "salary_min": entry["salary_min"],
            "salary_max": entry["salary_max"], "salary_currency": entry["salary_currency"],
            "work_mode": entry["work_mode"],
            "matched_chunk": entry["matched_chunk"], "distance": round(score, 4),
            "by_section": entry["_by_section"],
        })

    results.sort(key=lambda r: r["distance"])
    return results[:top_k]


# ---------------------------------------------------------------------------
# LLM-grounding (generate.py): оценки релевантности + cover letter по паре
# (резюме, вакансия)
# ---------------------------------------------------------------------------

def upsert_generation(conn, resume_id: int, vacancy_id: int, model: str,
                       relevance: int, reasoning: str, cover_letter_sentences: list[str]) -> int:
    """Одна запись на (resume_id, vacancy_id) — повторный запуск (--force в
    generate.py) перезаписывает предыдущий результат, а не плодит дубли."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into generations (resume_id, vacancy_id, model, relevance, reasoning, cover_letter_sentences)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (resume_id, vacancy_id) do update set
                model=excluded.model,
                relevance=excluded.relevance,
                reasoning=excluded.reasoning,
                cover_letter_sentences=excluded.cover_letter_sentences,
                created_at=now()
            returning id
            """,
            (resume_id, vacancy_id, model, relevance, reasoning, psycopg2.extras.Json(cover_letter_sentences)),
        )
        generation_id = cur.fetchone()[0]
    conn.commit()
    return generation_id


def get_generation(conn, resume_id: int, vacancy_id: int) -> dict | None:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select id, resume_id, vacancy_id, model, relevance, reasoning, cover_letter_sentences, created_at
            from generations where resume_id = %s and vacancy_id = %s
            """,
            (resume_id, vacancy_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_generations(conn, resume_id: int, limit: int = 20) -> list[dict]:
    """Сохранённые оценки для резюме, отсортированные по релевантности
    (лучшие сверху), с подтянутыми title/company/source/url вакансии."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            select g.id, g.vacancy_id, g.model, g.relevance, g.reasoning,
                   g.cover_letter_sentences, g.created_at,
                   v.title, v.company, v.source, v.url
            from generations g
            join vacancies v on v.id = g.vacancy_id
            where g.resume_id = %s
            order by g.relevance desc, g.created_at desc
            limit %s
            """,
            (resume_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def _vec_to_pgvector(vec: list[float]) -> str:
    """psycopg2 не знает тип vector нативно — передаём его как текстовый литерал
    '[0.1,0.2,...]', pgvector сам приводит text -> vector на входе."""
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def _pgvector_to_vec(raw: str) -> list[float]:
    """Обратное преобразование: то, что pgvector отдаёт психопгу для колонки
    vector — обычный текст "[0.1,0.2,...]" (без адаптера типа), а не list."""
    if raw is None:
        return []
    return [float(x) for x in raw.strip("[]").split(",")]
