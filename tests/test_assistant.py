"""Тести без мережі: фейковий IMAP-сервер (відповіді у форматі imaplib) і фейковий AI."""
import csv
import email
import email.policy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from assistant.ai import Analysis, Category, Extracted, Urgency, build_prompt, parse_analysis
from assistant.mail import MailClient, _parse_list_line, build_draft, parse_email, strip_quoted
from assistant.outputs import CsvLog, format_notification
from assistant.pipeline import Processor, Rules, decide, should_notify
from assistant.storage import Store

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def sample(name: str) -> bytes:
    return (SAMPLES / f"{name}.eml").read_bytes()


def analysis(**kw) -> Analysis:
    base = dict(category=Category.order_status, urgency=Urgency.normal, confidence=0.92, language="uk",
                summary="Клієнтка питає про ТТН замовлення 48213.",
                extracted=Extracted(customer_name="Олена", order_number="48213", phone="067 123 45 67"),
                reply_needed=True, needs_human=False, reply="Добрий день, Олено! Перевіряємо замовлення №48213.")
    base.update(kw)
    return Analysis(**base)


class ParseTest(unittest.TestCase):
    def test_plain_with_encoded_headers(self):
        m = parse_email(sample("01_order_status"), uid=7)
        self.assertEqual((m.uid, m.from_name, m.from_addr), (7, "Олена Коваль", "olena.koval@example.com"))
        self.assertEqual(m.subject, "Де моє замовлення?")
        self.assertIn("№48213", m.text)
        self.assertFalse(m.auto_generated)

    def test_html_only_and_attachments(self):
        m = parse_email(sample("03_question"))
        self.assertIn("Розмір", m.subject)
        self.assertIn("лляної колекції", m.text)
        self.assertNotIn("<p>", m.text)
        self.assertEqual(parse_email(sample("04_complaint")).attachments, ["photo_1.jpg", "photo_2.jpg"])

    def test_bulk_is_auto_generated(self):
        self.assertTrue(parse_email(sample("06_spam")).auto_generated)

    def test_strip_quoted(self):
        text = "Дякую, все отримала!\n\n27.10.2026, Підтримка пише:\n> Добрий день\n> ваше замовлення"
        self.assertEqual(strip_quoted(text), "Дякую, все отримала!")
        self.assertEqual(strip_quoted("Ok\nOn Mon, Oct 26, 2026 John wrote:\n> hi"), "Ok")

    def test_build_draft_threads_and_quotes(self):
        original = parse_email(sample("01_order_status"))
        d = build_draft(original, "Перевіряємо!", "support@shop.example", "Підтримка")
        self.assertEqual(d["Subject"], "Re: Де моє замовлення?")
        self.assertEqual(d["In-Reply-To"], original.message_id)
        self.assertIn(original.message_id, d["References"])
        self.assertIn("olena.koval@example.com", d["To"])
        body = d.get_content()
        self.assertTrue(body.startswith("Перевіряємо!"))
        self.assertIn("> Добрий день!", body)
        again = build_draft(parse_email(sample("04_complaint")), "x", "s@shop.example")
        self.assertTrue(again["Subject"].startswith("Re: Замовлення"))  # без «Re: Re:»

    def test_prompt_wraps_untrusted_text(self):
        p = build_prompt(parse_email(sample("07_injection")))
        self.assertIn("<email>", p)
        self.assertIn("ignore all previous instructions", p)  # текст передаємо, але як дані між тегами

    def test_parse_analysis_accepts_code_fence(self):
        raw = analysis().model_dump_json()
        self.assertEqual(parse_analysis(f"```json\n{raw}\n```").category, Category.order_status)
        with self.assertRaises(Exception):
            parse_analysis('{"category": "hack", "urgency": "high"}')

    def test_null_strings_are_cleaned(self):
        raw = analysis().model_dump()
        raw["extracted"] = {"customer_name": "Max", "order_number": ":null", "phone": "null", "product": None}
        raw["human_reason"] = None
        a = Analysis.model_validate(raw)
        self.assertEqual((a.extracted.order_number, a.extracted.phone, a.extracted.product, a.human_reason),
                         ("", "", "", ""))


class RulesTest(unittest.TestCase):
    def test_decide(self):
        r = Rules(min_confidence=0.7)
        self.assertEqual(decide(analysis(), r), "draft")
        self.assertEqual(decide(analysis(confidence=0.5), r), "review")
        self.assertEqual(decide(analysis(needs_human=True), r), "review")
        self.assertEqual(decide(analysis(category=Category.complaint), r), "review")
        self.assertEqual(decide(analysis(category=Category.spam), r), "skip")
        self.assertEqual(decide(analysis(reply_needed=False), r), "skip")

    def test_notify(self):
        r = Rules()
        self.assertFalse(should_notify(analysis(), "draft", r))
        self.assertTrue(should_notify(analysis(urgency=Urgency.high), "draft", r))
        self.assertTrue(should_notify(analysis(category=Category.return_refund), "draft", r))
        self.assertTrue(should_notify(analysis(), "review", r))
        self.assertFalse(should_notify(analysis(category=Category.spam), "skip", r))

    def test_notification_text(self):
        m = parse_email(sample("04_complaint"))
        text = format_notification(m, analysis(category=Category.complaint, urgency=Urgency.high,
                                               needs_human=True, human_reason="Вимога компенсації"), "review")
        self.assertIn("🔴 <b>Скарга</b>", text)
        self.assertIn("Потрібна ручна перевірка", text)
        self.assertIn("photo_1.jpg", text)
        self.assertIn("№ 48213", text)


