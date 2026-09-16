"""Data model for a single scraped vacancy."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Vacancy:
    source: str            # site key: dou, djinni, work_ua, robota_ua, jooble, grc
    keyword: str            # search keyword that found this vacancy
    title: str
    company: str
    description: str
    url: str                 # canonical vacancy URL, used as dedup key
    published_at: Optional[str] = None   # ISO date string (YYYY-MM-DD) if parseable, else raw text
    external_id: Optional[str] = None     # site-native vacancy id, if extractable from the URL
    # city/salary_* — под фильтры, которые уже умеют postgres_store.py/
    # schema_postgres.sql. Ни один scraper их пока не заполняет (парсинг
    # зарплаты/города по сайтам — отдельная доработка) — остаются None,
    # это не баг, просто фича ещё не реализована на стороне scrapers/*.
    city: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_raw: Optional[str] = None
    # 'remote' | 'office' | 'hybrid' | None — заповнюється в main.py після
    # fetch_description() евристикою classify_work_mode() (scrapers/base.py).
    work_mode: Optional[str] = None
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def key(self) -> str:
        return self.url
