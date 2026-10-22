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

#: Files attached to every reply (Gmail draft, move-to-drafts and send alike).
#: They live in assets/ beside the package and are shipped in the Lambda zip.
REPLY_ATTACHMENTS = ("OPERATIONS Delivered Unbilled Loads Report Directions.pdf",)


def attachment_paths(names=REPLY_ATTACHMENTS) -> list:
    """Existing files for the named attachments, looked up in assets/."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "assets"
    found = [root / name for name in names if (root / name).is_file()]
    return found


def attach_files(message, paths) -> None:
    """Append the files to an EmailMessage (turns it into multipart/mixed)."""
    import mimetypes

    for path in paths:
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)


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
    #: File names (in assets/) attached when the reply is sent or drafted.
    attachments: list[str] = field(default_factory=list)

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
            attachments=[str(x) for x in d.get("attachments") or []],
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


# ----------------------------------------------------------------------------
# The standing tracker: one message, rewritten each run, pinned once by a human.
# ----------------------------------------------------------------------------
ACTION_QUEUE_FILTER = "queue_filter"
ACTION_QUEUE_PAGE = "queue_page"
QUEUE_BUCKETS: tuple[tuple[str, str], ...] = (("all", "All"), ("today", "Today"), ("yesterday", "Yesterday"), ("older", "2 days +"))
TRACKER_CARD_ID = "approval-queue-tracker"
_MESSAGE_BUDGET = 14_000  # bytes of cards per message that a click response is known to survive
_CARD_WIDGETS = 90


def chat_message_link(message_name: str) -> str:
    """spaces/S/messages/T.M -> https://chat.google.com/room/S/T/M?cls=10 ('' if unparseable)."""
    parts = message_name.strip().strip("/").split("/")
    if len(parts) != 4 or parts[0] != "spaces" or parts[2] != "messages":
        return ""
    space, message = parts[1], parts[3]
    thread, _, tail = message.partition(".")
    if not (space and thread and tail):
        return ""
    return f"https://chat.google.com/room/{space}/{thread}/{tail}?cls=10"


