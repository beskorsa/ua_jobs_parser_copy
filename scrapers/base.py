"""Общие утилиты для всех скрейперов."""
import re
from datetime import date, timedelta

UA_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}


def parse_ua_date(text: str) -> str | None:
    """
    Парсит украинские даты вида "19 серпня", "19 серпня 2026", "Сьогодні", "Вчора",
    а также относительные "2 дні тому", "1 тиж. тому" (work.ua) в ISO YYYY-MM-DD.

    Если распознать не удалось — возвращает None, а НЕ исходный текст. Раньше
    возвращался сырой текст ("лучше сохранить сырой текст, чем молча потерять
    дату") — это было безопасно, пока published_at шёл только в SQLite (там
    колонка TEXT), но postgres_store пишет его в Postgres-колонку типа `date`,
    куда произвольный текст вроде "2 дні тому" не влезет и уронит вставку
    (invalid input syntax for type date). Раз мы всё равно не смогли
    распознать дату — честнее вернуть NULL, чем текст, который где-то дальше
    по цепочке всё равно нельзя использовать как дату.
    """
    if not text:
        return None
    t = text.strip().lower()
    today = date.today()
    if "сьогодні" in t or "today" in t:
        return today.isoformat()
    if "вчора" in t or "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()

    # "2 дні тому", "5 днів тому", "1 день тому" — work.ua и подобные
    m = re.search(r"(\d+)\s*(?:день|дні|днів)\s*тому", t)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()

    # "1 тиж. тому", "2 тижні тому", "3 тижнів тому"
    m = re.search(r"(\d+)\s*(?:тиж\.?|тижні|тижнів)\s*тому", t)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).isoformat()

    # "1 міс. тому", "2 місяці тому" — приблизительно, 30 днів на місяць
    m = re.search(r"(\d+)\s*(?:міс\.?|місяці|місяців)\s*тому", t)
    if m:
        return (today - timedelta(days=30 * int(m.group(1)))).isoformat()

    m = re.search(r"(\d{1,2})\s+([а-яіїєґ]+)(?:\s+(\d{4}))?", t)
    if m:
        day = int(m.group(1))
        month = UA_MONTHS.get(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if month:
            try:
                d = date(year, month, day)
                # даты без года и "в будущем" относительно сегодня — на самом деле прошлый год
                if not m.group(3) and d > today:
                    d = date(year - 1, month, day)
                return d.isoformat()
            except ValueError:
                pass

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return m.group(0)

    return None


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


# Фрази, якими сайти явно повідомляють, що вакансія вже закрита/неактуальна/
# в архіві — навіть якщо вона ще з'являється у видачі пошуку (кеш індексації,
# затримка оновлення сторінки списку тощо). Перевіряється в описі, отриманому
# fetch_description() (детальна сторінка вакансії) — саме там сайти показують
# цей статус, а не в картці зі списку.
CLOSED_VACANCY_MARKERS = [
    "вакансія закрита", "вакансія закрито", "вакансію закрито", "вакансия закрыта",
    "вакансію закрили", "вакансию закрыли", "вакансія неактуальна", "вакансия неактуальна",
    "вакансія в архіві", "вакансия в архиве", "вакансія архівна", "вакансия архивная",
    "прийом заявок закрито", "приём заявок закрыт", "прийом відгуків закрито",
    "приём откликов закрыт", "вакансія завершена", "вакансия завершена",
    "вакансія більше не актуальна", "вакансия больше не актуальна",
    "no longer accepting applications", "position has been filled", "vacancy is closed",
    "this position is no longer available", "job is no longer available", "position is closed",
]



# Дзеркало REMOTE_MARKERS/HYBRID_MARKERS/OFFICE_ONLY_MARKERS у
# ua_jobs_web/src/lib/vacancies.ts (classifyWorkMode) — тримати списки
# синхронізованими, якщо редагуєш один з двох.
_REMOTE_MARKERS = [
    "віддалено", "видалено", "удалённо", "удаленно", "дистанційно", "дистанционно",
    "remote", "work from home", "wfh", "з дому", "из дома", "фултайм ремоут",
    "remote-first", "remote work", "повністю віддалена", "полностью удалённая",
]
_HYBRID_MARKERS = [
    "гібридний формат", "гибридный формат", "гібрид", "гибрид", "hybrid",
    "частково віддалено", "частично удаленно", "частково в офісі", "частично в офисе",
    "2 дні в офісі", "3 дні в офісі", "2 дня в офисе", "3 дня в офисе",
    "2 days in office", "3 days in office", "hybrid work",
]
_OFFICE_ONLY_MARKERS = [
    "тільки офіс", "только офис", "офісний формат", "офисный формат",
    "office only", "on-site only", "onsite only", "робота лише в офісі",
    "работа только в офисе", "присутність в офісі обов'язкова",
    "присутствие в офисе обязательно", "без можливості віддаленої роботи",
    "без возможности удаленной работы", "виключно офлайн формат", "исключительно офлайн формат",
]


# Слова заперечення біля маркера (до чи після, в МЕЖАХ тієї самої клаузи)
# скасовують збіг — без цього "віддалено не розглядаємо" хибно матчилось би
# як REMOTE_MARKERS. Заперечення трапляється по обидва боки природною мовою:
# "не працюємо віддалено" (перед маркером) і "віддалено не розглядаємо"
# (після маркера — "не" стосується самого "віддалено", хоч і йде за ним).
# Межа клаузи (кома/крапка тощо) обрізає вікно пошуку заперечення, щоб
# заперечення з СУСІДНЬОЇ, непов'язаної клаузи не гасило маркер помилково —
# напр. у "робота лише в офісі, віддалено не розглядаємо" заперечення "не"
# стосується "віддалено" в іншій клаузі через кому, а не "офісі". Дзеркало
# NEGATION_WORDS/hasMarker() у ua_jobs_web/src/lib/vacancies.ts — знайдено
# регресійним тестом там само на цьому ж прикладі.
_NEGATION_WORDS = ["не ", "без ", "нема ", "немає ", "no ", "not "]
_NEGATION_LOOKAROUND = 20
_CLAUSE_BOUNDARY_RE = re.compile(r"[,.;!?\n]")


def _clause_bounded_before(s: str) -> str:
    matches = list(_CLAUSE_BOUNDARY_RE.finditer(s))
    return s[matches[-1].end():] if matches else s


def _clause_bounded_after(s: str) -> str:
    m = _CLAUSE_BOUNDARY_RE.search(s)
    return s[:m.start()] if m else s


def _has_marker(text: str, markers: list[str]) -> bool:
    for marker in markers:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx == -1:
                break
            before = _clause_bounded_before(text[max(0, idx - _NEGATION_LOOKAROUND):idx])
            after = _clause_bounded_after(text[idx + len(marker):idx + len(marker) + _NEGATION_LOOKAROUND])
            negated = any(neg in before or neg in after for neg in _NEGATION_WORDS)
            if not negated:
                return True
            start = idx + 1
    return False


def classify_work_mode(title: str, description: str) -> str | None:
    """Евристична класифікація 'remote' | 'office' | 'hybrid' | None за
    ключовими словами в назві+описі вакансії. hybrid-маркер має пріоритет
    (навіть якщо поруч згадано "віддалено" — формат вже не суто remote),
    office-маркер рахується ЛИШЕ якщо ніде поруч не згадано remote (інакше
    "переважно офіс, можливо віддалено за домовленістю" хибно стало б
    'office'). Викликається в main.py ПІСЛЯ fetch_description() — до цього
    моменту description ще порожній для сайтів, де він добирається окремо."""
    text = f"{title}\n{description}".lower()
    if _has_marker(text, _HYBRID_MARKERS):
        return "hybrid"
    has_remote = _has_marker(text, _REMOTE_MARKERS)
    has_office_only = _has_marker(text, _OFFICE_ONLY_MARKERS)
    if has_office_only and not has_remote:
        return "office"
    if has_remote:
        return "remote"
    return None


# Пост-фільтр релевантності: main.py довіряє пошуку сайту-джерела (кожен
# scrape(page, keyword) начебто вже фільтрує за keyword), але деякі джерела
# (grc.ua — загальний, не IT-спеціалізований сайт; dou.ua теж) на нішеві
# запити з keywords.csv ("vibe-coding", "prompt engineer", "ai automation"
# тощо) замість порожньої видачі fallback'яться на щось "схоже"/популярне з
# тієї ж категорії — і це потрапляє в базу як ніби знайдене САМЕ за цим
# keyword. Перевіряємо це тут: чи справді title+description містять слова
# з keyword, а не просто "сайт щось повернув".
_STOPWORDS = {
    "a", "an", "the", "of", "for", "with", "and", "or",
    "з", "зі", "та", "і", "й", "на", "до", "за",
}


def _significant_words(text: str) -> list[str]:
    words = re.findall(r"[a-zа-яіїєґ0-9]+", text.lower())
    return [w for w in words if len(w) >= 2 and w not in _STOPWORDS]


def _word_regex(word: str) -> re.Pattern:
    if len(word) >= 6:
        # довші слова (переважно українські, зі змінюваними закінченнями) —
        # збіг за коренем (перші 6 символів): "автоматизації" ≠
        # "автоматизація" посимвольно, але корінь "автома" збігається.
        return re.compile(rf"\b{re.escape(word[:6])}[a-zа-яіїєґ]*", re.IGNORECASE)
    return re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)


