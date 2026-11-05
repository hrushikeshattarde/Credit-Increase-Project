"""Offline checks for the monthly PITT workbook: tabs, columns, sorting, Days
formula and color scale, the email summary, checkpoint/resume, and the
once-a-month send guard. Transport Pro and Gmail are faked.

Run:  python -m pytest tests -q      (or:  python tests/test_pitt_report.py)
"""
from __future__ import annotations

import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook  # noqa: E402

from credit_bot import pitt_report as pr  # noqa: E402
from credit_bot.config import Settings  # noqa: E402
from credit_bot.state import LocalDocs  # noqa: E402

AS_OF = date(2026, 11, 5)
SETTINGS = Settings(tpro_base_url="x", tpro_username="u", tpro_password="p")


def _load(lid, bill_to, delivery, rc=False, ci=False, hold=False, carrier=""):
    return pr.ReportLoad(
        load_id=lid, bill_to=bill_to, terminal_code="282", order_taker="Brian Thalman",
        pickup_date=delivery, delivery_date=delivery, freight=100.0, fuel=10.5, accessorial=-5.0,
        billing_lock=hold, missing_rate_confirmation=rc, missing_carrier_invoice=ci, carrier=carrier,
    )


def _report():
    loads = [
        _load(3, "Niagara Bottling, LLC", date(2026, 10, 1), rc=True, ci=True, carrier="Koki Express Llc"),
        _load(1, "Niagara Bottling, LLC", date(2026, 6, 1), hold=True),
        _load(2, "LMT Mercer Group, Inc.", date(2026, 9, 1), rc=True),
        _load(4, "Lam Research C/O AFS GTS dba SSUI", date(2024, 11, 19), hold=True),
    ]
    return pr.PittReport(as_of=AS_OF, terminals={1126: "282", 1140: "296"}, loads=loads, warnings=[])


# ---------------------------------------------------------------- fakes
def _raw_load(lid, terminal, customer, pickup, delivery, hold=False, charges=None):
    return {
        "id": lid, "assignedTerminal": terminal,
        "internalContacts": [{"type": "ORDERTAKER", "id": 4757}],
        "status": {"loadStatus": "Delivered", "billingStatus": "Billing Open", "billingLock": hold},
        "billingInfo": {"customer": {"companyName": customer}, "charges": charges},
        "waypoints": [
            {"type": "SH", "appointmentTime": {"open": f"{pickup}T12:00:00Z"},
             "location": {"ianaTimezone": "America/New_York"}},
            {"type": "CN", "appointmentTime": {"open": f"{delivery}T14:00:00Z"},
             "location": {"ianaTimezone": "America/New_York"}},
        ],
    }


class FakeTPro:
    def __init__(self, n=60):
        self.server_error_backoff = 2.0
        self.file_calls = 0
        self.loads = [
            _raw_load(1000 + i, 1126 if i % 3 else 1140, f"Customer {i % 4}", "2026-09-01", "2026-09-02",
                      hold=(i % 10 == 0), charges={"totalFreight": 100, "totalFuel": 5, "totalOther": 1})
            for i in range(n)
        ]

    def terminals(self):
        return [
            {"id": 1126, "terminalCode": "282", "parentTerminalId": None},
            {"id": 1140, "terminalCode": "296", "parentTerminalId": "1126"},
            {"id": 1003, "terminalCode": "1001", "parentTerminalId": None},
        ]

    def search_loads(self, customer_id, lookback_days, load_status=None, billing_status=None,
                     has_billing_lock=None, future_days=60, terminal_id=None):
        return [l for l in self.loads if l["assignedTerminal"] == terminal_id]

    def user(self, uid):
        return {"firstName": "Brandon", "lastName": "Hogan"}

    def files_for_load(self, lid):
        self.file_calls += 1
        types = []
        if lid % 2:
            types.append("Rate Confirmation")
        if lid % 3 == 0:
            types.append("Carrier Invoice")
        return [{"fileTypeName": t} for t in types]

    def dispatches_for_load(self, lid):
        return [{"id": 1, "status": "Delivered", "dispatchDate": "2026-09-01",
                 "assignedTo": {"carrier": {"companyName": f"Carrier {lid}"}}}]


class FakeGmail:
    def __init__(self):
        self.sent = []

    def send_email(self, to, subject, text, html, attachments, cc=None):
        self.sent.append((to, cc, subject, [p.name for p in attachments]))
        return f"msg{len(self.sent)}"


