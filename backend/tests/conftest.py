import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def lab(tmp_path, monkeypatch):
    """Fresh isolated lab database in stub provider mode."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("PROVIDER_MODE", "stub")
    monkeypatch.setenv("RESEARCH_LAB_MODE", "true")
    monkeypatch.setenv("ALLOW_PRODUCTION_WRITES", "false")
    import config
    import db
    config.reset_config()
    db.reset_engine()
    db.init_db()
    yield db
    db.reset_engine()
    config.reset_config()


@pytest.fixture()
def seeded(lab):
    from seeds import seed
    seed()
    return lab


@pytest.fixture()
def project_id(seeded):
    from sqlalchemy import select
    from db import session_scope
    from models import ResearchProject
    with session_scope() as session:
        return session.execute(select(ResearchProject)).scalars().first().id
