"""FastAPI entrypoint for the initial scaffold."""

from pathlib import Path

from fastapi import FastAPI, HTTPException

from diagnostic_platform.config import settings
from diagnostic_platform.evaluation.workflow_audit import audit_workflow
from diagnostic_platform.graph.diagnostic_graph import build_diagnostic_graph, search_graph_paths
from diagnostic_platform.indexing.local_builder import build_local_index
from diagnostic_platform.indexing.vector_index import build_vector_index
from diagnostic_platform.ingestion.flow_excel import parse_flow_xlsx
from diagnostic_platform.ingestion.mineru_client import load_mineru_output, parse_document_with_mineru
from diagnostic_platform.generation.xml_plan import generate_xml_plan
from diagnostic_platform.normalizer.evidence import evidence_from_mineru
from diagnostic_platform.normalizer.mineru import normalize_request
from diagnostic_platform.evaluation.regression import RegressionReport, run_regression_evaluation
from diagnostic_platform.orchestration.pipeline import PipelineRunReport, run_pipeline
from diagnostic_platform.orchestration.workload import (
    WorkloadManifest,
    WorkloadPipelineRunRequest,
    WorkloadRegressionRunRequest,
    inject_workload_manifest_path,
    load_workload_manifest,
    workload_to_pipeline_config,
    workload_to_regression_config,
)
from diagnostic_platform.retrieval.local_sparse import query_local_index
from diagnostic_platform.resolution.audit_resolver import resolve_workflow_audit
from diagnostic_platform.resolution.llm_resolver import resolve_audit_with_llm
from diagnostic_platform.resolution.template_contract import build_template_contracts
from diagnostic_platform.renderers.c_template import render_c_function
from diagnostic_platform.renderers.template_registry import build_template_registry
from diagnostic_platform.renderers.workflow_fusion import fuse_workflow
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
    LocalIndexBuildRequest,
    LocalIndexBuildResult,
    LoadMinerUOutputRequest,
    NormalizeMinerURequest,
    ParseFlowXlsxRequest,
    ParseDocumentRequest,
    ParseDocumentResult,
    RenderedCode,
    RenderedXml,
    RetrievalQueryRequest,
    RetrievalResponse,
    ValidationResult,
    VectorIndexBuildRequest,
    VectorIndexBuildResult,
    XmlGenerationPlan,
    XmlPlanGenerateRequest,
    XmlPlanValidationRequest,
    XmlRenderRequest,
    XmlTemplateRegistry,
    XmlTemplateRegistryBuildRequest,
    XmlValidationRequest,
    XmlValidationResult,
    WorkflowFusionRequest,
    WorkflowFusionResult,
    WorkflowAuditRequest,
    WorkflowAuditReport,
    WorkflowResolveAuditRequest,
    WorkflowResolveAuditReport,
    WorkflowLlmResolveAuditRequest,
    WorkflowLlmResolveAuditReport,
    XmlTemplateContractBuildRequest,
    XmlTemplateContractRegistry,
)
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles
from diagnostic_platform.validation.rules import validate_plan
from diagnostic_platform.validation.xml_validator import validate_xml
from diagnostic_platform.validation.xml_plan_validator import validate_xml_plan

REPO_ROOT = Path(__file__).resolve().parents[2]

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


@app.post(f"{settings.api_prefix}/xml/plan/generate")
def generate_xml_intermediate_plan(request: XmlPlanGenerateRequest) -> XmlGenerationPlan:
    """Generate XML intermediate DSL from step evidence bundles."""

    return generate_xml_plan(request)


@app.post(f"{settings.api_prefix}/xml/plan/validate")
def validate_xml_intermediate_plan(request: XmlPlanValidationRequest) -> ValidationResult:
    """Validate XML intermediate DSL before rendering."""

    return validate_xml_plan(request)


@app.post(f"{settings.api_prefix}/xml/templates/build")
def build_xml_template_registry(request: XmlTemplateRegistryBuildRequest) -> XmlTemplateRegistry:
    """Scan XML files into a lightweight ScriptNode template registry."""

    try:
        return build_template_registry(request)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/xml/templates/contracts/build")
