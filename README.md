# VenuePulseAI

VenuePulseAI is a Flask-based event operations platform that combines:
- Patron event discovery and ticket booking
- Admin event operations and analytics
- AI concierge chat grounded in live event data and uploaded knowledge documents
- Predictive pricing and attendance forecasting for event planning
- CrewAI-powered operational reports for staffing and support triage

## Core Features

- User authentication and role-based access (`user` and `admin`)
- Event catalog with filters and pagination
- Booking flow and personal ticket history
- Helpdesk ticket creation and admin resolution
- Admin dashboard with revenue, staffing, and helpdesk metrics
- Knowledge base upload (`PDF`, `TXT`, `MD`) for chat context
- AI chat endpoint via Groq models (`/api/chat`)
- Forecast endpoint for demand/pricing (`/api/predict-price/<event_id>`)
- CSV analytics export for Power BI

## Tech Stack

- Backend: Flask, Flask-Login, SQLAlchemy
- Database: SQLite (local-first)
- AI/LLM: Groq SDK, langchain-groq, CrewAI
- ML: pandas, scikit-learn, joblib
- Frontend: Jinja templates, Bootstrap, custom CSS/JS
- Deployment: Gunicorn, Docker

## Project Layout

```text
VenuePulseAI/
|-- app.py                      # Main Flask application and route layer
|-- models.py                   # SQLAlchemy models
|-- venue_health_crew.py        # CrewAI orchestration logic
|-- crew_tools.py               # CrewAI tools for staffing and helpdesk updates
|-- train_pricing_model.py      # Trains demand/pricing model artifacts
|-- seed.py                     # Small sample dataset seed
|-- seed_db_advanced.py         # Large synthetic dataset seed
|-- export_analytics.py         # Exports Power BI CSV datasets
|-- templates/                  # Jinja templates
|-- static/                     # CSS, JS, images
|-- ml_models/                  # Trained model artifacts
|-- powerbi_exports/            # Generated dashboard CSV files
`-- instance/
    `-- knowledge_docs/         # Uploaded local knowledge files
```

## Prerequisites

- Python 3.13 recommended
- `pip` and `venv`

Why Python 3.13: CrewAI and related dependencies are known to install cleanly on 3.13 in this repository.

## Quick Start (Local)

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

3. (Optional) Install Faker if you plan to use advanced seeding:

```bash
pip install faker
```

4. Create a `.env` file (optional for local dev, recommended for AI features):

```bash
FLASK_ENV=development
FLASK_SECRET_KEY=your-local-secret
GROQ_API_KEY=your-groq-key
GROQ_MODEL=llama-3.1-8b-instant
CREW_GROQ_MODEL=llama-3.1-8b-instant
CREW_MAX_TOKENS=1200
```

5. Start the app:

```bash
python app.py
```

6. Open in browser:

```text
http://127.0.0.1:8080
```

## Demo Credentials

The app bootstraps default users if none exist:

- Admin
  - Email: `admin@venuepulse.com`
  - Password: `admin123`
- Patron
  - Email: `patron@example.com`
  - Password: `password`

If you run advanced seeding, it also creates:
- Email: `user@venuepulse.com`
- Password: `user123`

## Database and Seeding

By default, the app uses SQLite at `instance/venue.db`.

### Small sample seed

```bash
python seed.py
```

### Large synthetic seed

```bash
python seed_db_advanced.py
```

Note: both seed scripts reset schema/data (`drop_all` + `create_all`).

## ML Forecasting Workflow

Train/retrain the model used by prediction APIs:

```bash
python train_pricing_model.py
```

Expected artifacts:
- `ml_models/demand_pricing_multi_output_model.pkl`
- `ml_models/demand_pricing_metadata.json`
- `ml_models/event_type_encoder.pkl`

If model artifacts are missing, `/api/predict-price/<event_id>` returns an error until training is completed.

## Analytics Export Workflow

Generate CSVs for Power BI dashboards:

```bash
python export_analytics.py
```

Outputs:
- `powerbi_exports/dashboard_a_heatmap.csv`
- `powerbi_exports/dashboard_b_ops.csv`
- `powerbi_exports/dashboard_c_staffing.csv`

## Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `FLASK_SECRET_KEY` | Required in non-local envs | Flask session secret |
| `FLASK_ENV` / `ENV` | Optional | Environment detection (`development` enables local defaults) |
| `DATABASE_URL` | Optional | Override SQLite DB connection |
| `GROQ_API_KEY` | Required for AI chat/crew | Groq API authentication |
| `GROQ_MODEL` | Optional | Chat model name (default: `llama-3.1-8b-instant`) |
| `CREW_GROQ_MODEL` | Optional | CrewAI model name |
| `CREW_MAX_TOKENS` | Optional | Crew response token cap |
| `AZURE_STORAGE_CONNECTION_STRING` | Optional | Enables Azure Blob upload path for knowledge documents |

## Key Routes and APIs

### Web Routes

- `GET /` - Home page
- `GET /events` - Authenticated event catalog
- `GET /event/<event_id>` - Event detail page
- `POST /book/<event_id>` - Create booking and ticket
- `GET /my-tickets` - Patron ticket history
- `GET,POST /support/submit` - Submit support ticket
- `GET /admin` - Admin dashboard

### API Endpoints

- `POST /api/chat` - Login-protected AI concierge
- `GET /api/search-events?q=...` - Event search
- `GET /api/analytics/dashboard` - Admin analytics payload
- `GET /api/predict-price/<event_id>` - Admin forecasting payload
- `GET /health` - Health check

## Docker

Build and run:

```bash
docker build -t venuepulseai .
docker run --rm -p 8000:8000 --env-file .env venuepulseai
```

Container runtime serves with Gunicorn on port `8000`.

## Notes on Knowledge Documents

- Admins can upload `PDF`, `TXT`, `MD` from the dashboard.
- Local mode stores files under `instance/knowledge_docs`.
- If Azure Blob is configured, uploads are pushed to blob storage with local fallback.

## Troubleshooting

- `Groq request failed` or chat errors:
  - Check `GROQ_API_KEY` and selected model name.
- Forecast endpoint says model missing:
  - Run `python train_pricing_model.py`.
- `faker` import error when advanced seeding:
  - Run `pip install faker`.
- CrewAI install problems on newer Python versions:
  - Use Python 3.13 for this repository.

## Current Status

- No formal automated test suite is included yet.
- Main operational scripts are available for local demo and development workflows.
