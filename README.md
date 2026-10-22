# Credit Increase Request Bot

Watches the credit team mailbox for form emails with the subject
`<customer id> CREDIT LIMIT INCREASE REQUEST <customer name>`, pulls the
customer's open-billing picture from Transport Pro, and creates a Gmail reply
draft (threaded on the request) using the "Operations Action Items" template.

## Setup

    pip install -r requirements.txt

`.env` holds the Transport Pro API credentials (required) and the optional
Gmail settings (see comments in the file).  The Google service-account key
file must have domain-wide delegation for the `gmail.readonly` and
`gmail.compose` scopes on the mailbox in `GMAIL_USER`.

## Run

    python main.py --customer 5245      # figures + HTML preview for one customer (out/preview_5245.html)
    python main.py --dry-run            # find new request emails, write previews to out/, no drafts
    python main.py                      # create drafts for new request emails
    python main.py --force              # recompute and update existing drafts in place
    python main.py --watch              # keep polling (POLL_SECONDS)
    python main.py --message-id <id>    # process one specific Gmail message

Processed message ids are recorded in `state/processed.json`; a thread that
already contains a draft is skipped too. Logs go to `logs/credit_bot.log`.

## Which requests are handled

The form email's subject carries the customer id. A plain number
(`10343 CREDIT LIMIT INCREASE REQUEST ...`) is a Transport Pro customer and gets
the full reply. A `CT` prefix (`CT 1562 CREDIT LIMIT INCREASE REQUEST ...`,
"Needed In: Circle Transportation") is a Circle Transportation customer, whose
loads are not in this Transport Pro tenant: the bot posts a **notice card** with the
request details and an "Open the email in Gmail" button, no draft and no action
buttons, so the request is visible and answered by hand. Any other subject shape is
logged as unrecognised and skipped.

## What is computed (mirrors the manual Transport Pro load search)

| Template line | Transport Pro search |
|---|---|
| Unbilled Load Balance | customer id + Dispatch status *Delivered* + Billing status *Billing Open*: load count, sum of freight + fuel + accessorial charges, oldest and latest pickup date |
| Loads Missing Rate Confirmations | same loads, Missing Image *Rate Confirmation*: count, oldest and latest pickup date |
| Loads Missing Carrier Invoices | same loads, Missing Image *Carrier Invoice*: count, oldest and latest pickup date |
| Loads on Billing Hold | same loads, Billing Hold *Enabled*: count, oldest and latest pickup date |

The public API caps each load search at a 60-day date window, so the lookback
(`LOOKBACK_DAYS`) is walked in 60-day pickup-date windows.

"Missing Image <type>" on the web screen means *no file of that document type
is attached to the load*, whatever the load's document status says. The bot
therefore lists each open load's attached files (`GET /files/search`) and
looks for the exact document types "Rate Confirmation" and "Carrier Invoice".
(The tenant-wide `/load/missing_documents` listing is not equivalent: it only
covers loads still "Waiting for Documents" and misses, for example, a
delivered load whose carrier invoice never arrived.) Validated against the
web screen for seven customers on 2026-10-21, counts and load ids identical.