def is_relevant_to_keyword(title: str, description: str, keyword: str) -> bool:
    """True, якщо title+description РЕАЛЬНО стосуються keyword (усі значущі
    слова keyword десь зустрічаються) — а не просто "сайт щось повернув по
    цьому запиту". Порожній/безглуздий keyword (лише стоп-слова) не блокує
    нічого — повертає True, щоб не відкидати все підряд через дірку в
    евристиці."""
    kw_words = _significant_words(keyword)
    if not kw_words:
        return True
    haystack = f"{title or ''}\n{description or ''}"
    return all(_word_regex(w).search(haystack) for w in kw_words)


def is_closed_vacancy_text(text: str | None) -> bool:
    """Груба евристика: текст детальної сторінки вакансії сам повідомляє, що
    вакансія закрита/неактуальна/в архіві. Використовується ПІСЛЯ
    fetch_description() у main.py — такі вакансії виключаються з upsert і з
    seen_urls для mark_inactive(), щоб не зберігати і не тримати активними
    те, що сайт вже закрив (навіть якщо вона все ще потрапляє у видачу
    пошуку — раніше єдиним сигналом закриття було зникнення з видачі, що
    залежить від того, чи взагалі повторно ганяли парсер по тому самому
    ключовому слову)."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in CLOSED_VACANCY_MARKERS)
