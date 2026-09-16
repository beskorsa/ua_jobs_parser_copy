"""
Парсер IT-вакансий украинских джоб-сайтов по ключевым словам.

Использование:
    python main.py                       # по расписанию: keywords.csv (IT/AI, каждый день)
                                          # + сегодняшняя категория из keywords_weekly.csv
    python main.py --all-weekly          # keywords.csv + ВСЕ категории keywords_weekly.csv сразу
                                          # (например, для ручного разового полного прогона)
    python main.py --day wed             # прогнать так, будто сегодня среда (для теста расписания)
    python main.py --keywords "ai automation,python developer"
                                          # явный список — расписание полностью игнорируется
    python main.py --sources dou,djinni --max-items 30
    python main.py --headful             # показать браузер (для отладки)
    python main.py --sequential          # старое поведение — один браузер, сайты по очереди

Расписание (keywords_weekly.csv):
    IT/AI-ключевые слова (keywords.csv) скрейпятся КАЖДЫЙ день — это приоритет.
    Остальные профессии (keywords_weekly.csv, колонка day: mon/tue/wed/thu/fri/sat/sun)
    разбиты по категориям и разнесены по дням недели, чтобы не гонять весь
    огромный список каждый раз — раз в неделю по кругу. День определяется по
    системной дате машины (--day переопределяет для теста/ручного запуска).

Что делает:
    1. Для каждого (сайт, ключевое слово) заходит в поиск сайта и собирает
       карточки вакансий: title, company, url, дата (где сайт её отдаёт).
    2. Для сайтов, где полного описания нет в списке (dou, djinni, work.ua,
       robota.ua), дополнительно открывает каждую вакансию и забирает описание.
    3. Сохраняет всё в SQLite (vacancies.db) через upsert — активные вакансии,
       которых сегодня не было в выдаче поиска, помечаются is_active=0
       (закрыты/удалены с сайта), но не удаляются из базы.
    4. В конце печатает сводку и путь к базе.

Параллелизм:
    По умолчанию каждый источник (сайт) обрабатывается в СВОЁМ потоке со своим
    собственным браузером Playwright — источники не ждут друг друга, весь
    прогон занимает время самого медленного сайта, а не суммы всех. Ключевые
    слова внутри одного источника по-прежнему идут последовательно (сайт не
    любит параллельные запросы с одного и того же браузера/IP).

    Добавление нового источника в будущем ничего здесь не требует менять —
    достаточно зарегистрировать его в scrapers/__init__.py (REGISTRY), он
    автоматически попадёт в пул потоков наравне с остальными.
"""
import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from threading import Lock

from playwright.sync_api import sync_playwright

import db
from scrapers import REGISTRY
from scrapers.base import classify_work_mode, is_closed_vacancy_text, is_relevant_to_keyword

DEFAULT_KEYWORDS_FILE = Path(__file__).parent / "keywords.csv"
DEFAULT_WEEKLY_KEYWORDS_FILE = Path(__file__).parent / "keywords_weekly.csv"
WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_print_lock = Lock()


def _log(msg: str) -> None:
    # Несколько потоков пишут в stdout одновременно — без лока строки от
    # разных источников могут перемешаться посимвольно.
    with _print_lock:
        print(msg)


def load_keywords(path: Path) -> list[str]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row["keyword"].strip() for row in reader if row.get("keyword", "").strip()]


def current_weekday_code() -> str:
    """Понедельник='mon' ... воскресенье='sun', по системной дате машины
    (у нас это Europe/Kyiv — часовой пояс пользователя)."""
    return WEEKDAY_CODES[date.today().weekday()]


def load_weekly_keywords(path: Path, day_code: str | None = None) -> list[str]:
    """Ключевые слова НЕ-IT категорий (см. docstring модуля) — если day_code
    задан, только строки этого дня; day_code=None — все строки файла сразу
    (используется с --all-weekly для ручного полного прогона)."""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        out = []
        for row in reader:
            kw = row.get("keyword", "").strip()
            if not kw:
                continue
            if day_code is not None and row.get("day", "").strip().lower() != day_code:
                continue
            out.append(kw)
        return out


