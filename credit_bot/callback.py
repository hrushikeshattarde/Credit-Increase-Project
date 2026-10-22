"""Google Chat click callback for the credit approval cards — the ONE place this
system sends email. Runs as an AWS Lambda behind a public Function URL that the
credit bot's Chat app calls on every button click.

Rules this module keeps (the same ones the payment bot's callback holds):

* **The send is always executed as the person who clicked.** Google Chat's
  interaction event carries the clicker's verified Workspace address; that mailbox
  is impersonated, through the service account's domain-wide delegation, for that
  one send. There is no "send as" input anywhere.
* **Content never comes from the click.** Buttons carry an entry id and a verb; the
  recipients, subject, threading id and body come from the entry the worker stored.
* **Verify before parse.** Google authenticates itself with a bearer ID token signed
  by Google for the Chat app's project-number audience (or the endpoint URL). Nothing
  touches S3 or Gmail until it verifies.
* **Exactly one click wins.** A conditional S3 write claims the entry; a second click
  or a platform retry loses the claim and is told who won. A failed action releases
  the claim so a later click can retry.

Environment (set by deploy/template.yaml):
  CREDIT_STATE_BUCKET      S3 bucket holding approvals/{pending,claims,results}/<id>.json
  CREDIT_REVIEWERS         JSON list of addresses allowed to click
  CREDIT_CHAT_AUDIENCE     Google Cloud project NUMBER of the Chat app (token audience)
  CREDIT_SA_S3_KEY         key, in the state bucket, of the service-account JSON with
                           Gmail domain-wide delegation (uploaded by deploy.py)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate

from credit_bot.approval_cards import (
    ACTION_APPROVE,
    ACTION_MOVE,
    ACTION_QUEUE_FILTER,
    ACTION_QUEUE_PAGE,
    ACTION_REJECT,
    REPLY_ATTACHMENTS,
    PendingEntry,
    attach_files,
    attachment_paths,
    build_card,
    queue_cards,
)

log = logging.getLogger("credit_callback")
log.setLevel(logging.INFO)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
_CHAT_ISSUERS = {
    "chat@system.gserviceaccount.com",
    "accounts.google.com",
    "https://accounts.google.com",
}
_CERT_URLS = (
    "https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com",
    "https://www.googleapis.com/oauth2/v1/certs",
)
_CERTS: dict[str, str] = {}
_CERTS_AT = 0.0
_SA_INFO: dict | None = None


# ---------------------------------------------------------------- settings
def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _reviewers() -> set[str]:
    raw = _env("CREDIT_REVIEWERS", "[]")
    try:
        people = json.loads(raw)
    except ValueError:
        people = [p for p in raw.split(",")]
    return {str(p).strip().lower() for p in people if str(p).strip()}


def _service_account_info() -> dict:
    """The Gmail-delegated service-account key: inline (tests) or a private object in
    the state bucket that only this function's role can read."""
    global _SA_INFO
    if _SA_INFO is None:
        inline = _env("CREDIT_GOOGLE_SA_JSON")
        if not inline:
            import boto3

            obj = boto3.client("s3").get_object(
                Bucket=_env("CREDIT_STATE_BUCKET"),
                Key=_env("CREDIT_SA_S3_KEY", "config/google-service-account.json"),
            )
            inline = obj["Body"].read().decode("utf-8")
        _SA_INFO = json.loads(inline)
    return _SA_INFO


# ----------------------------------------------------- token verification
def _fetch_certs() -> dict[str, str]:
    global _CERTS, _CERTS_AT
    if _CERTS and time.time() - _CERTS_AT < 3600:
        return _CERTS
    merged: dict[str, str] = {}
    for url in _CERT_URLS:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                merged.update(json.loads(resp.read().decode("utf-8")))
        except Exception as exc:  # one set degrading must not kill the other
            log.warning("cert fetch failed for %s: %s", url, exc)
    if merged:
        _CERTS, _CERTS_AT = merged, time.time()
    return _CERTS


def verify_token(token: str, audience: str, extra_audiences: tuple[str, ...] = ()) -> bool:
    if not token or not audience:
        return False
    try:
        from google.auth import jwt as google_jwt

        claims = google_jwt.decode(token, certs=_fetch_certs(), audience=None)
    except Exception as exc:
        log.warning("chat token rejected: %s", str(exc)[:200])
        return False
    if str(claims.get("iss") or "") not in _CHAT_ISSUERS:
        log.warning("chat token wrong issuer: %s", str(claims.get("iss"))[:100])
        return False
    aud = str(claims.get("aud") or "")
    if aud not in {audience, *[a for a in extra_audiences if a]}:
        log.warning("chat token wrong audience: %s (expected %s)", aud[:200], audience)
        return False
    return True


