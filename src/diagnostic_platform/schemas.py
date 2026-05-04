"""Shared API and domain schemas."""

from __future__ import annotations

from typing import Any, Literal

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
ChunkingMode = Literal["legacy", "semantic"]
SemanticChunkType = Literal["paragraph", "section", "table", "table_row", "image", "flowchart", "equation"]
Severity = Literal["error", "warning"]
XmlScriptType = Literal["START", "NORMAL", "END"]
XmlRenderMode = Literal["script_node", "serial_node"]
WorkflowArgMergeStrategy = Literal["fill_missing", "overwrite"]
WorkflowResolveAction = Literal[
    "no_action",
    "keep_base",
    "accept_plan",
    "append_plan_arg",
    "ignore_plan_arg",
    "needs_review",
]
WorkflowArgAuditStatus = Literal[
    "confirmed_by_plan",
    "base_only_unchanged",
    "filled_from_plan",
    "appended_from_plan",
    "overwritten_from_plan",
    "plan_conflict_kept_base",
    "plan_conflict_fused_diff",
    "plan_not_applied",
    "plan_arg_missing_in_fused",
    "extra_plan_arg_ignored",
    "workflow_changed_without_plan",
    "node_missing_in_base",
    "node_missing_in_fused",
]
EvidenceType = Literal["text", "table", "image", "flowchart", "equation", "template", "rule"]
GraphEntityType = Literal[
    "FlowNode",
    "FlowTask",
    "TemplateClass",
    "ActionIntent",
    "Document",
    "Section",
    "Table",
    "TableRow",
    "ParameterField",
    "FlowchartTitle",
    "FlowchartImage",
    "XmlTemplate",
    "AliasTerm",
    "Evidence",
    "ECU",
    "DID",
    "RID",
    "Service",
    "Session",
    "SecurityLevel",
]
MetadataValue = str | int | float | bool | list[str] | list[int] | dict[str, str]


class VllmModelConfig(BaseModel):
    """OpenAI-compatible vLLM endpoint configuration."""

    base_url: str = "http://127.0.0.1:8008/v1"
    model: str = "qwen-audit-resolver"
    api_key: str = "EMPTY"
    timeout_seconds: int = Field(default=120, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1)


class ChatMessage(BaseModel):
    """OpenAI-compatible chat message."""

    role: str
    content: str


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
    table_body: str | None = None
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
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class SourceBlockRef(BaseModel):
    """Reference back to one MinerU content_list block."""

    block_id: str
    block_type: MinerUBlockType
    page_idx: int
    bbox: list[int] = Field(default_factory=list)
    img_path: str = ""


class SemanticChunk(BaseModel):
    """Semantic RAG chunk with provenance back to MinerU layout blocks."""

    chunk_id: str
    chunk_type: SemanticChunkType
    content: str
    doc_id: str
    protocol: str = "UDS"
    module: str = ""
    ecu: str = ""
    source: str = "semantic_chunk"
    source_path: str = ""
    page_idx: int = 0
    bbox: list[int] = Field(default_factory=list)
    source_block_ids: list[str] = Field(default_factory=list)
    source_blocks: list[SourceBlockRef] = Field(default_factory=list)
    parent_chunk_id: str = ""
    section_title: str = ""
    table_fields: dict[str, str] = Field(default_factory=dict)
    flowchart_title: str = ""
    reference_text: str = ""
    img_path: str = ""
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


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


class ParseDocumentRequest(BaseModel):
    """Request for invoking MinerU on a PDF or image path."""

    source_path: str
    output_dir: str
    metadata: DocumentMetadata
    mineru_executable: str = "mineru"
    backend: str = "pipeline"
    method: str = "auto"
    lang: str = "ch"
    server_url: str | None = None
    start_page_id: int = Field(default=0, ge=0)
    end_page_id: int | None = Field(default=None, ge=0)
    formula_enable: bool = True
    table_enable: bool = True
    device_mode: str | None = None
    model_source: str | None = None
    timeout_seconds: int = Field(default=3600, ge=1)
    load_output: bool = True


