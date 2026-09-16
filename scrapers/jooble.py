"""
ua.jooble.org — агрегатор (не специализирован на IT, фильтруем ключевым словом).
Описание в карточке — короткий сниппет, не полный текст: jooble ведёт на
внешний сайт-первоисточник, у которого своя структура на каждый случай.

ВАЖНО: параметр ukw= на jooble фильтрует очень нестрого (проверено вручную —
запрос "ai automation" возвращает тысячи вакансий без AI/automation в названии
вообще, похоже на общую ленту с приоритизацией, а не точный фильтр). Поэтому
здесь дополнительно отсекаем на своей стороне карточки, где ни title, ни
сниппет описания не содержат ни одного слова из ключевой фразы.
"""
from urllib.parse import quote

from models import Vacancy
from .base import clean_text

SOURCE = "jooble"
LIST_URL = "https://ua.jooble.org/SearchResult?ukw={q}"


def _is_relevant(text: str, keyword_tokens: list[str]) -> bool:
    low = text.lower()
    return any(tok in low for tok in keyword_tokens)


def scrape(page, keyword: str, max_items: int = 60) -> list[Vacancy]:
    keyword_tokens = [t for t in keyword.lower().split() if len(t) >= 2]

    page.goto(LIST_URL.format(q=quote(keyword)), wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

    for _ in range(4):
        if len(page.query_selector_all('[data-test-name="_jobCard"]')) >= max_items:
            break
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(700)

    cards = page.query_selector_all('[data-test-name="_jobCard"]')[:max_items]
    out = []
    for c in cards:
        link_el = c.query_selector("a.job_card_link")
        if not link_el:
            continue
        href = link_el.get_attribute("href") or ""
        if not href:
            continue
        url_full = href.split("?")[0] if href.startswith("http") else f"https://ua.jooble.org{href.split('?')[0]}"
        external_id = c.get_attribute("id") or url_full.rstrip("/").split("/")[-1]

        desc_el = None
        for div in c.query_selector_all("div"):
            txt = div.inner_text().strip()
            if len(txt) > 150:
                desc_el = div
                break

        title = clean_text(link_el.inner_text())
        description = clean_text(desc_el.inner_text()) if desc_el else ""
        if keyword_tokens and not _is_relevant(title + " " + description, keyword_tokens):
            continue

        out.append(Vacancy(
            source=SOURCE,
            keyword=keyword,
            title=title,
            company="",  # jooble на карточке компанию отдельно не подписывает у большинства объявлений
            description=description,
            url=url_full,
            published_at=None,  # не показывается в выдаче стабильно
            external_id=external_id,
        ))
    return out
