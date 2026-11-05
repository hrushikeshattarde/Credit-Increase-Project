"""Monthly PITT delivered-unbilled workbook (see credit_bot/pitt_report.py).

Usage:
  python pitt_report.py                     build "out/PITT <Month> <Year>.xlsx" only, no email
  python pitt_report.py --send --to a@x.com test send to someone else (not recorded as this month's send)
  python pitt_report.py --send              build and email it to PITT_REPORT_TO (once per month)
  python pitt_report.py --send --force      send again although this month's report already went out
  python pitt_report.py --fresh             discard today's checkpoint and pull everything again

A run takes ~13 minutes (Transport Pro throttles the per-load lookups). Progress is
checkpointed in state/pitt_report_progress.json, so an interrupted run resumes, and
later runs on the same day reuse the finished data (a test send and the real send
then carry identical figures).

In AWS the worker Lambda runs the same code on the monthly EventBridge rule
(event {"task": "pitt_report"}), with its state in the bucket's state/ folder.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from main import OUT_DIR, make_tpro, setup_logging
from credit_bot.config import load_settings
from credit_bot.pitt_report import run
from credit_bot.state import LocalDocs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send", action="store_true", help="email the workbook (default: build only)")
    parser.add_argument("--to", default="", help="comma-separated recipients overriding PITT_REPORT_TO (a test send)")
    parser.add_argument("--cc", default=None, help="comma-separated Cc overriding PITT_REPORT_CC")
    parser.add_argument("--force", action="store_true", help="send even if this month's report was already sent")
    parser.add_argument("--fresh", action="store_true", help="ignore today's checkpoint and pull everything again")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings)
    gmail = None
    if args.send:
        from credit_bot.gmail_client import GmailClient

        gmail = GmailClient(settings.service_account_file, settings.gmail_user)
    docs = LocalDocs(Path(os.environ.get("PITT_STATE_DIR") or settings.state_file.parent))
    result = run(
        settings,
        make_tpro(settings),
        OUT_DIR,
        docs,
        gmail=gmail,
        send=args.send,
        to=[a.strip() for a in args.to.split(",") if a.strip()] or None,
        cc=None if args.cc is None else [a.strip() for a in args.cc.split(",") if a.strip()],
        force=args.force,
        fresh=args.fresh,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
