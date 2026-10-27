"""Запуск:
    python -m assistant            — працювати постійно (перевірка пошти кожні POLL_INTERVAL секунд)
    python -m assistant --once     — один прохід і вихід (для cron / планувальника Windows)
    python -m assistant --dry-run  — аналізувати, але не створювати чернеток
    python -m assistant --file samples/*.eml — прогнати збережені листи без пошти (для перевірки промпту)
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import time
from pathlib import Path

from .ai import GeminiAnalyzer
from .mail import MailClient, parse_email
from .outputs import CsvLog, SheetsLog, Telegram, format_notification
from .pipeline import Processor, Rules, decide
from .storage import Store

log = logging.getLogger("assistant")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def main() -> None:
    p = argparse.ArgumentParser(description="AI-помічник для пошти")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--file", nargs="+", help=".eml файли для перевірки без пошти")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    knowledge_path = Path(env("KNOWLEDGE_FILE", "knowledge.md"))
    if not knowledge_path.exists():
        knowledge_path = Path("knowledge.example.md")
    knowledge = knowledge_path.read_text(encoding="utf-8")
    if not env("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY не заданий (.env)")
    llm = GeminiAnalyzer(env("GEMINI_API_KEY"), env("GEMINI_MODEL", "gemini-3.6-flash"), knowledge,
                         [m.strip() for m in env("GEMINI_FALLBACK_MODELS").split(",") if m.strip()])
    rules = Rules(min_confidence=float(env("MIN_CONFIDENCE", "0.7")))

    if args.file:  # режим перевірки: друкуємо аналіз і чернетку
        for path in [f for pattern in args.file for f in glob.glob(pattern)]:
            mail = parse_email(Path(path).read_bytes())
            a = llm.analyze(mail)
            action = decide(a, rules)
            print("=" * 70, f"\n{path}\n")
            print(format_notification(mail, a, action))
            print("\n--- чернетка ---\n" + (a.reply or "(без відповіді)") + "\n")
        return

    data = Path(env("DATA_DIR", "data"))
    store = Store(data / "assistant.db")
    journals = [CsvLog(data / "journal.csv")]
    if env("SHEET_ID"):
        try:
            journals.append(SheetsLog(Path(env("GOOGLE_CREDENTIALS", "data/credentials.json")), env("SHEET_ID")))
        except Exception as e:
            log.warning("Google Sheets недоступний: %s — пишу лише CSV", e)
    notifier = Telegram(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")) if env("TELEGRAM_BOT_TOKEN") else None
    user = env("IMAP_USER")
    processor = Processor(llm, store, rules, from_addr=user, from_name=env("FROM_NAME"), notifier=notifier,
                          journals=journals, dry_run=args.dry_run)
    folder = env("IMAP_FOLDER", "INBOX")
    interval = int(env("POLL_INTERVAL", "60"))
    lookback = int(env("LOOKBACK_DAYS", "3"))

    while True:
        try:
            with MailClient(env("IMAP_HOST"), int(env("IMAP_PORT", "993")), user, env("IMAP_PASSWORD"), folder,
                            env("DRAFTS_FOLDER")) as client:
                done = processor.run_once(client, folder, lookback)
                if done:
                    log.info("Оброблено: %s", done)
        except Exception as e:  # обрив з'єднання тощо — спробуємо наступного разу
            log.error("Помилка циклу: %s", e)
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
