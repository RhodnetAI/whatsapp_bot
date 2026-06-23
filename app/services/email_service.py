import datetime
import html
import logging

import resend

from app.core.config import settings

logger = logging.getLogger("whatsapp")

resend.api_key = settings.resend_api_key


def build_booking_body(
    user_name: str,
    user_email: str,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    meet_link: str,
    purpose: str,
    calendar_event_link: str = "",
    recipient_is_admin: bool = False,
) -> str:
    """Build the plain-text booking confirmation body.

    Used for the WhatsApp follow-up message.
    """
    date_label = meeting_datetime.strftime("%A, %B %d, %Y")
    time_label = meeting_datetime.strftime("%I:%M %p")
    intro = (
        f"A meeting has been booked by {user_name}."
        if recipient_is_admin
        else f"Hi {user_name}, your meeting has been confirmed!"
    )
    lines = [
        intro,
        "",
        "Meeting Details",
        "───────────────────────────────",
        f"Name:      {user_name}",
        f"Email:     {user_email}",
        f"Date:      {date_label}",
        f"Time:      {time_label}",
        f"Duration:  {duration_minutes} minutes",
        f"Meet Link: {meet_link}",
    ]
    if recipient_is_admin and calendar_event_link:
        lines.append(f"Calendar:  {calendar_event_link}")
    lines += [
        "",
        "Purpose",
        "───────────────────────────────",
        purpose or "Not specified.",
        "",
        "───────────────────────────────",
        "This is an automated confirmation from your WhatsApp bot.",
    ]
    return "\n".join(lines)


# ── Shared email chrome (brand tokens mirrored from frontend/src/index.css) ───
# Primary purple scale: #5b38f0 (primary-1), #4a2dc4 (primary-2, used as the
# header badge tint). Neutrals: #f2f0f8 (base-2, page bg), #ffffff (card),
# #eceaf3 (base-3, dividers), #f0edfe (secondary-1, callout bg),
# #2b2b2b (contrast-2, body text), #6b6b6b (mid-4, secondary text),
# #959595 (mid-2, footer text).
_FONT = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
_PRIMARY = "#5b38f0"
_PRIMARY_DARK = "#4a2dc4"


def _email_open() -> str:
    return f"""<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background-color:#f2f0f8;font-family:{_FONT};">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f2f0f8;padding:40px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background-color:#ffffff;border:1px solid #eceaf3;border-radius:16px;overflow:hidden;">"""


def _email_header(badge_glyph: str, kicker: str, title: str) -> str:
    return f"""
            <tr>
              <td style="background-color:{_PRIMARY};padding:36px 32px 30px;text-align:center;">
                <table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:0 auto;">
                  <tr>
                    <td width="52" height="52" align="center" valign="middle" style="width:52px;height:52px;border-radius:50%;background-color:{_PRIMARY_DARK};font-size:24px;line-height:52px;color:#ffffff;font-family:{_FONT};">{badge_glyph}</td>
                  </tr>
                </table>
                <p style="margin:16px 0 4px;color:rgba(255,255,255,0.7);font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;font-family:{_FONT};">{kicker}</p>
                <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;font-family:{_FONT};">{title}</h1>
              </td>
            </tr>"""


def _email_footer(note: str = "This is an automated message — no reply needed.") -> str:
    return f"""
            <tr>
              <td style="padding:22px 32px 28px;text-align:center;border-top:1px solid #eceaf3;">
                <p style="margin:0;color:#959595;font-size:12px;font-family:{_FONT};">{note}</p>
              </td>
            </tr>"""


