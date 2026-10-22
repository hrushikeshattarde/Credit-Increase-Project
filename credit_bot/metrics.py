"""Computes the four figures the Operations Action Items template needs.

Mirrors the manual Transport Pro load-search steps:
  1. customer + Dispatch status "Delivered" + Billing status "Billing Open"
     -> load count, total charges, oldest / latest pickup date
  2. same search + Missing Image "Rate Confirmation" -> count
  3. same search + Missing Image "Carrier Invoice"   -> count
  4. same search, no missing image, Billing Hold "Enabled" -> count
     (API: `status.billingLock` on each load / `hasBillingLock=true` filter)

"Missing Image <type>" on the web screen means: no file of that document
type is attached to the load, whatever the load's document status says. So
steps 2 and 3 list each open load's attached files (GET /files/search) and
look for the exact document types "Rate Confirmation" and "Carrier Invoice".
(The tenant-wide /load/missing_documents listing is NOT the same thing: it
only covers loads still "Waiting for Documents".)
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .tpro import TransportProClient

log = logging.getLogger(__name__)

LOAD_STATUS = "Delivered"
BILLING_STATUS = "Billing Open"
DOC_RATE_CONFIRMATION = "Rate Confirmation"
DOC_CARRIER_INVOICE = "Carrier Invoice"


@dataclass
class LoadSummary:
    load_id: int
    pickup_date: date | None
    delivery_date: date | None
    total: float
    document_status: str
    billing_status: str
    load_status: str


@dataclass
class CreditMetrics:
    customer_id: int
    customer_name: str
    open_loads: list[LoadSummary] = field(default_factory=list)
    missing_rate_confirmation: list[int] = field(default_factory=list)
    missing_carrier_invoice: list[int] = field(default_factory=list)
    billing_hold_count: int | None = None
    billing_hold_loads: list[int] = field(default_factory=list)
    credit_info: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def open_load_count(self) -> int:
        return len(self.open_loads)

    @property
    def open_load_total(self) -> float:
        return round(sum(l.total for l in self.open_loads), 2)

    @property
    def oldest_pickup(self) -> date | None:
        dates = [l.pickup_date for l in self.open_loads if l.pickup_date]
        return min(dates) if dates else None

    @property
    def latest_pickup(self) -> date | None:
        dates = [l.pickup_date for l in self.open_loads if l.pickup_date]
        return max(dates) if dates else None

    @property
    def missing_rate_confirmation_count(self) -> int:
        return len(self.missing_rate_confirmation)

    @property
    def missing_carrier_invoice_count(self) -> int:
        return len(self.missing_carrier_invoice)

    def summary(self) -> str:
        hold = self.billing_hold_count if self.billing_hold_count is not None else "n/a"
        return (
            f"customer {self.customer_id} ({self.customer_name}): "
            f"{self.open_load_count} delivered/billing-open loads, total ${self.open_load_total:,.2f}, "
            f"pickup {self.oldest_pickup} .. {self.latest_pickup}; "
            f"missing RC {self.missing_rate_confirmation_count} {self.missing_rate_confirmation}, "
            f"missing CI {self.missing_carrier_invoice_count} {self.missing_carrier_invoice}, "
            f"billing hold {hold}"
        )


def _waypoint_date(load: dict, wp_type: str) -> date | None:
    for wp in load.get("waypoints") or []:
        if wp.get("type") != wp_type:
            continue
        appt = wp.get("appointmentTime") or {}
        stamp = appt.get("open") or appt.get("close")
        if not stamp:
            return None
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        tz_name = (wp.get("location") or {}).get("ianaTimezone")
        if tz_name:
            try:
                dt = dt.astimezone(ZoneInfo(tz_name))
            except Exception:  # unknown zone name -> keep UTC
                pass
        return dt.date()
    return None


def _total(load: dict) -> float:
    charges = (load.get("billingInfo") or {}).get("charges") or {}
    return float(
        (charges.get("totalFreight") or 0)
        + (charges.get("totalFuel") or 0)
        + (charges.get("totalOther") or 0)
    )


def summarize_load(load: dict) -> LoadSummary:
    status = load.get("status") or {}
    return LoadSummary(
        load_id=int(load["id"]),
        pickup_date=_waypoint_date(load, "SH"),
        delivery_date=_waypoint_date(load, "CN"),
        total=_total(load),
        document_status=status.get("documentStatus") or "",
        billing_status=status.get("billingStatus") or "",
        load_status=status.get("loadStatus") or "",
    )


def attached_document_types(client: TransportProClient, load_id: int) -> set[str]:
    """Document type names of every file attached to a load."""
    return {(f.get("fileTypeName") or "").strip() for f in client.files_for_load(load_id)}


def compute_metrics(
    client: TransportProClient,
    settings: Settings,
    customer_id: int,
    customer_name: str = "",
) -> CreditMetrics:
    metrics = CreditMetrics(customer_id=customer_id, customer_name=customer_name)

    try:
        credit = client.customer_credit(customer_id)
        metrics.customer_name = credit.get("companyName") or customer_name
        metrics.credit_info = credit.get("creditInformation") or {}
    except Exception as exc:  # non-fatal: the name/credit info is informational
        metrics.warnings.append(f"customer credit lookup failed: {exc}")

    # Step 1 -------------------------------------------------------------
    raw_loads = client.search_loads(
        customer_id,
        settings.lookback_days,
        load_status=LOAD_STATUS,
        billing_status=BILLING_STATUS,
    )
    loads = [summarize_load(l) for l in raw_loads]
    # Server-side filters are trusted, but keep the client-side guard in case
    # the API ever ignores a filter value.
    loads = [
        l for l in loads
        if l.load_status.lower() == LOAD_STATUS.lower()
        and l.billing_status.lower() == BILLING_STATUS.lower()
    ]
    loads.sort(key=lambda l: (l.pickup_date or date.min, l.load_id))
    metrics.open_loads = loads
    log.info("customer %s: %s Delivered/Billing Open loads", customer_id, len(loads))

    # Steps 2 & 3 --------------------------------------------------------
    if loads:
        def lookup(load: LoadSummary) -> set[str] | None:
            try:
                return attached_document_types(client, load.load_id)
            except Exception as exc:
                log.warning("file lookup failed for load %s: %s", load.load_id, exc)
                return None

        workers = max(1, min(settings.missing_docs_workers, 4))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            types_by_load = dict(zip([l.load_id for l in loads], pool.map(lookup, loads)))
        for l in loads:
            types = types_by_load[l.load_id]
            if types is None:
                metrics.warnings.append(
                    f"load {l.load_id}: could not list its files, so it is not counted as missing documents"
                )
                continue
            if DOC_RATE_CONFIRMATION not in types:
                metrics.missing_rate_confirmation.append(l.load_id)
            if DOC_CARRIER_INVOICE not in types:
                metrics.missing_carrier_invoice.append(l.load_id)

    # Step 4 -------------------------------------------------------------
    # Billing Hold = the web screen's "Billing Hold: Hold Enabled" filter. Since
    # 2026-10-21 (Transport Pro support ticket #911213) the API exposes it as
    # `status.billingLock` on every load and as the `hasBillingLock=true`
    # search filter. The flag on the loads already fetched is used first; the
    # filtered search is the fallback if the flag is ever missing.
    held = [l for l in raw_loads if (l.get("status") or {}).get("billingLock") is True]
    flag_missing = any("billingLock" not in (l.get("status") or {}) for l in raw_loads)
    if flag_missing:
        try:
            held = client.search_loads(
                customer_id,
                settings.lookback_days,
                load_status=LOAD_STATUS,
                billing_status=BILLING_STATUS,
                has_billing_lock=True,
            )
        except Exception as exc:
            metrics.billing_hold_count = None
            metrics.warnings.append(f"billing hold lookup failed: {exc}")
            held = None
    if held is not None:
        metrics.billing_hold_loads = sorted(int(l["id"]) for l in held)
        metrics.billing_hold_count = len(metrics.billing_hold_loads)

    log.info(metrics.summary())
    return metrics
