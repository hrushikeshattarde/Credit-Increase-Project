"""Credit limit increase request bot.

Usage:
  python main.py                 process new request emails, create reply drafts
  python main.py --watch         keep running, polling the mailbox every POLL_SECONDS
  python main.py --dry-run       compute and render only; write HTML previews to out/, no drafts
  python main.py --force         re-process already handled emails, updating their drafts in place
  python main.py --customer 5245 compute the Transport Pro figures for one customer
  python main.py --message-id ID process one specific Gmail message id
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from credit_bot.config import Settings, load_settings
from credit_bot.gmail_client import SYSTEM_TPRO, GmailClient, RequestEmail
from credit_bot.metrics import compute_metrics
from credit_bot.state import ProcessedStore
from credit_bot.template import render_html, render_text
from credit_bot.tpro import TransportProClient

log = logging.getLogger("credit_bot")
# Dry-run previews go here; OUT_DIR overrides it (the Lambda uses /tmp/out, the
# code directory there is read-only).
import os as _os  # noqa: E402

OUT_DIR = Path(_os.environ.get("OUT_DIR") or (Path(__file__).resolve().parent / "out"))


def setup_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    file_handler = RotatingFileHandler(
        settings.log_dir / "credit_bot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


def write_preview(path: Path, html: str) -> None:
    """Local preview file (the draft itself carries the charset in its MIME headers)."""
    OUT_DIR.mkdir(exist_ok=True)
    path.write_text(
        f'<!doctype html><html><head><meta charset="utf-8"></head><body>{html}</body></html>',
        encoding="utf-8",
    )


def make_tpro(settings: Settings) -> TransportProClient:
    return TransportProClient(
        settings.tpro_base_url,
        settings.tpro_username,
        settings.tpro_password,
        settings.tpro_timeout_seconds,
    )


def chat_enabled(settings: Settings) -> bool:
    if settings.approval_mode != "chat":
        return False
    missing = [
        name for name, value in (
            ("CHAT_SPACE", settings.chat_space),
            ("CHAT_ACTION_URL", settings.chat_action_url),
            ("APPROVALS_BUCKET", settings.approvals_bucket),
        ) if not value
    ]
    if not settings.chat_sa_file.exists():
        missing.append(f"CHAT_SA_FILE ({settings.chat_sa_file})")
    if missing:
        log.warning("APPROVAL_MODE=chat but %s not set; falling back to Gmail drafts", ", ".join(missing))
        return False
    return True


def publish_to_chat(req: RequestEmail, metrics, html: str, text: str, settings: Settings, store: ProcessedStore) -> str | None:
    """Post the reply as an approval card. Returns the Chat message name, or None
    when the card could not be posted (the caller then falls back to a Gmail draft)."""
    from credit_bot.chat_approvals import ApprovalStore, ChatPoster, build_entry, publish

    entry = build_entry(req, metrics, html, text, settings.teamcredit_address, settings.form_sender, settings.extra_cc)
    approvals = ApprovalStore(settings.approvals_bucket)
    status = approvals.status(entry.entry_id)
    if status:
        log.info("Entry %s already %s; not posting again", entry.entry_id, status)
        store.mark(req.message_id, entry_id=entry.entry_id, thread_id=req.thread_id,
                   customer_id=req.customer_id, note=f"chat entry already {status}")
        return entry.chat_message or "(existing)"
    poster = ChatPoster(settings.chat_sa_file, settings.chat_space, settings.chat_action_url)
    try:
        name = publish(entry, approvals, poster)
    except Exception as exc:
        log.warning("Chat card could not be posted for %s (%s); falling back to a Gmail draft", req.message_id, exc)
        return None
    store.mark(
        req.message_id,
        entry_id=entry.entry_id,
        chat_message=name,
        thread_id=req.thread_id,
        customer_id=req.customer_id,
        subject=req.subject,
        summary=metrics.summary(),
    )
    return name


def process_request(
    req: RequestEmail,
    settings: Settings,
    tpro: TransportProClient,
    gmail: GmailClient,
    store: ProcessedStore,
    dry_run: bool,
) -> str | None:
    log.info("Processing %s | %s | customer %s", req.message_id, req.subject, req.customer_label)

    if req.system != SYSTEM_TPRO:
        # Circle Transportation customers live in a different TMS: no figures from
        # Transport Pro, so no draft. In chat mode the space still gets a notice card
        # (the task marker); otherwise the request is only logged.
        reason = (
            "Circle Transportation customer (CT id): its loads are not in Transport Pro, so the "
            "unbilled balance, missing documents and billing holds must be checked by hand."
        )
        log.warning("%s: %s", req.message_id, reason)
        if dry_run:
            return None
        name = ""
        if chat_enabled(settings):
            from credit_bot.chat_approvals import ChatPoster, publish_notice

            try:
                poster = ChatPoster(settings.chat_sa_file, settings.chat_space, settings.chat_action_url)
                name = publish_notice(req, reason, poster)
                log.info("Notice card %s posted for %s", name, req.message_id)
            except Exception as exc:
                log.warning("Notice card could not be posted for %s: %s", req.message_id, exc)
                return None
        store.mark(req.message_id, thread_id=req.thread_id, customer_id=req.customer_label,
                   subject=req.subject, chat_message=name, note="circle transportation: manual")
        return name or None

    metrics = compute_metrics(tpro, settings, req.customer_id, req.customer_name)
    for warning in metrics.warnings:
        log.warning("customer %s: %s", req.customer_id, warning)
    html = render_html(metrics)
    text = render_text(metrics)

    if dry_run:
        path = OUT_DIR / f"draft_{req.customer_id}_{req.message_id}.html"
        write_preview(path, html)
        if chat_enabled(settings):
            from credit_bot.chat_approvals import build_card, build_entry

            entry = build_entry(req, metrics, html, text, settings.teamcredit_address, settings.form_sender, settings.extra_cc)
            card_path = OUT_DIR / f"card_{req.customer_id}_{req.message_id}.json"
            card_path.write_text(json.dumps(build_card(entry, settings.chat_action_url), indent=2), encoding="utf-8")
            log.info("DRY RUN: card written to %s", card_path)
        log.info("DRY RUN: preview written to %s", path)
        return None

    if chat_enabled(settings):
        name = publish_to_chat(req, metrics, html, text, settings, store)
        if name:
            return name

    existing_draft = store.get(req.message_id).get("draft_id")
    if existing_draft:
        draft_id = gmail.update_reply_draft(
            existing_draft, req, html, text, settings.teamcredit_address, settings.form_sender, settings.extra_cc
        )
    else:
        draft_id = gmail.create_reply_draft(
            req, html, text, settings.teamcredit_address, settings.form_sender, settings.extra_cc
        )
    store.mark(
        req.message_id,
        draft_id=draft_id,
        thread_id=req.thread_id,
        customer_id=req.customer_id,
        subject=req.subject,
        summary=metrics.summary(),
    )
    return draft_id


def run_once(
    settings: Settings,
    tpro: TransportProClient,
    gmail: GmailClient,
    *,
    store: ProcessedStore | None = None,
    since_days: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    message_id: str = "",
) -> int:
    """One pass over the mailbox. Shared by the CLI, --watch and the worker Lambda."""
    store = store or ProcessedStore(settings.state_file)

    if message_id:
        req = gmail.get_request(message_id)
        if not req:
            log.error("Message %s is not a credit limit increase request", message_id)
            return 1
        requests_found = [req]
    else:
        requests_found = gmail.find_requests(
            settings.form_sender,
            settings.teamcredit_address,
            since_days or settings.email_lookback_days,
        )
    log.info("Found %s request email(s)", len(requests_found))

    created = 0
    for req in requests_found:
        if store.has(req.message_id) and not force:
            log.info("Skip %s (already processed)", req.message_id)
            continue
        if not force and not dry_run and not chat_enabled(settings) and gmail.thread_has_draft(req.thread_id):
            log.info("Skip %s (thread %s already has a draft)", req.message_id, req.thread_id)
            store.mark(
                req.message_id,
                thread_id=req.thread_id,
                customer_id=req.customer_id,
                note="draft already present",
            )
            continue
        try:
            if process_request(req, settings, tpro, gmail, store, dry_run):
                created += 1
        except Exception:
            log.exception("Failed to process %s (%s)", req.message_id, req.subject)
    log.info("Run complete: %s draft(s)/card(s) created or updated", created)
    return 0


def show_customer(settings: Settings, customer_id: int) -> int:
    tpro = make_tpro(settings)
    metrics = compute_metrics(tpro, settings, customer_id)
    print(metrics.summary())
    if metrics.billing_hold_loads:
        print("  billing hold loads:", metrics.billing_hold_loads)
    for l in metrics.open_loads:
        print(
            f"  load {l.load_id}: pickup {l.pickup_date} delivery {l.delivery_date} "
            f"${l.total:,.2f} docs={l.document_status}"
        )
    if metrics.credit_info:
        print("  credit info:", metrics.credit_info)
    for warning in metrics.warnings:
        print("  warning:", warning)
    path = OUT_DIR / f"preview_{customer_id}.html"
    write_preview(path, render_html(metrics))
    print(f"  preview: {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="compute and render, but do not create drafts")
    parser.add_argument("--customer", type=int, help="only compute metrics for this customer id and print them")
    parser.add_argument("--message-id", help="process a single Gmail message id")
    parser.add_argument("--since-days", type=int, help="override EMAIL_LOOKBACK_DAYS")
    parser.add_argument(
        "--force", action="store_true",
        help="re-process even if already processed; existing drafts are updated in place",
    )
    parser.add_argument("--watch", action="store_true", help="keep running, polling every POLL_SECONDS")
    parser.add_argument(
        "--spaces", action="store_true",
        help="list the Google Chat spaces the credit bot's Chat app is a member of (to find CHAT_SPACE)",
    )
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings)

    if args.spaces:
        from credit_bot.chat_approvals import ChatPoster

        if not settings.chat_sa_file.exists():
            log.error("CHAT_SA_FILE not found: %s", settings.chat_sa_file)
            return 1
        poster = ChatPoster(settings.chat_sa_file, "spaces/none", settings.chat_action_url)
        spaces = poster.spaces()
        if not spaces:
            print("The Chat app is not a member of any space yet. Add it to the space in Google Chat first.")
        for s in spaces:
            print(f"  {s.get('name')}  |  {s.get('displayName') or '(direct message)'}  |  {s.get('spaceType')}")
        return 0

    if args.customer:
        return show_customer(settings, args.customer)

    tpro = make_tpro(settings)
    gmail = GmailClient(settings.service_account_file, settings.gmail_user)
    kwargs = dict(since_days=args.since_days, force=args.force, dry_run=args.dry_run, message_id=args.message_id or "")
    if not args.watch:
        return run_once(settings, tpro, gmail, **kwargs)
    while True:
        try:
            run_once(settings, tpro, gmail, **kwargs)
        except Exception:
            log.exception("Run failed")
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
