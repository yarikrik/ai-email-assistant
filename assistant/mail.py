"""Робота з поштою через IMAP: читання нових листів і збереження чернеток.

Листи читаються через BODY.PEEK[], тому не позначаються прочитаними.
Бот нічого не відправляє: у коді немає SMTP взагалі, відповіді лише лягають у «Чернетки».
"""
from __future__ import annotations

import email
import email.policy
import html
import imaplib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, format_datetime, getaddresses, make_msgid, parsedate_to_datetime

log = logging.getLogger(__name__)
MAX_BODY_CHARS = 8000  # досить для будь-якого звернення і захищає від гігантських розсилок


@dataclass
class IncomingEmail:
    uid: int
    message_id: str
    from_name: str
    from_addr: str
    subject: str
    date: datetime
    text: str
    attachments: list[str] = field(default_factory=list)
    references: str = ""
    auto_generated: bool = False  # розсилка, автовідповідь, bounce — відповідати не треба

    @property
    def sender(self) -> str:
        return f"{self.from_name} <{self.from_addr}>" if self.from_name else self.from_addr


# ---------- розбір листа ----------

def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(raw)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def strip_quoted(text: str) -> str:
    """Прибирає цитату попереднього листування («> ...», «On ... wrote:», «... пише:»)."""
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^(On .+wrote:|.{0,120}(пише|писав|написав|писала|написала|пишет|написал)\s*:)$", s) \
                or re.match(r"^-{2,}\s*(Original Message|Пересланное|Переслане)", s, re.I):
            break
        if s.startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _body(msg: EmailMessage) -> tuple[str, list[str]]:
    plain, htm, attachments = None, None, []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        if disp == "attachment" or (disp == "inline" and part.get_filename()):
            attachments.append(part.get_filename() or "file")
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            content = part.get_payload(decode=True).decode("utf-8", "replace")
        if ctype == "text/plain" and plain is None:
            plain = content
        elif ctype == "text/html" and htm is None:
            htm = content
    text = plain if plain and plain.strip() else html_to_text(htm or "")
    return strip_quoted(text)[:MAX_BODY_CHARS], attachments


def parse_email(raw: bytes, uid: int = 0) -> IncomingEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = (getaddresses([str(msg.get("From", ""))]) or [("", "")])[0]
    try:
        date = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        date = datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    text, attachments = _body(msg)
    auto = (str(msg.get("Auto-Submitted", "no")).lower() != "no"
            or str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk")
            or bool(msg.get("List-Unsubscribe"))
            or re.match(r"(mailer-daemon|postmaster|no-?reply)@", addr.lower()) is not None)
    return IncomingEmail(
        uid=uid, message_id=str(msg.get("Message-ID", "")).strip(), from_name=name, from_addr=addr.lower(),
        subject=str(msg.get("Subject", "")).strip(), date=date, text=text, attachments=attachments,
        references=str(msg.get("References", "")).strip(), auto_generated=auto)


def build_draft(original: IncomingEmail, body: str, from_addr: str, from_name: str = "") -> EmailMessage:
    """Відповідь-чернетка у тому ж ланцюжку (In-Reply-To / References), з цитатою оригіналу."""
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = formataddr((original.from_name, original.from_addr))
    subject = original.subject or ""
    msg["Subject"] = subject if re.match(r"^(re|відп|отв)\s*:", subject, re.I) else f"Re: {subject}".strip()
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] or "localhost")
    if original.message_id:
        msg["In-Reply-To"] = original.message_id
        msg["References"] = f"{original.references} {original.message_id}".strip()
    when = original.date.strftime("%d.%m.%Y %H:%M")
    quoted = "\n".join("> " + line for line in original.text.splitlines())
    msg.set_content(f"{body.strip()}\n\n{when}, {original.sender} пише:\n{quoted}\n")
    return msg


# ---------- IMAP ----------

def _parse_list_line(line: bytes) -> tuple[set[str], str]:
    """b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"' → ({'\\Drafts', ...}, '[Gmail]/Drafts')"""
    m = re.match(rb'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
    if not m:
        return set(), ""
    name = m.group("name").decode()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"')
    return set(m.group("flags").decode().split()), name


class MailClient:
    """Тонка обгортка над imaplib. imap_factory дозволяє підставити фейковий сервер у тестах."""

    def __init__(self, host: str, port: int, user: str, password: str, folder: str = "INBOX",
                 drafts_folder: str = "", imap_factory=None):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.folder, self.drafts_folder = folder, drafts_folder
        self.factory = imap_factory or (lambda: imaplib.IMAP4_SSL(host, port))
        self.conn = None

    def __enter__(self):
        self.conn = self.factory()
        self.conn.login(self.user, self.password)
        return self

    def __exit__(self, *exc):
        try:
            self.conn.logout()
        except Exception:
            pass

    @staticmethod
    def _ok(resp, what: str):
        typ, data = resp
        if typ != "OK":
            raise RuntimeError(f"IMAP {what}: {data}")
        return data

    def uidvalidity(self) -> int:
        self._ok(self.conn.select(_quote(self.folder), readonly=True), "select")
        resp = self.conn.response("UIDVALIDITY")[1]
        return int(resp[0]) if resp and resp[0] else 0

    def new_uids(self, after_uid: int, since: datetime) -> list[int]:
        """UID листів після after_uid, але не старших за since (щоб перший запуск не тягнув усю скриньку)."""
        crit = f"UID {after_uid + 1}:* SINCE {since.strftime('%d-%b-%Y')}"
        data = self._ok(self.conn.uid("SEARCH", None, crit), "search")
        uids = [int(x) for x in (data[0] or b"").split()]
        return sorted(u for u in uids if u > after_uid)  # «N:*» завжди повертає хоча б останній лист

    def fetch(self, uid: int) -> IncomingEmail | None:
        data = self._ok(self.conn.uid("FETCH", str(uid), "(BODY.PEEK[])"), "fetch")
        raw = next((part[1] for part in data if isinstance(part, tuple)), None)
        return parse_email(raw, uid) if raw else None

    def find_drafts_folder(self) -> str:
        if self.drafts_folder:
            return self.drafts_folder
        data = self._ok(self.conn.list(), "list")
        names = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            flags, name = _parse_list_line(line)
            if "\\Drafts" in flags:
                self.drafts_folder = name
                return name
            names.append(name)
        for guess in ("Drafts", "[Gmail]/Drafts", "INBOX.Drafts", "Draft"):
            if guess in names:
                self.drafts_folder = guess
                return guess
        raise RuntimeError("Не знайдено папку чернеток — вкажіть DRAFTS_FOLDER у .env")

    def save_draft(self, msg: EmailMessage) -> None:
        folder = self.find_drafts_folder()
        self._ok(self.conn.append(_quote(folder), "(\\Draft \\Seen)", imaplib.Time2Internaldate(time.time()),
                                  msg.as_bytes(policy=email.policy.SMTP)), "append")


def _quote(folder: str) -> str:
    return folder if folder.startswith('"') else '"' + folder.replace('"', '\\"') + '"'


def since_date(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
