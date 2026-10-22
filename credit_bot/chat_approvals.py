"""Worker side of the Google Chat approval flow: store the entry, post the card.

The credit bot has its own Chat app (its own Google Cloud project and service
account), its own click callback (deploy/template.yaml) and its own state bucket,
so nothing here touches the payment bot.

* the entry goes to S3 ``approvals/pending/<entry_id>.json`` in APPROVALS_BUCKET;
* the card goes to CHAT_SPACE as the Chat app (CHAT_SA_FILE, app auth, chat.bot
  scope), with buttons pointing at CHAT_ACTION_URL (the stack's CallbackUrl output);
* a click is handled by credit_bot/callback.py running in the Lambda, which sends
  the stored reply as the clicker and rewrites the card.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2 import service_account
import google.auth.transport.requests as google_requests

from .approval_cards import PendingEntry, build_card, build_notice_card, entry_id_for, queue_cards
from .gmail_client import RequestEmail, reply_recipients, reply_subject
from .metrics import CreditMetrics
from .template import billing_hold_value, missing_ci_value, missing_rc_value, unbilled_value

log = logging.getLogger(__name__)

CHAT_API = "https://chat.googleapis.com/v1"
CHAT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
S3_PREFIX = "approvals"


def build_entry(
    req: RequestEmail,
    metrics: CreditMetrics,
    html: str,
    text: str,
    teamcredit: str,
    form_sender: str,
    extra_cc: list[str] | None = None,
) -> PendingEntry:
    to, cc = reply_recipients(req, teamcredit, form_sender, "", extra_cc)
    facts = [
        ["Customer", f"{metrics.customer_id} — {metrics.customer_name or req.customer_name}"],
        ["Requester", req.requester_email or "(unknown)"],
        ["Unbilled Load Balance", unbilled_value(metrics)],
        ["Loads Missing Rate Confirmations", missing_rc_value(metrics)],
        ["Loads Missing Carrier Invoices", missing_ci_value(metrics)],
        ["Loads on Billing Hold", billing_hold_value(metrics)],
    ]
    message_id = req.rfc_message_id or req.message_id
    return PendingEntry(
        entry_id=entry_id_for(message_id),
        message_id=message_id,
        thread_id=req.thread_id,
        to=to[0] if to else "",
        cc=cc,
        reply_to=teamcredit,
        subject=reply_subject(req.subject),
        body=text,
        body_html=html,
        facts=facts,
    )


class ApprovalStore:
    """pending / results families under approvals/ in the credit bot's bucket."""

    def __init__(self, bucket: str, prefix: str = S3_PREFIX):
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._s3 = boto3.client("s3")

    def _key(self, family: str, entry_id: str) -> str:
        return f"{self.prefix}/{family}/{entry_id}.json"

    def _get(self, key: str) -> dict | None:
        try:
            raw = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self._s3.exceptions.NoSuchKey:
            return None
        return json.loads(raw.decode("utf-8"))

    def pending(self, entry_id: str) -> dict | None:
        return self._get(self._key("pending", entry_id))

    def result(self, entry_id: str) -> dict | None:
        return self._get(self._key("results", entry_id))

    def status(self, entry_id: str) -> str:
        """'' when unknown, 'pending' while a card waits, else the recorded outcome."""
        result = self.result(entry_id)
        if result:
            return str(result.get("status") or "done")
        return "pending" if self.pending(entry_id) else ""

    def put_pending(self, entry: PendingEntry) -> None:
        self._s3.put_object(
            Bucket=self.bucket,
            Key=self._key("pending", entry.entry_id),
            Body=entry.to_json().encode("utf-8"),
            ContentType="application/json",
        )

    def put_result(self, entry_id: str, result: dict) -> None:
        self._s3.put_object(
            Bucket=self.bucket, Key=self._key("results", entry_id),
            Body=json.dumps(result, sort_keys=True).encode("utf-8"), ContentType="application/json",
        )

    def _list_ids(self, family: str) -> list[str]:
        ids: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/{family}/"):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                if name.endswith(".json"):
                    ids.append(name[:-5])
        return ids

    def live_entries(self) -> list[PendingEntry]:
        """Pending entries with no result yet: the cards still waiting for a click."""
        results = set(self._list_ids("results"))
        entries: list[PendingEntry] = []
        for entry_id in self._list_ids("pending"):
            if entry_id in results:
                continue
            raw = self._get(self._key("pending", entry_id))
            if raw:
                entries.append(PendingEntry.from_json(json.dumps(raw)))
        return entries

    # -- the tracker message's name, one per space --------------------------
    _TRACKER_KEY = "state/tracker.json"

    def tracker(self) -> str:
        data = self._get(self._TRACKER_KEY) or {}
        return str(data.get("name") or "")

    def set_tracker(self, name: str) -> None:
        self._s3.put_object(
            Bucket=self.bucket, Key=self._TRACKER_KEY,
            Body=json.dumps({"name": name}).encode("utf-8"), ContentType="application/json",
        )


