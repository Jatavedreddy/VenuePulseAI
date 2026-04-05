"""VenuePulseAI – Main Flask Application."""

import os
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from flask_login import LoginManager, login_required, current_user, login_user, logout_user

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

# ---------------------------------------------------------------------------
# Auto-create tables before the first request
# ---------------------------------------------------------------------------
with app.app_context():
    db.create_all()
    
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
    event_type = request.args.get("event_type")
    
    query = Event.query.filter(Event.date >= datetime.now(timezone.utc))
    if event_type:
        query = query.filter(Event.event_type == event_type)
        
    events = query.order_by(Event.date.asc()).all()
    return render_template("index.html", events=events)

@app.route("/events")
@login_required
def events_catalog():
    """Logged in user events catalog."""
    from datetime import datetime, timedelta
    
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
        
    return render_template("events_catalog.html", events=events, filters=request.args)

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
    total_concession_revenue = (
        db.session.query(db.func.coalesce(db.func.sum(ConcessionSale.price), 0))
        .scalar()
    )
    upcoming_events = Event.query.filter(
        Event.date >= datetime.now(timezone.utc)
    ).order_by(Event.date.asc()).limit(5).all()

    # Fetch open helpdesk tickets
    open_tickets = HelpdeskTicket.query.filter_by(status="open").all()

    return render_template(
        "admin.html",
        total_events=total_events,
        total_tickets_sold=total_tickets_sold,
        total_concession_revenue=total_concession_revenue,
        upcoming_events=upcoming_events,
        open_tickets=open_tickets
    )


# ---- Phase 2: Dynamic Pricing ML (placeholder) ----------------------------

@app.route("/api/predict-price/<int:event_id>", methods=["GET"])
def predict_price(event_id):
    """Placeholder – will return ML-predicted ticket price."""
    event = Event.query.get_or_404(event_id)
    return jsonify({
        "event_id": event.id,
        "event_name": event.name,
        "base_price": event.base_ticket_price,
        "predicted_price": event.base_ticket_price,  # stub
        "model_version": None,
        "message": "Phase 2 – ML pricing model not yet integrated.",
    })


# ---- Phase 3: GenAI Chatbot (placeholder) ---------------------------------

@app.route("/api/chat", methods=["POST"])
def chat():
    """Placeholder – will handle GenAI chatbot queries."""
    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "")
    return jsonify({
        "reply": (
            "I'm the VenuePulseAI assistant. "
            "This feature is coming in Phase 3!"
        ),
        "user_message": user_message,
        "model_version": None,
        "message": "Phase 3 – GenAI chatbot not yet integrated.",
    })


# ---- Phase 4: CrewAI Workflow (placeholder) --------------------------------

@app.route("/admin/run-agents", methods=["POST"])
def run_agents():
    """Placeholder – will trigger CrewAI multi-agent workflow."""
    return jsonify({
        "status": "pending",
        "agents_triggered": [],
        "message": "Phase 4 – CrewAI agent workflow not yet integrated.",
    })


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