class MinerUOutputFiles(BaseModel):
    """Files discovered in a MinerU output directory."""

    markdown_files: list[str] = Field(default_factory=list)
    content_list_files: list[str] = Field(default_factory=list)
    middle_json_files: list[str] = Field(default_factory=list)
    image_dirs: list[str] = Field(default_factory=list)


class ParseDocumentResult(BaseModel):
    """Result of a synchronous MinerU parse command."""

    source_path: str
    output_dir: str
    command: list[str]
    return_code: int
    stdout: str = ""
    stderr: str = ""
    files: MinerUOutputFiles
    normalized: NormalizeMinerURequest | None = None


class LoadMinerUOutputRequest(BaseModel):
    """Request for loading existing MinerU output into normalized blocks."""

    output_dir: str
    metadata: DocumentMetadata
    content_list_paths: list[str] = Field(default_factory=list)
    include_markdown_fallback: bool = True


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
    """One ordered flow column. Nodes in the same Excel column are parallel."""

    step_key: str
    display_name: str
    order: int
    row: int = 0
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
    source: str = "graph"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskEvidenceCandidate(BaseModel):
    """Graph RAG candidate evidence for one flow task."""

    evidence: EvidenceUnit
    score: float
    matched_terms: list[str] = Field(default_factory=list)
    graph_paths: list[str] = Field(default_factory=list)
    kg_candidate_ids: list[str] = Field(default_factory=list)
    kg_paths: list[str] = Field(default_factory=list)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    provenance_refs: list[str] = Field(default_factory=list)
    raw_source_path: str = ""
    needs_review: list[str] = Field(default_factory=list)
    block_role: str = ""
    parameter_fields: dict[str, str] = Field(default_factory=dict)
    flowchart_title: str = ""
    template_match: str = ""
    retrieval_source: str = "graph"
    retrieval_rank: int = 0
    retrieval_score: float | None = None
    retrieval_metadata: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class NodeEvidenceBundle(BaseModel):
    """Evidence bundle for one flow node."""

    step_key: str
    node_name: str
    template_name: str
    row: int = 0
    column: int = 0
    matches: list[EvidenceMatch] = Field(default_factory=list)
    candidates: list[TaskEvidenceCandidate] = Field(default_factory=list)
    graph_paths: list[str] = Field(default_factory=list)
    retrieval_trace: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class StepEvidenceBundle(BaseModel):
    """Evidence bundle for one ordered flow step."""

    step_key: str
    display_name: str
    order: int
    row: int = 0
    column: int
    node_bundles: list[NodeEvidenceBundle] = Field(default_factory=list)


class BuildStepEvidenceRequest(BaseModel):
    """Request for linking flow steps with evidence units."""

    flow_plan: FlowStepPlan
    evidence_units: list[EvidenceUnit]
    top_k_per_node: int = Field(default=5, ge=1, le=50)
    index_dir: str = ""
    enable_dense: bool = False
    dense_top_k: int = Field(default=8, ge=1, le=200)
    hybrid_top_k: int = Field(default=8, ge=1, le=200)
    dense_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    sparse_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    milvus_uri: str = "http://127.0.0.1:19530"
    vector_collection: str = ""
    embedding_model: str = "BAAI/bge-m3"
    vector_manifest_path: str | None = None
    reference_max_depth: int = Field(default=3, ge=0, le=8)
    reference_limit: int = Field(default=5, ge=1, le=20)


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
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class GraphRelation(BaseModel):
    """Directed relation in the lightweight diagnostic graph."""

    source_id: str
    relation_type: str
    target_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


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


class KnowledgeGraphManifest(BaseModel):
    """Versioned local full-KG snapshot metadata."""

    schema_version: str = "kg.v1"
    collection_name: str = "default"
    source: str = ""
    node_count: int = 0
    edge_count: int = 0
    provenance_count: int = 0
    evidence_unit_count: int = 0
    files: dict[str, str] = Field(default_factory=dict)