def enrich_description(source: str, page, vacancies: list) -> set[str]:
    """Донабирает description (и иногда company/published_at) отдельным заходом
    на детальную страницу — только там, где это нужно (dou, djinni, work_ua, robota_ua).

    Возвращает множество URL вакансий, чей текст на детальной странице сам
    сообщает, что вакансия уже закрыта/неактуальна/в архиве (is_closed_vacancy_text)
    — такие вакансии затем исключаются из upsert и из seen_urls для mark_inactive
    в run_source(), чтобы не сохранять и не держать активными закрытые вакансии,
    даже если они всё ещё попадаются в выдаче поиска."""
    module = REGISTRY[source]
    closed_urls: set[str] = set()

    if source == "dou":
        for v in vacancies:
            try:
                v.description = module.fetch_description(page, v.url)
                if is_closed_vacancy_text(v.description):
                    closed_urls.add(v.url)
            except Exception as e:
                print(f"  [dou] описание не получено для {v.url}: {e}", file=sys.stderr)

    elif source == "work_ua":
        for v in vacancies:
            try:
                v.description = module.fetch_description(page, v.url)
                if is_closed_vacancy_text(v.description):
                    closed_urls.add(v.url)
            except Exception as e:
                print(f"  [work_ua] описание не получено для {v.url}: {e}", file=sys.stderr)

    elif source == "djinni":
        for v in vacancies:
            try:
                desc, published_at = module.fetch_description(page, v.url)
                v.description = desc
                if published_at:
                    v.published_at = published_at
                if is_closed_vacancy_text(desc):
                    closed_urls.add(v.url)
            except Exception as e:
                print(f"  [djinni] описание не получено для {v.url}: {e}", file=sys.stderr)

    elif source == "robota_ua":
        for v in vacancies:
            try:
                desc, company, published_at = module.fetch_description(page, v.url)
                v.description = desc
                if company:
                    v.company = company
                if published_at:
                    v.published_at = published_at
                if is_closed_vacancy_text(desc):
                    closed_urls.add(v.url)
            except Exception as e:
                print(f"  [robota_ua] описание не получено для {v.url}: {e}", file=sys.stderr)

    # jooble и grc уже приходят с описанием из scrape()

    return closed_urls


def run_source(source: str, keywords: list[str], max_items: int, headless: bool) -> tuple[int, int]:
    """Один источник — свой Playwright/браузер/контекст с нуля до конца, чтобы
    поток не делил состояние браузера с другими источниками. Ключевые слова
    внутри источника — последовательно (вежливая пауза, чтобы не словить бан
    по частоте запросов с одного и того же контекста/IP)."""
    module = REGISTRY[source]
    saved_total = 0
    deactivated_total = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            locale="uk-UA",
        )
        page = context.new_page()

        for keyword in keywords:
            _log(f"[{source}] поиск по '{keyword}'...")
            try:
                vacancies = module.scrape(page, keyword, max_items=max_items)
            except Exception as e:
                _log(f"  [{source}] ошибка сбора списка по '{keyword}': {e}")
                continue

            _log(f"  [{source}] найдено {len(vacancies)} вакансий по '{keyword}', добираю описания...")
            closed_urls = enrich_description(source, page, vacancies)

            # work_mode — по title+description, уже после enrich_description
            # (для dou/work_ua/djinni/robota_ua описание до этого момента
            # пустое; jooble/grc приходят с описанием сразу из scrape()).
            for v in vacancies:
                v.work_mode = classify_work_mode(v.title, v.description)

            # Вакансии, чей текст сам сообщает "закрыта/неактуальна/в архиве" —
            # не сохраняем как активные и не считаем "увиденными" для
            # mark_inactive ниже (тем самым уже существующая в базе активная
            # запись с этим url будет помечена is_active=false, как если бы
            # вакансия вовсе пропала из выдачи).
            open_vacancies = [v for v in vacancies if v.url not in closed_urls]
            if closed_urls:
                _log(f"  [{source}] распознано как закрытые по тексту: {len(closed_urls)}")

            # Пост-фильтр релевантности (см. is_relevant_to_keyword в
            # scrapers/base.py) — некоторые источники (grc, иногда dou) на
            # нишевые keywords.csv-запросы возвращают вакансии не по теме
            # вместо пустой выдачи; без этой проверки такой мусор молча
            # сохранялся бы как будто реально найден по этому keyword.
            relevant_vacancies = [
                v for v in open_vacancies if is_relevant_to_keyword(v.title, v.description, keyword)
            ]
            irrelevant_count = len(open_vacancies) - len(relevant_vacancies)
            if irrelevant_count:
                _log(f"  [{source}] отброшено как нерелевантные keyword '{keyword}': {irrelevant_count}")

            saved = db.upsert_vacancies(relevant_vacancies)
            seen_urls = {v.url for v in relevant_vacancies}
            deactivated = db.mark_inactive(source, keyword, seen_urls)
            saved_total += saved
            deactivated_total += deactivated
            _log(f"  [{source}] сохранено/обновлено: {saved}, помечено неактивными: {deactivated}")
            time.sleep(1)  # вежливая пауза между запросами внутри одного источника

        browser.close()

    return saved_total, deactivated_total


