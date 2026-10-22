"""Offline checks for the click callback: event shapes, MIME, and the claim/act rules.

Run:  python -m pytest tests -q      (or:  python tests/test_callback.py)
"""
from __future__ import annotations

import email
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from credit_bot import callback  # noqa: E402
from credit_bot.approval_cards import PendingEntry, build_card, entry_id_for  # noqa: E402


class FakeStore:
    def __init__(self):
        self.pendings: dict[str, PendingEntry] = {}
        self.results: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}

    def pending(self, entry_id):
        return self.pendings.get(entry_id)

    def result(self, entry_id):
        return self.results.get(entry_id)

    def put_result(self, entry_id, result):
        self.results[entry_id] = result

    def claim(self, entry_id, payload):
        if entry_id in self.claims:
            return False
        self.claims[entry_id] = payload
        return True

    def release_claim(self, entry_id):
        self.claims.pop(entry_id, None)


class FakeGmail:
    def __init__(self, clicker, fail=False):
        self.clicker, self.fail, self.sent, self.drafted = clicker, fail, [], []

    def resolve_thread(self, message_id):
        return "t-clicker"

    def send(self, raw, thread_id):
        if self.fail:
            raise RuntimeError("gmail 503")
        self.sent.append((raw, thread_id))
        return "sent-1"

    def create_draft(self, raw, thread_id):
        self.drafted.append((raw, thread_id))
        return "draft-1"


def _entry() -> PendingEntry:
    return PendingEntry(
        entry_id=entry_id_for("<autogen-java-1@google.com>"),
        message_id="<autogen-java-1@google.com>",
        thread_id="t-reading",
        to="cole.cascio@circledelivers.com",
        cc=["teamcredit@circledelivers.com", "james.hacker@circledelivers.com"],
        reply_to="teamcredit@circledelivers.com",
        subject="Re: 10343 CREDIT LIMIT INCREASE REQUEST Shepard Exposition Services, Inc.",
        body="Operations Action Items - Immediate Attention Required\n- Loads on Billing Hold: 5",
        body_html='<div dir="ltr"><font face="arial, sans-serif"><b>Operations Action Items</b></font></div>',
        facts=[["Customer", "10343 — Shepard Exposition Services, Inc."], ["Loads on Billing Hold", "5"]],
    )


def test_entry_roundtrip_and_card():
    e = _entry()
    assert PendingEntry.from_json(e.to_json()) == e
    card = build_card(e, "https://cb/")
    dump = json.dumps(card)
    assert "Approve & send as me" in dump and "Reject" in dump and "Open the email in Gmail" in dump
    assert '"topLabel": "Customer"' in dump and "rfc822msgid" in dump
    done = build_card(e, "", status="Sent by x")
    assert "Approve & send as me" not in json.dumps(done) and "Sent by x" in json.dumps(done)


def test_mime_has_text_and_html_and_threading():
    raw = callback.build_mime(_entry(), "brandon@circledelivers.com")
    msg = email.message_from_bytes(raw)
    assert msg["From"] == "brandon@circledelivers.com"
    assert msg["To"] == "cole.cascio@circledelivers.com"
    assert msg["Cc"] == "teamcredit@circledelivers.com, james.hacker@circledelivers.com"
    assert msg["Reply-To"] == "teamcredit@circledelivers.com"
    assert msg["In-Reply-To"] == "<autogen-java-1@google.com>"
    assert {p.get_content_type() for p in msg.walk() if not p.is_multipart()} == {"text/plain", "text/html"}


def test_normalise_both_schemas():
    addons = {
        "chat": {"buttonClickedPayload": {}, "user": {"email": "Brandon@circledelivers.com"}},
        "commonEventObject": {"invokedFunction": "https://cb/", "parameters": {"action": "approve", "entry": "abc"}},
    }
    assert callback.normalise_event(addons) == ("CARD_CLICKED", "brandon@circledelivers.com", "approve", "abc", True)
    legacy = {
        "type": "CARD_CLICKED", "user": {"email": "sam@circledelivers.com"},
        "action": {"actionMethodName": "https://cb/", "parameters": [{"key": "action", "value": "reject"}, {"key": "entry", "value": "xyz"}]},
    }
    assert callback.normalise_event(legacy) == ("CARD_CLICKED", "sam@circledelivers.com", "reject", "xyz", False)


def test_approve_sends_as_clicker_and_second_click_loses():
    store, e = FakeStore(), _entry()
    store.pendings[e.entry_id] = e
    fake = FakeGmail("brandon@circledelivers.com")
    resp = callback.act(store, e.entry_id, "approve", "brandon@circledelivers.com", True, gmail_factory=lambda c: fake)
    assert len(fake.sent) == 1 and fake.sent[0][1] == "t-clicker"
    assert store.results[e.entry_id]["status"] == "sent"
    assert "Sent by brandon@circledelivers.com" in resp["body"] and "updateMessageAction" in resp["body"]
    again = callback.act(store, e.entry_id, "approve", "sam@circledelivers.com", True, gmail_factory=lambda c: FakeGmail(c))
    assert "Already sent by brandon@circledelivers.com" in again["body"]


def test_move_reject_and_failed_send_releases_claim():
    store, e = FakeStore(), _entry()
    store.pendings[e.entry_id] = e
    fake = FakeGmail("h@circledelivers.com")
    callback.act(store, e.entry_id, "move_to_drafts", "h@circledelivers.com", False, gmail_factory=lambda c: fake)
    assert fake.drafted and not fake.sent and store.results[e.entry_id]["status"] == "moved"

    store2, e2 = FakeStore(), _entry()
    store2.pendings[e2.entry_id] = e2
    failing = FakeGmail("h@circledelivers.com", fail=True)
    resp = callback.act(store2, e2.entry_id, "approve", "h@circledelivers.com", True, gmail_factory=lambda c: failing)
    assert "try again" in resp["body"] and e2.entry_id not in store2.claims and e2.entry_id not in store2.results
    resp = callback.act(store2, e2.entry_id, "reject", "h@circledelivers.com", True, gmail_factory=lambda c: failing)
    assert store2.results[e2.entry_id]["status"] == "rejected"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL OK")