class KnowledgeGraphNode(BaseModel):
    """One node in the local full knowledge graph snapshot."""

    node_id: str
    node_type: str
    name: str
    canonical_name: str = ""
    normalized_tokens: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class KnowledgeGraphEdge(BaseModel):
    """One directed edge in the local full knowledge graph snapshot."""

    edge_id: str
    source_id: str
    relation_type: str
    target_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    provenance_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class KnowledgeGraphProvenance(BaseModel):
    """Source provenance for a KG node or edge."""

    provenance_id: str
    evidence_id: str
    node_id: str = ""
    edge_id: str = ""
    source_path: str = ""
    doc_id: str = ""
    page_idx: int = 0
    bbox: list[int] = Field(default_factory=list)
    content_hash: str = ""
    content_excerpt: str = ""
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class KnowledgeGraphSnapshot(BaseModel):
    """In-memory representation of a local full-KG snapshot."""

    manifest: KnowledgeGraphManifest
    nodes: list[KnowledgeGraphNode] = Field(default_factory=list)
    edges: list[KnowledgeGraphEdge] = Field(default_factory=list)
    provenance: list[KnowledgeGraphProvenance] = Field(default_factory=list)


class ReferenceResolution(BaseModel):
    """Resolved or unresolved reference from evidence text to KG entities."""

    reference_text: str
    resolved_node_ids: list[str] = Field(default_factory=list)
    kg_paths: list[str] = Field(default_factory=list)
    provenance_refs: list[str] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    score: float = 0.0


class NodeIntent(BaseModel):
    """Generic intent parsed from a flow node for retrieval planning."""

    node_name: str
    template_name: str = ""
    target_ecu: str = ""
    operation_phrase: str = ""
    operation_family: str = ""
    type_hint: str = ""
    qualifiers: list[str] = Field(default_factory=list)
    raw_tokens: list[str] = Field(default_factory=list)


class QuerySpec(BaseModel):
    """One typed retrieval query generated for a flow node."""

    query_type: str
    query: str
    source: str = "query_planner"
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class QueryPlan(BaseModel):
    """Multi-query retrieval plan for one flow node."""

    node_intent: NodeIntent
    queries: list[QuerySpec] = Field(default_factory=list)


class ReferenceHop(BaseModel):
    """One refer-to expansion hop in the evidence chain."""

    hop: int
    from_evidence_id: str
    reference_text: str
    query: str = ""
    resolved_title: str = ""
    resolved_evidence_ids: list[str] = Field(default_factory=list)
    status: str = "not_found"
    score: float = 0.0
    needs_review: list[str] = Field(default_factory=list)


class EvidenceFilterDecision(BaseModel):
    """Keep/demote/drop decision for a candidate evidence item."""

    evidence_id: str
    decision: str
    reason: str
    score_delta: float = 0.0
    trace: dict[str, MetadataValue] = Field(default_factory=dict)


class EvidenceChainNodeTrace(BaseModel):
    """Human-readable evidence chain trace for one flow node."""

    step_key: str
    step_display_name: str = ""
    step_order: int = 0
    node_name: str
    template_name: str = ""
    action: str = ""
    matched_section: str = "not_available"
    parameter_source_markdown: str = "not_available"
    parameter_source_lines: str = "not_available"
    parameter_source_pdf: str = "not_available"
    parameter_source_page: str = "not_available"
    parameter_page_image: str = "not_available"
    refer_texts: list[str] = Field(default_factory=list)
    final_flow_title: str = "not_found"
    flowchart_status: str = "not_found"
    flowchart_image: str = "not_available"
    parameter_text: str = ""
    reference_hops: list[ReferenceHop] = Field(default_factory=list)
    filter_decisions: list[EvidenceFilterDecision] = Field(default_factory=list)
    kept_evidence_ids: list[str] = Field(default_factory=list)
    demoted_evidence_ids: list[str] = Field(default_factory=list)
    dropped_evidence_ids: list[str] = Field(default_factory=list)


class EvidenceChainReport(BaseModel):
    """Evidence chain report for all flow nodes."""

    source_flow_path: str
    nodes: list[EvidenceChainNodeTrace] = Field(default_factory=list)
    summary: dict[str, int | float | str] = Field(default_factory=dict)


class LocalIndexBuildRequest(BaseModel):
    """Request for building local JSONL indexes from MinerU outputs."""

    mineru_output_dir: str
    index_dir: str
    collection_name: str = "default"
    doc_type: str = "pdf_protocol"
    protocol: str = "UDS"
    source: str = "mineru"
    include_graph: bool = True
    chunking_mode: ChunkingMode = "legacy"


