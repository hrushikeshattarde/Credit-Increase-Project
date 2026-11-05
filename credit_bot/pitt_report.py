"""Monthly PITT delivered-unbilled workbook, emailed as an .xlsx attachment.

Rebuilds the four tabs of the hand-made "PITT September.xlsx" from Transport Pro,
for the Pittsburgh Office terminal (code 282) and every child terminal (the PITT
PODs: 296, 305, 309, 319, 327 as of 2026-11):

  Unbilled         Dispatch status Delivered + Billing status Billing Open
  Rate Cons        those loads with no "Rate Confirmation" file attached
  Carrier Invoice  those loads with no "Carrier Invoice" file attached, plus the carrier
  Billing Holds    those loads on Billing Hold (status.billingLock)

These are the same four searches the credit-increase reply uses (metrics.py), run
per terminal instead of per customer. Each tab is sorted by Bill To (A to Z), then
Days (Z to A, oldest delivery first), then Terminal (A to Z), then Order Taker
(A to Z). "Days" is =TODAY()-<Delivery Date>, so it keeps counting when the file is
opened later, with the original's green / yellow / red color scale at 30 / 60 / 90.

Resumable: Transport Pro serves the per-load lookups (attached files, dispatches)
at only ~2.5 per second whatever the concurrency, answering HTTP 500 when pushed
(measured 2026-11-05), so a full run takes ~13 minutes, too close to Lambda's limit.
Progress is therefore checkpointed after every batch ("pitt_report_progress.json"
in the state store) and the next run resumes it; the monthly rule fires every 10
minutes on send day until one run finishes and sends.
"""
from __future__ import annotations

import html
import logging
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from .config import Settings
from .metrics import (
    BILLING_STATUS,
    DOC_CARRIER_INVOICE,
    DOC_RATE_CONFIRMATION,
    LOAD_STATUS,
    _waypoint_date,
    attached_document_types,
)
from .tpro import TransportProClient

log = logging.getLogger(__name__)

# ---- workbook layout, copied from "PITT September.xlsx" ----------------------
FONT_NAME = "Arial"
FONT_SIZE = 10
DATE_FORMAT = "mm/dd/yyyy"
MONEY_FORMAT = '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)'
DAYS_FORMAT = "#,##0.00"
# Days color scale: green at 30, yellow at 60, red at 90 (Google Sheets colors).
SCALE = (("30", "FF34A853"), ("60", "FFFBBC04"), ("90", "FFEA4335"))

BASE_COLUMNS = [
    ("Load", 15.8), ("Bill To", 35.5), ("Terminal", 7.9), ("Order Taker", 14.0),
    ("Pick-up Date", 11.1), ("Delivery Date", 11.5), ("Freight", 11.5), ("Fuel", 11.5),
    ("Accessorial", 10.9), ("Total Bill", 12.8), ("Days", 9.0),
]
CARRIER_COLUMNS = BASE_COLUMNS[:3] + [("Driver", 44.0)] + BASE_COLUMNS[3:]

TAB_UNBILLED = "Unbilled"
TAB_RATE_CONS = "Rate Cons"
TAB_CARRIER_INVOICE = "Carrier Invoice"
TAB_BILLING_HOLDS = "Billing Holds"
TABS = (TAB_UNBILLED, TAB_RATE_CONS, TAB_CARRIER_INVOICE, TAB_BILLING_HOLDS)

# ---- Transport Pro pacing ----------------------------------------------------
# More than a couple of parallel per-load calls only adds HTTP 500s (8 workers
# failed ~5% of /files/search outright; 1 and 3 workers both top out at ~2.4/s).
WORKERS = 2
BATCH = 25                 # loads per checkpoint
SERVER_ERROR_BACKOFF = 0.25  # seconds (x attempt) between 5xx retries inside one lookup
MAX_TRIES = 3              # lookups per load across batches/runs (each = 4 HTTP attempts)
# A finished run with more than this share of loads whose documents could not be
# checked is not mailed (the Rate Cons / Carrier Invoice tabs would be wrong).
MAX_LOOKUP_FAILURE_SHARE = 0.02
LEASE_SECONDS = 120        # a run holding the checkpoint refreshes this at every save