# ------------------------------------------------------------------ store
class Store:
    PREFIX = "approvals"

    def __init__(self, bucket: str):
        import boto3

        self.bucket = bucket
        self.s3 = boto3.client("s3")

    def _key(self, family: str, entry_id: str) -> str:
        return f"{self.PREFIX}/{family}/{entry_id}.json"

    def _get(self, key: str) -> str | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")
        except self.s3.exceptions.NoSuchKey:
            return None

    def pending(self, entry_id: str) -> PendingEntry | None:
        raw = self._get(self._key("pending", entry_id))
        return PendingEntry.from_json(raw) if raw else None

    def result(self, entry_id: str) -> dict | None:
        raw = self._get(self._key("results", entry_id))
        return json.loads(raw) if raw else None

    def put_result(self, entry_id: str, result: dict) -> None:
        self.s3.put_object(
            Bucket=self.bucket, Key=self._key("results", entry_id),
            Body=json.dumps(result, sort_keys=True).encode("utf-8"), ContentType="application/json",
        )

    def claim(self, entry_id: str, payload: dict) -> bool:
        try:
            self.s3.put_object(
                Bucket=self.bucket, Key=self._key("claims", entry_id),
                Body=json.dumps(payload, sort_keys=True).encode("utf-8"),
                ContentType="application/json",
                IfNoneMatch="*",  # atomic: exactly one caller creates the object
            )
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "412") or "PreconditionFailed" in str(exc):
                return False
            raise

    def release_claim(self, entry_id: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=self._key("claims", entry_id))
        except Exception as exc:
            log.warning("release claim failed for %s: %s", entry_id, exc)

    def _list_ids(self, family: str) -> list[str]:
        ids: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=f"{self.PREFIX}/{family}/"):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                if name.endswith(".json"):
                    ids.append(name[:-5])
        return ids

    def live_entries(self) -> list[PendingEntry]:
        """Pending entries with no result: what the tracker lists."""
        results = set(self._list_ids("results"))
        out: list[PendingEntry] = []
        for entry_id in self._list_ids("pending"):
            if entry_id in results:
                continue
            raw = self._get(self._key("pending", entry_id))
            if raw:
                out.append(PendingEntry.from_json(raw))
        return out