def build_xml_template_contracts(request: XmlTemplateContractBuildRequest) -> XmlTemplateContractRegistry:
    """Build ClassName argument contracts from an XML template registry."""

    try:
        return build_template_contracts(request)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/xml/workflow/render")
def render_xml_workflow(request: WorkflowFusionRequest) -> WorkflowFusionResult:
    """Fuse XML intermediate DSL into a base workflow XML."""

    try:
        return fuse_workflow(request)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/xml/workflow/audit")
def audit_xml_workflow(request: WorkflowAuditRequest) -> WorkflowAuditReport:
    """Audit XML plan parameters against base and fused workflow XML."""

    try:
        return audit_workflow(request)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/xml/workflow/resolve-audit")
def resolve_xml_workflow_audit(request: WorkflowResolveAuditRequest) -> WorkflowResolveAuditReport:
    """Resolve workflow audit findings with template argument contracts."""

    try:
        return resolve_workflow_audit(request)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/xml/workflow/resolve-audit-llm")
def resolve_xml_workflow_audit_with_llm(request: WorkflowLlmResolveAuditRequest) -> WorkflowLlmResolveAuditReport:
    """Resolve workflow audit findings with a vLLM OpenAI-compatible endpoint."""

    try:
        return resolve_audit_with_llm(request)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/xml/graph/build")
def build_xml_graph(request: BuildDiagnosticGraphRequest) -> DiagnosticGraph:
    """Build a lightweight diagnostic graph from flow steps and evidence."""

    return build_diagnostic_graph(request)


@app.post(f"{settings.api_prefix}/xml/graph/search")
def search_xml_graph(request: GraphPathSearchRequest) -> GraphPathSearchResponse:
    """Search paths in the lightweight diagnostic graph."""

    return search_graph_paths(request)


@app.post(f"{settings.api_prefix}/index/build")
def build_index(request: LocalIndexBuildRequest) -> LocalIndexBuildResult:
    """Build local JSONL indexes from an existing MinerU output directory."""

    try:
        return build_local_index(request)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/index/vector/build")
def build_vector_index_endpoint(request: VectorIndexBuildRequest) -> VectorIndexBuildResult:
    """Build a Milvus vector index from an existing local JSONL index."""

    try:
        return build_vector_index(request)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/retrieval/query")
def query_retrieval(request: RetrievalQueryRequest) -> RetrievalResponse:
    """Query local sparse and optional dense retrieval indexes."""

    try:
        return query_local_index(request)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(f"{settings.api_prefix}/workloads/manifest")
def get_workload_manifest(path: str = "configs/workloads/treg_20260402.json") -> WorkloadManifest:
    """Load a workload manifest."""

    try:
        return load_workload_manifest(Path(path))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/workloads/regression/run")
def run_workload_regression(request: WorkloadRegressionRunRequest) -> RegressionReport:
    """Run regression evaluation from a workload manifest."""

    try:
        manifest = load_workload_manifest(Path(request.workload_path))
        report = run_regression_evaluation(workload_to_regression_config(manifest, use_llm_xml=request.use_llm_xml))
        output_path = Path(request.output_path or manifest.paths["regression_report"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            report.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        return report
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{settings.api_prefix}/workloads/pipeline/run")
def run_workload_pipeline(request: WorkloadPipelineRunRequest) -> PipelineRunReport:
    """Run the end-to-end pipeline from a workload manifest."""

    try:
        workload_path = Path(request.workload_path)
        manifest = load_workload_manifest(workload_path)
        pipeline_config = inject_workload_manifest_path(
            workload_to_pipeline_config(manifest, enable_llm=request.enable_llm),
            workload_path,
        )
        report = run_pipeline(
            config=pipeline_config,
            repo_root=REPO_ROOT,
            skip_steps=set(request.skip_steps),
        )
        output_path = Path(request.output_path or manifest.paths["pipeline_report"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            report.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        return report
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
