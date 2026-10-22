"""The mailbox poller as an AWS Lambda (EventBridge runs it every 5 minutes).

Same code path as `python main.py` on a workstation (main.run_once); only the
plumbing differs:

* Transport Pro credentials come from the Secrets Manager secret in
  CREDIT_SECRET_TRANSPORTPRO ({"username": ..., "password": ...});
* the two Google keys are private objects in the state bucket
  (config/google-service-account.json for Gmail, config/chat-service-account.json
  for the Chat app), copied to /tmp at cold start;
* the processed-message ledger lives in the bucket (state/processed.json);
* everything else is plain environment variables set by deploy/template.yaml.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("credit_bot.lambda")

_READY = False


def _prepare_environment() -> None:
    """Resolve secrets and keys once per container, then let config.load_settings
    read them like any other environment variable."""
    global _READY
    if _READY:
        return
    import boto3

    bucket = os.environ["CREDIT_STATE_BUCKET"]
    s3 = boto3.client("s3")
    tmp = Path("/tmp")

    for env_name, key, filename in (
        ("GOOGLE_SERVICE_ACCOUNT_FILE", os.environ.get("CREDIT_SA_S3_KEY", "config/google-service-account.json"), "gmail-sa.json"),
        ("CHAT_SA_FILE", os.environ.get("CREDIT_CHAT_SA_S3_KEY", "config/chat-service-account.json"), "chat-sa.json"),
    ):
        target = tmp / filename
        s3.download_file(bucket, key, str(target))
        os.environ[env_name] = str(target)

    secret_id = os.environ.get("CREDIT_SECRET_TRANSPORTPRO", "").strip()
    if secret_id and not os.environ.get("TPRO_PASSWORD"):
        raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
        creds = json.loads(raw)
        os.environ["TPRO_USERNAME"] = str(creds.get("username") or os.environ.get("TPRO_USERNAME", ""))
        os.environ["TPRO_PASSWORD"] = str(creds.get("password") or "")

    os.environ.setdefault("APPROVALS_BUCKET", bucket)
    os.environ.setdefault("STATE_FILE", str(tmp / "processed.json"))
    os.environ.setdefault("LOG_DIR", str(tmp / "logs"))
    os.environ.setdefault("OUT_DIR", str(tmp / "out"))
    _READY = True


def handler(event: dict | None = None, context=None) -> dict:
    _prepare_environment()

    # main.py lives beside the package in the deployment zip.
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import main as bot
    from credit_bot.config import load_settings
    from credit_bot.gmail_client import GmailClient
    from credit_bot.state import S3ProcessedStore

    settings = load_settings()
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    else:
        root_logger.setLevel(logging.INFO)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

    event = event or {}
    tpro = bot.make_tpro(settings)
    gmail = GmailClient(settings.service_account_file, settings.gmail_user)
    store = S3ProcessedStore(os.environ["CREDIT_STATE_BUCKET"])
    rc = bot.run_once(
        settings,
        tpro,
        gmail,
        store=store,
        since_days=int(event.get("since_days") or 0) or None,
        force=bool(event.get("force")),
        dry_run=bool(event.get("dry_run")),
        message_id=str(event.get("message_id") or ""),
    )
    return {"ok": rc == 0, "chat_mode": bot.chat_enabled(settings)}
