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
]
Severity = Literal["error", "warning"]


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

