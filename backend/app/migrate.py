"""Run Alembic migrations programmatically (used on API/worker start-up)."""

from __future__ import annotations

import logging
import time

from alembic.config import Config

from alembic import command
from app.config import BACKEND_DIR

log = logging.getLogger(__name__)

DB_PASSWORD_HELP = (
    "De database weigert het wachtwoord. De database is aangemaakt met een ander POSTGRES_PASSWORD dan er nu "
    "in .env staat (bijvoorbeeld omdat je de app vanuit een andere map start, of .env opnieuw is aangemaakt). "
    "Start de app vanuit je oorspronkelijke map (waar je .env en de map data/ staan) met: docker compose up -d "
    "--build - de database neemt het wachtwoord uit .env dan automatisch over."
)


class DatabaseConfigError(RuntimeError):
    pass


def wait_for_database(timeout: float = 60.0) -> None:
    """Connect before migrating: wait while the database starts, and explain a wrong password plainly."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.db import get_engine

    deadline = time.monotonic() + timeout
    while True:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as e:
            if "password authentication failed" in str(e).lower():
                log.error(DB_PASSWORD_HELP)
                raise DatabaseConfigError(DB_PASSWORD_HELP) from e
            if time.monotonic() > deadline:
                log.error("De database is niet bereikbaar: %s", e)
                raise
            log.info("Wachten op de database...")
            time.sleep(2)


def upgrade_db() -> None:
    wait_for_database()
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    log.info("Database schema is up to date")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    upgrade_db()