def run(sources: list[str], keywords: list[str], max_items: int, headless: bool, sequential: bool = False):
    db.init_db()
    total_saved = 0
    total_deactivated = 0

    known_sources = []
    for source in sources:
        if source not in REGISTRY:
            print(f"Неизвестный источник: {source}", file=sys.stderr)
            continue
        known_sources.append(source)

    if sequential or len(known_sources) <= 1:
        for source in known_sources:
            saved, deactivated = run_source(source, keywords, max_items, headless)
            total_saved += saved
            total_deactivated += deactivated
    else:
        # По потоку на источник — Playwright sync API не потокобезопасен
        # между потоками для ОДНОГО браузера, но отдельные инстансы в разных
        # потоках работают независимо. Число воркеров = число источников:
        # добавишь новый сайт в REGISTRY — он просто получит свой поток,
        # ничего в этой функции менять не нужно.
        with ThreadPoolExecutor(max_workers=len(known_sources)) as executor:
            futures = {
                executor.submit(run_source, source, keywords, max_items, headless): source
                for source in known_sources
            }
            for future in as_completed(futures):
                source = futures[future]
                try:
                    saved, deactivated = future.result()
                except Exception as e:
                    _log(f"[{source}] источник упал целиком: {e}")
                    continue
                total_saved += saved
                total_deactivated += deactivated

    print(f"\nГотово. Всего сохранено/обновлено записей: {total_saved}, "
          f"помечено неактивными: {total_deactivated}")
    print(f"База: {db.DB_PATH}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keywords", help="ключевые слова через запятую (иначе — по расписанию, см. docstring)")
    parser.add_argument("--all-weekly", action="store_true",
                         help="взять keywords.csv + ВСЕ категории keywords_weekly.csv разом, "
                              "а не только сегодняшнюю (игнорируется, если задан --keywords)")
    parser.add_argument("--day", choices=WEEKDAY_CODES,
                         help="считать, что сегодня этот день недели (для теста расписания; "
                              "по умолчанию — реальная системная дата)")
    parser.add_argument("--sources", help="сайты через запятую: dou,djinni,work_ua,robota_ua,jooble,grc (иначе — все)")
    parser.add_argument("--max-items", type=int, default=40, help="макс. вакансий на пару (сайт, ключевое слово)")
    parser.add_argument("--headful", action="store_true", help="показывать браузер (по умолчанию headless)")
    parser.add_argument("--sequential", action="store_true",
                         help="старое поведение — один браузер, источники по очереди (для отладки)")
    args = parser.parse_args()

    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",")]
    else:
        # IT/AI — приоритет, скрейпится каждый день независимо от расписания.
        it_keywords = load_keywords(DEFAULT_KEYWORDS_FILE)
        day_code = None if args.all_weekly else (args.day or current_weekday_code())
        weekly_keywords = load_weekly_keywords(DEFAULT_WEEKLY_KEYWORDS_FILE, day_code)
        keywords = it_keywords + weekly_keywords
        if not args.all_weekly:
            _log(f"Расписание на сегодня ({day_code}): {len(it_keywords)} IT/AI-ключей (ежедневно) "
                 f"+ {len(weekly_keywords)} по категориям дня.")

    if not keywords:
        print("Нет ключевых слов: заполните keywords.csv/keywords_weekly.csv или передайте --keywords",
              file=sys.stderr)
        sys.exit(1)

    sources = [s.strip() for s in args.sources.split(",")] if args.sources else list(REGISTRY.keys())

    run(sources, keywords, args.max_items, headless=not args.headful, sequential=args.sequential)


if __name__ == "__main__":
    main()
