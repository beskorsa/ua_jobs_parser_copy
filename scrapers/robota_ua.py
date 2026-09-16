"""
robota.ua (бренд rabota.ua редиректит сюда) — Angular SPA, полностью CSR.
Список вакансий не отдаёт company/description — их можно взять только
с детальной страницы, поэтому туда так или иначе приходится заходить.
"""
import re
from urllib.parse import quote

from models import Vacancy
from .base import clean_text, parse_ua_date

SOURCE = "robota_ua"
LIST_URL = "https://robota.ua/zapros/{q}/ukraine"


def scrape(page, keyword: str, max_items: int = 60) -> list[Vacancy]:
    # "networkidle" тут ненадёжен: robota.ua постоянно шлёт аналитику/бикон-запросы
    # в фоне, сеть никогда не "затихает" — Playwright ждёт networkidle до таймаута
    # и падает с TimeoutError, весь сайт молча пропускается. Ждём вместо этого
    # появления самих карточек.
    page.goto(LIST_URL.format(q=quote(keyword)), wait_until="domcontentloaded")
    try:
        page.wait_for_selector("alliance-vacancy-card-desktop", timeout=10000)
    except Exception:
        pass  # возможно, по этому запросу вакансий просто нет — проверим ниже по факту

    # подгрузка следующих карточек через скролл (infinite scroll)
    for _ in range(5):
        if len(page.query_selector_all("alliance-vacancy-card-desktop")) >= max_items:
            break
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(700)

    cards = page.query_selector_all("alliance-vacancy-card-desktop")[:max_items]
    out = []
    for c in cards:
        link_el = c.query_selector("a.card")
        title_el = c.query_selector("h2")
        if not link_el or not title_el:
            continue
        href = link_el.get_attribute("href") or ""
        if not href:
            continue
        url_full = href if href.startswith("http") else f"https://robota.ua{href}"
        m = re.search(r"vacancy(\d+)", href)
        external_id = m.group(1) if m else href.strip("/").split("/")[-1]
        out.append(Vacancy(
            source=SOURCE,
            keyword=keyword,
            title=clean_text(title_el.inner_text()),
            company="",       # добирается с детальной страницы
            description="",   # добирается с детальной страницы
            url=url_full,
            published_at=None,
            external_id=external_id,
        ))
    return out


def fetch_description(page, url: str) -> tuple[str, str, str | None]:
    """Возвращает (описание, компания, дата публикации)."""
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector("h1", timeout=10000)
    except Exception:
        pass

    h1 = page.query_selector("h1")
    company = ""
    if h1:
        # первая непустая ссылка рядом с заголовком — обычно ссылка на карточку компании
        handle = h1.evaluate_handle("el => el.closest('div')")
        container = handle.as_element()
        if container:
            a = container.query_selector("a")
            if a:
                company = clean_text(a.inner_text())

    body_text = page.inner_text("body")
    published_at = None
    m = re.search(r"(\d{1,2}\s+[а-яіїєґ]+\s+\d{4})", body_text.lower())
    if m:
        published_at = parse_ua_date(m.group(1))

    # узкого селектора под "описание вакансии" на robota.ua не нашлось (весь контент
    # рендерится внутри одного lib-content) — берём его целиком; в тексте будут также
    # заголовок/локация/контакты, это ограничение сайта, а не бага парсера.
    content_el = page.query_selector("lib-content")
    description = clean_text(content_el.inner_text()) if content_el else clean_text(body_text)

    return description, company, published_at