**Billing Hold** uses the API flag Transport Pro support exposed on
2026-10-21 (ticket #911213): every load carries `status.billingLock`
(true = the web screen's "Billing Hold: Hold Enabled"), and
`GET /load/search` accepts `hasBillingLock=true`. The bot counts the flag on
the loads it already fetched for step 1, so the hold count costs no extra
calls; the filtered search is the fallback if the flag is ever absent.
Validated against the web screen for customers 10343 (5), 5245 (0) and
10509 (3).

"Unanswered Operations Requests" comes from the Operations Request Sheet (not
Transport Pro) and is rendered as `0`.

## Approval cards in Google Chat (optional)

With `APPROVAL_MODE=chat` each reply is posted as a card in the *Credit Increase
Draft* space instead of being saved as a Gmail draft: the customer and the figures,
the recipients, the draft text, and the buttons **Approve & send as me**,
**Move to my Gmail Drafts**, **Reject**, plus a link that opens the request in the
clicker's Gmail. A click sends the stored reply **from the clicker's own address**
with teamcredit in Cc and Reply-To, and the card is rewritten with who clicked and
when. Exactly one click wins; a failed send leaves the card retryable.

Everything is the credit bot's own, independent of the payment bot:

| Piece | Where |
|---|---|
| Chat app + service account | the credit bot's Google Cloud project (manual setup below) |
| Click callback | AWS stack `creditbot-prod` (`deploy/template.yaml`; deploy with `python deploy/deploy.py --profile paybot-admin`, the profile with IAM rights): Lambda `creditbot-chat-callback-prod` behind a public Function URL, verifying Google's bearer token |
| Pending entries | S3 `creditbot-state-<account>/approvals/{pending,claims,results}/` |
| Gmail delegation | the existing service-account key, uploaded by `deploy.py --sa-key` as a private object `config/google-service-account.json` in the state bucket (readable only by the Lambda role) |
| Reviewer roster | stack parameter `Reviewers` in `deploy/params.prod.json` |

### One-time Google Cloud setup (console)
1. Create a Google Cloud project for the bot (e.g. `credit-increase-bot`) and note its
   project **number**.
2. APIs & Services → enable **Google Chat API**.
3. Google Chat API → Configuration: app name (e.g. *Credit Approvals*), avatar,
   description; Functionality: *Receive 1:1 messages* and *Join spaces and group
   conversations*; Connection settings: **HTTP endpoint URL** = the stack's
   `CallbackUrl` output; Visibility: make it available to the people/groups who will
   use the space.
4. IAM → Service accounts → create one (no roles needed), create a **JSON key**, save it
   as `credit-chat-service-account.json` in this folder (git-ignored). It only needs
   the Chat API (app auth); no domain-wide delegation.
5. `deploy/params.prod.json`: set `ChatAudience` to the project number and
   `Reviewers` to the people allowed to click; run
   `python deploy/deploy.py --profile paybot-admin --skip-build`.
6. In Chat, open the *Credit Increase Draft* space → Add apps & integrations → add the
   app. Then `python main.py --spaces` prints the space id for `CHAT_SPACE`.
7. `.env`: `APPROVAL_MODE=chat`, `CHAT_SPACE`, `CHAT_ACTION_URL`, `APPROVALS_BUCKET`
   (both from the stack outputs). `python main.py --dry-run` writes each card's JSON
   to `out/` before anything is posted.

If a card cannot be posted the bot falls back to a Gmail draft, so a Chat outage
degrades to the old workflow, never to silence.

## Running it all day (AWS)

The bot runs in AWS as two Lambda functions in stack `creditbot-prod`
(`deploy/template.yaml`, deployed with `python deploy/deploy.py --profile paybot-admin`):

| Function | Trigger | Job |
|---|---|---|
| `creditbot-worker-prod` | EventBridge, every 5 minutes (`ScheduleEnabled` parameter) | reads the request emails, pulls the figures from Transport Pro, posts the card (or a Gmail draft when chat is not configured) |
| `creditbot-chat-callback-prod` | the Chat app's HTTP endpoint (Function URL) | handles Approve / Move / Reject clicks, sends as the clicker, rewrites the card |

State and secrets live in the stack: pending entries and the processed ledger in
`creditbot-state-<account>`, the two Google keys as private objects there
(`config/`), and the Transport Pro credentials in Secrets Manager
(`creditbot/prod/transportpro`). First-time uploads:

    python deploy/deploy.py --profile paybot-admin --sa-key <gmail-key>.json --chat-key <chat-key>.json --tpro-secret

To test a change without posting anything:

    aws lambda invoke --function-name creditbot-worker-prod --cli-binary-format raw-in-base64-out --payload '{"dry_run": true}' out.json

Logs: CloudWatch `/aws/lambda/creditbot-worker-prod` and `/aws/lambda/creditbot-chat-callback-prod`.
The workstation commands above still work (`python main.py`, `--watch`) and share the
same approvals store, so a local run never duplicates a card the Lambda posted.