# ------------------------------------------------------- gmail as clicker
class ClickerGmail:
    def __init__(self, info: dict, clicker: str):
        from google.oauth2 import service_account

        self.clicker = clicker
        self.creds = service_account.Credentials.from_service_account_info(
            info, scopes=GMAIL_SCOPES, subject=clicker
        )

    def _headers(self) -> dict:
        import google.auth.transport.requests as tr

        if not self.creds.valid:
            self.creds.refresh(tr.Request())
        return {"Authorization": f"Bearer {self.creds.token}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{GMAIL_API}/users/{urllib.parse.quote(self.clicker)}/{path}",
            data=data, method=method, headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Gmail {method} {path} failed: HTTP {exc.code}: {exc.read()[:300]!r}") from exc

    def resolve_thread(self, message_id: str) -> str:
        """The clicker's own thread id for the request message, or '' (the reply still
        threads for everyone else through In-Reply-To / References)."""
        q = urllib.parse.quote(f"rfc822msgid:{message_id.strip('<>')}")
        try:
            data = self._call("GET", f"messages?q={q}&maxResults=1")
        except RuntimeError as exc:
            log.warning("thread unresolved for %s: %s", self.clicker, exc)
            return ""
        msgs = data.get("messages") or []
        return str(msgs[0].get("threadId") or "") if msgs else ""

    def send(self, raw: bytes, thread_id: str) -> str:
        body: dict = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        if thread_id:
            body["threadId"] = thread_id
        return str(self._call("POST", "messages/send", body).get("id") or "")

    def create_draft(self, raw: bytes, thread_id: str) -> str:
        message: dict = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        if thread_id:
            message["threadId"] = thread_id
        return str(self._call("POST", "drafts", {"message": message}).get("id") or "")


def build_mime(entry: PendingEntry, from_address: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = entry.to
    if entry.cc:
        msg["Cc"] = ", ".join(entry.cc)
    if entry.reply_to:
        msg["Reply-To"] = entry.reply_to
    msg["Subject"] = entry.subject
    msg["Date"] = formatdate(localtime=True)
    if entry.message_id:
        msg["In-Reply-To"] = entry.message_id
        msg["References"] = entry.message_id
    msg.set_content(entry.body)
    if entry.body_html:
        msg.add_alternative(entry.body_html, subtype="html")
    paths = attachment_paths(entry.attachments or REPLY_ATTACHMENTS)
    missing = set(entry.attachments or REPLY_ATTACHMENTS) - {p.name for p in paths}
    if missing:
        log.warning("attachments not found in the package, reply goes without them: %s", sorted(missing))
    attach_files(msg, paths)
    return msg.as_bytes()


def _approve_enabled() -> bool:
    return _env("CREDIT_APPROVE_ENABLED", "false").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------- events
def normalise_event(event: dict) -> tuple[str, str, str, str, bool]:
    """(kind, clicker, action, entry_id, addons_schema) for both Chat event shapes."""
    if isinstance(event.get("chat"), dict):
        chat = event["chat"]
        if "buttonClickedPayload" in chat:
            kind = "CARD_CLICKED"
        elif "messagePayload" in chat or "appCommandPayload" in chat:
            kind = "MESSAGE"
        elif "addedToSpacePayload" in chat:
            kind = "ADDED_TO_SPACE"
        else:
            kind = ""
        user = chat.get("user") if isinstance(chat.get("user"), dict) else {}
        common = event.get("commonEventObject") or {}
        params = common.get("parameters") if isinstance(common.get("parameters"), dict) else {}
        action = str(params.get("action") or common.get("invokedFunction") or "")
        return kind, str(user.get("email") or "").strip().lower(), action, str(params.get("entry") or ""), True

    kind = str(event.get("type") or "")
    user = event.get("user") if isinstance(event.get("user"), dict) else {}
    values: dict[str, str] = {}
    common = event.get("common") or {}
    if isinstance(common.get("parameters"), dict):
        values = {str(k): str(v) for k, v in common["parameters"].items()}
    for item in (event.get("action") or {}).get("parameters") or []:
        if isinstance(item, dict) and item.get("key") is not None:
            values.setdefault(str(item["key"]), str(item.get("value") or ""))
    action = str(values.get("action") or common.get("invokedFunction")
                 or (event.get("action") or {}).get("actionMethodName") or "")
    return kind, str(user.get("email") or "").strip().lower(), action, values.get("entry", ""), False


def _http(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def _text(text: str, addons: bool) -> dict:
    if addons:
        return _http(200, {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": {"text": text}}}}})
    return _http(200, {"text": text})


def _replace_cards(cards: list[dict], addons: bool) -> dict:
    if addons:
        return _http(200, {"hostAppDataAction": {"chatDataAction": {"updateMessageAction": {"message": {"cardsV2": cards}}}}})
    return _http(200, {"actionResponse": {"type": "UPDATE_MESSAGE"}, "cardsV2": cards})


def _replace_card(card: dict, addons: bool) -> dict:
    return _replace_cards([card], addons)


def _click_param(event: dict, key: str) -> str:
    """One click parameter, whether Chat sends parameters as a mapping or a key/value list."""
    for holder in (event.get("commonEventObject"), event.get("common"), event.get("action")):
        if not isinstance(holder, dict):
            continue
        block = holder.get("parameters")
        if isinstance(block, dict) and block.get(key) is not None:
            return str(block[key])
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict) and str(item.get("key")) == key:
                    return str(item.get("value") or "")
    return ""


def queue_response(store: Store, bucket: str, offset: int, action_url: str, addons: bool) -> dict:
    """Redraw the pinned tracker filtered to one day bucket (nothing is claimed or sent)."""
    now = datetime.now(timezone.utc)
    cards = queue_cards(
        store.live_entries(), now=now, expiry_days=int(_env("CREDIT_APPROVAL_EXPIRY_DAYS", "3") or 3),
        refreshed=now.strftime("%b %d %H:%M UTC"), bucket=bucket or "all", offset=offset, action_url=action_url,
    )
    return _replace_cards(cards, addons)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------- handler
def act(store: Store, entry_id: str, action: str, clicker: str, addons: bool, gmail_factory=None) -> dict:
    existing = store.result(entry_id)
    if existing:
        entry = store.pending(entry_id)
        status = f"Already {existing.get('status', 'done')} by {existing.get('by', 'someone')} — nothing further to do."
        return _replace_card(build_card(entry, "", status=status), addons) if entry else _text(status, addons)

    entry = store.pending(entry_id)
    if entry is None:
        return _text("This card's entry is gone (expired or never stored). Handle the request from Gmail.", addons)

    if action == ACTION_APPROVE and not _approve_enabled():
        # Switched off for now: nothing is claimed, the card stays live, and the
        # reply still goes out via Move to my Gmail Drafts + a manual send.
        log.info("approve refused (disabled) for %s by %s", entry_id, clicker)
        return _text("Approve & send is switched off for now — use Move to my Gmail Drafts and send from Gmail.", addons)

    now = _now()
    if not store.claim(entry_id, {"by": clicker, "action": action, "at": now}):
        return _text("Someone is already handling this one — check the card.", addons)

    try:
        if action == ACTION_REJECT:
            store.put_result(entry_id, {"status": "rejected", "by": clicker, "at": now})
            log.info("rejected %s by %s", entry_id, clicker)
            return _replace_card(build_card(entry, "", status=f"Rejected by {clicker} · {now}"), addons)

        gmail = (gmail_factory or (lambda c: ClickerGmail(_service_account_info(), c)))(clicker)
        thread_id = gmail.resolve_thread(entry.message_id)
        raw = build_mime(entry, clicker)

        if action == ACTION_MOVE:
            draft_id = gmail.create_draft(raw, thread_id)
            store.put_result(entry_id, {"status": "moved", "by": clicker, "at": now, "draft_id": draft_id})
            log.info("moved %s to drafts of %s", entry_id, clicker)
            return _replace_card(
                build_card(entry, "", status=f"With {clicker} in Gmail Drafts · {now} — edit there and send from Gmail."),
                addons,
            )

        if action == ACTION_APPROVE:
            sent_id = gmail.send(raw, thread_id)
            store.put_result(entry_id, {"status": "sent", "by": clicker, "at": now, "gmail_id": sent_id})
            log.info("sent %s by %s (gmail %s)", entry_id, clicker, sent_id)
            return _replace_card(build_card(entry, "", status=f"Sent by {clicker} · {now} · from {clicker}"), addons)

        store.release_claim(entry_id)
        return _text(f"Unknown action {action!r} — nothing was done.", addons)
    except Exception as exc:
        store.release_claim(entry_id)
        log.warning("action %s failed for %s by %s: %s", action, entry_id, clicker, exc)
        return _text(f"That failed ({str(exc)[:200]}) — the card is still live, try again.", addons)


def handler(event: dict | None = None, context=None) -> dict:
    event = event or {}
    method = str(((event.get("requestContext") or {}).get("http") or {}).get("method") or "").upper()
    if method and method != "POST":
        return _http(405, {"text": "POST only"})

    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
    bearer = headers.get("authorization", "")
    token = bearer[len("Bearer "):] if bearer.startswith("Bearer ") else ""
    host = headers.get("host", "").strip()
    own = (f"https://{host}/", f"https://{host}") if host else ()
    if not verify_token(token, _env("CREDIT_CHAT_AUDIENCE"), own):
        return _http(401, {"text": "unverified"})

    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")
    try:
        chat_event = json.loads(body)
        if not isinstance(chat_event, dict):
            raise ValueError
    except ValueError:
        return _http(400, {"text": "unreadable event"})

    kind, clicker, action, entry_id, addons = normalise_event(chat_event)
    log.info("chat event kind=%s action=%s entry=%s clicker=%s schema=%s", kind, action, entry_id, clicker, "addons" if addons else "legacy")

    if kind != "CARD_CLICKED":
        return _text("Credit Increase bot: approval cards are posted here by the bot; the buttons on each card are the interface.", addons)
    if not action:
        return _text("That button carried no action — repost the card.", addons)
    if clicker not in _reviewers():
        log.warning("denied: %s is not on the credit reviewer roster (entry %s)", clicker, entry_id)
        return _text(f"Sorry — {clicker or 'this account'} is not on the credit reviewer roster.", addons)

    bucket = _env("CREDIT_STATE_BUCKET")
    if not bucket:
        return _text("Misconfigured: no state bucket. Nothing was done.", addons)

    if action in (ACTION_QUEUE_FILTER, ACTION_QUEUE_PAGE):
        # A date chip or page button on the pinned tracker: redraw it, filtered. The
        # chips must point back at this very URL, derived from the request itself.
        chosen = _click_param(chat_event, "bucket")
        page = _click_param(chat_event, "offset")
        own_url = f"https://{host}/" if host else ""
        log.info("tracker chip: bucket=%s offset=%s by %s", chosen or "all", page or 0, clicker)
        return queue_response(Store(bucket), chosen, int(page) if page.isdigit() else 0, own_url, addons)

    if not entry_id:
        return _text("That button carried no entry — repost the card.", addons)
    return act(Store(bucket), entry_id, action, clicker, addons)
