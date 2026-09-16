# Парсер IT-вакансій (UA job-сайти)

Збирає вакансії за ключовими словами з 6 українських джоб-сайтів: **dou.ua**,
**djinni.co**, **work.ua**, **robota.ua** (rabota.ua редиректить сюди ж),
**ua.jooble.org**, **grc.ua**. Для кожної вакансії зберігає: `title`,
`company`, `description`, `url`, `published_at` (дата публікації), і стежить,
які вакансії ще активні.

## Встановлення

```bash
pip install -r requirements.txt
playwright install chromium
```

## Запуск і розклад ключових слів

`keywords.csv` містить IT/AI-ключові слова — вони скрейпляться **щодня**,
це пріоритет. `keywords_weekly.csv` — решта професій/категорій, розбиті по
днях тижня (колонка `day`: `mon`/`tue`/`wed`/`thu`/`fri`/`sat`/`sun`), щоб не
ганяти весь величезний список щоразу — раз на тиждень по колу. День
визначається за системною датою машини.

```bash
python main.py                                        # за розкладом: keywords.csv (щодня) + сьогоднішня категорія з keywords_weekly.csv
python main.py --all-weekly                           # keywords.csv + УСІ категорії keywords_weekly.csv разом (ручний повний прогін)
python main.py --day wed                               # прогнати так, ніби сьогодні середа (тест розкладу)
python main.py --keywords "ai automation,ai agent"    # явний список — розклад повністю ігнорується
python main.py --sources dou,djinni --max-items 30    # тільки обрані сайти
python main.py --headful                              # з видимим браузером — для дебагу
python main.py --sequential                            # старий режим — один браузер, сайти по черзі (для дебагу)
```

За замовчуванням кожне джерело (сайт) обробляється у своєму потоці зі своїм
браузером Playwright — джерела не чекають одне одного, весь прогін займає
час найповільнішого сайту, а не суму всіх.

### Watchdog (стійкий запуск)

`main.py` може впасти — краш браузера, мережа, змінена розмітка сайту.
`watchdog.py` запускає `main.py` як підпроцес і перезапускає його при
падінні, з обмеженням кількості спроб і паузою між ними, все логуючи:

```bash
python watchdog.py                                            # main.py без аргументів, до 3 спроб, 30 сек пауза
python watchdog.py --max-restarts 5 --restart-delay 60 -- --sources dou,djinni
python watchdog.py -- --keywords "python developer" --max-items 20
```

Все після `--` передається в `main.py` як є. Лог — `watchdog.log` поруч
зі скриптом (ротація 5 МБ × 3 файли) і дублюється в stdout; вивід самого
`main.py` потрапляє туди ж порядково, в реальному часі. Exit code
`watchdog.py`: `0` — `main.py` зрештою відпрацював успішно, `1` — вперлися
в ліміт спроб.

### Авто-підбір ключових слів (feedback-цикл із сайту)

Веб-частина (`ua_jobs_web`) логує реальні запити користувачів (пошук,
чат, факт завантаження резюме) у таблицю `search_queries`. `suggest_keywords.py`
читає її і знаходить "провали" — запити, на які семантичний пошук знайшов
мало або нічого (менше `GAP_THRESHOLD` результатів), і яких ще немає в
`keywords.csv`:

```bash
python suggest_keywords.py                     # кандидати за 30 днів, топ-15, нічого не змінює
python suggest_keywords.py --days 14 --top 10
python suggest_keywords.py --apply              # дописати кандидатів у keywords.csv
python suggest_keywords.py --gaps-only          # тільки запити з < GAP_THRESHOLD результатів
```

