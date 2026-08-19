"""Notification email for a new website contact-form message.

A branded, email-safe HTML alert (matching the invoice email's look) to the
staff inbox, with a plain-text fallback and reply-to set to the customer so
staff can reply straight from their mailbox. Failures here must never break the
submission (the message is already saved) — the caller sends best-effort.
"""

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils.html import escape

from apps.ships.models import Ship


def notify_recipient():
    """The inbox new messages are emailed to: the (single) ship's override if
    set, otherwise the system default. Ship.contact_notify_recipient already
    resolves the fallback; we just pick the ship."""
    ship = Ship.objects.order_by("id").first()
    if ship:
        return ship.contact_notify_recipient
    return getattr(settings, "CONTACT_NOTIFY_EMAIL", "")


def _ship_name():
    ship = Ship.objects.order_by("id").first()
    return ship.name if ship else "MV Alaska"


def _detail_rows(message):
    """The customer-supplied fields, only the ones actually filled in."""
    rows = [
        ("Name", message.name),
        ("Inquiry", message.get_inquiry_type_display()),
        ("Email", message.email),
        ("Phone", message.phone),
    ]
    if message.departure_date:
        rows.append(("Departure", f"{message.departure_date:%d %b %Y}"))
    if message.guests:
        rows.append(("Guests", str(message.guests)))
    return [(label, value) for label, value in rows if value]


def _plain_body(message):
    lines = ["New inquiry from the website contact form.", ""]
    for label, value in _detail_rows(message):
        lines.append(f"{label + ':':<11}{value}")
    lines += ["", "Message:", message.message or "—"]
    return "\n".join(lines)


def _html_body(message):
    ship_name = escape(_ship_name())

    label_style = 'style="padding:9px 0;color:#69737d;font-size:13px;width:32%;"'
    value_style = (
        'style="padding:9px 0;color:#28323c;font-size:13px;font-weight:bold;"'
    )
    detail_rows = "".join(
        f'<tr style="border-bottom:1px solid #eef1f5;">'
        f"<td {label_style}>{escape(label)}</td>"
        f"<td {value_style}>{escape(value)}</td></tr>"
        for label, value in _detail_rows(message)
    )

    message_html = (
        escape(message.message).replace("\n", "<br>")
        if message.message
        else '<span style="color:#9aa4ad;">— no message —</span>'
    )

    # A tidy reply hint: the reply-to is set on the email, but surface the
    # customer's channels so staff can act at a glance.
    reply_channels = []
    if message.email:
        reply_channels.append(
            f'<a href="mailto:{escape(message.email)}" '
            f'style="color:#c8a24a;text-decoration:none;">{escape(message.email)}</a>'
        )
    if message.phone:
        reply_channels.append(
            f'<a href="tel:{escape(message.phone)}" '
            f'style="color:#c8a24a;text-decoration:none;">{escape(message.phone)}</a>'
        )
    reply_line = " &nbsp;·&nbsp; ".join(reply_channels) or "no contact details"

    return f"""\
<div style="background:#eef1f5;padding:28px 12px;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #dfe4ea;
                border-radius:8px;border-collapse:separate;overflow:hidden;">
    <tr>
      <td style="background:#102e50;padding:24px 30px;">
        <div style="color:#ffffff;font-size:22px;font-weight:bold;">{ship_name}</div>
        <div style="color:#c2d0e0;font-size:11px;letter-spacing:2px;margin-top:3px;">
          WEBSITE CONTACT INQUIRY</div>
      </td>
    </tr>
    <tr>
      <td style="padding:26px 30px 0;color:#28323c;font-size:14px;line-height:1.65;">
        You have a new inquiry from the website contact form.
      </td>
    </tr>
    <tr>
      <td style="padding:20px 30px 0;">
        <div style="color:#102e50;font-size:12px;font-weight:bold;letter-spacing:1px;
                    border-bottom:2px solid #102e50;padding-bottom:5px;">
          CONTACT DETAILS</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="border-collapse:collapse;">{detail_rows}</table>
      </td>
    </tr>
    <tr>
      <td style="padding:22px 30px 0;">
        <div style="color:#102e50;font-size:12px;font-weight:bold;letter-spacing:1px;
                    border-bottom:2px solid #102e50;padding-bottom:5px;">
          MESSAGE</div>
        <div style="background:#f7f9fb;border-radius:6px;padding:14px 16px;margin-top:12px;
                    color:#28323c;font-size:13px;line-height:1.6;">{message_html}</div>
      </td>
    </tr>
    <tr>
      <td style="padding:18px 30px 0;">
        <div style="background:#fdf9ef;border:1px solid #efe3c8;border-radius:6px;
                    padding:12px 16px;color:#8a6d2f;font-size:13px;">
          Reply to this customer: {reply_line}
        </div>
      </td>
    </tr>
    <tr>
      <td style="padding:24px 30px 26px;color:#69737d;font-size:12px;line-height:1.6;
                 border-top:1px solid #eef1f5;text-align:center;margin-top:18px;">
        This is an automated notification from the {ship_name} website.<br>
        <span style="font-size:11px;">Hitting “Reply” will respond directly to the customer.</span>
      </td>
    </tr>
  </table>
</div>
"""


def send_contact_notification(message):
    recipient = notify_recipient()
    if not recipient:
        return

    email = EmailMultiAlternatives(
        subject=(
            f"New {message.get_inquiry_type_display().lower()} — {message.name}"
        ),
        body=_plain_body(message),
        to=[recipient],
        # Let staff hit "Reply" and land in the customer's inbox.
        reply_to=[message.email] if message.email else None,
    )
    email.attach_alternative(_html_body(message), "text/html")
    email.send(fail_silently=True)
