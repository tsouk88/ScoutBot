"""
ScoutBot — Weekly Email Digest

Sends a weekly Sunday digest to all subscribers containing:
  - Nigerian opportunities (from the "Nigeria" tab)
  - International opportunities (from the "International" tab)

Only entries added in the last RECENT_DAYS days are included.
Each subscriber receives a personal, individually addressed email.
Batched in groups of 30 with 6-minute pauses to respect Gmail limits.
"""

import os
import smtplib
import logging
import time
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SENDER_EMAIL         = os.getenv("SENDER_EMAIL", "")
GMAIL_APP_PASSWORD   = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
SPREADSHEET_ID       = os.getenv("SPREADSHEET_ID", "1pLCEvDI1btjtOe1H3VgzCqpC6R0nRsEtnTwQhY6BqmU")
FORM_SHEET_ID        = os.getenv("FORM_SHEET_ID", "1dFcnVvQjWkuYhN1rplICTY0j88KgvGqQ3FzYId2ru4s")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")

_ENV_EMAILS = [
    e.strip().lower()
    for e in os.getenv(
        "RECIPIENT_EMAILS",
        "tegazion7@gmail.com,successolamide46@gmail.com,"
        "ayanfeoluwaalalade2000@gmail.com,kamsirichard1960@gmail.com",
    ).split(",")
    if e.strip() and "@" in e
]

EMAIL_BATCH_SIZE      = int(os.getenv("EMAIL_BATCH_SIZE", "30"))
EMAIL_BATCH_PAUSE_SEC = int(os.getenv("EMAIL_BATCH_PAUSE_SEC", "360"))

# Only include opportunities added within this many days
RECENT_DAYS = 7

SHEET_URL       = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
FUNDRAISING_DOC = "https://docs.google.com/document/d/1SqxaAg4tvuWp3LgGzqSSSw4_bxBWHmgmrQ9IyyKHtE8/edit"
GITHUB_URL      = "https://github.com/TechHub-Extensions/ScoutBot"

# Gmail search link — lets subscribers find and delete all ScoutBot emails in one click
_GMAIL_SEARCH = (
    "https://mail.google.com/mail/u/0/#search/"
    "from%3Akamsirichard1960%40gmail.com+subject%3AScoutBot"
)
# Unsubscribe mailto
_UNSUB_MAILTO = (
    f"mailto:{SENDER_EMAIL}"
    "?subject=Unsubscribe%20from%20ScoutBot"
    "&body=Please%20remove%20me%20from%20the%20ScoutBot%20mailing%20list."
)


def _resolve_json_path():
    p = SERVICE_ACCOUNT_JSON
    if not os.path.isabs(p):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
    return p


def _get_sheet_client():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        _resolve_json_path(),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def fetch_form_subscribers():
    try:
        client = _get_sheet_client()
        ws = client.open_by_key(FORM_SHEET_ID).worksheets()[0]
        emails = [
            v.strip().lower()
            for v in ws.col_values(4)[1:]
            if v.strip() and "@" in v
        ]
        logger.info(f"notify: {len(emails)} emails from Form responses.")
        return emails
    except Exception as exc:
        logger.error(f"notify: Form sheet error — {exc}")
        return []


def fetch_subscribers_tab():
    try:
        client = _get_sheet_client()
        ss = client.open_by_key(SPREADSHEET_ID)
        try:
            sub_ws = ss.worksheet("Subscribers")
        except Exception:
            return []
        emails = [
            v.strip().lower()
            for v in sub_ws.col_values(2)[2:]
            if v.strip() and "@" in v
        ]
        logger.info(f"notify: {len(emails)} emails from Subscribers tab.")
        return emails
    except Exception as exc:
        logger.error(f"notify: Subscribers tab error — {exc}")
        return []


def build_recipient_list():
    combined = fetch_form_subscribers() + fetch_subscribers_tab() + _ENV_EMAILS
    seen, result = set(), []
    for e in combined:
        if e and e not in seen:
            seen.add(e)
            result.append(e)
    logger.info(f"notify: {len(result)} unique recipients.")
    return result