`auto_refresh.py` — повністю автоматична версія цього циклу, без ручного
review, розрахована на Task Scheduler кожні ~30 хв: шукає провали за
`--days` днів, дописує топ-N нових ключових слів у `keywords.csv` без
підтвердження, і якщо щось дописали — ганяє `watchdog.py` тільки по нових
словах (не по всьому списку), потім синкає результат у Supabase
(`sync_supabase.py`). Якщо нових провалів немає — кроки з watchdog/sync
пропускаються, скрипт одразу завершується. Лог — `auto_refresh.log`
(ротація 5 МБ × 3 файли).

```bash
python auto_refresh.py
python auto_refresh.py --days 3 --top 8 --max-items 30
```

## Як визначається "чи активна вакансія"

Окремої ознаки "вакансія закрита" у більшості сайтів немає — закриті
вакансії просто зникають з пошукової видачі. Тому логіка така:

1. При кожному запуску для пари (сайт, ключове слово) збирається **весь**
   поточний список знайдених вакансій.
2. Все, що знайшлося — upsert у базу, `is_active=1`.
3. Все, що раніше лежало в базі з `is_active=1` для цієї ж пари
   (сайт, ключове слово), але цього разу не зустрілося — позначається
   `is_active=0`. З бази нічого не видаляється, історія зберігається.

З бази можна діставати тільки активні записи через `db.fetch_active()`.

## Сховище

Два шари, незалежні один від одного:

1. **SQLite (`vacancies.db`)** — те, у що пише `main.py` (скрейпінг).
   Без зовнішніх залежностей, працює з коробки.
2. **Supabase/Postgres + pgvector** — опціональний шар поверх: переливає
   вакансії з SQLite, ріже описи на чанки і рахує ембеддинги. Якщо
   він не налаштований — `main.py` працює як зазвичай, нічого не ламається.

### Налаштування Supabase-шару

```bash
pip install -r requirements-supabase.txt
# + один із двох, залежно від EMBEDDING_PROVIDER:
pip install -r requirements-embeddings-local.txt    # локально, безкоштовно (тягне torch, ~1-2 ГБ)
pip install -r requirements-embeddings-openai.txt    # через API OpenAI, платно (копійки)

cp .env.example .env
# заповнити DATABASE_URL значенням із Supabase:
# Project Settings → Database → Connection string → URI (Session pooler, порт 5432)
```

`.env` — `EMBEDDING_PROVIDER=local` за замовчуванням, модель
`intfloat/multilingual-e5-small` (384-вимірна, нормально розуміє українську —
у "чистого" `bge-small-en-v1.5`, який згадувало ТЗ, розмітка тільки
англійська, для описів українською він дасть посередні вектори;
якщо все ж потрібен саме bge-small — перемикається одним рядком у `.env`,
міняти розмірність у схемі не доведеться, у нього теж 384). Для
`EMBEDDING_PROVIDER=openai` потрібен `OPENAI_API_KEY`, модель
`text-embedding-3-small` (1536-вимірна — `EMBEDDING_DIM` підставиться сама).

### Запуск

```bash
python main.py                       # 1. спочатку звичайний скрейпінг у SQLite
python sync_supabase.py              # 2. потім перелити у Supabase + порахувати ембеддинги

python sync_supabase.py --active-only          # синкнути тільки активні вакансії
python sync_supabase.py --limit 20             # швидкий тест на малому обсязі
python sync_supabase.py --search "python розробник, віддалено, від 2 років"
                                                 # семантичний пошук по вже засинканих вакансіях
```

Схема в Postgres (таблиці `vacancies`, `vacancy_chunks`, `resumes`,
`resume_sections`, `generations`, `scraper_keywords`) створюється сама при
першому запуску `sync_supabase.py` — накочувати `schema_postgres.sql` руками
не обов'язково, він у проєкті як документація/варіант для тих, хто воліє
керувати міграціями через Supabase SQL Editor.