# ---------------------------------------------------------------- tests
def test_tabs_columns_and_order():
    report = _report()
    with tempfile.TemporaryDirectory() as tmp:
        path = pr.build_workbook(report, Path(tmp) / report.filename)
        assert path.name == "PITT November 2026.xlsx"
        wb = load_workbook(path)
        assert wb.sheetnames == ["Unbilled", "Rate Cons", "Carrier Invoice", "Billing Holds"]

        ws = wb["Unbilled"]
        assert [c.value for c in ws[1]] == [
            "Load", "Bill To", "Terminal", "Order Taker", "Pick-up Date", "Delivery Date",
            "Freight", "Fuel", "Accessorial", "Total Bill", "Days",
        ]
        # Bill To case-insensitively ("Lam" < "LMT"), then delivery date.
        assert [ws.cell(r, 1).value for r in range(2, 6)] == [4, 2, 1, 3]
        assert ws["K2"].value == "=TODAY()-F2"
        assert ws["J2"].value == 105.5
        assert ws["F2"].number_format == "mm/dd/yyyy"
        assert ws["A1"].font.b and ws["A1"].font.name == "Arial"
        assert ws["B2"].alignment.horizontal == "left" and ws["K2"].alignment.horizontal == "center"
        assert ws.freeze_panes == "A2"
        rules = [(str(cf.sqref), rule) for cf in ws.conditional_formatting for rule in cf.rules]
        assert len(rules) == 1 and rules[0][0] == "K2:K5"
        scale = rules[0][1].colorScale
        assert [(v.type, float(v.val)) for v in scale.cfvo] == [("num", 30), ("num", 60), ("num", 90)]
        assert [c.rgb for c in scale.color] == ["FF34A853", "FFFBBC04", "FFEA4335"]

        ci = wb["Carrier Invoice"]
        assert [c.value for c in ci[1]][3] == "Driver"
        assert ci["D2"].value == "Koki Express Llc"
        assert ci["L2"].value == "=TODAY()-G2"
        assert ci.max_row == 2

        assert [wb["Rate Cons"].cell(r, 1).value for r in (2, 3)] == [2, 3]
        assert [wb["Billing Holds"].cell(r, 1).value for r in (2, 3)] == [4, 1]


def test_sort_bill_to_then_days_desc_then_terminal_then_order_taker():
    def l(lid, bill_to, delivery, terminal, taker):
        return pr.ReportLoad(
            load_id=lid, bill_to=bill_to, terminal_code=terminal, order_taker=taker,
            pickup_date=delivery, delivery_date=delivery, freight=1.0, fuel=0.0, accessorial=0.0,
            billing_lock=False,
        )

    sep1, sep9 = date(2026, 9, 1), date(2026, 9, 9)
    loads = [
        l(10, "niagara bottling, LLC", sep1, "305", "Luis Contreras"),
        l(11, "Niagara Bottling, LLC", sep9, "282", "Brian Thalman"),
        l(12, "Niagara Bottling, LLC", sep1, "282", "cory OMalley"),
        l(13, "Niagara Bottling, LLC", sep1, "282", "Brian Thalman"),
        l(14, "Niagara Bottling, LLC", sep1, "282", ""),
        l(15, "Niagara Bottling, LLC", None, "282", "Brian Thalman"),
        l(16, "Niagara Bottling, LLC", sep1, "1001", "Alanis Penalba"),
        l(17, "LMT Mercer Group, Inc.", sep9, "309", "Cody Steigerwald"),
    ]
    report = pr.PittReport(as_of=AS_OF, terminals={}, loads=loads, warnings=[])
    order = [x.load_id for x in report.tab("Unbilled")]
    # LMT before Niagara (case-insensitive); within Niagara the most Days (Sept 1)
    # first; among Sept 1 terminal 282 < 305 < 1001, and within 282 the order takers
    # A to Z with the blank one last; Sept 9 next; no delivery date (blank Days) last.
    assert order == [17, 13, 12, 14, 10, 16, 11, 15]


def test_email_summary():
    text, html = pr.email_bodies(_report())
    assert "terminals 282, 296" in text
    assert "Unbilled: 4 loads, $422.00" in text
    assert "90+" in html and "Billing Holds" in html


def test_carrier_name_joins_live_dispatches_in_order():
    dispatches = [
        {"id": 1, "status": "Delivered", "dispatchDate": "2026-09-01T00:00:00Z",
         "assignedTo": {"carrier": {"companyName": "First Leg Inc"}}},
        {"id": 2, "status": "Delivered", "dispatchDate": "2026-09-02T00:00:00Z",
         "assignedTo": {"carrier": {"companyName": "LIN TRANSPORT INC"}}},
        {"id": 3, "status": "Cancelled", "dispatchDate": "2026-09-03T00:00:00Z",
         "assignedTo": {"carrier": {"companyName": "Cancelled Carrier"}}},
    ]
    assert pr.carrier_name(dispatches) == "First Leg Inc, LIN TRANSPORT INC"
    assert pr.carrier_name(list(reversed(dispatches))) == "First Leg Inc, LIN TRANSPORT INC"
    assert pr.carrier_name([]) == ""


