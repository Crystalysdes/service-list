FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

# pg_dump of the same major version as the database (16) for the second copy inside backups
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y curl gnupg \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/bot
COPY pyproject.toml ./
COPY app ./app
RUN pip install .
COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 1000 bot && mkdir -p /data && chown bot:bot /data
USER bot
VOLUME ["/data"]

# applies migrations, then starts long polling
CMD ["python", "-m", "app"]