PROGRESS_DOC = "pitt_report_progress.json"


def sent_doc(month: str) -> str:
    return f"pitt_report_sent_{month}.json"


def claim_doc(month: str) -> str:
    return f"pitt_report_claim_{month}.json"


@dataclass
class ReportLoad:
    load_id: int
    bill_to: str
    terminal_code: str
    order_taker: str
    pickup_date: date | None
    delivery_date: date | None
    freight: float
    fuel: float
    accessorial: float
    billing_lock: bool
    # None until the load's attached files were listed.
    missing_rate_confirmation: bool | None = None
    missing_carrier_invoice: bool | None = None
    # None until the dispatches were read (only loads missing a carrier invoice).
    carrier: str | None = None
    doc_tries: int = 0
    carrier_tries: int = 0

    @property
    def total(self) -> float:
        return round(self.freight + self.fuel + self.accessorial, 2)

    def days(self, as_of: date) -> int | None:
        return (as_of - self.delivery_date).days if self.delivery_date else None

    def to_json(self) -> dict:
        data = asdict(self)
        for key in ("pickup_date", "delivery_date"):
            data[key] = data[key].isoformat() if data[key] else None
        return data

    @classmethod
    def from_json(cls, data: dict) -> "ReportLoad":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}
        for key in ("pickup_date", "delivery_date"):
            data[key] = date.fromisoformat(data[key]) if data.get(key) else None
        return cls(**data)


@dataclass
class PittReport:
    as_of: date
    terminals: dict[int, str]  # terminal id -> terminal code
    loads: list[ReportLoad]
    warnings: list[str]

    def tab(self, name: str) -> list[ReportLoad]:
        if name == TAB_UNBILLED:
            rows = self.loads
        elif name == TAB_RATE_CONS:
            rows = [l for l in self.loads if l.missing_rate_confirmation]
        elif name == TAB_CARRIER_INVOICE:
            rows = [l for l in self.loads if l.missing_carrier_invoice]
        elif name == TAB_BILLING_HOLDS:
            rows = [l for l in self.loads if l.billing_lock]
        else:
            raise KeyError(name)
        return sorted(rows, key=_sort_key)

    @property
    def title(self) -> str:
        return f"PITT {self.as_of:%B %Y}"

    @property
    def filename(self) -> str:
        return f"{self.title}.xlsx"

    @property
    def terminal_codes(self) -> list[str]:
        return sorted(self.terminals.values(), key=lambda c: (len(c), c))


def _text_key(value: str) -> tuple:
    """A to Z the way Google Sheets sorts text: case-insensitive, blanks last."""
    value = (value or "").strip()
    return (value == "", value.lower())


def _terminal_key(code: str) -> tuple:
    """Terminal codes are numbers in the sheet, so 282 < 296 < 1001; blanks last."""
    code = (code or "").strip()
    return (code == "", int(code) if code.isdigit() else 0, code)


def _sort_key(l: ReportLoad):
    """Every tab's row order, as the team sorts the sheet by hand:
    Bill To A to Z, then Days Z to A, then Terminal A to Z, then Order Taker A to Z.

    Days = TODAY() - Delivery Date, so "Days Z to A" is the oldest delivery first
    (it stays correct on any day the file is opened). Loads without a delivery
    date have a blank Days and go last, as Sheets puts blanks last. The load id
    is a final tie-break so the order is the same on every run."""
    return (
        _text_key(l.bill_to),
        l.delivery_date is None, l.delivery_date or date.max,
        _terminal_key(l.terminal_code),
        _text_key(l.order_taker),
        l.load_id,
    )


