"""Проганяє samples/*.eml через справжній Gemini і зберігає результат у samples/results.json.
Потрібен GEMINI_API_KEY у .env. Запуск: python tools/run_samples.py"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assistant.ai import GeminiAnalyzer  # noqa: E402
from assistant.mail import build_draft, parse_email  # noqa: E402
from assistant.outputs import format_notification  # noqa: E402
from assistant.pipeline import Rules, decide, REVIEW_NOTE  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

llm = GeminiAnalyzer(os.environ["GEMINI_API_KEY"], os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
                     (ROOT / "knowledge.example.md").read_text(encoding="utf-8"),
                     ["gemini-flash-latest", "gemini-3.1-flash-lite"])
rules = Rules()
results = []
for path in sorted((ROOT / "samples").glob("*.eml")):
    mail = parse_email(path.read_bytes())
    a = llm.analyze(mail)
    action = decide(a, rules)
    body = a.reply if action != "review" else REVIEW_NOTE.format(reason=a.human_reason) + a.reply
    draft = build_draft(mail, body, "support@zatyshna-hata.example", "Служба підтримки «Затишна Хата»") if a.reply else None
    results.append({"file": path.name, "from": mail.sender, "subject": mail.subject, "text": mail.text,
                    "auto_generated": mail.auto_generated, "attachments": mail.attachments,
                    "analysis": json.loads(a.model_dump_json()), "action": action,
                    "notification": format_notification(mail, a, action),
                    "draft_subject": draft["Subject"] if draft else "", "draft_body": body})
    print(f"{path.name:22} {a.category.value:16} {a.urgency.value:6} {a.confidence:.2f} {action}")
(ROOT / "samples" / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
