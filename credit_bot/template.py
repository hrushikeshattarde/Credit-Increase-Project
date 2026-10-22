"""Renders the "Operations Action Items" reply exactly as the credit team's
Gmail template (same tags, inline styles, colours and fonts)."""
from __future__ import annotations

from html import escape

from .metrics import CreditMetrics

# Inline styles copied verbatim from the team's template.
_LI = (
    "margin-left:15px;list-style-type:disc;font-size:11pt;background-color:transparent;"
    "font-variant-numeric:normal;font-variant-east-asian:normal;font-variant-alternates:normal;"
    "vertical-align:baseline"
)
_ITEM_P = "line-height:1.8;margin-top:0pt;margin-bottom:0pt"
_LABEL = (
    "font-size:11pt;background-color:transparent;font-weight:700;font-variant-numeric:normal;"
    "font-variant-east-asian:normal;font-variant-alternates:normal;vertical-align:baseline"
)
_VALUE = (
    "font-size:11pt;background-color:transparent;font-variant-numeric:normal;"
    "font-variant-east-asian:normal;font-variant-alternates:normal;vertical-align:baseline"
)
_NOTE_P = "line-height:1.8;margin-left:72pt;margin-top:0pt;margin-bottom:0pt"
_NOTE_SPAN = (
    "font-size:10pt;background-color:transparent;font-weight:700;font-style:italic;"
    "font-variant-numeric:normal;font-variant-east-asian:normal;font-variant-alternates:normal;"
    "vertical-align:baseline"
)
_FOOT_P = "line-height:1.2;margin-top:0pt;margin-bottom:0pt"
_FOOT_SPAN = (
    "font-size:10pt;color:rgb(0,0,0);background-color:transparent;font-weight:700;"
    "font-style:italic;font-variant-numeric:normal;font-variant-east-asian:normal;"
    "font-variant-alternates:normal;vertical-align:baseline"
)

ARROW = "➤"  # ➤
DASH = "–"  # –

HOLD_PLACEHOLDER = "[VERIFY IN TRANSPORT PRO]"


def _item(label: str, value: str, bold_value: bool = False) -> str:
    if bold_value:
        inner = f'<span style="{_LABEL}">{escape(label)} {escape(value)}</span>'
    else:
        inner = (
            f'<span style="{_LABEL}">{escape(label)}</span>'
            f'<span style="{_VALUE}"> {escape(value)}</span>'
        )
    return (
        f'<ul style="margin-top:0px;margin-bottom:0px"><li dir="ltr" style="{_LI}">'
        f'<p dir="ltr" role="presentation" style="{_ITEM_P}">{inner}</p></li></ul>'
    )


def _note(text: str) -> str:
    return (
        f'<p dir="ltr" style="{_NOTE_P}"><span style="{_NOTE_SPAN}">'
        f'<font color="#cc0000">{ARROW} {escape(text)}</font></span></p>'
    )


def money(amount: float) -> str:
    return f"${amount:,.2f}"


def fmt_date(d) -> str:
    return d.strftime("%m/%d/%Y") if d else "n/a"


def unbilled_value(m: CreditMetrics) -> str:
    if m.open_load_count == 0:
        return "$0 (0 loads)"
    loads = "1 load" if m.open_load_count == 1 else f"{m.open_load_count} loads"
    return (
        f"{money(m.open_load_total)} ({loads}, oldest {fmt_date(m.oldest_pickup)}, "
        f"latest {fmt_date(m.latest_pickup)})"
    )


def billing_hold_value(m: CreditMetrics) -> str:
    return str(m.billing_hold_count) if m.billing_hold_count is not None else HOLD_PLACEHOLDER