# ---------------------------------------------------------------- collecting
def pitt_terminals(client: TransportProClient, parent_id: int) -> dict[int, str]:
    """The parent terminal and its direct children, as {id: terminal code}."""
    found: dict[int, str] = {}
    for t in client.terminals():
        tid = int(t["id"])
        if tid == parent_id or str(t.get("parentTerminalId") or "") == str(parent_id):
            found[tid] = str(t.get("terminalCode") or tid)
    if parent_id not in found:
        raise RuntimeError(f"terminal {parent_id} not found in Transport Pro")
    return found


def _charges(load: dict) -> tuple[float, float, float]:
    c = (load.get("billingInfo") or {}).get("charges") or {}
    return (
        float(c.get("totalFreight") or 0),
        float(c.get("totalFuel") or 0),
        float(c.get("totalOther") or 0),
    )


def _order_taker_id(load: dict) -> int | None:
    for contact in load.get("internalContacts") or []:
        if contact.get("type") == "ORDERTAKER" and contact.get("id"):
            return int(contact["id"])
    return None


def carrier_name(dispatches: list[dict]) -> str:
    """The "Driver" column: every live dispatch's carrier in dispatch order, joined
    with ", " as in the September workbook ("Kk Freight Corp, CIRCLE LOGISTICS, INC."
    for a load moved by two carriers). Cancelled dispatches are left out."""
    live = [d for d in dispatches if "cancel" not in (d.get("status") or "").lower()] or dispatches
    live = sorted(live, key=lambda d: (d.get("dispatchDate") or "", int(d.get("id") or 0)))
    names: list[str] = []
    for d in live:
        assigned = d.get("assignedTo") or {}
        name = ((assigned.get("carrier") or {}).get("companyName") or "").strip()
        if not name:
            driver = assigned.get("driver") or {}
            name = " ".join(p for p in (driver.get("firstName"), driver.get("lastName")) if p).strip()
        if not name:
            name = next(
                (c.get("name") for c in assigned.get("contacts") or [] if c.get("type") == "DRIVER" and c.get("name")),
                "",
            )
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


