from . import dou, djinni, work_ua, robota_ua, jooble, grc

# Реестр сайтов: source key -> модуль скрейпера.
# Модуль обязан экспортировать scrape(page, keyword, max_items) -> list[Vacancy].
# Если полное описание собирается отдельным заходом на детальную страницу —
# модуль дополнительно экспортирует fetch_description(page, url) -> str или tuple.
REGISTRY = {
    "dou": dou,
    "djinni": djinni,
    "work_ua": work_ua,
    "robota_ua": robota_ua,
    "jooble": jooble,
    "grc": grc,
}
