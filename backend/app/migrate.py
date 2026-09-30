"""Run Alembic migrations programmatically (used on API/worker start-up)."""

from __future__ import annotations

import logging

from alembic.config import Config

from alembic import command
from app.config import BACKEND_DIR

log = logging.getLogger(__name__)


def upgrade_db() -> None:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    log.info("Database schema is up to date")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    upgrade_db()
