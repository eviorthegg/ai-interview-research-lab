"""Initial schema — creates all lab tables from models metadata.

Subsequent migrations should use `alembic revision --autogenerate`.

Revision ID: 0001
Revises:
Create Date: 2026-07-11
"""
import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from db import Base
    import models  # noqa: F401
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    from db import Base
    import models  # noqa: F401
    Base.metadata.drop_all(op.get_bind())
