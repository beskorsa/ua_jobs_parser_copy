"""Регресійний тест на баг, знайдений вживу: пряме посилання на закриту/
неактуальну вакансію (dou.ua) продовжувало оцінюватись як звичайна активна
вакансія, бо єдиним сигналом закриття було зникнення з видачі пошуку (а не
факт відвідання самої сторінки вакансії). is_closed_vacancy_text() —
додатковий сигнал прямо з тексту детальної сторінки, перевіряється в
main.py ПІСЛЯ fetch_description().

Запуск: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base import is_closed_vacancy_text


class TestIsClosedVacancyText(unittest.TestCase):
    def test_detects_ukrainian_closed_markers(self):
        self.assertTrue(is_closed_vacancy_text("На жаль, вакансія закрита. Дякуємо за цікавість."))
        self.assertTrue(is_closed_vacancy_text("Вакансія неактуальна, набір завершено"))
        self.assertTrue(is_closed_vacancy_text("Ця вакансія в архіві"))

    def test_detects_english_closed_markers(self):
        self.assertTrue(is_closed_vacancy_text("Sorry, this position is no longer available"))
        self.assertTrue(is_closed_vacancy_text("We are no longer accepting applications for this role"))

    def test_open_vacancy_description_not_flagged(self):
        self.assertFalse(is_closed_vacancy_text(
            "Ми шукаємо Python розробника з досвідом 3+ роки. Обов'язки: розробка API, "
            "код-рев'ю, менторинг молодших розробників.",
        ))

    def test_empty_or_none_text_not_flagged(self):
        self.assertFalse(is_closed_vacancy_text(""))
        self.assertFalse(is_closed_vacancy_text(None))


if __name__ == "__main__":
    unittest.main()
