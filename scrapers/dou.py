"""jobs.dou.ua — полностью IT-специализированный сайт."""
from urllib.parse import quote

from models import Vacancy
from .base import parse_ua_date, clean_text

SOURCE = "dou"
LIST_URL = "https://jobs.dou.ua/vacancies/?search={q}"


def scrape(page, keyword: str, max_items: int = 60) -> list[Vacancy]:
    page.goto(LIST_URL.format(q=quote(keyword)), wait_until="domcontentloaded")
    page.wait_for_timeout(1000)

    # dou подгружает вакансии по клику "Показати ще" (AJAX), а не пагинацией через URL
    for _ in range(6):
        if len(page.query_selector_all("li.l-vacancy")) >= max_items:
            break
        more = page.query_selector("div.more-btn a")
        if not more or not more.is_visible():
            break
        more.click()
        page.wait_for_timeout(800)

    items = page.query_selector_all("li.l-vacancy")[:max_items]
    out = []
    for it in items:
        title_el = it.query_selector("a.vt")
        company_el = it.query_selector("a.company")
        date_el = it.query_selector("div.date")
        if not title_el:
            continue
        url = (title_el.get_attribute("href") or "").split("?")[0]
        if not url:
            continue
        external_id = url.rstrip("/").split("/")[-1]
        out.append(Vacancy(
            source=SOURCE,
            keyword=keyword,
            title=clean_text(title_el.inner_text()),
            company=clean_text(company_el.inner_text()) if company_el else "",
            description="",  # добирается отдельно, см. fetch_description
            url=url,
            published_at=parse_ua_date(date_el.inner_text()) if date_el else None,
            external_id=external_id,
        ))
    return out


def fetch_description(page, url: str) -> str:
    page.goto(url, wait_until="domcontentloaded")
    el = page.query_selector("div.b-typo.vacancy-section")
    return clean_text(el.inner_text()) if el else ""
