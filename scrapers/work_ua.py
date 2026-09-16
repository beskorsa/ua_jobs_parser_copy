"""work.ua — общий сайт, поиск по ключевому слову работает через /jobs-<keyword>/."""
from models import Vacancy
from .base import clean_text, parse_ua_date

SOURCE = "work_ua"
LIST_URL = "https://www.work.ua/jobs-{q}/"


def scrape(page, keyword: str, max_items: int = 60) -> list[Vacancy]:
    out = []
    page_num = 1
    # work.ua ждёт литеральный "+" между словами в самом слаге URL, а не %20/%2B —
    # поэтому здесь НЕЛЬЗЯ применять urllib.parse.quote() поверх (иначе "+"
    # закодируется в "%2B" и work.ua вернёт 404 / пустую выдачу).
    # "/" в keyword (например "ai/ml") ломает путь URL — превращает его в лишний
    # сегмент (jobs-ai/ml/ вместо jobs-ai+ml/), из-за чего work.ua отдаёт 404/пусто.
    slug = keyword.strip().replace("/", " ").replace(" ", "+")
    while len(out) < max_items and page_num <= 5:
        url = LIST_URL.format(q=slug)
        if page_num > 1:
            url += f"?page={page_num}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(800)

        items = page.query_selector_all("div.job-link")
        if not items:
            break
        for it in items:
            title_link = it.query_selector("h2 a")
            if not title_link:
                continue
            href = title_link.get_attribute("href") or ""
            if not href:
                continue
            url_full = href if href.startswith("http") else f"https://www.work.ua{href}"

            company_icon = it.query_selector("span.glyphicon-company")
            company = ""
            if company_icon:
                block = company_icon.evaluate_handle("el => el.closest('div')")
                strong = block.as_element().query_selector(".strong-600") if block.as_element() else None
                if strong:
                    company = clean_text(strong.inner_text())

            time_el = it.query_selector("time")
            published_at = parse_ua_date(time_el.inner_text()) if time_el else None

            external_id = href.strip("/").split("/")[-1]
            out.append(Vacancy(
                source=SOURCE,
                keyword=keyword,
                title=clean_text(title_link.inner_text()),
                company=company,
                description="",
                url=url_full,
                published_at=published_at,
                external_id=external_id,
            ))
        page_num += 1
        if len(items) < 7:
            break
    return out[:max_items]


def fetch_description(page, url: str) -> str:
    page.goto(url, wait_until="domcontentloaded")
    el = page.query_selector("#job-description")
    return clean_text(el.inner_text()) if el else ""
