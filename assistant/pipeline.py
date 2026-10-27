"""Обробка листів: що робити з кожним — чернетка, ручна перевірка чи пропуск."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .ai import LLM, Analysis, Category
from .mail import IncomingEmail, MailClient, build_draft, since_date
from .outputs import format_notification, log_row
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class Rules:
    min_confidence: float = 0.7
    # про що сповіщати в Telegram: «review» — завжди; для чернеток — лише за цими умовами
    notify_categories: set[str] = field(default_factory=lambda: {"complaint", "return_refund", "partnership"})
    notify_urgency: set[str] = field(default_factory=lambda: {"high"})
    notify_all_drafts: bool = False


def decide(a: Analysis, rules: Rules) -> str:
    """draft — створюємо чернетку; review — чернетку створюємо, але просимо людину подивитись; skip — нічого."""
    if a.category == Category.spam or not a.reply_needed:
        return "skip"
    if a.needs_human or a.confidence < rules.min_confidence or a.category == Category.complaint:
        return "review"
    return "draft"


def should_notify(a: Analysis, action: str, rules: Rules) -> bool:
    if action == "skip":
        return False
    if action == "review" or rules.notify_all_drafts:
        return True
    return a.category.value in rules.notify_categories or a.urgency.value in rules.notify_urgency


def message_key(mail: IncomingEmail, folder: str, uidvalidity: int) -> str:
    return mail.message_id or f"{folder}:{uidvalidity}:{mail.uid}"


REVIEW_NOTE = "[Перевірте перед відправкою: {reason}]\n\n"


class Processor:
    def __init__(self, llm: LLM, store: Store, rules: Rules, from_addr: str, from_name: str = "",
                 notifier=None, journals=(), dry_run: bool = False):
        self.llm, self.store, self.rules = llm, store, rules
        self.from_addr, self.from_name = from_addr, from_name
        self.notifier, self.journals, self.dry_run = notifier, list(journals), dry_run

    def handle(self, mail: IncomingEmail, client: MailClient | None, key: str) -> str:
        if mail.auto_generated or mail.from_addr == self.from_addr.lower():
            self.store.save(key, mail.uid, mail.sender, mail.subject, "auto", "low", 1.0, "skip")
            return "skip"
        a = self.llm.analyze(mail)
        action = decide(a, self.rules)
        if action in ("draft", "review") and a.reply:
            body = a.reply
            if action == "review":
                body = REVIEW_NOTE.format(reason=a.human_reason or "низька впевненість") + body
            draft = build_draft(mail, body, self.from_addr, self.from_name)
            if not self.dry_run and client is not None:
                client.save_draft(draft)
        self.store.save(key, mail.uid, mail.sender, mail.subject, a.category.value, a.urgency.value,
                        a.confidence, action)
        for j in self.journals:  # журнал і сповіщення не повинні ламати обробку
            try:
                j.append(log_row(mail, a, action))
            except Exception as e:
                log.warning("Журнал %s: %s", type(j).__name__, e)
        if self.notifier and should_notify(a, action, self.rules):
            try:
                self.notifier.send(format_notification(mail, a, action))
            except Exception as e:
                log.warning("Telegram: %s", e)
        log.info("[%s] %s — %s (%s, %.0f%%)", action, mail.sender, mail.subject, a.category.value, a.confidence * 100)
        return action

    def run_once(self, client: MailClient, folder: str, lookback_days: int) -> dict[str, int]:
        uidvalidity = client.uidvalidity()
        last = self.store.last_uid(folder, uidvalidity)
        done: dict[str, int] = {}
        for uid in client.new_uids(last, since_date(lookback_days)):
            mail = client.fetch(uid)
            if mail is None:
                continue
            key = message_key(mail, folder, uidvalidity)
            if not self.store.seen(key):
                try:
                    action = self.handle(mail, client, key)
                except Exception as e:  # AI недоступний тощо — лист буде оброблено наступного разу
                    log.error("Помилка обробки %s: %s", mail.subject, e)
                    self.store.save(key, uid, mail.sender, mail.subject, "", "", 0, "error", str(e)[:500])
                    done["error"] = done.get("error", 0) + 1
                    break  # курсор не рухаємо далі проблемного листа
                done[action] = done.get(action, 0) + 1
            self.store.set_last_uid(folder, uidvalidity, uid)
        return done
