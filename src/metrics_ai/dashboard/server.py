"""FastAPI backend serving ledger JSON and the single-page Chart.js/CanvasJS dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from metrics_ai.storage import Ledger

INDEX_HTML = Path(__file__).with_name("index.html")
JSON_COLUMNS = {"files_modified_json", "ast_diff_json", "raw_benchmark_json", "structured_json"}


def _decode(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        decoded = dict(row)
        for key in JSON_COLUMNS & decoded.keys():
            try:
                decoded[key] = json.loads(decoded[key]) if decoded[key] else None
            except (TypeError, ValueError):
                pass
        out.append(decoded)
    return out


def create_app(ledger_path: Path | str) -> FastAPI:
    ledger = Ledger(ledger_path)
    app = FastAPI(title="metrics.ai dashboard", docs_url=None, redoc_url=None)

    @app.get("/api/ledger")
    def get_ledger() -> dict[str, Any]:
        if not Path(ledger_path).exists():
            return {"commits": [], "telemetry_runs": [], "synthesized_impacts": []}
        return {table: _decode(rows) for table, rows in ledger.dump().items()}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML.read_text(encoding="utf-8")

    return app