def render_html(m: CreditMetrics, unanswered_ops_requests: int | str = 0) -> str:
    parts = [
        '<div dir="ltr"><div dir="ltr"><div dir="ltr"><div>'
        '<div class="gmail_default" style="color:rgb(0,0,0)"><div style="color:rgb(34,34,34)">'
        '<font face="arial, sans-serif">',
        '<h3 dir="ltr" style="line-height:1.2;margin-top:0pt;margin-bottom:0pt"><p>'
        '<font size="4" color="#000000" style="background-color:rgb(255,255,0)">Operations Action Items</font>'
        f'<font size="4" color="#ff0000" style="background-color:rgb(255,255,0)"> {DASH} <u>Immediate Attention Required</u></font>'
        "</p></h3>",
        '<p style="line-height:1.2;margin-top:0pt;margin-bottom:0pt">'
        '<font size="4" style="background-color:rgb(255,255,0)"><font color="#000000">'
        "Credit will be issued once a response is received on the items below "
        "(unless already addressed in your request). </font>"
        '<b style="color:rgb(255,0,0)">Failure to respond will delay or prevent completion of your request.</b>'
        "</font></p>",
        '<p style="line-height:1.2;margin-top:0pt;margin-bottom:0pt"><br></p>',
        _item("Unanswered Operations Requests:", str(unanswered_ops_requests)),
        _note("Please review and take action on all pending requests in the Operations Request Sheet."),
        _item("Unbilled Load Balance:", unbilled_value(m)),
        _note("This balance directly impacts your available customer credit limit."),
        _item("Loads Missing Rate Confirmations:", str(m.missing_rate_confirmation_count)),
        _note("Review and upload missing rate confirmations as soon as possible."),
        _note(
            "If any rate confirmations are being held for fuel updates or accessorial approvals, "
            "please include that note in your response."
        ),
        _item("Loads Missing Carrier Invoices:", str(m.missing_carrier_invoice_count), bold_value=True),
        _note("Review and upload missing carrier invoices to ensure timely billing and payment processing."),
        _item("Loads on Billing Hold:", billing_hold_value(m), bold_value=True),
        _note("Review and resolve these holds."),
        _note("Contact the billing team once resolved to ensure loads are released for invoicing."),
        _note("Credit team does not release the billing holds."),
        "<br>",
        f'<p dir="ltr" style="{_FOOT_P}"><span style="{_FOOT_SPAN}">Note:</span></p>',
        f'<p dir="ltr" style="{_FOOT_P}"><span style="{_FOOT_SPAN}">'
        "The unbilled load balance affects your available credit limit. Carriers remain unpaid, "
        "and Customer Net Terms do not begin until invoices are issued.&nbsp; We appreciate your "
        "assistance in resolving these items as soon as possible.</span></p>",
        "</font></div></div></div></div></div></div>",
    ]
    return "".join(parts)


def render_text(m: CreditMetrics, unanswered_ops_requests: int | str = 0) -> str:
    lines = [
        f"Operations Action Items {DASH} Immediate Attention Required",
        "",
        "Credit will be issued once a response is received on the items below (unless already "
        "addressed in your request). Failure to respond will delay or prevent completion of your request.",
        "",
        f"- Unanswered Operations Requests: {unanswered_ops_requests}",
        f"    {ARROW} Please review and take action on all pending requests in the Operations Request Sheet.",
        f"- Unbilled Load Balance: {unbilled_value(m)}",
        f"    {ARROW} This balance directly impacts your available customer credit limit.",
        f"- Loads Missing Rate Confirmations: {m.missing_rate_confirmation_count}",
        f"    {ARROW} Review and upload missing rate confirmations as soon as possible.",
        f"    {ARROW} If any rate confirmations are being held for fuel updates or accessorial approvals, "
        "please include that note in your response.",
        f"- Loads Missing Carrier Invoices: {m.missing_carrier_invoice_count}",
        f"    {ARROW} Review and upload missing carrier invoices to ensure timely billing and payment processing.",
        f"- Loads on Billing Hold: {billing_hold_value(m)}",
        f"    {ARROW} Review and resolve these holds.",
        f"    {ARROW} Contact the billing team once resolved to ensure loads are released for invoicing.",
        f"    {ARROW} Credit team does not release the billing holds.",
        "",
        "Note:",
        "The unbilled load balance affects your available credit limit. Carriers remain unpaid, and "
        "Customer Net Terms do not begin until invoices are issued.  We appreciate your assistance in "
        "resolving these items as soon as possible.",
    ]
    return "\n".join(lines)
