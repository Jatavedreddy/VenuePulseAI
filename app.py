"""VenuePulseAI – Main Flask Application."""

import os
from datetime import datetime, timezone
import math
import markdown
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from flask_login import LoginManager, login_required, current_user, login_user, logout_user
from sqlalchemy import text

load_dotenv()

# ---------------------------------------------------------------------------
# App & Database Initialization
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-fallback-key")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///venue.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# db is defined in models.py; bind it to this app
from models import db, Event, Ticket, ConcessionSale, StaffShift, User, Booking, HelpdeskTicket  # noqa: E402
from ai_crew import run_event_health_crew, run_support_triage_crew  # noqa: E402
db.init_app(app)

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


# ---- Phase 3: GenAI Chatbot (placeholder) ---------------------------------

@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    """Context-aware AI concierge endpoint backed by Groq."""
    support_button_html = (
        "<br><br><a href='/support/submit' class='btn btn-sm btn-primary text-white' "
        "style='border-radius: 8px;'>Open Support Ticket</a>"
    )

    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()

    if not user_message:
        return jsonify({"error": "A non-empty 'message' field is required."}), 400

    # Deterministic support escalation to avoid model-side refusal behavior.
    normalized_message = user_message.lower()
    support_keywords = [
        "human support",
        "human agent",
        "support",
        "refund",
        "cancel booking",
        "payment failed",
        "charged twice",
        "escalate",
        "help desk",
        "helpdesk",
    ]
    if any(keyword in normalized_message for keyword in support_keywords):
        escalation_response = (
            "I'm sorry you're dealing with this. Please use the button below so our support team can help right away."
            f"{support_button_html}"
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

    if upcoming_events:
        event_context = "\n".join(
            [
                (
                    f"Event: {event.name}, "
                    f"Date: {event.date.strftime('%Y-%m-%d %H:%M')}, "
                    f"Price: ${event.base_ticket_price:.2f}, "
                    f"Type: {event.event_type}"
                )
                for event in upcoming_events
            ]
        )
    else:
        event_context = "No upcoming events found in the database."

    system_prompt = (
        "You are the OmniEvent AI Concierge. Be concise, friendly, and helpful. "
        "Here is the real-time event schedule context from the database:\n"
        f"{event_context}\n"
        "Use this data to answer user questions. Do not invent events. "
        "If a user asks for human support, you MUST provide this exact HTML button: "
        "<br><br><a href='/support/submit' class='btn btn-sm btn-primary text-white' style='border-radius: 8px;'>Open Support Ticket</a>. "
        "Do not refuse or redirect away from this button for support requests. "
        "Do NOT use any other URL like /support/escalate."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        from groq import Groq  # type: ignore[import-not-found]

        client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
        preferred_model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
        candidate_models = [preferred_model, "llama-3.3-70b-versatile"]

        # Keep order but avoid duplicate model attempts.
        models_to_try = list(dict.fromkeys(candidate_models))
        assistant_response = ""
        last_error = None

        for model_name in models_to_try:
            try:
                completion = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                )
                assistant_response = (completion.choices[0].message.content or "").strip()
                if assistant_response:
                    break
            except Exception as exc:
                last_error = exc
                error_text = str(exc).lower()
                if "decommissioned" in error_text or "model_decommissioned" in error_text:
                    continue
                raise

        if not assistant_response:
            raise RuntimeError(f"Groq request failed: {str(last_error)}")
    except ImportError:
        return jsonify({"error": "Groq SDK is not installed. Add 'groq' to requirements."}), 500
    except Exception as exc:
        return jsonify({"error": f"Groq request failed: {str(exc)}"}), 502

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
