"""Alembic environment. DATABASE_URL comes from the lab config (env vars)."""
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_config  # noqa: E402
from db import Base  # noqa: E402
import models  # noqa: E402, F401  (register all mapped classes)

config = context.config
config.set_main_option("sqlalchemy.url", get_config().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=get_config().database_url,
                      target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
