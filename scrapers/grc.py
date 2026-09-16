"""
grc.ua — общий сайт (не IT-специализированный), но есть категория
"Інформаційні технології, інтернет, телеком" в фильтре. Поиск по ключевому
слову — через маршрут /searchjob/k-<keyword>.
Карточки в списке — React, без <a href>: попадание на детальную страницу
возможно только реальным кликом (открывается в новой вкладке).
"""
import re
from urllib.parse import quote

from models import Vacancy
from .base import clean_text

SOURCE = "grc"
LIST_URL = "https://grc.ua/searchjob/k-{q}"


def scrape(page, keyword: str, max_items: int = 60) -> list[Vacancy]:
    page.goto(LIST_URL.format(q=quote(keyword)), wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

    out = []
    page_num = 1
    while len(out) < max_items and page_num <= 5:
        cards = page.query_selector_all("div.card.Jcard")
        if not cards:
            break
        for c in cards:
            title_el = c.query_selector("h2.JcardTitle")
            company_el = c.query_selector("#jobcard-company-name")
            if not title_el:
                continue

            # реальный клик — открывает детальную страницу в новой вкладке (target=_blank)
            with page.context.expect_page(timeout=5000) as new_page_info:
                title_el.click()
            detail = new_page_info.value
            detail.wait_for_load_state("domcontentloaded")
            # React-контент рендерится ПОСЛЕ domcontentloaded — без явного ожидания
            # описание почти всегда ловится пустым. Ждём сам блок с текстом.
            try:
                detail.wait_for_selector("div.JobDescriptionBx", timeout=8000)
            except Exception:
                pass
            url_full = detail.url
            desc_el = detail.query_selector("div.JobDescriptionBx")
            description = clean_text(desc_el.inner_text()) if desc_el else ""

            # дата публикации на grc.ua нигде не подписана явно в UI, но зашита
            # в <title> вкладки: "... створена DD.MM.YYYY | GRC.ua"
            published_at = None
            m = re.search(r"створена (\d{2})\.(\d{2})\.(\d{4})", detail.title())
            if m:
                published_at = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

            detail.close()

            external_id = url_full.rstrip("/").split("/")[-1]
            out.append(Vacancy(
                source=SOURCE,
                keyword=keyword,
                title=clean_text(title_el.inner_text()),
                company=clean_text(company_el.inner_text()) if company_el else "",
                description=description,
                url=url_full,
                published_at=published_at,
                external_id=external_id,
            ))
            if len(out) >= max_items:
                break

        next_btn = page.query_selector("button:has-text('Наступна')")
        if not next_btn or not next_btn.is_enabled():
            break
        next_btn.click()
        page.wait_for_timeout(1000)
        page_num += 1

    return out[:max_items]
