"""FastAPI entrypoint for the initial scaffold."""

from fastapi import FastAPI

from diagnostic_platform.config import settings
from diagnostic_platform.normalizer.mineru import normalize_request
from diagnostic_platform.renderers.c_template import render_c_function
from diagnostic_platform.schemas import (
    DiagnosticPlan,
    NormalizeMinerURequest,
    RenderedCode,
    ValidationResult,
)
from diagnostic_platform.validation.rules import validate_plan

app = FastAPI(title=settings.app_name)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness endpoint."""

    return {"status": "ok"}


@app.post(f"{settings.api_prefix}/knowledge/normalize")
def normalize_mineru_payload(request: NormalizeMinerURequest):
    """Normalize MinerU output into unified knowledge units."""

    return normalize_request(request)


@app.post(f"{settings.api_prefix}/validation/diagnostic")
def validate_diagnostic_plan(plan: DiagnosticPlan) -> ValidationResult:
    """Validate a diagnostic DSL before rendering."""

    return validate_plan(plan)


@app.post(f"{settings.api_prefix}/render/c")
def render_c(plan: DiagnosticPlan) -> RenderedCode:
    """Render the DSL into a minimal C function template."""

    return RenderedCode(code=render_c_function(plan))

