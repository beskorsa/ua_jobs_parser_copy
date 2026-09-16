"""
Ці ж кейси (title/description -> очікуваний work_mode) продубльовані у
ua_jobs_web/tests/vacancies.workMode.test.ts для classifyWorkMode() — щоб
список маркерів REMOTE/HYBRID/OFFICE_ONLY, який вручну підтримується
синхронним у scrapers/base.py і vacancies.ts (TS і Python не діляться одним
пакетом), не розійшовся непомітно. Якщо змінюєш маркери в одному файлі —
онови й другий, і обидва тестові набори мають лишитись зеленими.

Запуск: python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.base import classify_work_mode


class TestClassifyWorkMode(unittest.TestCase):
    def test_remote(self):
        self.assertEqual(
            classify_work_mode("Python Developer", "Формат роботи: віддалено, повна зайнятість"),
            "remote",
        )
        self.assertEqual(
            classify_work_mode("Remote QA Engineer", "We are looking for a remote engineer, work from home"),
            "remote",
        )

    def test_office_only(self):
        self.assertEqual(
            classify_work_mode("Офіс-менеджер", "Робота лише в офісі, віддалено не розглядаємо"),
            "office",
        )
        self.assertEqual(
            classify_work_mode("Support Specialist", "Office only position, on-site only"),
            "office",
        )

    def test_hybrid_takes_priority_over_nearby_remote_mention(self):
        self.assertEqual(
            classify_work_mode("Backend Developer", "Гібридний формат — 2 дні в офісі, решта віддалено"),
            "hybrid",
        )

    def test_office_only_marker_ignored_when_remote_mentioned_nearby(self):
        self.assertEqual(
            classify_work_mode("Java Developer", "Робота лише в офісі, але можливо іноді віддалено"),
            "remote",
        )

    def test_no_markers_returns_none(self):
        self.assertIsNone(classify_work_mode("Product Manager", "Шукаємо досвідченого PM у команду"))

    def test_negation_before_marker(self):
        # Заперечення ПЕРЕД маркером у тій самій клаузі — теж має гасити збіг.
        self.assertEqual(
            classify_work_mode("Sales Manager", "Не працюємо віддалено, тільки офіс"),
            "office",
        )


if __name__ == "__main__":
    unittest.main()
