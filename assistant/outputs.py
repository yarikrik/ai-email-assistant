"""Куди йде результат: Telegram-сповіщення менеджеру, CSV-журнал, Google Sheets."""
from __future__ import annotations

import csv
import logging
from html import escape
from pathlib import Path

from .ai import CATEGORY_LABEL, Analysis
from .mail import IncomingEmail

log = logging.getLogger(__name__)

URGENCY_ICON = {"high": "🔴", "normal": "🟡", "low": "⚪"}
ACTION_LABEL = {"draft": "✍️ Чернетка готова — перевірте і надішліть",
                "review": "🟠 Потрібна ручна перевірка",
                "skip": "⏭ Без відповіді"}
LOG_HEADER = ["Дата", "Від", "Тема", "Категорія", "Терміновість", "Впевненість", "Дія", "Замовлення", "Телефон",
              "Підсумок", "Причина перевірки"]


def _e(s) -> str:
    return escape(str(s), quote=False)


def format_notification(mail: IncomingEmail, a: Analysis, action: str) -> str:
    x = a.extracted
    lines = [f"{URGENCY_ICON[a.urgency.value]} <b>{_e(CATEGORY_LABEL[a.category.value])}</b> · "
             f"впевненість {a.confidence:.0%}",
             f"✉️ {_e(mail.sender)}",
             f"📌 {_e(mail.subject or '(без теми)')}",
             "",
             _e(a.summary)]
    facts = [f"№ {_e(x.order_number)}" if x.order_number else "", f"📞 {_e(x.phone)}" if x.phone else "",
             f"🛍 {_e(x.product)}" if x.product else ""]
    if any(facts):
        lines += ["", " · ".join(f for f in facts if f)]
    lines += ["", ACTION_LABEL[action]]
    if action == "review" and a.human_reason:
        lines.append(f"<i>{_e(a.human_reason)}</i>")
    if mail.attachments:
        lines.append(f"📎 {_e(', '.join(mail.attachments))}")
    return "\n".join(lines)


def log_row(mail: IncomingEmail, a: Analysis, action: str) -> list:
    return [mail.date.strftime("%Y-%m-%d %H:%M"), mail.sender, mail.subject, CATEGORY_LABEL[a.category.value],
            a.urgency.value, round(a.confidence, 2), action, a.extracted.order_number, a.extracted.phone,
            a.summary, a.human_reason if action == "review" else ""]


class Telegram:
    def __init__(self, token: str, chat_id: str, client=None):
        import httpx
        self.token, self.chat_id = token, chat_id
        self.client = client or httpx.Client(timeout=15)

    def send(self, text: str) -> None:
        r = self.client.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                             json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True})
        if not r.json().get("ok"):
            raise RuntimeError(f"Telegram: {r.text[:200]}")


class CsvLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, row: list) -> None:
        new = not self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8-sig") as f:  # utf-8-sig — щоб Excel відкрив кирилицю
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(LOG_HEADER)
            w.writerow(row)


class SheetsLog:
    def __init__(self, credentials: Path, sheet_id: str):
        import gspread
        self.ws = gspread.service_account(filename=str(credentials)).open_by_key(sheet_id).sheet1
        if self.ws.row_values(1) != LOG_HEADER:
            self.ws.update(range_name="A1", values=[LOG_HEADER])

    def append(self, row: list) -> None:
        self.ws.append_row(row, value_input_option="USER_ENTERED")
