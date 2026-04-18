"""Shared API and domain schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MinerUBlockType = Literal["text", "table", "image", "equation"]
KnowledgeUnitType = Literal[
    "text_chunk",
    "table_chunk",
    "image_region",
    "equation_chunk",
    "code_template",
    "flow_step",
    "xml_template",
    "rule_chunk",
]
Severity = Literal["error", "warning"]
XmlScriptType = Literal["START", "NORMAL", "END"]
XmlRenderMode = Literal["script_node", "serial_node"]
EvidenceType = Literal["text", "table", "image", "flowchart", "equation", "template", "rule"]
GraphEntityType = Literal[
    "FlowNode",
    "XmlTemplate",
    "Evidence",
    "ECU",
    "DID",
    "Service",
    "Session",
    "SecurityLevel",
]


class DocumentMetadata(BaseModel):
    """Metadata attached to a source document."""

    doc_id: str
    doc_type: str = "pdf_protocol"
    protocol: str = "UDS"
    module: str | None = None
    vehicle_model: str | None = None
    ecu: str | None = None
    version: str | None = None
    source: str | None = None
    source_path: str | None = None


class MinerUContentBlock(BaseModel):
    """Simplified MinerU content block."""

    type: MinerUBlockType
    page_idx: int = Field(ge=0)
    bbox: list[int] = Field(default_factory=list)
    text: str | None = None
    text_level: int | None = None
    table_caption: list[str] = Field(default_factory=list)
    table_footnote: list[str] = Field(default_factory=list)
    image_caption: list[str] = Field(default_factory=list)
    image_footnote: list[str] = Field(default_factory=list)
    img_path: str | None = None


class NormalizeMinerURequest(BaseModel):
    """Normalization request for MinerU output."""

    metadata: DocumentMetadata
    blocks: list[MinerUContentBlock]


class KnowledgeUnit(BaseModel):
    """Unified knowledge unit used by retrieval and validation."""

    chunk_id: str
    unit_type: KnowledgeUnitType
    content: str
    protocol: str
    module: str | None = None
    ecu: str | None = None
    service_ids: list[str] = Field(default_factory=list)
    dids: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    security_levels: list[str] = Field(default_factory=list)
    page_idx: int
    bbox: list[int] = Field(default_factory=list)
    source_path: str | None = None


class EvidenceUnit(BaseModel):
    """Traceable evidence extracted from documents, templates, or rules."""

    evidence_id: str
    evidence_type: EvidenceType
    content: str
    doc_id: str
    source_path: str | None = None
    page_idx: int
    bbox: list[int] = Field(default_factory=list)
    metadata: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)


class DiagnosticStep(BaseModel):
    """Single protocol step in the intermediate DSL."""

    send: str
    expect: str
    description: str | None = None


class DiagnosticPlan(BaseModel):
    """Intermediate DSL that later becomes final C code."""

    function_name: str
    preconditions: list[str] = Field(default_factory=list)
    steps: list[DiagnosticStep] = Field(default_factory=list)
    on_fail: str = "return FAILED;"


class ValidationIssue(BaseModel):
    """Single validation issue."""

    severity: Severity
    code: str
    message: str
    step_index: int | None = None


class ValidationResult(BaseModel):
    """Validation summary."""

    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class RenderedCode(BaseModel):
    """Rendered code payload."""

    language: str = "c"
    code: str


class ParseFlowXlsxRequest(BaseModel):
    """Request for parsing a flow.xlsx file."""

    source_path: str
    include_non_flow: bool = False


class FlowNode(BaseModel):
    """One business node discovered from a flow.xlsx cell."""

    raw: str
    name: str
    template_name: str = ""
    sheet: str
    row: int
    column: int


class FlowStep(BaseModel):
    """One ordered flow step. Nodes in the same column are parallel."""

    step_key: str
    display_name: str
    order: int
    column: int
    parallel_nodes: list[FlowNode] = Field(default_factory=list)


class FlowStepPlan(BaseModel):
    """Structured flow-step plan parsed from flow.xlsx."""

    source_path: str
    steps: list[FlowStep] = Field(default_factory=list)


class EvidenceMatch(BaseModel):
    """One evidence retrieval result for a flow node."""

    evidence: EvidenceUnit
    score: float
    matched_terms: list[str] = Field(default_factory=list)


class NodeEvidenceBundle(BaseModel):
    """Evidence bundle for one flow node."""

    step_key: str
    node_name: str
    template_name: str
    matches: list[EvidenceMatch] = Field(default_factory=list)
    graph_paths: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class StepEvidenceBundle(BaseModel):
    """Evidence bundle for one ordered flow step."""

    step_key: str
    display_name: str
    order: int
    column: int
    node_bundles: list[NodeEvidenceBundle] = Field(default_factory=list)


class BuildStepEvidenceRequest(BaseModel):
    """Request for linking flow steps with evidence units."""

    flow_plan: FlowStepPlan
    evidence_units: list[EvidenceUnit]
    top_k_per_node: int = Field(default=5, ge=1, le=50)


class BuildStepEvidenceResponse(BaseModel):
    """Response for linked step evidence bundles."""

    source_flow_path: str
    bundles: list[StepEvidenceBundle] = Field(default_factory=list)


class GraphEntity(BaseModel):
    """Entity in the lightweight diagnostic graph."""

    entity_id: str
    entity_type: GraphEntityType
    name: str
    aliases: list[str] = Field(default_factory=list)
    metadata: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)


class GraphRelation(BaseModel):
    """Directed relation in the lightweight diagnostic graph."""

    source_id: str
    relation_type: str
    target_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)


class DiagnosticGraph(BaseModel):
    """Serializable diagnostic graph."""

    entities: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)


class BuildDiagnosticGraphRequest(BaseModel):
    """Request for building a lightweight diagnostic graph."""

    flow_plan: FlowStepPlan
    evidence_response: BuildStepEvidenceResponse | None = None
    evidence_units: list[EvidenceUnit] = Field(default_factory=list)


class GraphPathSearchRequest(BaseModel):
    """Request for graph path search."""

    graph: DiagnosticGraph
    source_id: str
    target_id: str | None = None
    relation_types: list[str] = Field(default_factory=list)
    max_depth: int = Field(default=3, ge=1, le=8)
    max_paths: int = Field(default=10, ge=1, le=100)


class GraphPath(BaseModel):
    """One graph path."""

    entity_ids: list[str] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list)


class GraphPathSearchResponse(BaseModel):
    """Graph path search response."""

    paths: list[GraphPath] = Field(default_factory=list)


class XmlArg(BaseModel):
    """Single XML argument with optional evidence trace."""

    name: str
    value: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class XmlScriptNodePlan(BaseModel):
    """Intermediate DSL for rendering a ScriptNode."""

    node_name: str
    class_name: str
    dead_ms: int = Field(default=0, ge=0)
    retry_times: int = Field(default=0, ge=0)
    script_type: XmlScriptType = "NORMAL"
    interupt_start: bool = False
    expression: str | None = None
    args: list[XmlArg] = Field(default_factory=list)


class XmlSerialNodePlan(BaseModel):
    """Intermediate DSL for rendering a SerialNode wrapper."""

    name: str = "MAC_ALL"
    dead_ms: int = Field(default=0, ge=0)
    expression: str | None = None
    scripts: list[XmlScriptNodePlan] = Field(default_factory=list)


class XmlRenderRequest(BaseModel):
    """Render request for XML intermediate DSL."""

    mode: XmlRenderMode = "script_node"
    script: XmlScriptNodePlan | None = None
    serial: XmlSerialNodePlan | None = None


class RenderedXml(BaseModel):
    """Rendered XML payload."""

    root_tag: str
    xml: str


class XmlValidationRequest(BaseModel):
    """Request for XML structure validation."""

    xml: str
    required_arg_names: list[str] = Field(default_factory=list)
    require_script_types: list[XmlScriptType] = Field(default_factory=list)
    min_script_nodes: int = Field(default=0, ge=0)


class XmlValidationResult(BaseModel):
    """XML validation result."""

    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    stats: dict[str, int | str] = Field(default_factory=dict)