def _email_close() -> str:
    return """
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def _detail_row(label: str, value: str, is_last: bool = False) -> str:
    border = "" if is_last else "border-bottom:1px solid #eceaf3;"
    return f"""
                  <tr>
                    <td style="padding:13px 18px;{border}">
                      <p style="margin:0 0 2px;color:#6b6b6b;font-size:11px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase;font-family:{_FONT};">{html.escape(label)}</p>
                      <p style="margin:0;color:#2b2b2b;font-size:14px;font-weight:600;font-family:{_FONT};">{html.escape(value)}</p>
                    </td>
                  </tr>"""


def _build_email_html(
    user_name: str,
    user_email: str,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    meet_link: str,
    purpose: str,
    calendar_event_link: str = "",
    recipient_is_admin: bool = False,
) -> str:
    """Build an HTML confirmation email styled to match the website's brand
    (primary purple #5b38f0, rounded cards, lavender accents — see
    frontend/src/index.css for the source design tokens)."""
    date_label = meeting_datetime.strftime("%A, %B %d, %Y")
    time_label = meeting_datetime.strftime("%I:%M %p")

    if recipient_is_admin:
        badge_glyph, kicker, title = "&#128197;", "New Booking", "Meeting Booked"
        intro = f"A new meeting has been booked by <strong>{html.escape(user_name)}</strong>."
    else:
        badge_glyph, kicker, title = "&#10003;", "Booking Confirmed", "You're All Set!"
        intro = f"Hi {html.escape(user_name)}, your meeting has been confirmed. Here are the details:"

    rows = [
        _detail_row("Name", user_name),
        _detail_row("Email", user_email),
        _detail_row("Date", date_label),
        _detail_row("Time", time_label),
        _detail_row("Duration", f"{duration_minutes} minutes", is_last=True),
    ]

    calendar_button = ""
    if recipient_is_admin and calendar_event_link:
        calendar_button = f"""
                <table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:12px auto 0;">
                  <tr>
                    <td style="border-radius:10px;border:1.5px solid {_PRIMARY};">
                      <a href="{html.escape(calendar_event_link)}" style="display:inline-block;padding:11px 28px;color:{_PRIMARY};text-decoration:none;font-weight:600;font-size:13.5px;font-family:{_FONT};">View in Google Calendar</a>
                    </td>
                  </tr>
                </table>"""

    purpose_text = html.escape(purpose) if purpose else "Not specified."

    return f"""{_email_open()}{_email_header(badge_glyph, kicker, title)}
            <tr>
              <td style="padding:32px;">
                <p style="margin:0 0 22px;color:#2b2b2b;font-size:15px;line-height:1.6;font-family:{_FONT};">{intro}</p>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#faf9fe;border:1px solid #eceaf3;border-radius:12px;">{"".join(rows)}
                </table>
                <table role="presentation" cellpadding="0" cellspacing="0" align="center" style="margin:28px auto 0;">
                  <tr>
                    <td style="border-radius:10px;background-color:{_PRIMARY};">
                      <a href="{html.escape(meet_link)}" style="display:inline-block;padding:14px 36px;color:#ffffff;text-decoration:none;font-weight:600;font-size:14.5px;font-family:{_FONT};">Join Google Meet &rarr;</a>
                    </td>
                  </tr>
                </table>{calendar_button}
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:28px;background-color:#f0edfe;border-radius:0 10px 10px 0;border-left:3px solid {_PRIMARY};">
                  <tr>
                    <td style="padding:14px 18px;">
                      <p style="margin:0 0 4px;color:#4a2dc4;font-size:11px;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;font-family:{_FONT};">Purpose</p>
                      <p style="margin:0;color:#2b2b2b;font-size:14px;line-height:1.5;font-family:{_FONT};">{purpose_text}</p>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>{_email_footer()}{_email_close()}"""


def _send_email(to_address: str, subject: str, html_body: str) -> bool:
    """Send a single email via the Resend API. Returns True on success."""
    try:
        resend.Emails.send({
            "from": settings.resend_from_email,
            "to": to_address,
            "subject": subject,
            "html": html_body,
        })
        logger.info("Confirmation email sent to %s via Resend", to_address)
        return True
    except Exception:
        logger.exception("Resend API request failed for recipient=%s", to_address)
        return False


def send_meeting_confirmation(
    user_email: str,
    user_name: str,
    meeting_datetime: datetime.datetime,
    duration_minutes: int,
    meet_link: str,
    purpose: str,
    calendar_event_link: str = "",
    email_enabled: bool = False,
) -> None:
    """Send booking confirmation emails, gated entirely by the Scheduler's Email
    notification toggle: when off, neither the user nor the admin receives an
    email; when on, the user always gets a confirmation and the admin
    (ADMIN_NOTIFICATION_EMAIL) gets a separate notification copy."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not configured; skipping confirmation email")
        return

    if not email_enabled:
        logger.info("Email notification toggle is off; skipping booking confirmation emails")
        return

    when = meeting_datetime.strftime("%b %d, %Y %I:%M %p UTC")

    user_body = _build_email_html(
        user_name=user_name,
        user_email=user_email,
        meeting_datetime=meeting_datetime,
        duration_minutes=duration_minutes,
        meet_link=meet_link,
        purpose=purpose,
        calendar_event_link=calendar_event_link,
        recipient_is_admin=False,
    )
    if _send_email(user_email, f"Meeting Confirmed – {when}", user_body):
        logger.info("Confirmation email sent successfully to %s", user_email)
    else:
        logger.error("Confirmation email failed to send to %s", user_email)

    if settings.admin_notification_email:
        admin_body = _build_email_html(
            user_name=user_name,
            user_email=user_email,
            meeting_datetime=meeting_datetime,
            duration_minutes=duration_minutes,
            meet_link=meet_link,
            purpose=purpose,
            calendar_event_link=calendar_event_link,
            recipient_is_admin=True,
        )
        if _send_email(settings.admin_notification_email, f"New Meeting Booked – {when}", admin_body):
            logger.info(
                "Admin notification email sent successfully to %s", settings.admin_notification_email
            )
        else:
            logger.error(
                "Admin notification email failed to send to %s", settings.admin_notification_email
            )
    else:
        logger.warning("ADMIN_NOTIFICATION_EMAIL not configured; skipping admin notification email")