class LocalIndexBuildResult(BaseModel):
    """Summary of a local index build."""

    collection_name: str
    index_dir: str
    document_count: int
    knowledge_unit_count: int
    evidence_unit_count: int
    graph_entity_count: int = 0
    graph_relation_count: int = 0
    kg_node_count: int = 0
    kg_edge_count: int = 0
    kg_provenance_count: int = 0
    files: dict[str, str] = Field(default_factory=dict)
    summary: dict[str, int | str | list[str] | dict[str, int]] = Field(default_factory=dict)


class VectorIndexBuildRequest(BaseModel):
    """Request for building a Milvus vector index from local JSONL indexes."""

    index_dir: str
    collection_name: str = ""
    milvus_uri: str = "http://127.0.0.1:19530"
    embedding_model: str = "BAAI/bge-m3"
    metric_type: str = "COSINE"
    batch_size: int = Field(default=64, ge=1, le=4096)
    drop_existing: bool = False
    include_knowledge_units: bool = True
    include_evidence_units: bool = True
    manifest_path: str | None = None


class VectorIndexManifest(BaseModel):
    """Local manifest describing a Milvus vector collection."""

    schema_version: str = "vector.v1"
    index_dir: str
    collection_name: str
    milvus_uri: str
    embedding_model: str
    embedding_dimension: int
    metric_type: str = "COSINE"
    vector_count: int = 0
    knowledge_unit_count: int = 0
    evidence_unit_count: int = 0
    source_files: dict[str, str] = Field(default_factory=dict)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    built_at: str = ""


class VectorIndexBuildResult(BaseModel):
    """Result of building a vector index."""

    collection_name: str
    milvus_uri: str
    embedding_model: str
    embedding_dimension: int
    metric_type: str
    vector_count: int
    knowledge_unit_count: int = 0
    evidence_unit_count: int = 0
    manifest_path: str
    source_hashes: dict[str, str] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)


class RetrievalFilters(BaseModel):
    """Metadata filters for local retrieval."""

    doc_ids: list[str] = Field(default_factory=list)
    unit_types: list[KnowledgeUnitType] = Field(default_factory=list)
    protocol: str | None = None
    module: str | None = None
    ecu: str | None = None
    service_ids: list[str] = Field(default_factory=list)
    dids: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    security_levels: list[str] = Field(default_factory=list)


class RetrievalQueryRequest(BaseModel):
    """Request for querying local sparse and optional dense indexes."""

    query: str
    index_dir: str
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    top_k: int = Field(default=8, ge=1, le=50)
    enable_sparse: bool = True
    enable_dense: bool = False
    enable_graph: bool = False
    dense_top_k: int = Field(default=8, ge=1, le=200)
    hybrid_top_k: int = Field(default=8, ge=1, le=200)
    dense_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    sparse_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    milvus_uri: str = "http://127.0.0.1:19530"
    vector_collection: str = ""
    embedding_model: str = "BAAI/bge-m3"
    vector_manifest_path: str | None = None


class RetrievedChunk(BaseModel):
    """One retrieved knowledge chunk."""

    chunk_id: str
    score: float
    source: str = "sparse"
    content: str
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class RetrievalResponse(BaseModel):
    """Retrieval response with packed context."""

    query: str
    rewritten_query: str
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    context: str = ""
    trace: dict[str, Any] = Field(default_factory=dict)


class XmlArg(BaseModel):
    """Single XML argument with optional evidence trace."""

    name: str
    value: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    selection_score: float | None = None
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    graph_paths: list[str] = Field(default_factory=list)
    kg_paths: list[str] = Field(default_factory=list)
    provenance_refs: list[str] = Field(default_factory=list)
    raw_source_path: str = ""
    needs_review: list[str] = Field(default_factory=list)
    selection_notes: list[str] = Field(default_factory=list)


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


class XmlPlanGenerateRequest(BaseModel):
    """Request for generating XML intermediate DSL from step evidence."""

    evidence_response: BuildStepEvidenceResponse
    serial_name: str = "MAC_ALL"
    default_dead_ms: int = Field(default=0, ge=0)
    default_retry_times: int = Field(default=0, ge=0)
    default_args: list[XmlArg] = Field(default_factory=list)
    boundary_script_types: bool = True