На кожному запуску `sync_supabase.py` також синкає таблицю
`scraper_keywords` — повний список налаштованих ключових слів
(`keywords.csv` + всі дні `keywords_weekly.csv`, не тільки сьогоднішній).
Її використовує веб-частина (`isQueryWithinScrapedScope` в `ua_jobs_web`),
щоб не позначати "поза зоною охоплення" ключі, які реально скрейпляться,
але поки що дають 0 результатів.

`vacancies` — raw-запис (той самий набір полів і та сама логіка активності,
що й у SQLite). `vacancy_chunks` — опис, порізаний на чанки по ~1000
символів з нахлестом 150 (`config.CHUNK_SIZE` / `CHUNK_OVERLAP`), у кожного
чанка свій вектор. Пошук (`semantic_search` у `postgres_store.py`, він же
за `--search`) іде по чанках через `pgvector`-оператор `<=>` (косинусна
відстань), повертає по одній найкращій вакансії.

## Резюме як джерело запиту

`resume_match.py` — здобувач надсилає резюме (PDF, або .md/.txt),
далі це перетворюється на підбір вакансій із вже засинканої Supabase-бази.
Резюме зберігається **окремо** від вакансій — свої таблиці `resumes` /
`resume_sections` (див. `postgres_store.py`), у `vacancies`/`vacancy_chunks`
не підмішується нічого.

```bash
pip install -r requirements-resume.txt   # PyMuPDF, тільки для PDF; для .md/.txt не потрібен

python resume_match.py upload resume.pdf --match          # завантажити + одразу підібрати (1 ембеддинг на все резюме)
python resume_match.py upload resume.pdf --match --by-section   # те саме, але з розбивкою за секціями
python resume_match.py match --resume-id 3 --by-section --top-k 20
python resume_match.py list                                 # які резюме вже завантажені
```

OCR для сканованих PDF-резюме (без текстового шару) тут **не підтримується**
— на відміну від веб-частини (`ua_jobs_web`), де для цього є фоллбек через
tesseract.js. Тут PDF читається тільки через PyMuPDF.

Як це працює:

1. **Витяг тексту.** PDF — через PyMuPDF (`resume_parser.py`), бере
   текстовий шар; для скан-копій без текстового шару потрібен OCR — цього тут
   немає. Markdown/txt читаються як є.
2. **Секції.** Евристика за заголовками (укр/рос/англ, з урахуванням markdown
   `## Заголовок`) ріже текст на `skills` / `experience` / `education` /
   `summary` / `projects` / `languages`. Що не розпізналося — не страшно:
   `full` (весь текст резюме) є завжди, це самодостатній фолбек.
3. **Ембеддинги.** `full` і кожна розпізнана секція — свій вектор.
   Довгі секції (і `full`, якщо резюме довше за один чанк) ріжуться тим
   самим `chunk_text()`, що й описи вакансій, ембеддяться частинами і
   усереднюються (mean pooling + ре-нормалізація) — так у підсумковий вектор
   робить внесок весь текст, а не тільки перші ~500 токенів, у які
   моделі на кшталт e5-small інакше б усе мовчки обрізали.
4. **Підбір.** Два режими:
   - `mode="full"` (за замовчуванням) — вектор `full` резюме шукається по
     `vacancy_chunks` тим самим `semantic_search()`, що й у `sync_supabase.py
     --search`. Просто і швидко.
   - `mode="sections"` (`--by-section`) — кожна секція резюме шукає
     найближчі вакансії окремо, відстані комбінуються зважено
     (skills важить найбільше, `DEFAULT_SECTION_WEIGHTS` у
     `postgres_store.py`), ваги нормалізуються за фактом наявних
     секцій. Точніше там, де секції розпізналися нормально, але чутливіше
     до якості розбивки резюме на секції.

Обидва режими повертають вакансії, відсортовані за релевантністю, з URL і
(у режимі `sections`) розбивкою відстані за кожною секцією — видно, що
саме збіглося (наприклад, високий скор за skills, але низький за experience).

