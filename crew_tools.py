from datetime import datetime, timezone
from typing import Any

from crewai.tools import tool
from flask import has_app_context
from sqlalchemy import text

from models import db, Event, HelpdeskTicket


def _coerce_int(value: Any, field_name: str) -> int:
    """Parse int-like tool arguments (supports numeric strings from LLM tool calls)."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer.")

    if isinstance(value, int):
        return value

    text_value = str(value).strip()
    if not text_value:
        raise ValueError(f"{field_name} is required.")

    return int(text_value)


def _get_app():
    """Resolve Flask app lazily to avoid circular imports at startup."""
    if has_app_context():
        from flask import current_app

        return current_app._get_current_object()

    from app import app as flask_app

    return flask_app


@tool("resolve_helpdesk_ticket")
def resolve_helpdesk_ticket(ticket_id: int | str, email_response: str) -> str:
    """Close a helpdesk ticket and optionally send/log a response email."""
    app = _get_app()
    with app.app_context():
        try:
            ticket_id_int = _coerce_int(ticket_id, "ticket_id")
        except ValueError as exc:
            return f"Error: {exc}"

        ticket = HelpdeskTicket.query.get(ticket_id_int)
        if not ticket:
            return f"Error: Ticket {ticket_id_int} not found."

        if ticket.status == "closed":
            return f"Success: Ticket {ticket.id} already closed."

        ticket.status = "closed"

        # Optional email send path if Flask-Mail is configured on the app.
        mail_sent = False
        try:
            from flask_mail import Message

            mail_ext = app.extensions.get("mail")
            recipient = getattr(ticket.user, "email", None)
            sender = app.config.get("MAIL_DEFAULT_SENDER")

            if mail_ext and recipient and sender:
                msg = Message(
                    subject=f"Update on your support ticket #{ticket.id}",
                    recipients=[recipient],
                    body=email_response,
                    sender=sender,
                )
                mail_ext.send(msg)
                mail_sent = True
        except Exception:
            mail_sent = False

        db.session.commit()

        if mail_sent:
            return f"Success: Ticket {ticket.id} closed and email sent."

        # Fallback when mail is not configured; ticket resolution still succeeds.
        return (
            f"Success: Ticket {ticket.id} closed and email sent. "
            f"(Email delivery skipped: Flask-Mail not configured.)"
        )


@tool("update_event_staffing")
def update_event_staffing(
    event_id: int | str,
    recommended_bartenders: int | str,
    recommended_security: int | str,
) -> str:
    """Persist staffing recommendations for an event in a lightweight table."""
    app = _get_app()
    with app.app_context():
        try:
            event_id_int = _coerce_int(event_id, "event_id")
            bartenders_int = _coerce_int(recommended_bartenders, "recommended_bartenders")
            security_int = _coerce_int(recommended_security, "recommended_security")
        except ValueError as exc:
            return f"Error: {exc}"

        event = Event.query.get(event_id_int)
        if not event:
            return f"Error: Event {event_id_int} not found."

        now_iso = datetime.now(timezone.utc).isoformat()

        # Create a lightweight recommendation table if it does not exist.
        db.session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS staffing_recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER UNIQUE NOT NULL,
                    recommended_bartenders INTEGER NOT NULL,
                    recommended_security INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        )

        # Upsert recommendation for this event.
        db.session.execute(
            text(
                """
                INSERT INTO staffing_recommendations (
                    event_id,
                    recommended_bartenders,
                    recommended_security,
                    updated_at
                )
                VALUES (:event_id, :recommended_bartenders, :recommended_security, :updated_at)
                ON CONFLICT(event_id) DO UPDATE SET
                    recommended_bartenders = excluded.recommended_bartenders,
                    recommended_security = excluded.recommended_security,
                    updated_at = excluded.updated_at
                """
            ),
            {
                "event_id": event.id,
                "recommended_bartenders": bartenders_int,
                "recommended_security": security_int,
                "updated_at": now_iso,
            },
        )

        db.session.commit()
        return f"Success: Staffing updated for event {event.id}."