def fetch_recent_from_tab(tab_name, limit=25):
    """
    Fetch up to `limit` rows from the named tab that were added
    within the last RECENT_DAYS days.
    """
    try:
        client = _get_sheet_client()
        ss     = client.open_by_key(SPREADSHEET_ID)
        try:
            ws = ss.worksheet(tab_name)
        except Exception:
            logger.warning(f"notify: Tab '{tab_name}' not found.")
            return []

        rows = ws.get_all_records()
        cutoff = date.today() - timedelta(days=RECENT_DAYS)
        recent = []

        for row in rows:
            date_added_str = str(row.get("Date Added", "")).strip()
            if date_added_str:
                try:
                    from dateutil.parser import parse as dateutil_parse
                    added = dateutil_parse(date_added_str, fuzzy=True).date()
                    if added >= cutoff:
                        recent.append(row)
                except Exception:
                    recent.append(row)  # include if date unparseable
            else:
                recent.append(row)  # include legacy rows without Date Added

        # Return the most recent entries up to the limit
        result = recent[-limit:] if len(recent) > limit else recent
        logger.info(f"notify: {len(result)} recent entries from '{tab_name}' tab.")
        return result

    except Exception as exc:
        logger.error(f"notify: Could not fetch '{tab_name}' tab — {exc}")
        return []


def _opp_table(opps):
    """Build the HTML rows for an opportunity table."""
    category_colors = {
        "Scholarship":    "#1a5276",
        "Fellowship":     "#6c3483",
        "Internship":     "#117a65",
        "Bootcamp":       "#b7950b",
        "Apprenticeship": "#784212",
        "Conference":     "#1f618d",
        "Competition":    "#922b21",
        "Award":          "#7d6608",
        "Opportunity":    "#555",
    }
    rows_html = ""
    for opp in reversed(opps):
        link  = opp.get("Application Link", "#")
        title = opp.get("Title", "Untitled")
        cat   = opp.get("Category", "Opportunity")
        color = category_colors.get(cat, "#555")
        badge = (
            f'<span style="background:{color};color:#fff;padding:2px 8px;'
            f'border-radius:4px;font-size:11px;">{cat}</span>'
        )
        deadline = opp.get("Deadline", "") or "—"
        rows_html += f"""
        <tr>
          <td style="padding:9px 6px;border-bottom:1px solid #eee;">
            <a href="{link}" style="color:#1a5276;font-weight:600;text-decoration:none;"
               target="_blank">{title}</a><br>{badge}
          </td>
          <td style="padding:9px 6px;border-bottom:1px solid #eee;">{opp.get('Industry','')}</td>
          <td style="padding:9px 6px;border-bottom:1px solid #eee;">{opp.get('Education Level','')}</td>
          <td style="padding:9px 6px;border-bottom:1px solid #eee;">{opp.get('Organization','')}</td>
          <td style="padding:9px 6px;border-bottom:1px solid #eee;color:#e74c3c;font-weight:600;">{deadline}</td>
        </tr>"""
    return rows_html


def _section(title, emoji, bg, opps):
    """Build one section (Nigeria or International) of the email."""
    if not opps:
        return f"""
        <div style="padding:14px 24px;border:1px solid #ddd;border-top:none;color:#999;font-size:13px;">
          {emoji} <strong>{title}</strong> — No new opportunities this week.
        </div>"""

    return f"""
  <div style="background:{bg};color:#fff;padding:14px 24px;border-top:none;
              border-left:1px solid #ccc;border-right:1px solid #ccc;">
    <strong style="font-size:15px;">{emoji} {title}</strong>
    <span style="font-size:12px;opacity:.8;margin-left:8px;">{len(opps)} new this week</span>
  </div>
  <div style="padding:0 0 4px;border:1px solid #ddd;border-top:none;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;">
      <thead>
        <tr style="background:{bg};color:#fff;">
          <th style="padding:8px 6px;text-align:left;">Title &amp; Category</th>
          <th style="padding:8px 6px;text-align:left;">Industry</th>
          <th style="padding:8px 6px;text-align:left;">Level</th>
          <th style="padding:8px 6px;text-align:left;">Organization</th>
          <th style="padding:8px 6px;text-align:left;">Deadline</th>
        </tr>
      </thead>
      <tbody>{_opp_table(opps)}</tbody>
    </table>
  </div>"""


