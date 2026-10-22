"""Gmail access through the service account's domain-wide delegation.

teamcredit@circledelivers.com is a Google Group, which cannot be impersonated,
so the bot impersonates a member mailbox (GMAIL_USER) and filters that inbox
for the form emails addressed to the group. Drafts are created in that same
mailbox, threaded onto the original request.
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

from .approval_cards import attach_files, attachment_paths

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

# "5245 CREDIT LIMIT INCREASE REQUEST Quality Pork International"  (Transport Pro id)
# "CT 1562 CREDIT LIMIT INCREASE REQUEST Nolan Transportation Group" (Circle
# Transportation id — a different operating company and TMS, not in this
# Transport Pro tenant). Also tolerates "[5245] CREDIT LIMIT INCREASE REQUEST ...".
SUBJECT_RE = re.compile(
    r"^\s*\[?\s*(?P<system>CT)?\s*(?P<customer_id>\d{1,9})\s*\]?\s+CREDIT\s+LIMIT\s+INCREASE\s+REQUEST\b\s*(?P<customer_name>.*?)\s*$",
    re.IGNORECASE,
)
SYSTEM_TPRO = "TPRO"
SYSTEM_CT = "CT"
REPLY_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd)\s*:", re.IGNORECASE)
BODY_FIELD_RE = re.compile(r"^\s*(?P<key>[^:]{3,60}?)\s*:\s*(?P<value>.*?)\s*$")


@dataclass
class RequestEmail:
    message_id: str
    thread_id: str
    rfc_message_id: str
    subject: str
    date: str
    sender: str
    to: list[str]
    cc: list[str]
    customer_id: int
    customer_name: str
    requester_email: str | None
    fields: dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    #: SYSTEM_TPRO (Transport Pro customer id) or SYSTEM_CT (Circle Transportation id).
    system: str = SYSTEM_TPRO

    @property
    def customer_label(self) -> str:
        return f"CT {self.customer_id}" if self.system == SYSTEM_CT else str(self.customer_id)


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _addresses(value: str) -> list[str]:
    return [addr.lower() for _, addr in getaddresses([value]) if addr]


def _walk_parts(payload: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    stack = [payload]
    while stack:
        part = stack.pop()
        for child in part.get("parts") or []:
            stack.append(child)
        data = (part.get("body") or {}).get("data")
        if data:
            text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            out.append((part.get("mimeType", ""), text))
    return out


def parse_subject(subject: str) -> tuple[int, str, str] | None:
    """(customer id, customer name, system) or None for replies / other subjects."""
    if REPLY_PREFIX_RE.match(subject):
        return None
    m = SUBJECT_RE.match(subject)
    if not m:
        return None
    system = SYSTEM_CT if (m.group("system") or "").upper() == "CT" else SYSTEM_TPRO
    return int(m.group("customer_id")), m.group("customer_name").strip(), system


def reply_recipients(
    req: RequestEmail,
    teamcredit: str,
    form_sender: str,
    sender: str = "",
    extra_cc: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """(to, cc) for the reply: the requester, with the group and the request's own
    recipients copied; the form bot and the sending mailbox itself are left out."""

    to = [req.requester_email.lower()] if req.requester_email else []
    cc: list[str] = []
    for addr in [teamcredit, *req.to, *req.cc, *(extra_cc or [])]:
        addr = addr.lower()
        if addr in to or addr in cc or addr == form_sender.lower() or addr == sender.lower():
            continue
        cc.append(addr)
    if not to and cc:
        to = [cc.pop(0)]
    return to, cc


def reply_subject(subject: str) -> str:
    return subject if REPLY_PREFIX_RE.match(subject) else f"Re: {subject}"


def parse_body_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        m = BODY_FIELD_RE.match(line)
        if m:
            fields[m.group("key").strip()] = m.group("value").strip()
    return fields


class GmailClient:
    def __init__(self, service_account_file: Path, user: str):
        creds = service_account.Credentials.from_service_account_file(
            str(service_account_file), scopes=SCOPES, subject=user
        )
        self.user = user
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ------------------------------------------------------------- reading
    def find_requests(
        self, form_sender: str, teamcredit: str, newer_than_days: int
    ) -> list[RequestEmail]:
        query = (
            f'from:{form_sender} to:{teamcredit} subject:"CREDIT LIMIT INCREASE REQUEST" '
            f"newer_than:{newer_than_days}d"
        )
        ids: list[str] = []
        token = None
        while True:
            resp = (
                self.svc.users()
                .messages()
                .list(userId="me", q=query, maxResults=100, pageToken=token)
                .execute()
            )
            ids.extend(m["id"] for m in resp.get("messages", []))
            token = resp.get("nextPageToken")
            if not token:
                break

        requests_found: list[RequestEmail] = []
        for mid in ids:
            req = self.get_request(mid)
            if req:
                requests_found.append(req)
            else:
                log.warning("Message %s matched the search but its subject could not be parsed as a request; ignored", mid)
        # oldest first so drafts are created in arrival order
        requests_found.sort(key=lambda r: r.date)
        return requests_found

    def get_request(self, message_id: str) -> RequestEmail | None:
        msg = self.svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        headers = msg["payload"].get("headers", [])
        subject = _header(headers, "Subject")
        parsed = parse_subject(subject)
        if not parsed:
            if not REPLY_PREFIX_RE.match(subject):
                log.warning("Unrecognised request subject on %s: %r", message_id, subject)
            return None
        customer_id, customer_name, system = parsed

        parts = _walk_parts(msg["payload"])
        text = next((t for mt, t in parts if mt == "text/plain"), "")
        if not text:
            html = next((t for mt, t in parts if mt == "text/html"), "")
            text = re.sub(r"<[^>]+>", "\n", html)
        fields = parse_body_fields(text)

        body_customer_id = re.sub(r"(?i)^\s*CT\s*", "", fields.get("Customer TMS ID Number", ""))
        if body_customer_id.strip().isdigit() and int(body_customer_id) != customer_id:
            log.warning(
                "Subject customer id %s differs from body id %s on message %s; using subject",
                customer_id, body_customer_id, message_id,
            )
        requester = fields.get("Requester Email") or None
        if requester:
            requester = parseaddr(requester)[1] or requester

        return RequestEmail(
            message_id=msg["id"],
            thread_id=msg["threadId"],
            rfc_message_id=_header(headers, "Message-ID"),
            subject=subject,
            date=_header(headers, "Date"),
            sender=parseaddr(_header(headers, "From"))[1],
            to=_addresses(_header(headers, "To")),
            cc=_addresses(_header(headers, "Cc")),
            customer_id=customer_id,
            customer_name=customer_name or fields.get("Customer Name", ""),
            requester_email=requester,
            fields=fields,
            body_text=text,
            system=system,
        )

    def thread_has_draft(self, thread_id: str) -> bool:
        thread = (
            self.svc.users()
            .threads()
            .get(userId="me", id=thread_id, format="minimal")
            .execute()
        )
        return any("DRAFT" in (m.get("labelIds") or []) for m in thread.get("messages", []))

    # ------------------------------------------------------------- drafting
    def _build_reply(
        self,
        req: RequestEmail,
        html_body: str,
        text_body: str,
        teamcredit: str,
        form_sender: str,
        extra_cc: list[str] | None = None,
    ) -> tuple[str, list[str], list[str]]:
        to, cc = reply_recipients(req, teamcredit, form_sender, self.user, extra_cc)

        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = reply_subject(req.subject)
        if req.rfc_message_id:
            msg["In-Reply-To"] = req.rfc_message_id
            msg["References"] = req.rfc_message_id
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        attach_files(msg, attachment_paths())

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return raw, to, cc

    def create_reply_draft(
        self,
        req: RequestEmail,
        html_body: str,
        text_body: str,
        teamcredit: str,
        form_sender: str,
        extra_cc: list[str] | None = None,
    ) -> str:
        raw, to, cc = self._build_reply(req, html_body, text_body, teamcredit, form_sender, extra_cc)
        draft = (
            self.svc.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw, "threadId": req.thread_id}})
            .execute()
        )
        log.info("Draft %s created on thread %s (to=%s cc=%s)", draft["id"], req.thread_id, to, cc)
        return draft["id"]

    def update_reply_draft(
        self,
        draft_id: str,
        req: RequestEmail,
        html_body: str,
        text_body: str,
        teamcredit: str,
        form_sender: str,
        extra_cc: list[str] | None = None,
    ) -> str:
        """Replace the content of an existing draft; falls back to creating a
        new one when that draft no longer exists (sent or deleted)."""
        from googleapiclient.errors import HttpError

        raw, to, cc = self._build_reply(req, html_body, text_body, teamcredit, form_sender, extra_cc)
        try:
            draft = (
                self.svc.users()
                .drafts()
                .update(userId="me", id=draft_id, body={"message": {"raw": raw, "threadId": req.thread_id}})
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            log.warning("Draft %s no longer exists; creating a new one", draft_id)
            return self.create_reply_draft(req, html_body, text_body, teamcredit, form_sender, extra_cc)
        log.info("Draft %s updated on thread %s (to=%s cc=%s)", draft["id"], req.thread_id, to, cc)
        return draft["id"]
