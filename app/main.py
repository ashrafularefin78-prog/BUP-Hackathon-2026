"""GridWise HTTP API service.

Endpoints (exact names per Problem Statement Section 06):
  GET  /health           -> 200 {"status": "ok"} when ready
  POST /optimize-energy  -> interpretation + optimization-plan JSON

Error mapping (Section 6.1):
  400  malformed JSON or structurally invalid request (Pydantic schema guard)
  500  controlled internal error — generic message only, no secrets or stack traces
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .pipeline import OptimizationFailedError, run_pipeline
from .schemas import OptimizeRequest, OptimizeResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gridwise.api")

app = FastAPI(
    title="GridWise Energy Optimization Service",
    description=(
        "LLM-assisted operator-directive interpretation and 24-hour "
        "smart-campus energy optimization (BUP CSE Fest 2026 preliminary)."
    ),
    version="1.0.0",
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the demo playground (index.html) from the repository root."""
    return FileResponse(Path(__file__).resolve().parents[1] / "index.html")


@app.get("/health")
def health() -> dict:
    """Readiness endpoint. No external dependency gates it (no false negatives)."""
    return {"status": "ok"}


@app.post(
    "/optimize-energy",
    response_model=OptimizeResponse,
    responses={500: {"description": "Controlled internal error"}},
)
def optimize_energy(request: OptimizeRequest) -> dict:
    try:
        outcome = run_pipeline(request)
    except OptimizationFailedError as exc:
        logger.error("controlled internal error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "The scenario could not be optimized to a valid schedule."},
        )
    return outcome.response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Structurally invalid requests -> 400 with a safe, generic body."""
    logger.info("rejected invalid request: %d error(s)", len(exc.errors()))
    return JSONResponse(
        status_code=400,
        content={"detail": "Malformed JSON or structurally invalid request."},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never leak stack traces or secrets; always a controlled 500."""
    logger.exception("unhandled error")  # logged server-side, redacted body
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal service error."},
    )
