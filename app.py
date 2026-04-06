"""VenuePulseAI – Main Flask Application."""

import os
import re
from datetime import datetime, timezone
import math
import time
import markdown
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from flask_login import LoginManager, login_required, current_user, login_user, logout_user
from sqlalchemy import text
from uuid import uuid4

load_dotenv()

# ---------------------------------------------------------------------------
# App & Database Initialization
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-fallback-key")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///venue.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# db is defined in models.py; bind it to this app
from models import db, Event, Ticket, ConcessionSale, StaffShift, User, Booking, HelpdeskTicket, KnowledgeDocument  # noqa: E402
from ai_crew import run_event_health_crew, run_support_triage_crew  # noqa: E402
db.init_app(app)

ALLOWED_KNOWLEDGE_EXTENSIONS = {"pdf", "txt", "md"}
KNOWLEDGE_UPLOAD_DIR = os.path.join(app.instance_path, "knowledge_docs")
os.makedirs(KNOWLEDGE_UPLOAD_DIR, exist_ok=True)
SUPPORT_BUTTON_HTML = (
    "<br><br><a href='/support/submit' class='btn btn-sm btn-primary text-white' "
    "style='border-radius: 8px;'>Open Support Ticket</a>"
)

SUPPORT_ESCALATION_PHRASES = [
    "human support",
    "human agent",
    "open support ticket",
    "raise a support ticket",
    "submit a support ticket",
    "talk to support",
    "contact support",
    "help desk",
    "helpdesk",
    "refund",
    "cancel booking",
    "payment failed",
    "charged twice",
    "escalate",
    "escalate this",
    "escalate to human",
    "speak to someone",
]

# Setup Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Monkeypatch User for basic Flask-Login compatibility (without importing UserMixin)
User.is_active = True
User.is_authenticated = True
User.is_anonymous = False
User.get_id = lambda self: str(self.id)


def ensure_schema_compatibility():
    """Lightweight runtime migration for legacy local SQLite databases."""
    event_columns_result = db.session.execute(text("PRAGMA table_info(events)"))
    event_columns = {row[1] for row in event_columns_result.fetchall()}

    if "total_budget" not in event_columns:
        db.session.execute(
            text("ALTER TABLE events ADD COLUMN total_budget FLOAT NOT NULL DEFAULT 0.0")
        )
        db.session.commit()


def is_allowed_knowledge_file(filename):
    return (
        bool(filename)
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_KNOWLEDGE_EXTENSIONS
    )


def extract_text_from_document(file_path):
    extension = os.path.splitext(file_path)[1].lower()

    if extension == ".pdf":
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(file_path)
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages).strip()

    with open(file_path, "r", encoding="utf-8", errors="ignore") as file_handle:
        return file_handle.read().strip()