def search_stage(client: TransportProClient, parent_terminal_id: int, lookback_days: int, as_of: date) -> dict:
    """Stage 1 (~1 minute): the Delivered / Billing Open loads of every PITT terminal,
    with order-taker names. Returns a fresh progress document."""
    terminals = pitt_terminals(client, parent_terminal_id)
    log.info("PITT terminals: %s", ", ".join(f"{tid} ({code})" for tid, code in terminals.items()))

    def search(tid: int) -> list[dict]:
        return client.search_loads(
            None, lookback_days, load_status=LOAD_STATUS, billing_status=BILLING_STATUS, terminal_id=tid
        )

    raw: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for tid, loads in zip(terminals, pool.map(search, terminals)):
            log.info("terminal %s (%s): %s Delivered / Billing Open loads", tid, terminals[tid], len(loads))
            for load in loads:
                status = load.get("status") or {}
                # Client-side guard in case the API ever ignores a filter.
                if (status.get("loadStatus") or "").lower() != LOAD_STATUS.lower():
                    continue
                if (status.get("billingStatus") or "").lower() != BILLING_STATUS.lower():
                    continue
                raw[int(load["id"])] = load

    warnings: list[str] = []
    user_ids = sorted({uid for uid in map(_order_taker_id, raw.values()) if uid})

    def user_name(uid: int) -> str:
        try:
            u = client.user(uid)
        except Exception as exc:
            warnings.append(f"order taker {uid}: lookup failed ({exc})")
            return ""
        return " ".join(p for p in (u.get("firstName"), u.get("lastName")) if p).strip()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        names = dict(zip(user_ids, pool.map(user_name, user_ids)))

    loads: list[ReportLoad] = []
    for lid, load in raw.items():
        freight, fuel, other = _charges(load)
        assigned = load.get("assignedTerminal")
        uid = _order_taker_id(load)
        loads.append(ReportLoad(
            load_id=lid,
            bill_to=(((load.get("billingInfo") or {}).get("customer") or {}).get("companyName") or "").strip(),
            terminal_code=terminals.get(int(assigned), str(assigned)) if assigned else "",
            order_taker=names.get(uid, "") if uid else "",
            pickup_date=_waypoint_date(load, "SH"),
            delivery_date=_waypoint_date(load, "CN"),
            freight=freight,
            fuel=fuel,
            accessorial=other,
            billing_lock=(load.get("status") or {}).get("billingLock") is True,
        ))
    log.info("PITT: %s Delivered / Billing Open loads in total", len(loads))
    return {
        "as_of": as_of.isoformat(),
        "stage": "lookups",
        "terminals": {str(k): v for k, v in terminals.items()},
        "loads": [l.to_json() for l in loads],
        "warnings": warnings,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def lookup_stage(client: TransportProClient, progress: dict, deadline: float | None, save) -> bool:
    """Stages 2 and 3, in batches of BATCH loads with save(progress) after each:
    attached files of every load (missing Rate Confirmation / Carrier Invoice), then
    the carrier of each load missing a carrier invoice. Returns True when nothing is
    left to look up, False when the deadline (time.monotonic()) stopped it first."""
    loads = [ReportLoad.from_json(d) for d in progress["loads"]]

    def doc_types(l: ReportLoad) -> set[str] | None:
        try:
            return attached_document_types(client, l.load_id)
        except Exception as exc:
            log.warning("file lookup failed for load %s: %s", l.load_id, exc)
            return None

    def dispatch_carrier(l: ReportLoad) -> str | None:
        try:
            return carrier_name(client.dispatches_for_load(l.load_id))
        except Exception as exc:
            log.warning("dispatch lookup failed for load %s: %s", l.load_id, exc)
            return None

    def store() -> None:
        progress["loads"] = [l.to_json() for l in loads]
        save(progress)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while True:
            docs_todo = [l for l in loads if l.missing_rate_confirmation is None and l.doc_tries < MAX_TRIES]
            carrier_todo = [
                l for l in loads
                if l.missing_carrier_invoice and l.carrier is None and l.carrier_tries < MAX_TRIES
            ]
            todo, kind = (docs_todo, "files") if docs_todo else (carrier_todo, "carriers")
            if not todo:
                return True
            if deadline is not None and time.monotonic() > deadline:
                log.info("PITT: deadline reached with %s file and %s carrier lookups left",
                         len(docs_todo), len(carrier_todo))
                return False
            batch = todo[:BATCH]
            if kind == "files":
                for l, types in zip(batch, pool.map(doc_types, batch)):
                    l.doc_tries += 1
                    if types is not None:
                        l.missing_rate_confirmation = DOC_RATE_CONFIRMATION not in types
                        l.missing_carrier_invoice = DOC_CARRIER_INVOICE not in types
            else:
                for l, name in zip(batch, pool.map(dispatch_carrier, batch)):
                    l.carrier_tries += 1
                    if name is not None:
                        l.carrier = name
            store()
            done_docs = sum(1 for l in loads if l.missing_rate_confirmation is not None)
            log.info("PITT: files checked %s/%s, carriers %s/%s", done_docs, len(loads),
                     sum(1 for l in loads if l.carrier is not None),
                     sum(1 for l in loads if l.missing_carrier_invoice))


def report_from(progress: dict) -> PittReport:
    """The finished report. Loads whose files could not be listed stay on Unbilled
    only; the email names them."""
    loads = [ReportLoad.from_json(d) for d in progress["loads"]]
    warnings = list(progress.get("warnings") or [])
    unchecked = [l.load_id for l in loads if l.missing_rate_confirmation is None]
    if unchecked:
        warnings.append(
            f"{len(unchecked)} load(s) could not be checked for attached documents and are left off "
            f"Rate Cons / Carrier Invoice: {', '.join(map(str, unchecked))}"
        )
    no_carrier = [l.load_id for l in loads if l.missing_carrier_invoice and l.carrier is None]
    if no_carrier:
        warnings.append(f"carrier could not be read for load(s) {', '.join(map(str, no_carrier))}")
    return PittReport(
        as_of=date.fromisoformat(progress["as_of"]),
        terminals={int(k): v for k, v in progress["terminals"].items()},
        loads=loads,
        warnings=warnings,
    )


# ------------------------------------------------------------------ workbook
def _write_tab(ws, rows: list[ReportLoad], with_carrier: bool) -> None:
    columns = CARRIER_COLUMNS if with_carrier else BASE_COLUMNS
    header_font = Font(name=FONT_NAME, size=FONT_SIZE, bold=True)
    body_font = Font(name=FONT_NAME, size=FONT_SIZE)
    # The original left-aligns every cell except the Days column, which is centered.
    left, center = Alignment(horizontal="left"), Alignment(horizontal="center")
    names = [c[0] for c in columns]
    col = {name: i + 1 for i, name in enumerate(names)}
    delivery_letter = get_column_letter(col["Delivery Date"])
    days_letter = get_column_letter(col["Days"])

    for name, width in columns:
        cell = ws.cell(row=1, column=col[name], value=name)
        cell.font = header_font
        cell.alignment = center if name == "Days" else left
        ws.column_dimensions[get_column_letter(col[name])].width = width

    for r, l in enumerate(rows, start=2):
        values = {
            "Load": l.load_id,
            "Bill To": l.bill_to,
            "Terminal": int(l.terminal_code) if l.terminal_code.isdigit() else l.terminal_code,
            "Driver": l.carrier or "",
            "Order Taker": l.order_taker,
            "Pick-up Date": l.pickup_date,
            "Delivery Date": l.delivery_date,
            "Freight": l.freight,
            "Fuel": l.fuel,
            "Accessorial": l.accessorial,
            "Total Bill": l.total,
            "Days": f"=TODAY()-{delivery_letter}{r}" if l.delivery_date else None,
        }
        for name in names:
            cell = ws.cell(row=r, column=col[name], value=values[name])
            cell.font = body_font
            cell.alignment = center if name == "Days" else left
            if name in ("Pick-up Date", "Delivery Date"):
                cell.number_format = DATE_FORMAT
            elif name in ("Freight", "Fuel", "Accessorial", "Total Bill"):
                cell.number_format = MONEY_FORMAT
            elif name == "Days":
                cell.number_format = DAYS_FORMAT

    ws.freeze_panes = "A2"
    if rows:
        (lo, lo_c), (mid, mid_c), (hi, hi_c) = SCALE
        ws.conditional_formatting.add(
            f"{days_letter}2:{days_letter}{len(rows) + 1}",
            ColorScaleRule(
                start_type="num", start_value=lo, start_color=lo_c,
                mid_type="num", mid_value=mid, mid_color=mid_c,
                end_type="num", end_value=hi, end_color=hi_c,
            ),
        )


def build_workbook(report: PittReport, path: Path) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    for name in TABS:
        _write_tab(wb.create_sheet(name), report.tab(name), with_carrier=(name == TAB_CARRIER_INVOICE))
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    log.info("Workbook written: %s", path)
    return path


# --------------------------------------------------------------------- email
BUCKETS = (("Under 30", "#34A853"), ("30-59", "#FBBC04"), ("60-89", "#F29900"), ("90+", "#EA4335"))


def _bucket(days: int | None) -> str | None:
    if days is None:
        return None
    if days < 30:
        return "Under 30"
    if days < 60:
        return "30-59"
    if days < 90:
        return "60-89"
    return "90+"


def tab_summary(report: PittReport, name: str) -> dict:
    rows = report.tab(name)
    pickups = [l.pickup_date for l in rows if l.pickup_date]
    buckets = Counter(_bucket(l.days(report.as_of)) for l in rows)
    return {
        "name": name,
        "count": len(rows),
        "total": round(sum(l.total for l in rows), 2),
        "oldest_pickup": min(pickups) if pickups else None,
        "buckets": {b: buckets.get(b, 0) for b, _ in BUCKETS},
    }


def _fmt_date(d: date | None) -> str:
    return f"{d:%m/%d/%Y}" if d else "-"


def email_subject(report: PittReport) -> str:
    return f"{report.title} - Delivered Unbilled Loads"


def email_bodies(report: PittReport) -> tuple[str, str]:
    """(text, html) bodies with a per-tab summary of the attached workbook."""
    summaries = [tab_summary(report, name) for name in TABS]
    codes = ", ".join(report.terminal_codes)
    intro = (
        f"Attached is the {report.title} workbook: Delivered / Billing Open loads for the "
        f"Pittsburgh Office and its PODs (terminals {codes}) as of {report.as_of:%B %d, %Y}."
    )
    note = (
        "Days = days since delivery; the Days column recalculates whenever the file is "
        "opened, colored green at 30, yellow at 60 and red at 90 days."
    )

    text_lines = ["Hello,", "", intro, ""]
    for s in summaries:
        aging = ", ".join(f"{b}: {s['buckets'][b]}" for b, _ in BUCKETS)
        text_lines.append(
            f"- {s['name']}: {s['count']} loads, ${s['total']:,.2f}, oldest pickup "
            f"{_fmt_date(s['oldest_pickup'])} (days since delivery - {aging})"
        )
    text_lines += ["", note]
    if report.warnings:
        text_lines += ["", "Notes:"] + [f"- {w}" for w in report.warnings]
    text_lines += ["", "This email is generated automatically from Transport Pro each month."]
    text = "\n".join(text_lines)

    th = 'style="text-align:left;padding:6px 10px;border-bottom:2px solid #444;font-weight:bold"'
    thr = 'style="text-align:right;padding:6px 10px;border-bottom:2px solid #444;font-weight:bold"'
    td = 'style="padding:6px 10px;border-bottom:1px solid #ddd"'
    tdr = 'style="padding:6px 10px;border-bottom:1px solid #ddd;text-align:right"'
    head = "".join(
        f'<th {thr}><span style="display:inline-block;width:9px;height:9px;border-radius:2px;'
        f'background:{color};margin-right:5px"></span>{html.escape(b)}</th>'
        for b, color in BUCKETS
    )
    body_rows = "".join(
        f"<tr><td {td}><b>{html.escape(s['name'])}</b></td><td {tdr}>{s['count']:,}</td>"
        f"<td {tdr}>${s['total']:,.2f}</td><td {tdr}>{_fmt_date(s['oldest_pickup'])}</td>"
        + "".join(f"<td {tdr}>{s['buckets'][b]:,}</td>" for b, _ in BUCKETS)
        + "</tr>"
        for s in summaries
    )
    warn_html = ""
    if report.warnings:
        warn_html = "<p><b>Notes</b></p><ul>" + "".join(f"<li>{html.escape(w)}</li>" for w in report.warnings) + "</ul>"
    html_body = f"""<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#222">
<p>Hello,</p>
<p>{html.escape(intro)}</p>
<table style="border-collapse:collapse;font-size:13px">
<tr><th {th}>Tab</th><th {thr}>Loads</th><th {thr}>Total Bill</th><th {thr}>Oldest pickup</th>{head}</tr>
{body_rows}
</table>
<p style="color:#555">Aging columns count loads by days since delivery. {html.escape(note)}</p>
{warn_html}
<p style="color:#888;font-size:11px">This email is generated automatically from Transport Pro each month.</p>
</div>"""
    return text, html_body


# ----------------------------------------------------------------------- run
def run(
    settings: Settings,
    client: TransportProClient,
    out_dir: Path,
    docs,
    gmail=None,
    send: bool = False,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    force: bool = False,
    fresh: bool = False,
    as_of: date | None = None,
    deadline: float | None = None,
) -> dict:
    """One run: resume (or start) today's report, and when it is complete build the
    workbook and, with send=True, email it.

    docs (state.LocalDocs / state.S3Docs) holds the checkpoint, the send claim and
    the per-month sent record, so a doubled or retried run mails at most once a
    month; force=True sends again. A send to an explicit `to` is a test: it neither
    checks nor records the month. A checkpoint is reused only on the day it was
    started (fresh=True discards it). deadline is a time.monotonic() value after
    which the lookups stop and the run returns {"status": "in_progress"}.
    """
    as_of = as_of or date.today()
    month = f"{as_of:%Y-%m}"
    recipients = list(to or settings.pitt_report_to)
    copy = list(settings.pitt_report_cc if cc is None else cc)
    is_test = bool(to)

    if send and not is_test and not force:
        already = docs.get(sent_doc(month))
        if already:
            log.info("PITT report for %s already sent (%s); skipping", month, already)
            return {"status": "already_sent", "sent": already}

    owner = uuid.uuid4().hex
    progress = None if fresh else docs.get(PROGRESS_DOC)
    if progress and progress.get("as_of") != as_of.isoformat():
        progress = None  # yesterday's (or last month's) data: start over
    if progress and progress.get("lease_owner") not in (None, owner) and progress.get("lease_until", 0) > time.time():
        log.info("PITT report is being built by another run (lease until %s); exiting",
                 datetime.fromtimestamp(progress["lease_until"], timezone.utc).isoformat())
        return {"status": "busy"}

    def save(doc: dict, hold: bool = True) -> None:
        doc["lease_owner"] = owner if hold else None
        doc["lease_until"] = time.time() + LEASE_SECONDS if hold else 0
        docs.put(PROGRESS_DOC, doc)

    if progress is None:
        # The load search keeps the client's normal 5xx back-off (2 s x attempt): with
        # quick retries /load/search gave up after four 500s in a row (2026-11-05).
        progress = search_stage(client, settings.pitt_parent_terminal_id, settings.pitt_lookback_days, as_of)
        save(progress)
    else:
        log.info("PITT: resuming the report started %s", progress.get("started_at"))
        save(progress)

    # The per-load lookups' 500s are transient: retry them quickly.
    client.server_error_backoff = SERVER_ERROR_BACKOFF
    try:
        complete = lookup_stage(client, progress, deadline, save)
    finally:
        save(progress, hold=False)
    if not complete:
        loads = progress["loads"]
        return {
            "status": "in_progress",
            "files_checked": sum(1 for l in loads if l.get("missing_rate_confirmation") is not None),
            "loads": len(loads),
        }

    report = report_from(progress)
    unchecked = sum(1 for l in report.loads if l.missing_rate_confirmation is None)
    if report.loads and unchecked / len(report.loads) > MAX_LOOKUP_FAILURE_SHARE:
        raise RuntimeError(
            f"attached files could not be listed for {unchecked} of {len(report.loads)} loads; "
            "report not sent (rerun with --fresh, or invoke the Lambda with force)"
        )
    path = build_workbook(report, out_dir / report.filename)
    text, html_body = email_bodies(report)
    (out_dir / f"{report.title}.html").write_text(
        f'<!doctype html><html><head><meta charset="utf-8"></head><body>{html_body}</body></html>',
        encoding="utf-8",
    )
    result = {
        "status": "built",
        "file": str(path),
        "counts": {name: len(report.tab(name)) for name in TABS},
        "warnings": report.warnings,
        "sent": False,
    }
    log.info("PITT report: %s", result["counts"])
    if not send:
        return result
    if gmail is None:
        raise RuntimeError("send=True needs a Gmail client")

    claim = claim_doc(month)
    if not is_test:
        if force:
            docs.delete(claim)
        if not docs.create(claim, {"at": datetime.now(timezone.utc).isoformat(), "by": owner}):
            log.info("PITT report for %s is being sent by another run; skipping", month)
            return {**result, "status": "already_sent"}
    try:
        message_id = gmail.send_email(recipients, email_subject(report), text, html_body, [path], cc=copy)
    except Exception:
        if not is_test:
            docs.delete(claim)  # let the next run try again
        raise
    result.update(status="sent", sent=True, message_id=message_id, to=recipients, cc=copy)
    if not is_test:
        docs.put(sent_doc(month), {
            "at": datetime.now(timezone.utc).isoformat(), "gmail_id": message_id, "to": recipients,
            "cc": copy, "file": report.filename, "counts": result["counts"],
        })
    return result
