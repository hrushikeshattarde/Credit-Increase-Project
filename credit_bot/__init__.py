"""Credit limit increase request automation.

Reads "<customer id> CREDIT LIMIT INCREASE REQUEST" emails sent to the
teamcredit@ group, pulls the customer's open-billing picture from Transport Pro,
and drops a reply draft in Gmail using the Operations Action Items template.
"""