class XmlPlannedNode(BaseModel):
    """One planned XML script node with evidence and extraction notes."""

    step_key: str
    order: int
    row: int = 0
    column: int = 0
    node_name: str
    template_name: str = ""
    script: XmlScriptNodePlan
    evidence_ids: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class XmlGenerationPlan(BaseModel):
    """XML intermediate DSL ready for validation and rendering."""

    source_flow_path: str
    serial: XmlSerialNodePlan
    nodes: list[XmlPlannedNode] = Field(default_factory=list)


class XmlTemplateEntry(BaseModel):
    """One reusable ScriptNode template discovered from XML files."""

    template_id: str
    node_name: str
    class_name: str
    root_tag: str
    source_path: str
    arg_names: list[str] = Field(default_factory=list)
    arg_values: dict[str, str | None] = Field(default_factory=dict)
    attrs: dict[str, str] = Field(default_factory=dict)
    xml: str = ""


class XmlTemplateRegistryBuildRequest(BaseModel):
    """Request for scanning XML files into a lightweight template registry."""

    source_dir: str
    output_path: str | None = None
    include_workflow_files: bool = True


class XmlTemplateRegistry(BaseModel):
    """Serializable XML template registry."""

    source_dir: str
    templates: list[XmlTemplateEntry] = Field(default_factory=list)
    by_class_name: dict[str, list[str]] = Field(default_factory=dict)
    by_node_name: dict[str, list[str]] = Field(default_factory=dict)


class XmlFlowchartSpec(BaseModel):
    """Flowchart/process description linked to a template family."""

    flowchart_id: str
    title: str
    normalized_title: str = ""
    source_path: str = ""
    start_line: int = 0
    end_line: int = 0
    content: str = ""
    aliases: list[str] = Field(default_factory=list)


class XmlTaskNodeSignature(BaseModel):
    """Structural signature extracted from a TaskNode implementation XML."""

    root_tag: str = "TaskNode"
    source_path: str = ""
    class_name: str = ""
    create_vars: dict[str, str] = Field(default_factory=dict)
    arg_names: list[str] = Field(default_factory=list)
    function_names: list[str] = Field(default_factory=list)
    assigned_vars: list[str] = Field(default_factory=list)
    service_ids: list[str] = Field(default_factory=list)
    positive_responses: list[str] = Field(default_factory=list)
    request_literals: list[str] = Field(default_factory=list)
    response_literals: list[str] = Field(default_factory=list)
    hex_literals: list[str] = Field(default_factory=list)
    logic_markers: list[str] = Field(default_factory=list)
    tag_counts: dict[str, int] = Field(default_factory=dict)


class TemplateFamilyEntry(BaseModel):
    """One reusable ClassName/TaskNode template family."""

    family_id: str
    aliases: list[str] = Field(default_factory=list)
    template_class_names: list[str] = Field(default_factory=list)
    xml_example_paths: list[str] = Field(default_factory=list)
    xml_examples: list[str] = Field(default_factory=list)
    flowcharts: list[XmlFlowchartSpec] = Field(default_factory=list)
    signatures: list[XmlTaskNodeSignature] = Field(default_factory=list)
    merged_signature: XmlTaskNodeSignature | None = None
    required_evidence: list[str] = Field(default_factory=list)
    validation_hints: list[str] = Field(default_factory=list)


class TemplateFamilyRegistryBuildRequest(BaseModel):
    """Request for building TaskNode template families from XML examples and flowchart docs."""

    xml_template_dir: str = "data/xml_template"
    flowchart_markdown_path: str = "流程图.md"
    output_path: str | None = None
    include_xml_examples: bool = True
    max_example_chars: int = Field(default=12000, ge=0)


class TemplateFamilyRegistry(BaseModel):
    """Serializable registry used by TemplateFamilyResolver and class XML generation."""

    xml_template_dir: str = ""
    flowchart_markdown_path: str = ""
    families: list[TemplateFamilyEntry] = Field(default_factory=list)
    by_family_id: dict[str, TemplateFamilyEntry] = Field(default_factory=dict)
    by_class_name: dict[str, str] = Field(default_factory=dict)
    by_alias: dict[str, list[str]] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


