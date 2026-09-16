"""
Watchdog для main.py (playwright-парсер): если процесс падает (ненулевой
exit code — необработанное исключение, краш браузера, сеть и т.п.) —
перезапускает его с ограничением числа попыток и паузой между ними
(защита от busy-loop, если сайт стабильно блокирует/меняет разметку).

Всё, что передано после "--", уходит в main.py как есть — watchdog сам эти
аргументы не разбирает (--keywords, --sources, --max-items, --headful —
любые флаги main.py работают без изменений).

Использование:
    python watchdog.py
        -> main.py без аргументов, до 3 рестартов при падении, 30 сек между попытками

    python watchdog.py --max-restarts 5 --restart-delay 60 -- --sources dou,djinni
    python watchdog.py -- --keywords "python developer" --max-items 20

Лог пишется в watchdog.log (рядом со скриптом, ротация 5 МБ x 3 файла) и
одновременно дублируется в stdout — вывод самого main.py (включая
[источник] строки прогресса) в лог попадает построчно и в реальном
времени, а не одним куском после завершения.

Exit code watchdog.py: 0 — main.py в итоге отработал успешно (возможно,
не с первой попытки); 1 — упёрлись в лимит рестартов, ни разу не завершился
с exit 0.
"""
import argparse
import logging
import logging.handlers
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DEFAULT_LOG_PATH = SCRIPT_DIR / "watchdog.log"
DEFAULT_MAX_RESTARTS = 3
DEFAULT_RESTART_DELAY = 30.0


def setup_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("watchdog")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # на случай повторного вызова setup_logging в тестах/REPL

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


def run_once(main_args: list[str], logger: logging.Logger) -> int:
    """Запускает main.py как подпроцесс, построчно проксирует его stdout/stderr
    в лог по мере поступления, возвращает exit code процесса. -1 означает, что
    процесс не удалось даже запустить (main.py не найден, нет прав и т.п.) —
    это тоже считается падением и попадает под лимит рестартов."""
    cmd = [sys.executable, str(SCRIPT_DIR / "main.py"), *main_args]
    logger.info(f"Запуск: {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered — чтобы строки main.py доходили до лога сразу, не пачкой в конце
            cwd=str(SCRIPT_DIR),
        )
    except OSError as e:
        logger.error(f"Не удалось запустить main.py: {e}")
        return -1

    assert proc.stdout is not None
    for line in proc.stdout:
        logger.info(f"[main.py] {line.rstrip()}")

    return proc.wait()


def watch(main_args: list[str], max_restarts: int, restart_delay: float, logger: logging.Logger) -> int:
    """max_restarts — общее число ПОПЫТОК (включая самую первую), а не число
    рестартов сверх первой попытки: max_restarts=3 значит максимум 3 запуска
    main.py за вызов watchdog.py, из них до 2 могут быть рестартами после падения."""
    attempt = 0
    while True:
        attempt += 1
        code = run_once(main_args, logger)
        if code == 0:
            logger.info(f"main.py завершился успешно (exit 0), попытка {attempt}/{max_restarts}.")
            return 0

        logger.warning(f"main.py упал (exit={code}), попытка {attempt}/{max_restarts}.")

        if attempt >= max_restarts:
            logger.error(f"Достигнут лимит попыток ({max_restarts}) — прекращаю рестарты.")
            return 1

        logger.info(f"Пауза {restart_delay:.0f} сек перед рестартом...")
        time.sleep(restart_delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS,
                         help=f"макс. число попыток запуска main.py, включая первую (по умолчанию {DEFAULT_MAX_RESTARTS})")
    parser.add_argument("--restart-delay", type=float, default=DEFAULT_RESTART_DELAY,
                         help=f"пауза между попытками, сек (по умолчанию {DEFAULT_RESTART_DELAY:.0f})")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_PATH), help="путь к лог-файлу")
    parser.add_argument("main_args", nargs=argparse.REMAINDER,
                         help="аргументы main.py — после --, например: -- --sources dou --max-items 10")
    args = parser.parse_args()

    if args.max_restarts < 1:
        parser.error("--max-restarts должен быть >= 1")

    main_args = args.main_args
    if main_args and main_args[0] == "--":
        main_args = main_args[1:]

    logger = setup_logging(Path(args.log_file))
    logger.info(f"Watchdog запущен. max_restarts={args.max_restarts}, restart_delay={args.restart_delay}s, "
                f"main.py args={main_args or '(нет)'}")

    exit_code = watch(main_args, args.max_restarts, args.restart_delay, logger)
    logger.info(f"Watchdog завершён, exit code {exit_code}.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
