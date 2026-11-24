"""Flask application entry point for the Evior Interview Research Lab API."""
from __future__ import annotations

import logging

from flask import Flask, jsonify

from api import api
from api_pipelines import pipelines_api
from config import get_config
from db import init_db


def create_app() -> Flask:
    cfg = get_config()
    cfg.assert_isolation()  # refuse to start without production-write isolation
    logging.basicConfig(level=cfg.log_level,
                        format='{"time":"%(asctime)s","level":"%(levelname)s",'
                               '"logger":"%(name)s","message":"%(message)s"}')
    app = Flask(__name__)
    app.register_blueprint(api)
    app.register_blueprint(pipelines_api)
    init_db()

    @app.after_request
    def cors(response):
        # Local development CORS only
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:5173"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Idempotency-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        return response

    @app.get("/health")
    def health():
        return jsonify({
            "status": "ok", "research_lab_mode": cfg.research_lab_mode,
            "allow_production_writes": cfg.allow_production_writes,
            "provider_mode": cfg.provider_mode})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5058, debug=False, threaded=True)