class TemplateFamilyResolution(BaseModel):
    """Result of resolving one FlowNode to a TaskNode template family."""

    node_name: str
    template_name: str = ""
    family_id: str = ""
    score: float = 0.0
    status: str = "not_found"
    match_reasons: list[str] = Field(default_factory=list)
    family: TemplateFamilyEntry | None = None


class XmlTemplateArgContract(BaseModel):
    """Argument contract inferred for one ScriptNode ClassName."""

    arg_name: str
    observed_count: int
    required: bool = False


class XmlTemplateClassContract(BaseModel):
    """Parameter contract for one ScriptNode ClassName."""

    class_name: str
    template_count: int
    required_args: list[str] = Field(default_factory=list)
    optional_args: list[str] = Field(default_factory=list)
    arg_contracts: list[XmlTemplateArgContract] = Field(default_factory=list)
    node_names: list[str] = Field(default_factory=list)
    source_paths: list[str] = Field(default_factory=list)


class XmlTemplateContractBuildRequest(BaseModel):
    """Request for building ClassName -> ArgName contracts from a template registry."""

    registry: XmlTemplateRegistry | None = None
    registry_path: str | None = None
    output_path: str | None = None
    required_observed_ratio: float = Field(default=1.0, ge=0.0, le=1.0)


class XmlTemplateContractRegistry(BaseModel):
    """Serializable ClassName parameter contract registry."""

    source_registry_path: str | None = None
    class_contracts: list[XmlTemplateClassContract] = Field(default_factory=list)
    by_class_name: dict[str, XmlTemplateClassContract] = Field(default_factory=dict)


class XmlTemplateExample(BaseModel):
    """A prompt-ready historical ScriptNode example."""

    template_id: str
    node_name: str
    class_name: str
    source_path: str = ""
    arg_names: list[str] = Field(default_factory=list)
    arg_values: dict[str, str | None] = Field(default_factory=dict)
    xml: str = ""
    match_reasons: list[str] = Field(default_factory=list)
    similarity_score: float = 0.0


class BaseWorkflowNodeReference(BaseModel):
    """Existing workflow node used as a strong generation reference."""

    node_name: str
    class_name: str = ""
    args: dict[str, str | None] = Field(default_factory=dict)
    xml: str = ""
    source_path: str = ""


class XmlPromptContextItem(BaseModel):
    """One budgeted context item supplied to the LLM prompt."""

    context_id: str
    kind: str
    score: float = 0.0
    evidence_id: str = ""
    source_path: str = ""
    page_idx: int = 0
    block_role: str = ""
    parameter_fields: dict[str, str] = Field(default_factory=dict)
    graph_paths: list[str] = Field(default_factory=list)
    kg_paths: list[str] = Field(default_factory=list)
    provenance_refs: list[str] = Field(default_factory=list)
    raw_source_path: str = ""
    retrieval_source: str = ""
    retrieval_score: float | None = None
    retrieval_metadata: dict[str, Any] = Field(default_factory=dict)
    content: str = ""


class XmlGenerationContext(BaseModel):
    """All bounded evidence and constraints used for one LLM XML generation call."""

    step_key: str
    order: int = 0
    row: int = 0
    column: int = 0
    node_name: str
    template_name: str = ""
    class_name: str = ""
    script_type: XmlScriptType = "NORMAL"
    relation: str = "operation_level_script_node"
    node_bundle: NodeEvidenceBundle
    prompt_context_ids: list[str] = Field(default_factory=list)
    context_items: list[XmlPromptContextItem] = Field(default_factory=list)
    graph_paths: list[str] = Field(default_factory=list)
    kg_paths: list[str] = Field(default_factory=list)
    reference_resolutions: list[ReferenceResolution] = Field(default_factory=list)
    template_examples: list[XmlTemplateExample] = Field(default_factory=list)
    template_contract: XmlTemplateClassContract | None = None
    base_workflow_node: BaseWorkflowNodeReference | None = None
    allowed_arg_names: list[str] = Field(default_factory=list)
    required_arg_names: list[str] = Field(default_factory=list)
    high_risk_arg_names: list[str] = Field(
        default_factory=lambda: ["EcuBOMName", "DID", "RID", "ServiceID", "RequestParameter"]
    )
    prompt_budget_report: dict[str, Any] = Field(default_factory=dict)