def bucket_of(hours_old: float) -> str:
    days = int(hours_old // 24)
    return "today" if days <= 0 else ("yesterday" if days == 1 else "older")


def _age(hours: float) -> str:
    return f"{max(hours, 0):.0f}h old" if hours < 24 else f"{hours / 24:.0f}d old"


def _created(entry: PendingEntry, fallback: datetime) -> datetime:
    try:
        dt = datetime.fromisoformat(entry.created_at)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return fallback


def _chip(key: str, label: str, count: int, selected: bool, action_url: str) -> dict:
    return {
        "text": f"{'● ' if selected else ''}{label} ({count})",
        "disabled": selected,
        "onClick": {"action": {"function": action_url, "parameters": [
            {"key": "action", "value": ACTION_QUEUE_FILTER}, {"key": "bucket", "value": key},
        ]}},
    }


def _page_button(label: str, bucket: str, offset: int, action_url: str) -> dict:
    return {
        "text": label,
        "onClick": {"action": {"function": action_url, "parameters": [
            {"key": "action", "value": ACTION_QUEUE_PAGE}, {"key": "bucket", "value": bucket},
            {"key": "offset", "value": str(max(offset, 0))},
        ]}},
    }


def queue_cards(
    entries: list[PendingEntry],
    *,
    now: datetime,
    expiry_days: int,
    refreshed: str = "",
    bucket: str = "all",
    offset: int = 0,
    action_url: str = "",
) -> list[dict]:
    """The tracker message's cards: date chips, the headline, and one row per pending
    card (oldest first, those about to expire at the top), linked to the card and to
    the email. Split into several cards when the rows would exceed Chat's widget cap."""
    cutoff_hours = max(expiry_days, 0) * 24
    aged = sorted(
        (((now - _created(e, now)).total_seconds() / 3600.0, e) for e in entries),
        key=lambda pair: pair[0], reverse=True,
    )
    counts = {key: 0 for key, _ in QUEUE_BUCKETS}
    for hours, _ in aged:
        counts["all"] += 1
        counts[bucket_of(hours)] += 1
    selected = bucket if bucket in counts else "all"
    shown = aged if selected == "all" else [p for p in aged if bucket_of(p[0]) == selected]
    start = 0 if offset >= len(shown) else max(offset, 0)
    page = shown[start:]
    expiring = sum(1 for hours, _ in aged if cutoff_hours and hours >= cutoff_hours - 24)

    widgets: list[dict] = []
    if action_url:
        widgets.append({"buttonList": {"buttons": [
            _chip(key, label, counts[key], key == selected, action_url) for key, label in QUEUE_BUCKETS
        ]}})
    total = len(aged)
    headline = f"<b>{total}</b> drafted repl{'y' if total == 1 else 'ies'}"
    headline += f" — <b>{expiring}</b> within 24h of expiring" if expiring else " — none near expiry"
    if selected != "all":
        headline += f"<br><b>{len(shown)}</b> from {dict(QUEUE_BUCKETS)[selected].lower()}"
    widgets.append({"decoratedText": {"topLabel": "Waiting on a click", "text": headline, "wrapText": True}})
    if not aged:
        widgets.append({"decoratedText": {"topLabel": "Queue", "text": "Empty — every drafted reply has been actioned."}})
    elif not page:
        widgets.append({"decoratedText": {"topLabel": "Nothing in this day", "text": "No unanswered drafts from that day. Tap All to see the rest.", "wrapText": True}})

    spent = len(json.dumps(widgets)) + 200
    listed = 0
    for hours, entry in page:
        left = cutoff_hours - hours
        when = "EXPIRED" if cutoff_hours and left <= 0 else (f"{left:.0f}h left" if cutoff_hours else _age(hours))
        customer = entry.facts[0][1] if entry.facts else entry.to
        card_link = chat_message_link(entry.chat_message)
        primary = f'<a href="{card_link}">{_esc(customer[:60])}</a>' if card_link else _esc(customer[:60])
        row = {"decoratedText": {
            "topLabel": f"{_age(hours)} · {when}",
            "text": f'<font color="#A32D2D"><b>UNANSWERED</b></font> {primary} · <a href="{gmail_link(entry.message_id)}">email</a>'
                    f"<br>{_esc(entry.subject[:90])}",
            "wrapText": True,
        }}
        cost = len(json.dumps(row))
        if listed and spent + cost > _MESSAGE_BUDGET:
            break
        widgets.append(row)
        spent += cost
        listed += 1

    if listed and len(shown) > listed:
        first, last = start + 1, start + listed
        widgets.append({"decoratedText": {"topLabel": "This page", "text": f"Showing <b>{first}-{last}</b> of <b>{len(shown)}</b>, oldest first.", "wrapText": True}})
        if action_url:
            buttons = []
            if start > 0:
                buttons.append(_page_button("◀ Newer", selected, max(start - listed, 0), action_url))
            if last < len(shown):
                buttons.append(_page_button("Older ▶", selected, last, action_url))
            if buttons:
                widgets.append({"buttonList": {"buttons": buttons}})

    header = {
        "title": "Drafts awaiting approval",
        "subtitle": f"expire after {expiry_days} day{'' if expiry_days == 1 else 's'}" + (f" · refreshed {refreshed}" if refreshed else ""),
    }
    cards: list[dict] = []
    current: list[dict] = []
    for widget in widgets:
        if current and len(current) >= _CARD_WIDGETS:
            cards.append({"cardId": f"{TRACKER_CARD_ID}-{len(cards)}", "card": {"sections": [{"widgets": current}]}})
            current = []
        current.append(widget)
    cards.append({"cardId": f"{TRACKER_CARD_ID}-{len(cards)}", "card": {"sections": [{"widgets": current}]}})
    cards[0]["card"]["header"] = header
    return cards


def build_card(
    entry: PendingEntry,
    action_url: str,
    interactive: bool = True,
    status: str = "",
    approve_enabled: bool = True,
) -> dict:
    """The card. ``status`` non-empty renders the terminal form: status line shown,
    action buttons gone, so the feed doubles as the audit trail. ``approve_enabled``
    False leaves out "Approve & send as me" (the reply then goes out from Gmail after
    "Move to my Gmail Drafts")."""
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
        if approve_enabled:
            buttons.append(_action_button("Approve & send as me", ACTION_APPROVE, entry.entry_id, action_url))
        buttons += [
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

    title = entry.title
    if not approve_enabled and title == CARD_TITLE:
        title = "Credit limit increase reply — move to your Drafts to send"
    return {
        "cardId": f"approval-{entry.entry_id}",
        "card": {
            "header": {"title": title, "subtitle": f"to {entry.to}"},
            "sections": [
                {"widgets": widgets},
                {"header": "Draft reply", "widgets": [{"textParagraph": {"text": body}}]},
                {"widgets": tail},
            ],
        },
    }
