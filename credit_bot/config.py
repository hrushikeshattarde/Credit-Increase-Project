"""Configuration loaded from .env / environment variables.

Only TPRO_BASE_URL, TPRO_USERNAME and TPRO_PASSWORD are required; everything
else has a sensible default so the bot runs out of the box.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # --- Transport Pro public API -------------------------------------------
    tpro_base_url: str
    tpro_username: str
    tpro_password: str
    tpro_timeout_seconds: int = 60
    # How far back (in days, by pickup date) to look for Delivered / Billing
    # Open loads. The API caps each query at 60 days, so this is walked in
    # 60-day windows.
    lookback_days: int = 365
    # Parallel per-load file lookups for the missing-document checks.
    missing_docs_workers: int = 8

    # --- Google / Gmail -----------------------------------------------------
    service_account_file: Path = PROJECT_ROOT / "gsheets-python-350615-d8d272fb5359.json"
    # Mailbox that is impersonated through domain-wide delegation. teamcredit@
    # is a Google Group, so it cannot be impersonated directly; the group mail
    # is read from a member mailbox instead.
    gmail_user: str = "hrushikesh.attarde@circledelivers.com"
    teamcredit_address: str = "teamcredit@circledelivers.com"
    form_sender: str = "formresponse@circledelivers.com"
    # Only emails newer than this many days are considered for new drafts.
    email_lookback_days: int = 14
    # Extra Cc recipients always added to the draft (besides the thread's own).
    extra_cc: list[str] = field(default_factory=list)

    # --- Google Chat approval cards ------------------------------------------
    # "drafts": the reply is saved to GMAIL_USER's Drafts (original behaviour).
    # "chat": the reply is posted as a card in CHAT_SPACE with Approve / Move to my
    # Drafts / Reject buttons, served by the credit bot's own callback stack
    # (deploy/template.yaml); a Gmail draft is only created if the card cannot be posted.
    approval_mode: str = "drafts"
    chat_space: str = ""
    # Service-account key of the credit bot's own Chat app (its Google Cloud project).
    chat_sa_file: Path = PROJECT_ROOT / "credit-chat-service-account.json"
    # CallbackUrl output of the creditbot stack.
    chat_action_url: str = ""
    # StateBucket output of the creditbot stack.
    approvals_bucket: str = ""

    # --- Runtime ------------------------------------------------------------
    state_file: Path = PROJECT_ROOT / "state" / "processed.json"
    log_dir: Path = PROJECT_ROOT / "logs"
    poll_seconds: int = 300


def load_settings() -> Settings:
    missing = [k for k in ("TPRO_BASE_URL", "TPRO_USERNAME", "TPRO_PASSWORD") if not _env(k)]
    if missing:
        raise SystemExit(f"Missing required .env values: {', '.join(missing)}")

    sa_file = _env("GOOGLE_SERVICE_ACCOUNT_FILE")
    return Settings(
        tpro_base_url=_env("TPRO_BASE_URL").rstrip("/"),
        tpro_username=_env("TPRO_USERNAME"),
        tpro_password=os.environ["TPRO_PASSWORD"],
        tpro_timeout_seconds=_env_int("TPRO_TIMEOUT_SECONDS", 60),
        lookback_days=_env_int("LOOKBACK_DAYS", 365),
        missing_docs_workers=_env_int("MISSING_DOCS_WORKERS", 8),
        service_account_file=Path(sa_file) if sa_file else PROJECT_ROOT / "gsheets-python-350615-d8d272fb5359.json",
        gmail_user=_env("GMAIL_USER", "hrushikesh.attarde@circledelivers.com"),
        teamcredit_address=_env("TEAMCREDIT_ADDRESS", "teamcredit@circledelivers.com"),
        form_sender=_env("FORM_SENDER", "formresponse@circledelivers.com"),
        email_lookback_days=_env_int("EMAIL_LOOKBACK_DAYS", 14),
        extra_cc=_env_list("EXTRA_CC", []),
        approval_mode=(_env("APPROVAL_MODE", "drafts") or "drafts").lower(),
        chat_space=_env("CHAT_SPACE", "") or "",
        chat_sa_file=Path(_env("CHAT_SA_FILE", str(PROJECT_ROOT / "credit-chat-service-account.json"))),
        chat_action_url=_env("CHAT_ACTION_URL", "") or "",
        approvals_bucket=_env("APPROVALS_BUCKET", "") or "",
        state_file=Path(_env("STATE_FILE", str(PROJECT_ROOT / "state" / "processed.json"))),
        log_dir=Path(_env("LOG_DIR", str(PROJECT_ROOT / "logs"))),
        poll_seconds=_env_int("POLL_SECONDS", 300),
    )
