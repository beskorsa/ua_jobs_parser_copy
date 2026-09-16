# Changelog

Версионирование: [SemVer](https://semver.org/) (MAJOR.MINOR.PATCH).

## v1.1.0

- Автоочистка неактивных вакансий: `sync_supabase.py` после каждого синка
  физически удаляет из Postgres записи, помеченные `is_active=false`
  (раньше они только помечались флагом и накапливались навсегда).
  Новая функция `postgres_store.delete_inactive()`.
- Список ключевых слов (`keywords.csv`) сужен до AI/ML/automation-ниши
  (~26 запросов вместо 226 общих).

## v1.0.0

- Первое рабочее состояние проекта на момент заведения git-репозитория:
  скраперы (work.ua, robota.ua, dou.ua, djinni.co, jooble, grc), синк в
  SQLite → Supabase/Postgres с pgvector-эмбеддингами, скоринг резюме,
  генерация cover letter, watchdog-обёртка, авто-подбор ключевых слов
  (`suggest_keywords.py`, `auto_refresh.py`).
