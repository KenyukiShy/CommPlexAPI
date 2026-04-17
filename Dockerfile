# CommPlexAPI/Dockerfile
# Domain: The Mouth -- FastAPI Gateway container
#
# Build:  docker build -t commplex-api .
# Run:    docker run -p 8080:8080 --env-file ../.env commplex-api
# Compose: docker-compose up (preferred)

FROM python:3.11-slim

# ── Labels ────────────────────────────────────────────────
LABEL maintainer="Kenyon Jones <kjonesmle@gmail.com>"
LABEL project="Arc Badlands CommPlex"
LABEL domain="CommPlexAPI"
LABEL role="The Mouth"

# ── System dependencies ───────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────
WORKDIR /app

# ── Python dependencies (cached layer) ───────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy CommPlexSpec contracts (The Law) ────────────────
# The Spec is a sibling directory -- mount or COPY at build time
COPY ../CommPlexSpec ./CommPlexSpec

# ── Copy CommPlexCore logic (The Brain) ──────────────────
COPY ../CommPlexCore ./CommPlexCore

# ── Copy CommPlexAPI source ───────────────────────────────
COPY . .

# ── Ensure data directory exists for SQLite ──────────────
RUN mkdir -p /app/data

# ── Non-root user for security ────────────────────────────
RUN useradd -m -u 1000 commplex && chown -R commplex:commplex /app
USER commplex

# ── Environment defaults ──────────────────────────────────
ENV PORT=8080
ENV DATABASE_URL=sqlite:////app/data/commplex_leads.db
ENV VERTEX_STATUS=STUB
ENV LOG_LEVEL=INFO
ENV DRY_RUN=true

# ── Health check ─────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# ── Expose port ───────────────────────────────────────────
EXPOSE ${PORT}

# ── Entrypoint ────────────────────────────────────────────
CMD ["sh", "-c", "uvicorn server.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-level info"]