Важливо: ембеддинги резюме повинні рахуватися тією ж моделлю, що й
ембеддинги вакансій (`EMBEDDING_PROVIDER`/`EMBEDDING_MODEL` з `.env`) —
інакше вектори з різних просторів і косинусна відстань між ними безглузда.
Якщо міняєте модель — потрібно перерахувати ембеддинги і вакансіям
(`sync_supabase.py`), і вже завантаженим резюме (`resume_match.py upload`
заново).

Окремо варто згадати захист від вироджених (нульових) ембеддингів: якщо у
секції резюме чомусь вийшов нульовий вектор, `pgvector`-відстань `<=>` для
нього була б `NaN` і псувала б скор у всіх вакансіях у режимі `sections` —
така секція просто виключається з матчингу, а не валить весь підбір.

## Фільтри за metadata (місто, ЗП, дата)

Крім текстового смислового пошуку, `vacancies` зберігає `city`,
`salary_min`, `salary_max`, `salary_currency`, `salary_raw` — розпізнаються
скрейперами прямо з картки/детальної сторінки (див. `parse_salary()` у
`scrapers/base.py`) і доступні як фільтри поверх косинусного пошуку і в
`sync_supabase.py --search`, і в `resume_match.py`:

```bash
python sync_supabase.py --search "python backend" --city Київ --min-salary 1000 --currency USD --since 2026-08-01

python resume_match.py match --resume-id 3 --by-section --city Київ --min-salary 1000 --currency USD --since 2026-08-01
```

- `--city` — підрядок, `ILIKE '%значення%'` (регістронезалежно).
- `--min-salary` / `--currency` — вакансія проходить, якщо її `salary_max >=
  --min-salary`; `--currency` (якщо задано) вимагає точного збігу
  валюти. **Конвертації валют немає** — вакансії без вказаної зарплати
  (`salary_max IS NULL`) фільтром за зарплатою виключаються.
- `--since` — `published_at >= значення` (`YYYY-MM-DD`); вакансії без дати
  публікації виключаються, якщо фільтр заданий.

Покриття полів по сайтах різне — це обмеження самих сайтів, не баг:

| Сайт | city | salary |
|---|---|---|
| dou.ua | є | є, але часто не вказана |
| djinni.co | є | часто замаскована (просто "$" без чисел) |
| work.ua | є (евристика за регекспом тексту, у сайту немає окремого класу) | є |
| robota.ua | є | є, якщо вказана |
| grc.ua | є | є |
| ua.jooble.org | **не заповнюється** — у видачі майже завжди тільки "Віддалено"/"Гібридно", реального міста немає | є, якщо вказана |

## Видача результату: звіт у Telegram (MVP)

`telegram_report.py` рендерить уже збережені оцінки `generate.py`
(таблиця `generations`) у повідомлення Telegram — список вакансій + score
1-10 + пояснення + варіанти cover letter. Сам він нічого не оцінює і не
підбирає — просто доставка того, що порахував `generate.py`.

```bash
python generate.py run --resume-id 3          # спочатку оцінки мають бути згенеровані
python telegram_report.py test                 # перевірити TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
python telegram_report.py send --resume-id 3
python telegram_report.py send --resume-id 3 --top 5 --min-relevance 6
```

Токен — від `@BotFather`, chat id — свій (після `/start` боту відкрити
`https://api.telegram.org/bot<TOKEN>/getUpdates` і взяти `message.chat.id`)
або id групи/каналу — обидва в `.env` (див. `.env.example`).

## Особливості сайтів (важливо при доробці)

