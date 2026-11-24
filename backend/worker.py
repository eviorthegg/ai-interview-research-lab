"""Dedicated worker process. Run separately from the Flask API:

    python worker.py
"""
from __future__ import annotations

import io
import logging
import os
import sys

# Force UTF-8 output on Windows to handle Unicode chars (e.g. → in experiment names)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

from config import get_config
from db import init_db
from orchestration import worker_loop

if __name__ == "__main__":
    cfg = get_config()
    cfg.assert_isolation()
    logging.basicConfig(level=cfg.log_level)
    init_db()
    name = f"worker-{os.getpid()}"
    print(f"[research-lab] worker '{name}' started "
          f"(provider_mode={cfg.provider_mode}, db={cfg.database_url})")
    worker_loop(worker_name=name)
