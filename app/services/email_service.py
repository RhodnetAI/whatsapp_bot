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


def _detail_row(label: str, value: str) -> str:
    return f"""
              <tr>
                <td style="padding:10px 16px;color:#6b6b6b;font-size:13px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;white-space:nowrap;">{html.escape(label)}</td>
                <td style="padding:10px 16px;color:#2b2b2b;font-size:13px;font-weight:600;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">{html.escape(value)}</td>
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
    """Build an HTML confirmation email styled to match the website theme."""
    date_label = meeting_datetime.strftime("%A, %B %d, %Y")
    time_label = meeting_datetime.strftime("%I:%M %p")

    if recipient_is_admin:
        intro = f"A new meeting has been booked by <strong>{html.escape(user_name)}</strong>."
    else:
        intro = f"Hi {html.escape(user_name)}, your meeting has been confirmed! Here are the details:"

    rows = [
        _detail_row("Name", user_name),
        _detail_row("Email", user_email),
        _detail_row("Date", date_label),
        _detail_row("Time", time_label),
        _detail_row("Duration", f"{duration_minutes} minutes"),
    ]

    calendar_section = ""
    if recipient_is_admin and calendar_event_link:
        calendar_section = f"""
              <div style="text-align:center;margin:16px 0 0;">
                <a href="{html.escape(calendar_event_link)}" style="color:#5b38f0;text-decoration:none;font-weight:600;font-size:13px;">View in Google Calendar &rarr;</a>
              </div>"""

    purpose_text = html.escape(purpose) if purpose else "Not specified."

    return f"""<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background-color:#f2f0f8;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f2f0f8;padding:32px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background-color:#ffffff;border:1px solid #d6d4df;border-radius:12px;overflow:hidden;">
            <tr>
              <td style="background-color:#5b38f0;padding:28px 32px;">
                <h1 style="margin:0;color:#ffffff;font-size:20px;font-weight:600;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">Meeting Confirmed</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:32px;">
                <p style="margin:0 0 20px;color:#2b2b2b;font-size:15px;line-height:1.6;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">{intro}</p>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0edfe;border-radius:8px;">{"".join(rows)}
                </table>
                <div style="text-align:center;margin:28px 0 0;">
                  <a href="{html.escape(meet_link)}" style="display:inline-block;background-color:#5b38f0;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;padding:12px 28px;border-radius:8px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">Join Google Meet</a>
                </div>{calendar_section}
                <p style="margin:28px 0 0;color:#2b2b2b;font-size:14px;line-height:1.6;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"><strong>Purpose</strong><br/>{purpose_text}</p>
              </td>
            </tr>
            <tr>
              <td style="background-color:#f2f0f8;padding:16px 32px;text-align:center;">
                <p style="margin:0;color:#808080;font-size:12px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">This is an automated confirmation from your WhatsApp bot.</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


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
) -> None:
    """Send a confirmation email to the user and a separate notification email to the admin."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not configured; skipping confirmation email")
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

    if settings.admin_email:
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
        if _send_email(settings.admin_email, f"New Meeting Booked – {when}", admin_body):
            logger.info("Admin notification email sent successfully to %s", settings.admin_email)
        else:
            logger.error("Admin notification email failed to send to %s", settings.admin_email)
    else:
        logger.warning("ADMIN_EMAIL not configured; skipping admin notification email")


def _build_flow_completion_html(sender: str, questions: list[dict]) -> str:
    """Build an HTML summary of a completed flow's responses for the admin."""
    rows = "".join(
        _detail_row(str(q.get("question") or "Question"), str(q.get("answer") or "—"))
        for q in questions
    )

    return f"""<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background-color:#f2f0f8;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f2f0f8;padding:32px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background-color:#ffffff;border:1px solid #d6d4df;border-radius:12px;overflow:hidden;">
            <tr>
              <td style="background-color:#5b38f0;padding:28px 32px;">
                <h1 style="margin:0;color:#ffffff;font-size:20px;font-weight:600;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">Flow Completed</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:32px;">
                <p style="margin:0 0 20px;color:#2b2b2b;font-size:15px;line-height:1.6;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">A user (<strong>{html.escape(sender)}</strong>) just completed the flow. Here are their responses:</p>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0edfe;border-radius:8px;">{rows}
                </table>
              </td>
            </tr>
            <tr>
              <td style="background-color:#f2f0f8;padding:16px 32px;text-align:center;">
                <p style="margin:0;color:#808080;font-size:12px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">This is an automated notification from your WhatsApp bot.</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def send_flow_completion_email(sender: str, questions: list[dict]) -> None:
    """Notify the admin that a user completed the flow, with their answers."""
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not configured; skipping flow completion email")
        return

    if not settings.admin_email:
        logger.warning("ADMIN_EMAIL not configured; skipping flow completion email")
        return

    body = _build_flow_completion_html(sender, questions)
    if _send_email(settings.admin_email, "Flow Completed - New Responses", body):
        logger.info("Flow completion email sent successfully to %s", settings.admin_email)
    else:
        logger.error("Flow completion email failed to send to %s", settings.admin_email)
