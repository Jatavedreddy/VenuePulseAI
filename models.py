"""Database models for VenuePulseAI – Live Event & Venue Management."""

from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------
class User(db.Model):
    """A user (patron or admin) of the platform."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True) # Set via registration
    role = db.Column(db.String(50), default="user", nullable=False) # e.g. 'user', 'admin'

    bookings = db.relationship("Booking", backref="user", lazy=True, cascade="all, delete-orphan")
    helpdesk_tickets = db.relationship("HelpdeskTicket", backref="user", lazy=True, cascade="all, delete-orphan")

    def __init__(self, name, email, role="user"):
        self.name = name
        self.email = email
        self.role = role

    def __repr__(self):
        return f"<User id={self.id} name='{self.name}'>"


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------
class Event(db.Model):
    """A live event hosted at the venue."""

    __tablename__ = "events"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    date = db.Column(db.DateTime, nullable=False)
    genre = db.Column(db.String(100), nullable=False)
    event_type = db.Column(db.String(100), nullable=False) # e.g. concert, conference, sports
    capacity = db.Column(db.Integer, nullable=False)
    base_ticket_price = db.Column(db.Float, nullable=False)

    # Relationships
    tickets = db.relationship("Ticket", backref="event", lazy=True, cascade="all, delete-orphan")
    concession_sales = db.relationship("ConcessionSale", backref="event", lazy=True, cascade="all, delete-orphan")
    staff_shifts = db.relationship("StaffShift", backref="event", lazy=True, cascade="all, delete-orphan")
    bookings = db.relationship("Booking", backref="event", lazy=True, cascade="all, delete-orphan")

    def __init__(self, name, date, genre, event_type, capacity, base_ticket_price):
        self.name = name
        self.date = date
        self.genre = genre
        self.event_type = event_type
        self.capacity = capacity
        self.base_ticket_price = base_ticket_price

    def __repr__(self):
        return (
            f"<Event id={self.id} name='{self.name}' "
            f"type='{self.event_type}' date={self.date:%Y-%m-%d} "
            f"capacity={self.capacity} base_price={self.base_ticket_price}>"
        )


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------
class Booking(db.Model):
    """Transaction record between Users and Events."""

    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    payment_status = db.Column(db.String(50), default="Pending", nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    tickets = db.relationship("Ticket", backref="booking", lazy=True)

    def __init__(self, user_id, event_id, total_amount, payment_status="Pending", timestamp=None):
        self.user_id = user_id
        self.event_id = event_id
        self.total_amount = total_amount
        self.payment_status = payment_status
        if timestamp is not None:
             self.timestamp = timestamp

    def __repr__(self):
        return f"<Booking id={self.id} user={self.user_id} event={self.event_id} status='{self.payment_status}'>"


# ---------------------------------------------------------------------------
# Ticket
# ---------------------------------------------------------------------------
class Ticket(db.Model):
    """A ticket issued for a specific event."""

    __tablename__ = "tickets"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=True)
    current_price = db.Column(db.Float, nullable=False)
    is_sold = db.Column(db.Boolean, default=False, nullable=False)
    patron_name = db.Column(db.String(150), nullable=True)

    def __init__(self, event_id, current_price, booking_id=None, is_sold=False, patron_name=None):
        self.event_id = event_id
        self.booking_id = booking_id
        self.current_price = current_price
        self.is_sold = is_sold
        self.patron_name = patron_name

    def __repr__(self):
        status = "SOLD" if self.is_sold else "AVAILABLE"
        return (
            f"<Ticket id={self.id} booking={self.booking_id} event_id={self.event_id} "
            f"price={self.current_price} status={status}>"
        )


# ---------------------------------------------------------------------------
# HelpdeskTicket
# ---------------------------------------------------------------------------
class HelpdeskTicket(db.Model):
    """Support issue raised by a User."""

    __tablename__ = "helpdesk_tickets"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), default="open", nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    def __init__(self, user_id, subject, description, status="open", created_at=None):
        self.user_id = user_id
        self.subject = subject
        self.description = description
        self.status = status
        if created_at is not None:
             self.created_at = created_at

    def __repr__(self):
        return f"<HelpdeskTicket id={self.id} subject='{self.subject}' status='{self.status}'>"


# ---------------------------------------------------------------------------
# ConcessionSale
# ---------------------------------------------------------------------------
class ConcessionSale(db.Model):
    """A food / beverage / merchandise sale at an event."""

    __tablename__ = "concession_sales"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)
    item_name = db.Column(db.String(150), nullable=False)
    price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    def __init__(self, event_id, item_name, price, timestamp=None):
        self.event_id = event_id
        self.item_name = item_name
        self.price = price
        if timestamp is not None:
            self.timestamp = timestamp

    def __repr__(self):
        return (
            f"<ConcessionSale id={self.id} event_id={self.event_id} "
            f"item='{self.item_name}' price={self.price} "
            f"at={self.timestamp}>"
        )


# ---------------------------------------------------------------------------
# StaffShift
# ---------------------------------------------------------------------------
class StaffShift(db.Model):
    """A staff member's shift assignment for an event."""

    __tablename__ = "staff_shifts"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)
    role = db.Column(db.String(100), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)

    def __init__(self, event_id, role, start_time, end_time):
        self.event_id = event_id
        self.role = role
        self.start_time = start_time
        self.end_time = end_time

    def __repr__(self):
        return (
            f"<StaffShift id={self.id} event_id={self.event_id} "
            f"role='{self.role}' "
            f"from={self.start_time:%H:%M} to={self.end_time:%H:%M}>"
        )
