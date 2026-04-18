"""FastAPI entrypoint for the initial scaffold."""

from pathlib import Path

from fastapi import FastAPI, HTTPException

from diagnostic_platform.config import settings
from diagnostic_platform.ingestion.flow_excel import parse_flow_xlsx
from diagnostic_platform.normalizer.mineru import normalize_request
from diagnostic_platform.renderers.c_template import render_c_function
from diagnostic_platform.renderers.xml_workflow import render_xml_request
from diagnostic_platform.schemas import (
    DiagnosticPlan,
    FlowStepPlan,
    NormalizeMinerURequest,
    ParseFlowXlsxRequest,
    RenderedCode,
    RenderedXml,
    ValidationResult,
    XmlRenderRequest,
    XmlValidationRequest,
    XmlValidationResult,
)
from diagnostic_platform.validation.rules import validate_plan
from diagnostic_platform.validation.xml_validator import validate_xml

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


@app.post(f"{settings.api_prefix}/xml/flow/parse")
def parse_xml_flow(request: ParseFlowXlsxRequest) -> FlowStepPlan:
    """Parse flow.xlsx into a structured flow-step plan."""

    try:
        return parse_flow_xlsx(
            source_path=Path(request.source_path),
            include_non_flow=request.include_non_flow,
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/xml/task/render")
def render_xml_task(request: XmlRenderRequest) -> RenderedXml:
    """Render XML intermediate DSL into ScriptNode or SerialNode XML."""

    try:
        return render_xml_request(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/xml/validate")
def validate_xml_payload(request: XmlValidationRequest) -> XmlValidationResult:
    """Validate generated XML fragments."""

    return validate_xml(request)
