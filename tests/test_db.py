"""Регресійні тести на db.py (SQLite-шар): upsert_vacancies/mark_inactive —
логіка, яка визначає, чи вакансія лишається активною між прогонами парсера.
Саме тут була дірка, яку закрили is_closed_vacancy_text()-фільтром у
main.py (main.py виключає закриті вакансії зі списку, який іде в
upsert_vacancies/mark_inactive, — тут тестуємо сам механізм mark_inactive,
на який ця фільтрація спирається).

Використовує тимчасовий SQLite-файл (не чіпає реальний vacancies.db) —
підміняє db.DB_PATH на час тесту.

Запуск: python -m unittest discover -s tests
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db
from models import Vacancy


def make_vacancy(url: str, title: str = "Test Vacancy") -> Vacancy:
    return Vacancy(source="dou", keyword="python", title=title, company="Acme",
                    description="desc", url=url)


class TestMarkInactive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._orig_db_path = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name)
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self._orig_db_path
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_vacancy_missing_from_new_listing_is_marked_inactive(self):
        v1 = make_vacancy("https://jobs.dou.ua/vacancies/1/")
        v2 = make_vacancy("https://jobs.dou.ua/vacancies/2/")
        db.upsert_vacancies([v1, v2])

        # Наступний прогін того самого (source, keyword) знайшов лише v1 —
        # v2 зникла з видачі (закрита/видалена).
        deactivated = db.mark_inactive("dou", "python", {v1.url})
        self.assertEqual(deactivated, 1)

        active = {row["url"] for row in db.fetch_active(source="dou", keyword="python")}
        self.assertIn(v1.url, active)
        self.assertNotIn(v2.url, active)

    def test_vacancy_excluded_as_closed_by_main_py_is_marked_inactive_too(self):
        # Емуляція того, що робить main.py: is_closed_vacancy_text()
        # виключає закриту вакансію зі списку ДО upsert_vacancies і ДО
        # mark_inactive — навіть якщо вона фізично ще присутня у видачі
        # пошуку сайту, у seen_urls її бути не повинно.
        v1 = make_vacancy("https://jobs.dou.ua/vacancies/1/")
        closed = make_vacancy("https://jobs.dou.ua/companies/futurra-group/vacancies/336569/")
        db.upsert_vacancies([v1, closed])

        # main.py: open_vacancies = [v for v in vacancies if v.url not in closed_urls]
        open_vacancies = [v1]  # closed виключена, ніби is_closed_vacancy_text() її впіймав
        db.upsert_vacancies(open_vacancies)
        seen_urls = {v.url for v in open_vacancies}
        deactivated = db.mark_inactive("dou", "python", seen_urls)

        self.assertEqual(deactivated, 1)
        active_urls = {row["url"] for row in db.fetch_active(source="dou", keyword="python")}
        self.assertNotIn(closed.url, active_urls)

    def test_vacancy_seen_again_stays_active(self):
        v1 = make_vacancy("https://jobs.dou.ua/vacancies/1/")
        db.upsert_vacancies([v1])
        deactivated = db.mark_inactive("dou", "python", {v1.url})
        self.assertEqual(deactivated, 0)
        active_urls = {row["url"] for row in db.fetch_active(source="dou", keyword="python")}
        self.assertIn(v1.url, active_urls)

    def test_reappearing_vacancy_is_reactivated_on_upsert(self):
        v1 = make_vacancy("https://jobs.dou.ua/vacancies/1/")
        db.upsert_vacancies([v1])
        db.mark_inactive("dou", "python", set())  # нічого не бачили — деактивувалась
        self.assertNotIn(v1.url, {r["url"] for r in db.fetch_active(source="dou", keyword="python")})

        # Вакансія знову з'явилась у видачі наступного разу — upsert має її реактивувати.
        db.upsert_vacancies([v1])
        self.assertIn(v1.url, {r["url"] for r in db.fetch_active(source="dou", keyword="python")})


if __name__ == "__main__":
    unittest.main()
