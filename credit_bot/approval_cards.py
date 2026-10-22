"""Approval entries and Google Chat cards for credit replies.

Shared by the worker (which posts the card) and the click callback Lambda (which
rewrites it after a click), so both render the same layout. Pure Python: no network,
no cloud SDKs, so the callback package stays small.

An **entry** holds everything a send needs (recipients, subject, threading id, text
and HTML body); a **card** shows the figures, the recipients, the draft text and the
buttons. Buttons carry only the entry id and a verb, never content.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

CARD_TITLE = "Credit limit increase reply — approve to send as yourself"
ACTION_APPROVE = "approve"
ACTION_MOVE = "move_to_drafts"
ACTION_REJECT = "reject"
_BODY_LIMIT = 3600


def entry_id_for(message_id: str) -> str:
    """Deterministic, key-safe id from the request's RFC Message-ID."""
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]


def gmail_link(message_id: str) -> str:
    """Opens the request in the CLICKER's own Gmail (thread ids are per mailbox,
    so a search by RFC id is the one link that works for every member)."""
    query = urllib.parse.quote(f"rfc822msgid:{message_id.strip('<>')}", safe="")
    return f"https://mail.google.com/mail/u/0/#search/{query}"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class PendingEntry:
    entry_id: str
    message_id: str
    thread_id: str
    to: str
    cc: list[str]
    reply_to: str
    subject: str
    body: str
    body_html: str = ""
    facts: list[list[str]] = field(default_factory=list)
    title: str = CARD_TITLE
    chat_message: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "PendingEntry":
        d = json.loads(raw)
        return cls(
            entry_id=str(d["entry_id"]),
            message_id=str(d["message_id"]),
            thread_id=str(d.get("thread_id") or ""),
            to=str(d.get("to") or ""),
            cc=[str(x) for x in d.get("cc") or []],
            reply_to=str(d.get("reply_to") or ""),
            subject=str(d.get("subject") or ""),
            body=str(d.get("body") or ""),
            body_html=str(d.get("body_html") or ""),
            facts=[[str(a), str(b)] for a, b in (d.get("facts") or []) if a is not None],
            title=str(d.get("title") or CARD_TITLE),
            chat_message=str(d.get("chat_message") or ""),
            created_at=str(d.get("created_at") or datetime.now(timezone.utc).isoformat()),
        )


def _action_button(text: str, action: str, entry_id: str, action_url: str) -> dict:
    # On the add-ons runtime console-configured Chat apps use, `function` must be the
    # HTTPS endpoint itself; the verb rides in the parameters.
    return {
        "text": text,
        "onClick": {
            "action": {
                "function": action_url or action,
                "parameters": [
                    {"key": "action", "value": action},
                    {"key": "entry", "value": entry_id},
                ],
            }
        },
    }


def build_notice_card(
    *,
    title: str,
    subtitle: str,
    facts: list[list[str]],
    reason: str,
    message_id: str,
    card_id: str,
) -> dict:
    """A card with no action buttons: what the request is and why the bot could not
    draft it (for example a Circle Transportation customer, whose figures are not
    in Transport Pro). The card IS the task: someone answers it from Gmail."""
    widgets = [
        {"decoratedText": {"topLabel": label, "text": _esc(value) or "(none)", "wrapText": True}}
        for label, value in facts
    ]
    widgets.append({"decoratedText": {"topLabel": "Why no draft", "text": _esc(reason), "wrapText": True}})
    widgets.append({"buttonList": {"buttons": [
        {"text": "Open the email in Gmail", "onClick": {"openLink": {"url": gmail_link(message_id)}}}
    ]}})
    return {
        "cardId": card_id,
        "card": {"header": {"title": title, "subtitle": subtitle}, "sections": [{"widgets": widgets}]},
    }


def build_card(entry: PendingEntry, action_url: str, interactive: bool = True, status: str = "") -> dict:
    """The card. ``status`` non-empty renders the terminal form: status line shown,
    action buttons gone, so the feed doubles as the audit trail."""
    widgets = [
        {"decoratedText": {"topLabel": label, "text": _esc(value) or "(none)", "wrapText": True}}
        for label, value in entry.facts
    ]
    widgets.append({"decoratedText": {"topLabel": "To", "text": entry.to or "(unknown)"}})
    if entry.cc:
        widgets.append({"decoratedText": {"topLabel": "Cc", "text": ", ".join(entry.cc), "wrapText": True}})
    if entry.reply_to:
        widgets.append({"decoratedText": {"topLabel": "Reply-To", "text": entry.reply_to}})
    widgets.append({"decoratedText": {"topLabel": "Subject", "text": _esc(entry.subject), "wrapText": True}})

    body = entry.body
    if len(body) > _BODY_LIMIT:
        body = body[: _BODY_LIMIT - 1].rstrip() + "…\n\n(card view truncated — the send uses the full stored text)"

    buttons: list[dict] = []
    if interactive and not status:
        buttons = [
            _action_button("Approve & send as me", ACTION_APPROVE, entry.entry_id, action_url),
            _action_button("Move to my Gmail Drafts", ACTION_MOVE, entry.entry_id, action_url),
            _action_button("Reject", ACTION_REJECT, entry.entry_id, action_url),
        ]
    buttons.append(
        {"text": "Open the email in Gmail", "onClick": {"openLink": {"url": gmail_link(entry.message_id)}}}
    )

    tail: list[dict] = []
    if status:
        tail.append({"decoratedText": {"topLabel": "Status", "text": _esc(status), "wrapText": True}})
    elif not interactive:
        tail.append({"decoratedText": {"topLabel": "Status", "text": "Shadow mode — the draft is also in Gmail Drafts."}})
    tail.append({"buttonList": {"buttons": buttons}})

    return {
        "cardId": f"approval-{entry.entry_id}",
        "card": {
            "header": {"title": entry.title, "subtitle": f"to {entry.to}"},
            "sections": [
                {"widgets": widgets},
                {"header": "Draft reply", "widgets": [{"textParagraph": {"text": body}}]},
                {"widgets": tail},
            ],
        },
    }
