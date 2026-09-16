"""
Полностью автоматический цикл "сайт -> парсер", без ручного review.

Раз в запуск (рассчитан на Task Scheduler каждые ~30 мин):
  1. Ищет в search_queries запросы пользователей сайта за последние
     --days дней, по которым семантический поиск в среднем находил
     МЕНЬШЕ GAP_THRESHOLD результатов (см. suggest_keywords.GAP_THRESHOLD) —
     это и есть "разрыв": то, что реально искали, но сайт не смог найти.
  2. Дописывает топ-N таких запросов в keywords.csv (без подтверждения).
  3. Если что-то дописали — гоняет watchdog.py ТОЛЬКО по новым ключевым
     словам (не по всему списку — иначе каждый прогон будет по 20+ минут).
  4. Синкает результат в Supabase (sync_supabase.py).

Если новых провалов нет — шаги 3-4 пропускаются, скрипт завершается сразу
(лёгкий прогон раз в 30 мин, не нагружает сайты-источники попусту).

Использование (обычно — из Task Scheduler):
    python auto_refresh.py
    python auto_refresh.py --days 3 --top 8 --max-items 30

Лог пишется в auto_refresh.log (рядом со скриптом, ротация 5 МБ x 3) и в stdout.
"""
import argparse
import logging
import logging.handlers
import subprocess
import sys
from pathlib import Path

import psycopg2

import config
import suggest_keywords as sk

SCRIPT_DIR = Path(__file__).parent
DEFAULT_LOG_PATH = SCRIPT_DIR / "auto_refresh.log"

DEFAULT_DAYS = 3
DEFAULT_TOP = 8
DEFAULT_MAX_ITEMS = 30


def setup_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("auto_refresh")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


def run_streamed(cmd: list[str], logger: logging.Logger) -> int:
    logger.info(f"Запуск: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=str(SCRIPT_DIR),
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        logger.info(f"  {line.rstrip()}")
    return proc.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                         help=f"окно поиска запросов-провалов, дней (по умолчанию {DEFAULT_DAYS})")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                         help=f"макс. новых ключевых слов за один запуск (по умолчанию {DEFAULT_TOP})")
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS,
                         help=f"--max-items для watchdog.py/main.py (по умолчанию {DEFAULT_MAX_ITEMS})")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_PATH))
    args = parser.parse_args()

    logger = setup_logging(Path(args.log_file))

    if not config.DATABASE_URL:
        logger.error("DATABASE_URL пустой — проверь .env")
        sys.exit(1)

    existing = sk.load_existing_keywords()
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        candidates = sk.find_candidates(conn, args.days, existing, gaps_only=True)
    finally:
        conn.close()

    top_candidates = candidates[: args.top]

    if not top_candidates:
        logger.info("Провалов поиска не найдено — база уже покрывает текущий спрос, ничего не делаю.")
        return

    new_keywords = [text for text, _, _ in top_candidates]
    logger.info(f"Найдено {len(top_candidates)} провалов: {new_keywords}")

    sk.apply_candidates(top_candidates)
    logger.info(f"Дописано в {sk.KEYWORDS_CSV.name}.")

    watchdog_code = run_streamed(
        [sys.executable, "watchdog.py", "--", "--keywords", ",".join(new_keywords), "--max-items", str(args.max_items)],
        logger,
    )
    if watchdog_code != 0:
        logger.warning(f"watchdog.py завершился с кодом {watchdog_code} — синкаю то, что успело собраться.")

    sync_code = run_streamed([sys.executable, "sync_supabase.py"], logger)
    if sync_code != 0:
        logger.error(f"sync_supabase.py завершился с кодом {sync_code}.")
        sys.exit(1)

    logger.info("Готово.")


if __name__ == "__main__":
    main()