class LlmXmlRepairAttempt(BaseModel):
    """One repair prompt attempt and its validation result."""

    attempt: int
    raw_response: str = ""
    validation_errors: list[ValidationIssue] = Field(default_factory=list)


class LlmGuardrailCorrection(BaseModel):
    """One deterministic correction applied after the raw LLM XML plan."""

    node_name: str = ""
    arg_name: str
    raw_value: str | None = None
    corrected_value: str | None = None
    raw_evidence_ids: list[str] = Field(default_factory=list)
    corrected_evidence_ids: list[str] = Field(default_factory=list)
    correction_reason: str = ""
    correction_source: str = ""


class LlmNodeXmlGeneration(BaseModel):
    """LLM output plus deterministic validation and trace metadata."""

    step_key: str
    order: int = 0
    row: int = 0
    column: int = 0
    node_name: str
    class_name: str
    template_name: str = ""
    args: list[XmlArg] = Field(default_factory=list)
    xml: str = ""
    evidence_map: dict[str, list[str]] = Field(default_factory=dict)
    arg_evidence_map: dict[str, list[str]] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review: bool = False
    missing_or_conflict_reason: str = ""
    prompt_context_ids: list[str] = Field(default_factory=list)
    template_examples: list[str] = Field(default_factory=list)
    llm_raw_response: str = ""
    repair_attempts: list[LlmXmlRepairAttempt] = Field(default_factory=list)
    xml_validation_errors: list[ValidationIssue] = Field(default_factory=list)
    valid: bool = False
    script: XmlScriptNodePlan | None = None
    raw_llm_args: list[XmlArg] = Field(default_factory=list)
    post_guardrail_args: list[XmlArg] = Field(default_factory=list)
    guardrail_corrections: list[LlmGuardrailCorrection] = Field(default_factory=list)
    prompt_estimated_tokens: int = 0
    output_estimated_tokens: int = 0
    generation_latency_seconds: float = 0.0
    repair_latency_seconds: float = 0.0


class LlmXmlGenerationTrace(BaseModel):
    """Trace artifact for all operation-level LLM XML generations."""

    source_flow_path: str = ""
    model: str = ""
    generations: list[LlmNodeXmlGeneration] = Field(default_factory=list)
    contexts: list[XmlGenerationContext] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class XmlPlanValidationRequest(BaseModel):
    """Request for validating XML intermediate DSL before rendering."""

    plan: XmlGenerationPlan
    required_arg_names: list[str] = Field(default_factory=list)
    require_arg_evidence: bool = False
    min_script_nodes: int = Field(default=0, ge=0)


class WorkflowFusionRequest(BaseModel):
    """Request for fusing an XML plan into a base workflow XML."""

    plan: XmlGenerationPlan
    base_xml: str | None = None
    base_xml_path: str | None = None
    output_path: str | None = None
    target_serial_name: str | None = None
    arg_merge_strategy: WorkflowArgMergeStrategy = "fill_missing"
    update_class_name: bool = False
    append_missing_args: bool = False
    insert_missing_nodes: bool = False


class WorkflowFusionResult(BaseModel):
    """Result of fusing XML plan into workflow XML."""

    xml: str
    root_tag: str
    matched_nodes: int
    updated_nodes: int
    inserted_nodes: int
    missing_nodes: list[str] = Field(default_factory=list)
    updated_args: int = 0
    appended_args: int = 0
    output_path: str | None = None


class WorkflowAuditRequest(BaseModel):
    """Request for auditing XML plan values against base and fused workflow XML."""

    plan: XmlGenerationPlan
    base_xml: str | None = None
    base_xml_path: str | None = None
    fused_xml: str | None = None
    fused_xml_path: str | None = None
    output_path: str | None = None
    required_arg_names: list[str] = Field(default_factory=list)
    include_base_only_args: bool = True