def _build_flow_completion_html(sender: str, questions: list[dict]) -> str:
    """Build an HTML summary of a completed flow's responses for the admin."""
    row_count = len(questions)
    rows = "".join(
        _detail_row(
            str(q.get("question") or "Question"),
            str(q.get("answer") or "—"),
            is_last=(i == row_count - 1),
        )
        for i, q in enumerate(questions)
    )

    return f"""{_email_open()}{_email_header("&#9998;", "Flow Completed", "New Responses")}
            <tr>
              <td style="padding:32px;">
                <p style="margin:0 0 22px;color:#2b2b2b;font-size:15px;line-height:1.6;font-family:{_FONT};">A user (<strong>{html.escape(sender)}</strong>) just completed the flow. Here are their responses:</p>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#faf9fe;border:1px solid #eceaf3;border-radius:12px;">{rows}
                </table>
              </td>
            </tr>{_email_footer("This is an automated notification from your WhatsApp bot.")}{_email_close()}"""


def build_flow_completion_body(sender: str, questions: list[dict]) -> str:
    """Build the plain-text flow-completion summary. Used for the admin
    WhatsApp notification (Flow Creation's WhatsApp notification toggle)."""
    lines = [
        "A user completed the flow.",
        "",
        f"Sender: {sender}",
        "",
        "Responses",
        "───────────────────────────────",
    ]
    for q in questions:
        lines.append(f"Q: {q.get('question') or 'Question'}")
        lines.append(f"A: {q.get('answer') or '—'}")
        lines.append("")
    lines += [
        "───────────────────────────────",
        "This is an automated notification from your WhatsApp bot.",
    ]
    return "\n".join(lines)


def send_flow_completion_email(sender: str, questions: list[dict], email_enabled: bool = False) -> None:
    """Notify the admin (ADMIN_NOTIFICATION_EMAIL) that a user completed the
    flow, gated by the Flow Creation section's Email notification toggle."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not configured; skipping flow completion email")
        return

    if not email_enabled:
        logger.info("Email notification toggle is off; skipping flow completion email")
        return

    if not settings.admin_notification_email:
        logger.warning("ADMIN_NOTIFICATION_EMAIL not configured; skipping flow completion email")
        return

    body = _build_flow_completion_html(sender, questions)
    if _send_email(settings.admin_notification_email, "Flow Completed - New Responses", body):
        logger.info("Flow completion email sent successfully to %s", settings.admin_notification_email)
    else:
        logger.error("Flow completion email failed to send to %s", settings.admin_notification_email)