class ChatPoster:
    """Posts cards into the credit space as the credit bot's Chat app."""

    def __init__(self, service_account_file, space: str, action_url: str, timeout: int = 30):
        self.space = space.strip().strip("/")
        self.action_url = action_url.strip()
        self.timeout = timeout
        self._creds = service_account.Credentials.from_service_account_file(
            str(service_account_file), scopes=[CHAT_SCOPE]
        )

    def _headers(self) -> dict:
        if not self._creds.valid:
            self._creds.refresh(google_requests.Request())
        return {"Authorization": f"Bearer {self._creds.token}", "Content-Type": "application/json"}

    def spaces(self) -> list[dict]:
        resp = requests.get(f"{CHAT_API}/spaces", headers=self._headers(), params={"pageSize": 100}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("spaces", [])

    def post_card(self, card: dict, fallback_text: str, thread_key: str) -> str:
        url = (
            f"{CHAT_API}/{urllib.parse.quote(self.space)}/messages"
            "?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        )
        payload = {"thread": {"threadKey": thread_key}, "text": fallback_text, "cardsV2": [card]}
        resp = requests.post(url, headers=self._headers(), data=json.dumps(payload), timeout=self.timeout)
        if not resp.ok:
            raise RuntimeError(f"Chat post failed: HTTP {resp.status_code}: {resp.text[:300]}")
        return str(resp.json().get("name") or "")

    def post_cards(self, cards: list[dict], fallback_text: str) -> str:
        """Post an UNTHREADED message (a pin applies to a message, so the tracker must
        not live inside a thread). Returns the message name."""
        url = f"{CHAT_API}/{urllib.parse.quote(self.space)}/messages"
        resp = requests.post(url, headers=self._headers(), data=json.dumps({"text": fallback_text, "cardsV2": cards}), timeout=self.timeout)
        if not resp.ok:
            raise RuntimeError(f"Chat post failed: HTTP {resp.status_code}: {resp.text[:300]}")
        return str(resp.json().get("name") or "")

    def patch_cards(self, message_name: str, cards: list[dict]) -> int:
        """Replace a message's cards in place. Returns the HTTP status: 403/404 means
        the message is gone or not ours (Chat reports both the same way)."""
        url = f"{CHAT_API}/{urllib.parse.quote(message_name)}?updateMask=cardsV2"
        resp = requests.patch(url, headers=self._headers(), data=json.dumps({"cardsV2": cards}), timeout=self.timeout)
        if not resp.ok:
            log.warning("Chat card update %s -> HTTP %s: %s", message_name, resp.status_code, resp.text[:200])
        return resp.status_code


def sweep_expired(store: ApprovalStore, poster: ChatPoster, expiry_days: int, now: datetime | None = None) -> int:
    """Expire cards nobody clicked within expiry_days: record the result and rewrite
    the card so the feed says so. Returns how many expired."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=expiry_days)
    expired = 0
    for entry in store.live_entries():
        try:
            created = datetime.fromisoformat(entry.created_at)
            created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created > cutoff:
            continue
        stamp = now.strftime("%Y-%m-%d %H:%M UTC")
        store.put_result(entry.entry_id, {"status": "expired", "by": "sweep", "at": stamp})
        if entry.chat_message:
            card = build_card(entry, "", status=f"Expired — nobody clicked within {expiry_days} days · {stamp}. Handle the request from Gmail.")
            status = poster.patch_cards(entry.chat_message, [card])
            if status != 200:
                log.warning("could not mark card %s expired: HTTP %s", entry.chat_message, status)
        log.info("expired entry %s (%s)", entry.entry_id, entry.subject[:60])
        expired += 1
    return expired


def refresh_tracker(store: ApprovalStore, poster: ChatPoster, expiry_days: int, now: datetime | None = None) -> str:
    """Rewrite the standing 'Drafts awaiting approval' message in place; post it the
    first time (or again if it was deleted). Returns the message name."""
    now = now or datetime.now(timezone.utc)
    cards = queue_cards(
        store.live_entries(), now=now, expiry_days=expiry_days,
        refreshed=now.strftime("%b %d %H:%M UTC"), action_url=poster.action_url,
    )
    name = store.tracker()
    if name:
        status = poster.patch_cards(name, cards)
        if status == 200:
            return name
        log.warning("tracker %s could not be updated (HTTP %s); posting a new one", name, status)
    name = poster.post_cards(cards, fallback_text="Drafts awaiting approval")
    store.set_tracker(name)
    log.info("tracker posted: %s — pin it in the space", name)
    return name


def publish_notice(req: RequestEmail, reason: str, poster: ChatPoster) -> str:
    """Post a no-buttons card for a request the bot cannot draft. Returns the message name."""
    f = req.fields
    facts = [
        ["Customer", f"{req.customer_label} — {req.customer_name}"],
        ["Needed in", f.get("Needed In", "")],
        ["Requester", req.requester_email or "(unknown)"],
        ["Amount requested for current loads", f.get("Amount Requested for current loads", "")],
        ["Estimated amount needed, next 30 days", f.get("Estimated Amount Needed for next 30 Days", "")],
        ["Team / POD", f.get("Team / POD", "")],
    ]
    message_id = req.rfc_message_id or req.message_id
    card = build_notice_card(
        title="Credit limit increase request — needs a manual reply",
        subtitle=req.subject,
        facts=facts,
        reason=reason,
        message_id=message_id,
        card_id=f"notice-{entry_id_for(message_id)}",
    )
    return poster.post_card(card, fallback_text=f"Credit request (manual): {req.subject}", thread_key=f"notice-{entry_id_for(message_id)}")


def publish(entry: PendingEntry, store: ApprovalStore, poster: ChatPoster, interactive: bool = True) -> str:
    """Store the entry, then post its card. Returns the Chat message name.

    The entry is written first so a click can never find a card without its entry.
    A failed post leaves a pending entry, which the next run reports as 'pending'
    rather than duplicating; the caller falls back to a Gmail draft."""
    store.put_pending(entry)
    card = build_card(entry, poster.action_url, interactive=interactive)
    customer = entry.facts[0][1] if entry.facts else entry.to
    name = poster.post_card(card, fallback_text=f"Credit limit increase reply for {customer}", thread_key=entry.entry_id)
    if name:
        entry.chat_message = name
        store.put_pending(entry)
    log.info("Chat card %s posted for entry %s", name, entry.entry_id)
    return name
