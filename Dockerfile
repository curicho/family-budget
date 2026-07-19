FROM python:3.12-slim

# pg_dump for backups, age for encryption, curl for probes
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client age curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install --no-cache-dir .

# migrations: base schema is 001
RUN mkdir -p /app/migrations
COPY db/schema.sql /app/migrations/001_base_schema.sql

RUN useradd -r -u 10001 appuser
USER appuser

ENTRYPOINT []
CMD ["app", "serve"]
