FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Use a non-root user at runtime.
RUN useradd --create-home --uid 10001 appuser

# Install dependencies first for better build caching.
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 8000

CMD ["gunicorn", "--workers", "4", "--timeout", "600", "--bind", "0.0.0.0:8000", "app:app"]