class WorkflowArgAuditItem(BaseModel):
    """Audit row for one ScriptNode Arg."""

    node_name: str
    arg_name: str
    status: WorkflowArgAuditStatus
    plan_value: str | None = None
    base_value: str | None = None
    fused_value: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    requires_review: bool = False
    notes: list[str] = Field(default_factory=list)


class WorkflowNodeAudit(BaseModel):
    """Audit group for one planned ScriptNode."""

    node_name: str
    class_name: str
    matched_in_base: bool
    matched_in_fused: bool
    args: list[WorkflowArgAuditItem] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class WorkflowAuditReport(BaseModel):
    """Workflow parameter audit report."""

    valid: bool
    summary: dict[str, int | str | dict[str, int]] = Field(default_factory=dict)
    nodes: list[WorkflowNodeAudit] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    output_path: str | None = None


class WorkflowResolveAuditRequest(BaseModel):
    """Request for resolving workflow audit findings with template contracts."""

    audit_report: WorkflowAuditReport | None = None
    audit_report_path: str | None = None
    contract_registry: XmlTemplateContractRegistry | None = None
    contract_registry_path: str | None = None
    output_path: str | None = None


class WorkflowResolveDecision(BaseModel):
    """Resolution decision for one audited workflow argument."""

    node_name: str
    class_name: str
    arg_name: str
    audit_status: WorkflowArgAuditStatus
    action: WorkflowResolveAction
    requires_review: bool
    reason: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    allowed_by_contract: bool = False
    required_by_contract: bool = False
    plan_value: str | None = None
    base_value: str | None = None
    fused_value: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class WorkflowResolveAuditReport(BaseModel):
    """Resolved workflow audit report."""

    valid: bool
    summary: dict[str, int | str | dict[str, int]] = Field(default_factory=dict)
    decisions: list[WorkflowResolveDecision] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    output_path: str | None = None


class WorkflowLlmResolveAuditRequest(BaseModel):
    """Request for resolving review-required audit decisions with vLLM."""

    resolution_report: WorkflowResolveAuditReport | None = None
    resolution_report_path: str | None = None
    vllm_config: VllmModelConfig = Field(default_factory=VllmModelConfig)
    output_path: str | None = None
    max_items: int = Field(default=20, ge=1, le=200)
    request_profile: str = "short_audit"
    enable_router: bool = False
    router_short_base_urls: list[str] = Field(default_factory=list)
    router_long_base_url: str | None = None
    router_extended_base_url: str | None = None
    router_extreme_base_url: str | None = None
    evidence_contexts: dict[str, str] = Field(default_factory=dict)
    prompt_budget_tokens: int = Field(default=4096, ge=512, le=65536)
    prompt_reserved_output_tokens: int = Field(default=512, ge=1, le=8192)


class WorkflowLlmResolveAuditReport(BaseModel):
    """vLLM-assisted workflow audit resolution report."""

    valid: bool
    summary: dict[str, int | str | dict[str, int]] = Field(default_factory=dict)
    decisions: list[WorkflowResolveDecision] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    raw_responses: dict[str, str] = Field(default_factory=dict)
    prompt_budget_reports: dict[str, dict] = Field(default_factory=dict)
    output_path: str | None = None


class ReplayWorkloadItem(BaseModel):
    """One replayable LLM request sampled from real workload artifacts."""

    item_id: str
    profile: Literal[
        "short_audit",
        "rerank",
        "long_context",
        "repair",
        "xml_generation",
        "xml_repair",
        "long_context_xml_generation",
        "default",
    ] = "default"
    prompt: str
    max_tokens: int = Field(default=256, ge=1, le=8192)
    min_tokens: int = Field(default=0, ge=0, le=8192)
    cache_mode: Literal["warm", "cold"] = "warm"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, MetadataValue] = Field(default_factory=dict)


class ReplayWorkloadDataset(BaseModel):
    """A replay dataset derived from real pipeline outputs."""

    workload: str
    source_manifest: str | None = None
    generated_at: str = ""
    items: list[ReplayWorkloadItem] = Field(default_factory=list)
    summary: dict[str, int | float | str | dict[str, int]] = Field(default_factory=dict)


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
