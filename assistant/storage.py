"""SQLite: які листи вже оброблено (щоб не робити дві чернетки на один лист) і до якого UID дочитали."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_key  TEXT PRIMARY KEY,     -- Message-ID або folder:uidvalidity:uid
    uid          INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    sender       TEXT NOT NULL,
    subject      TEXT NOT NULL,
    category     TEXT NOT NULL,
    urgency      TEXT NOT NULL,
    confidence   REAL NOT NULL,
    action       TEXT NOT NULL,        -- draft / review / skip / error
    error        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cursor (
    folder       TEXT PRIMARY KEY,
    uidvalidity  INTEGER NOT NULL,
    last_uid     INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def last_uid(self, folder: str, uidvalidity: int) -> int:
        row = self.conn.execute("SELECT uidvalidity, last_uid FROM cursor WHERE folder = ?", (folder,)).fetchone()
        if not row or row["uidvalidity"] != uidvalidity:  # скриньку перебудовано — UID змінились
            return 0
        return row["last_uid"]

    def set_last_uid(self, folder: str, uidvalidity: int, uid: int) -> None:
        self.conn.execute("INSERT INTO cursor (folder, uidvalidity, last_uid) VALUES (?, ?, ?) "
                          "ON CONFLICT(folder) DO UPDATE SET uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid",
                          (folder, uidvalidity, uid))
        self.conn.commit()

    def seen(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM processed WHERE message_key = ? AND action != 'error'",
                                 (key,)).fetchone() is not None

    def save(self, key: str, uid: int, sender: str, subject: str, category: str, urgency: str,
             confidence: float, action: str, error: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO processed (message_key, uid, processed_at, sender, subject, category, urgency,"
            " confidence, action, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, uid, datetime.now(timezone.utc).isoformat(timespec="seconds"), sender, subject, category, urgency,
             confidence, action, error))
        self.conn.commit()

    def stats(self) -> dict[str, int]:
        return {r["action"]: r["n"] for r in self.conn.execute("SELECT action, COUNT(*) n FROM processed GROUP BY action")}
