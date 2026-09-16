"""djinni.co — полностью IT-специализированный сайт."""
from urllib.parse import quote

from models import Vacancy
from .base import clean_text

SOURCE = "djinni"
# ВАЖНО: параметр "keywords" в URL сайтом тихо игнорируется (проверено вручную —
# отдаёт дефолтную ленту вакансий независимо от значения). Реальное поле формы
# поиска называется "all_keywords" (ищет вакансии, где встречаются ВСЕ слова).
LIST_URL = "https://djinni.co/jobs/?all_keywords={q}"


def scrape(page, keyword: str, max_items: int = 60) -> list[Vacancy]:
    out = []
    page_num = 1
    while len(out) < max_items and page_num <= 5:
        url = LIST_URL.format(q=quote(keyword))
        if page_num > 1:
            url += f"&page={page_num}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(800)

        items = page.query_selector_all("ul.list-jobs li.list-jobs__item, div.job-item")
        if not items:
            break
        for it in items:
            link_el = it.query_selector("a.job_item__header-link")
            title_el = it.query_selector("h2.job-item__position")
            company_el = it.query_selector("span.text-gray-800")
            if not link_el or not title_el:
                continue
            href = (link_el.get_attribute("href") or "").split("?")[0]
            if not href:
                continue
            url_full = href if href.startswith("http") else f"https://djinni.co{href}"
            external_id = href.strip("/").split("/")[-1].split("-")[0]
            out.append(Vacancy(
                source=SOURCE,
                keyword=keyword,
                title=clean_text(title_el.inner_text()),
                company=clean_text(company_el.inner_text()) if company_el else "",
                description="",
                url=url_full,
                published_at=None,  # дата берётся с детальной страницы, см. fetch_description
                external_id=external_id,
            ))
        page_num += 1
        if len(items) < 10:
            break
    return out[:max_items]


def fetch_description(page, url: str) -> tuple[str, str | None]:
    """Возвращает (описание, дата публикации) — обе доступны только на детальной странице."""
    from .base import parse_ua_date
    import re

    page.goto(url, wait_until="domcontentloaded")
    desc_el = page.query_selector("div.job-post__description")
    description = clean_text(desc_el.inner_text()) if desc_el else ""

    published_at = None
    body_text = page.inner_text("body")
    m = re.search(r"(\d{1,2}\s+[а-яіїєґ]+\s+\d{4})", body_text.lower())
    if m:
        published_at = parse_ua_date(m.group(1))
    return description, published_at