# ---------- фейковий IMAP-сервер з відповідями як у imaplib ----------
class FakeIMAP:
    def __init__(self, messages: dict[int, bytes], uidvalidity=555, drafts_flag=True):
        self.messages, self.uidvalidity, self.appended, self.flags_set = dict(messages), uidvalidity, [], []
        self.drafts_flag = drafts_flag
        self.readonly = None

    def login(self, user, password):
        return "OK", [b"Logged in"]

    def logout(self):
        return "BYE", [b""]

    def select(self, folder, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            crit = args[1]
            start = int(crit.split()[1].split(":")[0])
            found = [u for u in self.messages if u >= start] or ([max(self.messages)] if self.messages else [])
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            assert args[1] == "(BODY.PEEK[])", "лист не можна позначати прочитаним"
            raw = self.messages[uid]
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        raise AssertionError(command)

    def list(self):
        drafts = b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"' if self.drafts_flag else b'(\\HasNoChildren) "/" "Drafts"'
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', drafts, b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']

    def append(self, folder, flags, date, raw):
        self.appended.append((folder, flags, email.message_from_bytes(raw, policy=email.policy.default)))
        return "OK", [b"APPEND completed"]


class FakeLLM:
    def __init__(self, by_subject: dict[str, Analysis], fail_on: str = ""):
        self.by_subject, self.fail_on, self.calls = by_subject, fail_on, []

    def analyze(self, mail):
        self.calls.append(mail.subject)
        if self.fail_on and self.fail_on in mail.subject:
            raise RuntimeError("503 overloaded")
        for key, a in self.by_subject.items():
            if key in mail.subject:
                return a
        return analysis()


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.imap = FakeIMAP({101: sample("01_order_status"), 102: sample("04_complaint"),
                              103: sample("06_spam"), 104: sample("02_return")})
        self.llm = FakeLLM({
            "ВДРУГЕ": analysis(category=Category.complaint, urgency=Urgency.high, needs_human=True,
                               human_reason="Вимога компенсації", reply="Сергію, перепрошуємо!"),
            "Возврат": analysis(category=Category.return_refund, language="ru", reply="Здравствуйте, Дмитрий!"),
        })
        self.tg = FakeTelegram()
        self.store = Store(":memory:")
        self.csv = CsvLog(Path(self.tmp.name) / "journal.csv")
        self.proc = Processor(self.llm, self.store, Rules(), "support@shop.example", "Підтримка",
                              notifier=self.tg, journals=[self.csv])

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self):
        with MailClient("h", 993, "u", "p", imap_factory=lambda: self.imap) as c:
            return self.proc.run_once(c, "INBOX", 3)

    def test_full_pass(self):
        done = self.run_once()
        self.assertEqual(done, {"draft": 2, "review": 1, "skip": 1})
        self.assertTrue(self.imap.readonly)                         # INBOX відкрито лише для читання
        self.assertEqual(len(self.imap.appended), 3)                 # спам без чернетки
        folder, flags, draft = self.imap.appended[1]
        self.assertEqual(folder, '"[Gmail]/Drafts"')
        self.assertIn("\\Draft", flags)
        self.assertTrue(draft.get_content().startswith("[Перевірте перед відправкою: Вимога компенсації]"))
        self.assertEqual(self.llm.calls.count("Increase your traffic 10x!!!"), 0)  # розсилку навіть не шлемо в AI
        self.assertEqual(len(self.tg.sent), 2)                       # скарга (review) + повернення
        with open(self.csv.path, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f, delimiter=";"))
        self.assertEqual(len(rows), 4)                               # шапка + 3 проаналізовані листи

    def test_second_pass_does_nothing(self):
        self.run_once()
        self.assertEqual(self.run_once(), {})
        self.assertEqual(len(self.imap.appended), 3)
        self.imap.messages[105] = sample("03_question")             # прийшов новий лист
        self.assertEqual(self.run_once(), {"draft": 1})

    def test_uidvalidity_change_does_not_duplicate(self):
        self.run_once()
        self.imap.uidvalidity = 999                                  # сервер перенумерував листи
        self.assertEqual(self.run_once(), {})                        # але Message-ID ті самі — дублів немає
        self.assertEqual(len(self.imap.appended), 3)

    def test_ai_failure_stops_and_retries_later(self):
        self.llm.fail_on = "ВДРУГЕ"
        done = self.run_once()
        self.assertEqual(done, {"draft": 1, "error": 1})
        self.assertEqual(len(self.imap.appended), 1)
        self.llm.fail_on = ""
        done = self.run_once()                                       # курсор зупинився перед проблемним листом
        self.assertEqual(done, {"review": 1, "skip": 1, "draft": 1})
        self.assertEqual(len(self.imap.appended), 3)

    def test_dry_run_creates_nothing(self):
        self.proc.dry_run = True
        self.run_once()
        self.assertEqual(self.imap.appended, [])

    def test_drafts_folder_fallback(self):
        imap = FakeIMAP({}, drafts_flag=False)
        with MailClient("h", 993, "u", "p", imap_factory=lambda: imap) as c:
            self.assertEqual(c.find_drafts_folder(), "Drafts")

    def test_list_parsing(self):
        flags, name = _parse_list_line(b'(\\HasNoChildren \\Drafts) "." "INBOX.Drafts"')
        self.assertEqual(name, "INBOX.Drafts")
        self.assertIn("\\Drafts", flags)

    def test_own_messages_are_skipped(self):
        self.imap.messages = {200: build_draft(parse_email(sample("01_order_status")), "x",
                                               "support@shop.example").as_bytes()}
        self.assertEqual(self.run_once(), {"skip": 1})
        self.assertEqual(self.llm.calls, [])


if __name__ == "__main__":
    unittest.main()