def split_text_chunks(text, chunk_size=900, overlap=150):
    normalized = " ".join((text or "").split())
    if not normalized:
        return []

    chunks = []
    step = max(1, chunk_size - overlap)
    for start in range(0, len(normalized), step):
        chunk = normalized[start:start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def get_relevant_knowledge_snippets(query, top_k=4):
    documents = KnowledgeDocument.query.order_by(KnowledgeDocument.uploaded_at.desc()).all()
    if not documents:
        return [], []

    chunk_records = []
    for doc in documents:
        for chunk in split_text_chunks(doc.extracted_text):
            chunk_records.append({
                "text": chunk,
                "source": doc.original_filename,
            })

    if not chunk_records:
        return [], []

    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except Exception:
        # If sklearn is unavailable at runtime, return no snippets rather than crashing chat.
        return [], []

    corpus = [record["text"] for record in chunk_records]
    vectorizer = TfidfVectorizer(stop_words="english", max_features=6000)

    try:
        matrix = vectorizer.fit_transform(corpus + [query])
    except ValueError:
        return [], []

    query_vector = matrix[-1]
    doc_matrix = matrix[:-1]
    similarities = cosine_similarity(query_vector, doc_matrix).flatten()

    ranked_indices = similarities.argsort()[::-1]
    selected_snippets = []
    selected_sources = []

    for idx in ranked_indices:
        if len(selected_snippets) >= top_k:
            break
        score = float(similarities[idx])
        if score < 0.05:
            continue

        record = chunk_records[idx]
        selected_snippets.append(record["text"])
        if record["source"] not in selected_sources:
            selected_sources.append(record["source"])

    return selected_snippets, selected_sources


def is_human_support_request(message):
    normalized = " ".join((message or "").lower().split())
    if not normalized:
        return False
    return any(phrase in normalized for phrase in SUPPORT_ESCALATION_PHRASES)


def strip_support_button_markup(text):
    cleaned = text or ""

    # Remove the exact support button block (with optional <br> wrappers).
    cleaned = re.sub(
        r"(?:<br\\s*/?>\\s*){0,3}<a\\s+href=['\"]/support/submit['\"][^>]*>Open Support Ticket</a>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    # Remove common dangling phrase when the button was inserted mid-sentence.
    cleaned = re.sub(
        r"(?:,\\s*)?or you can\\s*for assistance\\.?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = re.sub(r"\\n{3,}", "\\n\\n", cleaned)
    return cleaned.strip()

# ---------------------------------------------------------------------------
# Auto-create tables before the first request
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()
    ensure_schema_compatibility()
    
    # Create test patron user if none exists
    if not User.query.filter_by(email="patron@example.com").first():
        test_user = User(name="Demo Patron", email="patron@example.com", role="user")
        test_user.password_hash = generate_password_hash("password")
        db.session.add(test_user)
        
    # Hardcode Admin User
    if not User.query.filter_by(role="admin").first():
        admin_user = User(name="Admin Director", email="admin@venuepulse.com", role="admin")
        admin_user.password_hash = generate_password_hash("admin123")
        db.session.add(admin_user)
        
    db.session.commit()


# ============================================================================
# ROUTES
# ============================================================================

# ---- Phase 1: Core Pages --------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    """Authenticates the user against database credentials."""
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        
        user = User.query.filter_by(email=email).first()
        if user and user.password_hash and check_password_hash(user.password_hash, password):
            login_user(user)
            flash(f"Welcome back, {user.name}!", "success")
            
            # Redirect admin to dashboard, regular users to the portal
            if user.role == 'admin':
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("events_catalog"))
            
        flash("Invalid email or password.", "danger")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    """Renders the registration template and creates a new user."""
    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        password = request.form.get("password")
        
        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
            return redirect(url_for('register'))
            
        new_user = User(name=name, email=email, role="user")
        new_user.password_hash = generate_password_hash(password)
        db.session.add(new_user)
        db.session.commit()
        
        login_user(new_user)
        flash("Account created! Welcome to VenuePulseAI.", "success")
        return redirect(url_for("index"))
    return render_template("register.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been successfully logged out.", "success")
    return redirect(url_for("index"))

@app.route("/")
def index():
    """Patron homepage – upcoming events."""
    query = Event.query.filter(Event.date >= datetime.now(timezone.utc))
    events = query.order_by(Event.date.asc()).limit(8).all()
    return render_template("index.html", events=events)

@app.route("/events")
@login_required
def events_catalog():
    """Logged in user events catalog."""
    from datetime import datetime, timedelta
    page = request.args.get('page', 1, type=int)
    per_page = 12
    
    query = Event.query.filter(Event.date >= datetime.now())
    
    # 1. Quick Pill Filter
    event_type = request.args.get("event_type")
    if event_type:
        query = query.filter(Event.event_type == event_type)
        
    # 2. Sidebar Categories Array
    categories = request.args.getlist("categories")
    if categories:
        query = query.filter(Event.event_type.in_(categories))
        
    # 3. Sidebar Price Range 
    price_filter = request.args.get("price")
    if price_filter:
        if price_filter == 'free':
            query = query.filter(Event.base_ticket_price == 0)
        elif price_filter == '0-5000':
            query = query.filter(Event.base_ticket_price <= (5000 / 83.0))
        elif price_filter == '5000-15000':
            query = query.filter(Event.base_ticket_price.between((5000 / 83.0), (15000 / 83.0)))
        elif price_filter == 'above-15000':
            query = query.filter(Event.base_ticket_price > (15000 / 83.0))
            
    # Execute query
    events = query.order_by(Event.date.asc()).all()
    
    # 4. Sidebar Date filtering in python natively
    date_filter = request.args.get("date")
    if date_filter:
        now = datetime.now()
        if date_filter == "today":
            end_today = now.replace(hour=23, minute=59, second=59, microsecond=999999)
            events = [e for e in events if e.date <= end_today]
        elif date_filter == "tomorrow":
            start_tmrw = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end_tmrw = start_tmrw.replace(hour=23, minute=59, second=59, microsecond=999999)
            events = [e for e in events if start_tmrw <= e.date <= end_tmrw]
        elif date_filter == "weekend":
            events = [e for e in events if e.date.weekday() in (5, 6)]

    total_events = len(events)
    total_pages = max(1, math.ceil(total_events / per_page))
    page = max(1, min(page, total_pages))

    start_index = (page - 1) * per_page
    end_index = start_index + per_page
    paged_events = events[start_index:end_index]

    pagination = {
        "page": page,
        "per_page": per_page,
        "total": total_events,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1,
    }

    return render_template(
        "events_catalog.html",
        events=paged_events,
        filters=request.args,
        pagination=pagination,
    )

@app.route("/my-tickets")
@login_required
def my_tickets():
    """User profile page showing their past and upcoming bookings."""
    if current_user.role == 'admin':
        flash("Admins do not have personal ticketing accounts.", "warning")
        return redirect(url_for('admin_dashboard'))

    from sqlalchemy.orm import joinedload
    from datetime import datetime
    
    page = request.args.get('page', 1, type=int)
    
    # Query all bookings bound strictly to the current session user, eagerly loading Event payload
    pagination = Booking.query.filter_by(user_id=current_user.id)\
        .options(joinedload(Booking.event))\
        .order_by(Booking.timestamp.desc())\
        .paginate(page=page, per_page=10, error_out=False)
        
    return render_template("my_tickets.html", 
                           bookings=pagination.items, 
                           pagination=pagination, 
                           now=datetime.now)

@app.route("/event/<int:event_id>")
@login_required
def event_details(event_id):
    """Detailed view of a specific event."""
    event = Event.query.get_or_404(event_id)
    return render_template("event_details.html", event=event)

@app.route("/event/<int:event_id>/calendar")
def event_calendar(event_id):
    """Generates an ICS calendar file for the event so users can natively add it to Apple Calendar / Outlook etc."""
    from datetime import timedelta
    event = Event.query.get_or_404(event_id)
    
    start_time = event.date
    end_time = event.date + timedelta(hours=2, minutes=30)
    
    dtstart = start_time.strftime('%Y%m%dT%H%M%S')
    dtend = end_time.strftime('%Y%m%dT%H%M%S')
    
    from flask import Response
    ics_content = f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//VenuePulseAI//EN\nBEGIN:VEVENT\n"
    ics_content += f"UID:event-{event.id}@venuepulseai.com\n"
    ics_content += f"DTSTAMP:{start_time.strftime('%Y%m%dT%H%M%S')}\n"
    ics_content += f"DTSTART:{dtstart}\n"
    ics_content += f"DTEND:{dtend}\n"
    ics_content += f"SUMMARY:{event.name}\n"
    ics_content += f"DESCRIPTION:Join us for {event.name} at VenuePulse.\n"
    ics_content += f"LOCATION:Main Stage, VenuePulse\n"
    ics_content += f"END:VEVENT\nEND:VCALENDAR"

    response = Response(ics_content, mimetype='text/calendar')
    response.headers['Content-Disposition'] = f'attachment; filename=venuepulse_event_{event.id}.ics'
    return response

@app.route("/book/<int:event_id>", methods=["GET", "POST"])
@login_required
def book_event(event_id):
    """Backend processing route triggered to simulate a transaction."""
    event = Event.query.get_or_404(event_id)
    
    booking = Booking(
        user_id=current_user.id,
        event_id=event.id,
        total_amount=event.base_ticket_price,
        payment_status="Simulated Success"
    )
    db.session.add(booking)
    db.session.commit()
    
    ticket = Ticket(
        event_id=event.id,
        booking_id=booking.id,
        current_price=event.base_ticket_price,
        is_sold=True,
        patron_name=current_user.name
    )
    db.session.add(ticket)
    db.session.commit()
    
    flash(f"Successfully booked a ticket for '{event.name}'!", "success")
    return redirect(url_for("events_catalog"))

@app.route("/support/submit", methods=["GET", "POST"])
@login_required
def support_submit():
    """Form to submit a HelpdeskTicket."""
    if request.method == "POST":
        subject = request.form.get("subject")
        description = request.form.get("description")
        
        new_ticket = HelpdeskTicket(
            user_id=current_user.id,
            subject=subject,
            description=description
        )
        db.session.add(new_ticket)
        db.session.commit()
        flash("Support ticket submitted to admins!", "success")
        return redirect(url_for("index"))
        
    return render_template("support.html")


@app.route("/admin")
@login_required
def admin_dashboard():
    """Admin dashboard – venue operations overview."""
    if current_user.role != 'admin':
        abort(403) # Strictly throw forbidden error if not admin
        
    total_events = Event.query.count()
    total_tickets_sold = Ticket.query.filter_by(is_sold=True).count()
    total_sales = (
        db.session.query(db.func.coalesce(db.func.sum(Booking.total_amount), 0))
        .scalar()
    ) or 0
    total_concession_revenue = (
        db.session.query(db.func.coalesce(db.func.sum(ConcessionSale.price), 0))
        .scalar()
    )
    recent_events = Event.query.order_by(Event.id.desc()).limit(5).all()
    all_events = Event.query.order_by(Event.date.desc()).all()
    knowledge_documents = KnowledgeDocument.query.order_by(KnowledgeDocument.uploaded_at.desc()).limit(10).all()
    upcoming_events = Event.query.filter(
        Event.date >= datetime.now(timezone.utc)
    ).order_by(Event.date.asc()).limit(5).all()

    # Fetch open helpdesk tickets (case-insensitive to support both 'open' and 'Open').
    open_tickets = HelpdeskTicket.query.filter(
        db.func.lower(HelpdeskTicket.status) == "open"
    ).all()
    open_tickets_count = HelpdeskTicket.query.filter(
        db.func.lower(HelpdeskTicket.status) == "open"
    ).count()

    return render_template(
        "admin.html",
        total_events=total_events,
        total_tickets_sold=total_tickets_sold,
        total_sales=total_sales,
        total_concession_revenue=total_concession_revenue,
        recent_events=recent_events,
        all_events=all_events,
        knowledge_documents=knowledge_documents,
        upcoming_events=upcoming_events,
        open_tickets=open_tickets,
        open_tickets_count=open_tickets_count,
    )


@app.route('/admin/resolve-ticket/<int:ticket_id>', methods=['POST'])
@login_required
def admin_resolve_ticket(ticket_id):
    """Admin-only endpoint to manually resolve a helpdesk ticket."""
    if current_user.role != 'admin':
        abort(403)

    ticket = HelpdeskTicket.query.get_or_404(ticket_id)
    ticket.status = "Closed"
    db.session.commit()

    flash(f"Ticket #{ticket.id} resolved successfully.", "success")
    return redirect(url_for('admin_dashboard'))


@app.route('/api/search-events', methods=['GET'])
def search_events():
    """Search events by partial name and return up to 10 lightweight records."""
    query_text = request.args.get('q', '').strip()

    if not query_text:
        return jsonify([])

    matches = (
        Event.query
        .filter(Event.name.ilike(f"%{query_text}%"))
        .order_by(Event.date.asc())
        .limit(10)
        .all()
    )

    payload = [
        {
            "id": event.id,
            "name": event.name,
            "date": event.date.isoformat() if event.date else None,
        }
        for event in matches
    ]
    return jsonify(payload)


@app.route('/api/analytics/dashboard', methods=['GET'])
@login_required
def analytics_dashboard():
    """Admin-only analytics payload used by dashboard widgets."""
    if (getattr(current_user, "role", "") or "").lower() != 'admin':
        return jsonify({"error": "Admin access required."}), 403

    booking_counts_sq = (
        db.session.query(
            Booking.event_id.label("event_id"),
            db.func.count(Booking.id).label("booking_count"),
        )
        .group_by(Booking.event_id)
        .subquery()
    )

    sold_tickets_sq = (
        db.session.query(
            Ticket.event_id.label("event_id"),
            db.func.count(Ticket.id).label("tickets_sold"),
        )
        .filter(Ticket.is_sold.is_(True))
        .group_by(Ticket.event_id)
        .subquery()
    )

    staff_counts_sq = (
        db.session.query(
            StaffShift.event_id.label("event_id"),
            db.func.count(StaffShift.id).label("staff_count"),
        )
        .group_by(StaffShift.event_id)
        .subquery()
    )

    event_metrics = (
        db.session.query(
            Event.name.label("event_name"),
            Event.base_ticket_price,
            Event.capacity,
            Event.total_budget,
            db.func.coalesce(booking_counts_sq.c.booking_count, 0).label("booking_count"),
            db.func.coalesce(sold_tickets_sq.c.tickets_sold, 0).label("tickets_sold"),
            db.func.coalesce(staff_counts_sq.c.staff_count, 0).label("staff_count"),
        )
        .outerjoin(booking_counts_sq, booking_counts_sq.c.event_id == Event.id)
        .outerjoin(sold_tickets_sq, sold_tickets_sq.c.event_id == Event.id)
        .outerjoin(staff_counts_sq, staff_counts_sq.c.event_id == Event.id)
        .order_by(Event.date.asc())
        .all()
    )

    revenue_demand = []
    operational_efficiency = []

    for metric in event_metrics:
        capacity = int(metric.capacity or 0)
        base_price = float(metric.base_ticket_price or 0)
        total_budget = float(metric.total_budget or 0)
        booking_count = int(metric.booking_count or 0)
        tickets_sold = int(metric.tickets_sold or 0)
        allocated_staff_count = int(metric.staff_count or 0)

        ticket_velocity = tickets_sold if tickets_sold > 0 else booking_count
        demand_ratio = (ticket_velocity / capacity) if capacity > 0 else 0.0

        if demand_ratio >= 0.75:
            demand_status = "Surge"
            multiplier = 1.20
        elif demand_ratio >= 0.35:
            demand_status = "Medium"
            multiplier = 1.05
        else:
            demand_status = "Cold"
            multiplier = 0.90

        ai_suggested_price = round(base_price * multiplier, 2) if base_price > 0 else 0.0

        revenue_demand.append(
            {
                "event_name": metric.event_name,
                "demand_status": demand_status,
                "ai_suggested_price": ai_suggested_price,
                "ticket_velocity": ticket_velocity,
            }
        )

        operational_efficiency.append(
            {
                "event_name": metric.event_name,
                "total_budget": total_budget,
                "staffing_cost": total_budget,
                "expected_capacity": capacity,
                "allocated_staff_count": allocated_staff_count,
            }
        )

    total_tickets = HelpdeskTicket.query.count()
    ai_resolved_simple = HelpdeskTicket.query.filter(
        db.func.lower(HelpdeskTicket.status).in_(
            ["closed", "closed_by_ai", "ai_closed", "resolved_by_ai"]
        )
    ).count()
    human_escalated_complex = HelpdeskTicket.query.filter(
        (db.func.lower(HelpdeskTicket.status).in_(
            ["open", "pending", "pending_human", "assigned_to_human", "escalated", "escalated_to_human"]
        ))
        | (db.func.lower(HelpdeskTicket.status).like("%human%"))
    ).count()

    return jsonify(
        {
            "revenue_demand": revenue_demand,
            "helpdesk_ops": {
                "total_tickets": int(total_tickets or 0),
                "ai_resolved_simple": int(ai_resolved_simple or 0),
                "human_escalated_complex": int(human_escalated_complex or 0),
            },
            "operational_efficiency": operational_efficiency,
        }
    )


@app.route('/admin/create-event', methods=['POST'])
@login_required
def admin_create_event():
    """Admin-only endpoint to create a new event from dashboard form payload."""
    if current_user.role != 'admin':
        abort(403)

    name = request.form.get('name', '').strip()
    date_raw = request.form.get('date', '').strip()
    event_type = request.form.get('event_type', '').strip()
    capacity_raw = request.form.get('capacity', '').strip()
    base_ticket_price_raw = request.form.get('base_ticket_price', '').strip()
    total_budget_raw = request.form.get('total_budget', '').strip()

    event_date = datetime.fromisoformat(date_raw)
    capacity = int(capacity_raw)
    base_ticket_price = float(base_ticket_price_raw)
    total_budget = float(total_budget_raw)

    new_event = Event(
        name=name,
        date=event_date,
        genre=event_type,
        event_type=event_type,
        capacity=capacity,
        base_ticket_price=base_ticket_price,
        total_budget=total_budget,
    )
    db.session.add(new_event)
    db.session.commit()

    flash('Event created successfully!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/edit-event/<int:event_id>', methods=['POST'])
@login_required
def admin_edit_event(event_id):
    """Admin-only endpoint to update an existing event."""
    if current_user.role != 'admin':
        abort(403)

    event = Event.query.get_or_404(event_id)

    event.name = request.form.get('name', event.name).strip()
    event.date = datetime.fromisoformat(request.form.get('date', event.date.isoformat()).strip())
    event.event_type = request.form.get('event_type', event.event_type).strip()
    event.genre = event.event_type
    event.capacity = int(request.form.get('capacity', event.capacity))
    event.base_ticket_price = float(request.form.get('base_ticket_price', event.base_ticket_price))
    event.total_budget = float(request.form.get('total_budget', event.total_budget))

    db.session.commit()
    flash('Event updated successfully!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete-event/<int:event_id>', methods=['POST'])
@login_required
def admin_delete_event(event_id):
    """Admin-only endpoint to delete an event."""
    if current_user.role != 'admin':
        abort(403)

    event = Event.query.get_or_404(event_id)
    db.session.delete(event)
    db.session.commit()

    flash('Event deleted successfully!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/upload-knowledge', methods=['POST'])
@login_required
def admin_upload_knowledge():
    """Admin-only endpoint for uploading PDF/text knowledge for user chat."""
    if current_user.role != 'admin':
        abort(403)

    uploaded_file = request.files.get('document')
    if uploaded_file is None or not uploaded_file.filename:
        flash('Please select a document to upload.', 'warning')
        return redirect(url_for('admin_dashboard'))

    if not is_allowed_knowledge_file(uploaded_file.filename):
        flash('Unsupported document type. Upload a PDF, TXT, or MD file.', 'danger')
        return redirect(url_for('admin_dashboard'))

    original_filename = uploaded_file.filename
    safe_filename = secure_filename(original_filename)
    extension = os.path.splitext(safe_filename)[1].lower()
    stored_filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid4().hex}{extension}"
    file_path = os.path.join(KNOWLEDGE_UPLOAD_DIR, stored_filename)

    uploaded_file.save(file_path)

    try:
        extracted_text = extract_text_from_document(file_path)
    except Exception as exc:
        if os.path.exists(file_path):
            os.remove(file_path)
        flash(f'Failed to read document: {exc}', 'danger')
        return redirect(url_for('admin_dashboard'))

    if not extracted_text.strip():
        if os.path.exists(file_path):
            os.remove(file_path)
        flash('Uploaded file does not contain readable text.', 'warning')
        return redirect(url_for('admin_dashboard'))

    knowledge_document = KnowledgeDocument(
        original_filename=original_filename,
        stored_filename=stored_filename,
        file_path=file_path,
        extracted_text=extracted_text,
        uploaded_by_user_id=current_user.id,
    )
    db.session.add(knowledge_document)
    db.session.commit()

    flash('Knowledge document uploaded successfully!', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete-knowledge/<int:document_id>', methods=['POST'])
@login_required
def admin_delete_knowledge(document_id):
    """Admin-only endpoint to delete an uploaded knowledge document."""
    if current_user.role != 'admin':
        abort(403)

    document = KnowledgeDocument.query.get_or_404(document_id)
    file_path = document.file_path
    file_delete_error = None

    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError as exc:
            file_delete_error = str(exc)

    db.session.delete(document)
    db.session.commit()

    if file_delete_error:
        flash(f'Knowledge record deleted, but file cleanup failed: {file_delete_error}', 'warning')
    else:
        flash('Knowledge document deleted successfully!', 'success')

    return redirect(url_for('admin_dashboard'))


# ---- Phase 3: GenAI Chatbot (placeholder) ---------------------------------

@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    """Context-aware AI concierge endpoint backed by Groq."""
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()

    if not user_message:
        return jsonify({"error": "A non-empty 'message' field is required."}), 400

    wants_human_support = is_human_support_request(user_message)

    # Deterministic support escalation to avoid model-side refusal behavior.
    if wants_human_support:
        escalation_response = (
            "I'm sorry you're dealing with this. Please use the button below so our support team can help right away."
            f"{SUPPORT_BUTTON_HTML}"
        )
        return jsonify({"response": escalation_response})

    now = datetime.now()
    upcoming_events = (
        Event.query
        .filter(Event.date >= now)
        .order_by(Event.date.asc())
        .limit(10)
        .all()
    )
    recently_added_events = Event.query.order_by(Event.id.desc()).limit(10).all()

    def format_events(events):
        return "\n".join(
            [
                (
                    f"Event: {event.name}, "
                    f"Date: {event.date.strftime('%Y-%m-%d %H:%M')}, "
                    f"Price: ${event.base_ticket_price:.2f}, "
                    f"Type: {event.event_type}"
                )
                for event in events
            ]
        )

    context_sections = []
    if upcoming_events:
        context_sections.append(
            "[Next 10 Upcoming Events]\n" + format_events(upcoming_events)
        )
    else:
        context_sections.append("[Next 10 Upcoming Events]\nNo upcoming events found in the database.")

    if recently_added_events:
        context_sections.append(
            "[Recently Added Events (Newest First)]\n" + format_events(recently_added_events)
        )

    knowledge_snippets, knowledge_sources = get_relevant_knowledge_snippets(user_message)
    if knowledge_snippets:
        formatted_snippets = "\n".join([f"- {snippet}" for snippet in knowledge_snippets])
        context_sections.append(
            "[Relevant Uploaded Knowledge Snippets]\n"
            + formatted_snippets
        )
    else:
        total_knowledge_docs = KnowledgeDocument.query.count()
        if total_knowledge_docs > 0:
            context_sections.append(
                "[Relevant Uploaded Knowledge Snippets]\nNo highly relevant snippets found for this question."
            )
        else:
            context_sections.append(
                "[Relevant Uploaded Knowledge Snippets]\nNo knowledge documents have been uploaded yet."
            )

    event_context = "\n\n".join(context_sections)

    system_prompt = (
        "You are the OmniEvent AI Concierge. Be concise, friendly, and helpful. "
        "Here is the real-time event schedule context from the database:\n"
        f"{event_context}\n"
        "Use this data to answer user questions, including newly created events. Do not invent events. "
        "If uploaded document snippets are provided, use them as factual ground truth and cite short source names when relevant. "
        f"Human support escalation intent for this user message: {'YES' if wants_human_support else 'NO'}. "
        "If intent is YES, provide this exact HTML button exactly once at the end: "
        f"{SUPPORT_BUTTON_HTML}. "
        "If intent is NO, do not mention support ticketing and do not include any support button. "
        "Do NOT use any other URL like /support/escalate."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        from groq import Groq  # type: ignore[import-not-found]

        client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        selected_model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
        assistant_response = ""
        last_error = None
        max_attempts_per_model = 3

        for attempt in range(max_attempts_per_model):
            try:
                completion = client.chat.completions.create(
                    model=selected_model,
                    messages=messages,
                )
                assistant_response = (completion.choices[0].message.content or "").strip()
                if assistant_response:
                    break
            except Exception as exc:
                last_error = exc
                error_text = str(exc).lower()
                is_over_capacity = (
                    "over capacity" in error_text
                    or "error code: 503" in error_text
                    or "internal_server_error" in error_text
                )
                is_rate_limited = (
                    "rate limit" in error_text
                    or "too many requests" in error_text
                    or "error code: 429" in error_text
                )

                if is_over_capacity or is_rate_limited:
                    # Retry selected model with exponential backoff.
                    if attempt < max_attempts_per_model - 1:
                        backoff_seconds = 0.6 * (2 ** attempt)
                        time.sleep(backoff_seconds)
                        continue
                    break

                # No fallback requested: surface non-capacity errors immediately.
                raise

        if not assistant_response:
            last_error_text = str(last_error).lower() if last_error else ""
            if "over capacity" in last_error_text or "error code: 503" in last_error_text:
                return jsonify(
                    {
                        "error": (
                            "Groq is temporarily over capacity for the selected model. "
                            "Please try again in a few seconds."
                        )
                    }
                ), 503
            if "rate limit" in last_error_text or "error code: 429" in last_error_text:
                return jsonify(
                    {
                        "error": (
                            "Rate limit reached for the selected model. "
                            "Please retry shortly."
                        )
                    }
                ), 429
            raise RuntimeError(f"Groq request failed: {str(last_error)}")
    except ImportError:
        return jsonify({"error": "Groq SDK is not installed. Add 'groq' to requirements."}), 500
    except Exception as exc:
        return jsonify({"error": f"Groq request failed: {str(exc)}"}), 502

    if wants_human_support:
        if SUPPORT_BUTTON_HTML not in assistant_response:
            assistant_response = assistant_response.rstrip() + SUPPORT_BUTTON_HTML
    else:
        assistant_response = strip_support_button_markup(assistant_response)

    if knowledge_sources:
        assistant_response += (
            "\n\nSources: " + ", ".join(knowledge_sources[:4])
        )

    return jsonify({"response": assistant_response})


# ---- Phase 4: CrewAI Workflow (placeholder) --------------------------------

@app.route("/admin/run-agents", methods=["POST"])
def run_agents():
    """Placeholder – will trigger CrewAI multi-agent workflow."""
    return jsonify({
        "status": "pending",
        "agents_triggered": [],
        "message": "Phase 4 – CrewAI agent workflow not yet integrated.",
    })


# ---- Phase 2: ML Inference API ---------------------------------------------

@app.route('/api/predict-price/<int:event_id>', methods=['GET'])
@login_required
def predict_price(event_id):
    """Comprehensive financial forecasting endpoint for admin event planning."""
    if current_user.role != 'admin':
        return jsonify({"error": "Admin privileges strictly required for predictive APIs."}), 403

    event = Event.query.get_or_404(event_id)

    # Fetch current operational values from bookings/tickets.
    tickets_sold = Ticket.query.filter_by(event_id=event_id, is_sold=True).count()
    bookings = Booking.query.filter_by(event_id=event_id).order_by(Booking.timestamp.asc()).all()
    booking_count = len(bookings)

    total_budget = float(getattr(event, "total_budget", 0.0) or 0.0)

    # Calculate time features in UTC.
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)

    if event.date.tzinfo is None:
        event_date_aware = event.date.replace(tzinfo=timezone.utc)
    else:
        event_date_aware = event.date

    days_left = max(0, (event_date_aware - now_utc).days)

    if bookings:
        first_booking_timestamp = bookings[0].timestamp
        if first_booking_timestamp.tzinfo is None:
            first_booking_timestamp = first_booking_timestamp.replace(tzinfo=timezone.utc)
        days_since_creation = max(1.0, (now_utc - first_booking_timestamp).total_seconds() / 86400.0)
    else:
        days_since_creation = 1.0

    current_sales_velocity = tickets_sold / days_since_creation

    # Load model artifacts lazily at request time.
    import os
    import json
    import pandas as pd
    import joblib

    model_path = os.path.join("ml_models", "demand_pricing_multi_output_model.pkl")
    metadata_path = os.path.join("ml_models", "demand_pricing_metadata.json")

    if not os.path.exists(model_path):
        return jsonify({"error": "Forecast model missing. Run train_pricing_model.py first."}), 404

    forecast_model = joblib.load(model_path)

    target_order = ["expected_total_attendance", "optimal_ticket_price"]
    if os.path.exists(metadata_path):
        with open(metadata_path, "r", encoding="utf-8") as metadata_file:
            metadata = json.load(metadata_file)
        target_order = metadata.get("targets", target_order)

    # Build model input using the same features used during training.
    input_df = pd.DataFrame([{
        "event_type": event.event_type,
        "days_until_event": days_left,
        "current_tickets_sold": tickets_sold,
        "current_sales_velocity": current_sales_velocity,
        "capacity": event.capacity,
        "base_ticket_price": event.base_ticket_price,
        "total_budget": total_budget,
    }])

    prediction = forecast_model.predict(input_df)[0]
    prediction_map = dict(zip(target_order, prediction))

    expected_attendance_count = max(0.0, float(prediction_map.get("expected_total_attendance", tickets_sold)))
    predicted_optimal_price = max(0.01, float(prediction_map.get("optimal_ticket_price", event.base_ticket_price)))

    expected_capacity_percentage = (
        (expected_attendance_count / event.capacity) * 100.0 if event.capacity > 0 else 0.0
    )
    projected_ticket_revenue = expected_attendance_count * predicted_optimal_price

    secondary_revenue_per_head = {
        "Concert": 25.0,
        "Conference": 15.0,
        "Sports": 30.0,
    }.get(event.event_type, 10.0)
    estimated_secondary_revenue = expected_attendance_count * secondary_revenue_per_head

    total_projected_revenue = projected_ticket_revenue + estimated_secondary_revenue
    net_profit = total_projected_revenue - total_budget
    break_even_tickets = (
        math.floor(total_budget / predicted_optimal_price)
        if predicted_optimal_price > 0
        else 0
    )

    if current_sales_velocity >= 20 and days_left > 14:
        demand_status = "Surge Potential"
    elif current_sales_velocity < 5 and days_left < 7:
        demand_status = "Cold"
    else:
        demand_status = "On Track"

    return jsonify({
        "event_name": event.name,
        "event_id": event.id,
        "event_type": event.event_type,
        "days_left": days_left,
        "booking_count": booking_count,
        "tickets_sold": tickets_sold,
        "current_sales_velocity": round(current_sales_velocity, 4),
        "base_price": round(event.base_ticket_price, 2),
        "total_budget": round(total_budget, 2),
        "predicted_optimal_price": round(predicted_optimal_price, 2),
        "expected_attendance_count": round(expected_attendance_count, 2),
        "expected_capacity_percentage": round(expected_capacity_percentage, 2),
        "projected_ticket_revenue": round(projected_ticket_revenue, 2),
        "estimated_secondary_revenue": round(estimated_secondary_revenue, 2),
        "total_projected_revenue": round(total_projected_revenue, 2),
        "net_profit": round(net_profit, 2),
        "break_even_tickets": break_even_tickets,
        "demand_status": demand_status,
        # Backward-compatible aliases for existing UI consumers.
        "expected_attendance_percentage": round(expected_capacity_percentage, 2),
        "revenue_delta": round(predicted_optimal_price - event.base_ticket_price, 2),
    })


@app.route('/admin/run-health-crew/<int:event_id>', methods=['POST'])
@login_required
def run_health_crew(event_id):
    """Admin-only endpoint to run event-health crew and return HTML report."""
    if current_user.role != 'admin':
        abort(403)

    event = Event.query.get_or_404(event_id)

    tickets_sold = Ticket.query.filter_by(event_id=event_id, is_sold=True).count()
    bookings = Booking.query.filter_by(event_id=event_id).order_by(Booking.timestamp.asc()).all()
    total_budget = float(getattr(event, "total_budget", 0.0) or 0.0)

    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)

    if event.date.tzinfo is None:
        event_date_aware = event.date.replace(tzinfo=timezone.utc)
    else:
        event_date_aware = event.date

    days_left = max(0, (event_date_aware - now_utc).days)

    if bookings:
        first_booking_timestamp = bookings[0].timestamp
        if first_booking_timestamp.tzinfo is None:
            first_booking_timestamp = first_booking_timestamp.replace(tzinfo=timezone.utc)
        days_since_creation = max(1.0, (now_utc - first_booking_timestamp).total_seconds() / 86400.0)
    else:
        days_since_creation = 1.0

    current_sales_velocity = tickets_sold / days_since_creation

    import json
    import pandas as pd
    import joblib

    model_path = os.path.join("ml_models", "demand_pricing_multi_output_model.pkl")
    metadata_path = os.path.join("ml_models", "demand_pricing_metadata.json")

    if not os.path.exists(model_path):
        return jsonify({"error": "Forecast model missing. Run train_pricing_model.py first."}), 404

    forecast_model = joblib.load(model_path)

    target_order = ["expected_total_attendance", "optimal_ticket_price"]
    if os.path.exists(metadata_path):
        with open(metadata_path, "r", encoding="utf-8") as metadata_file:
            metadata = json.load(metadata_file)
        target_order = metadata.get("targets", target_order)

    input_df = pd.DataFrame([{
        "event_type": event.event_type,
        "days_until_event": days_left,
        "current_tickets_sold": tickets_sold,
        "current_sales_velocity": current_sales_velocity,
        "capacity": event.capacity,
        "base_ticket_price": event.base_ticket_price,
        "total_budget": total_budget,
    }])

    prediction = forecast_model.predict(input_df)[0]
    prediction_map = dict(zip(target_order, prediction))

    expected_attendance_count = max(0.0, float(prediction_map.get("expected_total_attendance", tickets_sold)))
    predicted_optimal_price = max(0.01, float(prediction_map.get("optimal_ticket_price", event.base_ticket_price)))
    expected_capacity_percentage = (
        (expected_attendance_count / event.capacity) * 100.0 if event.capacity > 0 else 0.0
    )
    projected_ticket_revenue = expected_attendance_count * predicted_optimal_price
    break_even_tickets = (
        math.floor(total_budget / predicted_optimal_price)
        if predicted_optimal_price > 0
        else 0
    )

    financial_forecast = {
        "expected_capacity_percentage": round(expected_capacity_percentage, 2),
        "projected_profit": round(projected_ticket_revenue - total_budget, 2),
        "break_even_tickets": break_even_tickets,
        "predicted_optimal_price": round(predicted_optimal_price, 2),
        "expected_attendance_count": round(expected_attendance_count, 2),
        "current_sales_velocity": round(current_sales_velocity, 4),
        "days_left": days_left,
        "total_budget": round(total_budget, 2),
    }

    event_details = {
        "id": event.id,
        "name": event.name,
        "date": event.date.isoformat() if event.date else None,
        "event_type": event.event_type,
        "capacity": event.capacity,
        "base_ticket_price": event.base_ticket_price,
        "total_budget": total_budget,
        "tickets_sold": tickets_sold,
    }

    try:
        markdown_report = run_event_health_crew(
            event_data=event_details,
            financial_data=financial_forecast,
        )
        html_report = markdown.markdown(markdown_report, extensions=["extra", "sane_lists"])
        return jsonify({"report": html_report})
    except Exception as exc:
        error_text = str(exc)
        if "rate limit" in error_text.lower() or "429" in error_text:
            return jsonify({"error": f"Health crew throttled by Groq. Please retry in a few seconds. Details: {error_text}"}), 429
        return jsonify({"error": f"Health crew failed: {error_text}"}), 500


@app.route('/admin/run-support-crew', methods=['POST'])
@login_required
def run_support_crew():
    """Admin-only endpoint to run support-triage crew and return HTML report."""
    if current_user.role != 'admin':
        abort(403)

    open_tickets = (
        HelpdeskTicket.query
        .filter(db.func.lower(HelpdeskTicket.status) == "open")
        .order_by(HelpdeskTicket.created_at.asc())
        .all()
    )
    open_tickets_payload = [
        {
            "id": ticket.id,
            "user_id": ticket.user_id,
            "subject": ticket.subject,
            "description": ticket.description,
            "status": ticket.status,
            "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        }
        for ticket in open_tickets
    ]

    try:
        markdown_report = run_support_triage_crew(open_tickets=open_tickets_payload)
        html_report = markdown.markdown(markdown_report, extensions=["extra", "sane_lists"])
        return jsonify({"report": html_report})
    except Exception as exc:
        error_text = str(exc)
        if "rate limit" in error_text.lower() or "429" in error_text:
            return jsonify({"error": f"Support crew throttled by Groq. Please retry in a few seconds. Details: {error_text}"}), 429
        return jsonify({"error": f"Support crew failed: {error_text}"}), 500

# ---- Utility ---------------------------------------------------------------

@app.route("/health")
def health():
    """Quick health-check endpoint."""
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, port=8080)