def test_report_load_json_roundtrip():
    l = _load(7, "X", date(2026, 9, 2), rc=None, ci=None, carrier=None)
    assert pr.ReportLoad.from_json(l.to_json()) == l


def test_checkpoint_resume_then_send_once():
    with tempfile.TemporaryDirectory() as tmp:
        docs, out, tpro, gmail = LocalDocs(Path(tmp) / "state"), Path(tmp) / "out", FakeTPro(), FakeGmail()

        # A run whose deadline has already passed does the search, then stops.
        first = pr.run(SETTINGS, tpro, out, docs, gmail=gmail, send=True, as_of=AS_OF,
                       deadline=time.monotonic() - 1)
        assert first["status"] == "in_progress" and first["files_checked"] == 0 and first["loads"] == 60
        assert tpro.file_calls == 0 and gmail.sent == []
        assert docs.get(pr.PROGRESS_DOC)["lease_until"] == 0  # released for the next run

        # The next run resumes the checkpoint (no new search), finishes and sends.
        tpro.loads = []  # a fresh search would now find nothing
        done = pr.run(SETTINGS, tpro, out, docs, gmail=gmail, send=True, as_of=AS_OF)
        assert done["status"] == "sent" and tpro.file_calls == 60
        assert done["counts"] == {"Unbilled": 60, "Rate Cons": 30, "Carrier Invoice": 40, "Billing Holds": 6}
        assert gmail.sent == [(["jay.price@circledelivers.com"], [],
                               "PITT November 2026 - Delivered Unbilled Loads", ["PITT November 2026.xlsx"])]
        assert docs.get(pr.sent_doc("2026-11"))["gmail_id"] == "msg1"
        wb = load_workbook(out / "PITT November 2026.xlsx")
        assert wb["Carrier Invoice"]["D2"].value.startswith("Carrier ")
        assert wb["Unbilled"]["C2"].value in (282, 296) and wb["Unbilled"]["D2"].value == "Brandon Hogan"

        # Later runs that month do nothing; a test send to someone else still works.
        again = pr.run(SETTINGS, tpro, out, docs, gmail=gmail, send=True, as_of=AS_OF)
        assert again["status"] == "already_sent" and len(gmail.sent) == 1
        test = pr.run(SETTINGS, tpro, out, docs, gmail=gmail, send=True, to=["me@circledelivers.com"], as_of=AS_OF)
        assert test["status"] == "sent" and gmail.sent[-1][0] == ["me@circledelivers.com"]
        assert docs.get(pr.sent_doc("2026-11"))["gmail_id"] == "msg1"


def test_test_send_is_not_recorded_and_claim_blocks_a_second_sender():
    with tempfile.TemporaryDirectory() as tmp:
        docs, out, gmail = LocalDocs(Path(tmp) / "state"), Path(tmp) / "out", FakeGmail()
        pr.run(SETTINGS, FakeTPro(6), out, docs, gmail=gmail, send=True, to=["me@circledelivers.com"], as_of=AS_OF)
        assert docs.get(pr.sent_doc("2026-11")) is None
        # Another run holds this month's send claim: no second email.
        assert docs.create(pr.claim_doc("2026-11"), {"by": "other"})
        r = pr.run(SETTINGS, FakeTPro(6), out, docs, gmail=gmail, send=True, as_of=AS_OF)
        assert r["status"] == "already_sent" and len(gmail.sent) == 1


def test_busy_when_another_run_holds_the_lease():
    with tempfile.TemporaryDirectory() as tmp:
        docs = LocalDocs(Path(tmp))
        docs.put(pr.PROGRESS_DOC, {"as_of": AS_OF.isoformat(), "lease_owner": "other",
                                   "lease_until": time.time() + 60, "loads": [], "terminals": {}})
        r = pr.run(SETTINGS, FakeTPro(), Path(tmp) / "out", docs, as_of=AS_OF)
        assert r["status"] == "busy"


def test_failed_send_releases_the_claim():
    class BrokenGmail(FakeGmail):
        def send_email(self, *a, **k):
            raise RuntimeError("smtp down")

    with tempfile.TemporaryDirectory() as tmp:
        docs, out = LocalDocs(Path(tmp)), Path(tmp) / "out"
        try:
            pr.run(SETTINGS, FakeTPro(6), out, docs, gmail=BrokenGmail(), send=True, as_of=AS_OF)
            raise AssertionError("expected the send to fail")
        except RuntimeError as exc:
            assert "smtp down" in str(exc)
        assert docs.get(pr.claim_doc("2026-11")) is None and docs.get(pr.sent_doc("2026-11")) is None
        r = pr.run(SETTINGS, FakeTPro(6), out, docs, gmail=FakeGmail(), send=True, as_of=AS_OF)
        assert r["status"] == "sent"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