def build_html(nigeria_opps, intl_opps):
    total     = len(nigeria_opps) + len(intl_opps)
    week_str  = date.today().strftime("%B %d, %Y")

    nigeria_section = _section("Nigeria", "🇳🇬", "#1a5276", nigeria_opps)
    intl_section    = _section("International", "🌍", "#1d6348", intl_opps)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,sans-serif;color:#222;max-width:860px;margin:auto;padding:16px;">

  <div style="background:#1a1a2e;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0;">
    <h1 style="margin:0;font-size:22px;">ScoutBot — Weekly Digest</h1>
    <p style="margin:4px 0 0;font-size:13px;opacity:.8;">
      Week of {week_str} &nbsp;|&nbsp; {total} fresh opportunities for Nigerian students
    </p>
  </div>

  <div style="background:#f9f9f9;padding:12px 24px;border:1px solid #ddd;border-top:none;font-size:13px;">
    Showing only opportunities added <strong>this week</strong>.
    <a href="{SHEET_URL}" style="color:#1a5276;">View the full list on Google Sheets &rarr;</a>
  </div>

  {nigeria_section}
  {intl_section}

  <div style="background:#fff8e1;padding:12px 24px;border:1px solid #ffe082;border-top:none;font-size:12px;">
    <strong style="color:#b7950b;">Know someone who should receive this?</strong>
    &nbsp;<a href="{FUNDRAISING_DOC}" style="color:#1a5276;">Support ScoutBot &rarr;</a>
  </div>

  <!-- Unsubscribe + clear emails bar -->
  <div style="padding:12px 24px;font-size:12px;background:#fff3f3;border:1px solid #f5c6cb;border-top:none;">
    <strong>Manage your subscription:</strong>&nbsp;
    <a href="{_UNSUB_MAILTO}" style="color:#c0392b;font-weight:bold;">
      ✉ Unsubscribe from ScoutBot
    </a>
    &nbsp;&nbsp;|&nbsp;&nbsp;
    <a href="{_GMAIL_SEARCH}" style="color:#7f8c8d;" target="_blank">
      🗑 Find &amp; delete all ScoutBot emails in Gmail
    </a>
  </div>

  <div style="padding:10px 24px;font-size:11px;color:#aaa;border:1px solid #ddd;
              border-top:none;border-radius:0 0 8px 8px;background:#fafafa;">
    ScoutBot — Open Source &nbsp;|&nbsp;
    <a href="{GITHUB_URL}" style="color:#aaa;">GitHub</a>
    &nbsp;|&nbsp;
    <a href="{SHEET_URL}" style="color:#aaa;">Full Sheet</a>
    &nbsp;—&nbsp;
    ⚠️ <em>Vibecoded. Always verify opportunities at source before applying.</em>
  </div>

</body>
</html>"""


def _build_personal_email(html_body, subject, recipient_email):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"ScoutBot <{SENDER_EMAIL}>"
    msg["To"]      = recipient_email
    msg.attach(MIMEText(html_body, "html"))
    return msg


def send_email(nigeria_opps, intl_opps, recipients):
    if not SENDER_EMAIL or not GMAIL_APP_PASSWORD:
        logger.error("notify: SENDER_EMAIL or GMAIL_APP_PASSWORD not set.")
        return False
    if not recipients:
        logger.warning("notify: No recipients.")
        return False

    total = len(nigeria_opps) + len(intl_opps)
    subject   = f"ScoutBot Weekly — {total} Fresh Opportunities ({date.today().strftime('%b %d')})"
    html_body = build_html(nigeria_opps, intl_opps)

    batches       = [recipients[i:i + EMAIL_BATCH_SIZE]
                     for i in range(0, len(recipients), EMAIL_BATCH_SIZE)]
    total_batches = len(batches)
    logger.info(
        f"notify: Sending to {len(recipients)} recipients in "
        f"{total_batches} batch(es)."
    )

    successes = 0
    for i, batch in enumerate(batches, start=1):
        batch_ok = 0
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(SENDER_EMAIL, GMAIL_APP_PASSWORD)
                for addr in batch:
                    try:
                        msg = _build_personal_email(html_body, subject, addr)
                        server.sendmail(SENDER_EMAIL, [addr], msg.as_string())
                        batch_ok += 1
                    except Exception as exc:
                        logger.error(f"notify: Failed to send to {addr}: {exc}")
            successes += batch_ok
            logger.info(f"notify: Batch {i}/{total_batches} — {batch_ok}/{len(batch)} sent.")
        except Exception as exc:
            logger.error(f"notify: Batch {i}/{total_batches} SMTP error — {exc}")

        if i < total_batches:
            logger.info(f"notify: Pausing {EMAIL_BATCH_PAUSE_SEC}s...")
            time.sleep(EMAIL_BATCH_PAUSE_SEC)

    logger.info(f"notify: Done. {successes}/{len(recipients)} reached.")
    return successes > 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    nigeria_opps = fetch_recent_from_tab("Nigeria",       limit=25)
    intl_opps    = fetch_recent_from_tab("International", limit=25)

    if not nigeria_opps and not intl_opps:
        logger.warning("notify: No recent opportunities found. No email sent.")
        return

    recipients = build_recipient_list()
    send_email(nigeria_opps, intl_opps, recipients)


if __name__ == "__main__":
    main()