| Сайт | Як шукає | Опис | Примітки |
|---|---|---|---|
| dou.ua | `?search=` + "Показати ще" (AJAX) | окремий захід на сторінку вакансії | сайт повністю IT |
| djinni.co | `?all_keywords=` + `&page=N` | окремий захід | сайт повністю IT; дата теж тільки на детальній сторінці; **`?keywords=` сайтом ігнорується**, робочий параметр — `all_keywords` |
| work.ua | `/jobs-<keyword>/` | окремий захід (`#job-description`) | загальний сайт, не тільки IT |
| robota.ua | `/zapros/<keyword>/ukraine`, Angular SPA, infinite scroll | company + опис — тільки на детальній сторінці (`lib-content`, широкий селектор — сайт не дає вужчого) | rabota.ua редиректить сюди |
| ua.jooble.org | `?ukw=` | сніпет прямо в картці (коротший за повний) | агрегатор, посилання — на сторінку jooble (`/jdp/...`), не завжди на першоджерело; company в картці майже ніколи не вказано |
| grc.ua | `/searchjob/k-<keyword>` (React) | клік по картці → відкривається **нова вкладка** → `div.JobDescriptionBx` | картки без `<a href>`, перехід тільки реальним кліком; загальний сайт |

Усі селектори перевірені вручну через живий браузер. У сайтів властивість
міняти верстку без попередження — якщо скрипт раптом перестане знаходити
вакансії на якомусь одному сайті, дивіться в першу чергу на змінені
класи/структуру у відповідному файлі `scrapers/<site>.py`.

## Структура проєкту

```
main.py                       — CLI і оркестрація скрейпінгу, розклад ключових слів (щоденні + weekly-ротація)
models.py                      — dataclass Vacancy
db.py                          — SQLite-сховище (upsert, mark_inactive, fetch_active)
keywords.csv                    — IT/AI ключові слова, скрейпляться щодня
keywords_weekly.csv             — решта категорій, ротація по днях тижня (колонка day)
watchdog.py                     — обгортка над main.py: рестарт при падінні, ліміт спроб, логування
auto_refresh.py                  — повністю автоматичний feedback-цикл сайт->парсер (Task Scheduler, ~раз на 30 хв)
suggest_keywords.py              — пошук "провалів" у search_queries (запити без результатів), ручний або авто-режим
scrapers/
  base.py                       — парсинг українських дат, clean_text, parse_salary
  dou.py, djinni.py, work_ua.py, robota_ua.py, jooble.py, grc.py

config.py                       — конфіг Supabase/ембеддингів із .env
embeddings.py                    — чанкінг тексту, mean-pooling, LocalEmbedder/OpenAIEmbedder
postgres_store.py                — Supabase/Postgres-сховище: vacancies/vacancy_chunks/scraper_keywords
                                    + resumes/resume_sections + semantic_search/match_vacancies_for_resume
sync_supabase.py                 — CLI: SQLite -> Postgres (+ ембеддинги), семантичний пошук
resume_parser.py                  — витяг тексту з PDF/MD/txt резюме + розбивка на секції
resume_match.py                   — CLI: завантаження резюме, підбір вакансій (upload / match / list)
generate.py                       — CLI: LLM-оцінка релевантності вакансії + cover letter (таблиця generations)
telegram_report.py                — CLI: звіт по generations в Telegram (send / test)
schema_postgres.sql               — схема Postgres (для ручного застосування, опціонально)
.env.example                      — шаблон конфігу (DATABASE_URL, EMBEDDING_PROVIDER, TELEGRAM_*, ...)
tests/                            — регресійні тести (test_db.py, test_closed_vacancy.py, test_work_mode.py)
requirements.txt                   — залежності скрейпінгу (main.py)
requirements-supabase.txt           — psycopg2, python-dotenv (для sync_supabase.py / resume_match.py / suggest_keywords.py / auto_refresh.py)
requirements-embeddings-local.txt    — sentence-transformers (EMBEDDING_PROVIDER=local)
requirements-embeddings-openai.txt   — openai (EMBEDDING_PROVIDER=openai)
requirements-resume.txt              — PyMuPDF, для PDF-резюме (resume_match.py)
requirements-generation.txt          — openai (для generate.py; telegram_report.py — чистий stdlib)
```
