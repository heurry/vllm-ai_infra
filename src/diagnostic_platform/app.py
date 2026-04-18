"""FastAPI entrypoint for the initial scaffold."""

from pathlib import Path

from fastapi import FastAPI, HTTPException

from diagnostic_platform.config import settings
from diagnostic_platform.graph.diagnostic_graph import build_diagnostic_graph, search_graph_paths
from diagnostic_platform.ingestion.flow_excel import parse_flow_xlsx
from diagnostic_platform.ingestion.mineru_client import load_mineru_output, parse_document_with_mineru
from diagnostic_platform.normalizer.evidence import evidence_from_mineru
from diagnostic_platform.normalizer.mineru import normalize_request
from diagnostic_platform.renderers.c_template import render_c_function
from diagnostic_platform.renderers.xml_workflow import render_xml_request
from diagnostic_platform.schemas import (
    BuildDiagnosticGraphRequest,
    BuildStepEvidenceRequest,
    BuildStepEvidenceResponse,
    DiagnosticPlan,
    DiagnosticGraph,
    EvidenceUnit,
    FlowStepPlan,
    GraphPathSearchRequest,
    GraphPathSearchResponse,
    LoadMinerUOutputRequest,
    NormalizeMinerURequest,
    ParseFlowXlsxRequest,
    ParseDocumentRequest,
    ParseDocumentResult,
    RenderedCode,
    RenderedXml,
    ValidationResult,
    XmlRenderRequest,
    XmlValidationRequest,
    XmlValidationResult,
)
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles
from diagnostic_platform.validation.rules import validate_plan
from diagnostic_platform.validation.xml_validator import validate_xml

app = FastAPI(title=settings.app_name)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness endpoint."""

    return {"status": "ok"}


@app.post(f"{settings.api_prefix}/documents/parse")
def parse_document(request: ParseDocumentRequest) -> ParseDocumentResult:
    """Invoke MinerU to parse a PDF/image and optionally load structured output."""

    try:
        return parse_document_with_mineru(request)
    except (FileNotFoundError, TimeoutError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/documents/load-mineru")
def load_mineru_document_output(request: LoadMinerUOutputRequest) -> NormalizeMinerURequest:
    """Load existing MinerU output into normalized content blocks."""

    try:
        return load_mineru_output(request)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/knowledge/normalize")
def normalize_mineru_payload(request: NormalizeMinerURequest):
    """Normalize MinerU output into unified knowledge units."""

    return normalize_request(request)


@app.post(f"{settings.api_prefix}/xml/evidence/from-mineru")
def build_evidence_from_mineru(request: NormalizeMinerURequest) -> list[EvidenceUnit]:
    """Convert MinerU output into traceable evidence units."""

    return evidence_from_mineru(request)


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


@app.post(f"{settings.api_prefix}/xml/evidence/build")
def build_xml_step_evidence(request: BuildStepEvidenceRequest) -> BuildStepEvidenceResponse:
    """Build evidence bundles for parsed XML flow steps."""

    return build_step_evidence_bundles(request)


@app.post(f"{settings.api_prefix}/xml/graph/build")
def build_xml_graph(request: BuildDiagnosticGraphRequest) -> DiagnosticGraph:
    """Build a lightweight diagnostic graph from flow steps and evidence."""

    return build_diagnostic_graph(request)


@app.post(f"{settings.api_prefix}/xml/graph/search")
def search_xml_graph(request: GraphPathSearchRequest) -> GraphPathSearchResponse:
    """Search paths in the lightweight diagnostic graph."""

    return search_graph_paths(request)
