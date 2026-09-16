-- Схема для Supabase/Postgres. Применять руками не обязательно —
-- postgres_store.ensure_schema() создаёт то же самое автоматически при
-- первом запуске sync_supabase.py. Этот файл — для тех, кто предпочитает
-- катить миграции вручную через Supabase SQL Editor, и как документация.
--
-- Реальная колонка с эмбеддингом на вакансию НЕ хранится — вместо неё
-- вынесенная таблица vacancy_chunks (описание режется на чанки, у каждого
-- свой вектор — так работает нормальный поиск по длинным текстам).
-- Замените vector(384) на vector(1536), если EMBEDDING_PROVIDER=openai
-- (text-embedding-3-small отдаёт 1536-мерные вектора).

-- schema extensions, не public — інакше Supabase Security Advisor позначає
-- як "Extension in Public" (розширення потрапляє в публічний PostgREST API).
-- "extensions" вже в дефолтному search_path Supabase, тому тип vector і
-- оператори (<=>, <->) лишаються доступні без змін у запитах нижче.
create extension if not exists vector with schema extensions;

create table if not exists vacancies (
    id            bigint generated always as identity primary key,
    source        text not null,              -- dou | djinni | work_ua | robota_ua | jooble | grc
    external_id   text,
    keyword       text not null,
    title         text not null,
    company       text,
    description   text,
    url           text not null unique,
    published_at  date,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    is_active     boolean not null default true
);

create index if not exists idx_vacancies_source_keyword on vacancies (source, keyword);
create index if not exists idx_vacancies_active on vacancies (is_active);

create table if not exists vacancy_chunks (
    id           bigint generated always as identity primary key,
    vacancy_id   bigint not null references vacancies(id) on delete cascade,
    chunk_index  int not null,
    content      text not null,
    embedding    vector(384),                 -- 384: bge-small / multilingual-e5-small; 1536: OpenAI text-embedding-3-small
    unique (vacancy_id, chunk_index)
);

-- Индекс под приблизительный ANN-поиск (ivfflat) — создавать ПОСЛЕ того, как
-- в vacancy_chunks появятся данные (на пустой таблице кластеры обучить нечем):
-- create index idx_vacancy_chunks_embedding
--     on vacancy_chunks using ivfflat (embedding vector_cosine_ops)
--     with (lists = 100);
