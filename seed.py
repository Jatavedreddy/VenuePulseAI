"""Script to populate the database with sample events, tickets, and sales."""

from datetime import datetime, timedelta, timezone
import random
from app import app, db
from models import Event, Ticket, ConcessionSale, StaffShift, User, Booking, HelpdeskTicket

def seed_data():
    with app.app_context():
        print("Clearing existing data...")
        db.drop_all()
        db.create_all()

        print("Adding sample events...")
        now = datetime.now(timezone.utc)
        
        events_data = [
            ("Neon Nights Cyberpunk Rave", now + timedelta(days=2), "Electronic", "Concert", 800, 45.0),
            ("Symphony Under the Stars", now + timedelta(days=5), "Classical", "Concert", 1200, 75.0),
            ("Indie Rock Showcase", now + timedelta(days=10), "Rock", "Concert", 400, 30.0),
            ("Jazz & Blues Revival", now + timedelta(days=14), "Jazz", "Concert", 250, 55.0),
            ("Global EDM Festival pre-party", now + timedelta(days=20), "Electronic", "Concert", 1500, 65.0),
        ]
        
        events = []
        for name, date, genre, event_type, capacity, price in events_data:
            event = Event(name=name, date=date, genre=genre, event_type=event_type, capacity=capacity, base_ticket_price=price)
            db.session.add(event)
            events.append(event)
            
        db.session.commit()
        
        print("Adding sample tickets and sales...")
        for event in events:
            # Generate random sold tickets (between 30% and 80% capacity)
            sold_count = int(event.capacity * random.uniform(0.3, 0.8))
            for _ in range(sold_count):
                # Fluctuate price slightly
                price = event.base_ticket_price * random.uniform(0.9, 1.3)
                ticket = Ticket(event_id=event.id, current_price=price, is_sold=True, patron_name=f"Patron {random.randint(1000, 9999)}")
                db.session.add(ticket)
                
            # Gen concession sales
            num_sales = random.randint(50, 300)
            items = [("Beer", 8.0), ("Cocktail", 15.0), ("Water", 4.0), ("Merch T-Shirt", 35.0), ("Burger", 12.0)]
            for _ in range(num_sales):
                item_name, price = random.choice(items)
                sale_time = event.date - timedelta(hours=random.uniform(0, 4))
                sale = ConcessionSale(event_id=event.id, item_name=item_name, price=price, timestamp=sale_time)
                db.session.add(sale)
                
            # Staff Shifts
            roles = ["Security", "Bartender", "Usher", "Manager", "Sound Tech"]
            for role in roles:
                count = random.randint(1, 5)
                for _ in range(count):
                    shift_start = event.date - timedelta(hours=2)
                    shift_end = event.date + timedelta(hours=5)
                    shift = StaffShift(event_id=event.id, role=role, start_time=shift_start, end_time=shift_end)
                    db.session.add(shift)

        db.session.commit()
        print("Database seeding completed successfully!")

if __name__ == "__main__":
    seed_data()
