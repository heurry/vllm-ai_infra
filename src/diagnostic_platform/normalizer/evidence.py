"""Normalize parsed document blocks into traceable evidence units."""

from __future__ import annotations

import re

from diagnostic_platform.normalizer.mineru import (
    _block_content,
    _extract_dids,
    _extract_security_levels,
    _extract_service_ids,
    _extract_sessions,
)
from diagnostic_platform.schemas import EvidenceType, EvidenceUnit, MinerUContentBlock, NormalizeMinerURequest


def evidence_from_mineru(request: NormalizeMinerURequest) -> list[EvidenceUnit]:
    """Convert MinerU content blocks into evidence units."""

    evidence_units: list[EvidenceUnit] = []
    for index, block in enumerate(request.blocks, start=1):
        content = _evidence_content(block)
        if not content:
            continue

        evidence_units.append(
            EvidenceUnit(
                evidence_id=f"{request.metadata.doc_id}_ev_{index:04d}",
                evidence_type=_evidence_type(block, content),
                content=content,
                doc_id=request.metadata.doc_id,
                source_path=request.metadata.source_path,
                page_idx=block.page_idx,
                bbox=block.bbox,
                metadata={
                    "doc_type": request.metadata.doc_type,
                    "protocol": request.metadata.protocol,
                    "module": request.metadata.module or "",
                    "ecu": request.metadata.ecu or "",
                    "source": request.metadata.source or "",
                    "block_role": _block_role(block, content),
                    "img_path": block.img_path or "",
                    "text_level": block.text_level or 0,
                    "service_ids": _extract_service_ids(content),
                    "dids": _extract_dids(content),
                    "sessions": _extract_sessions(content),
                    "security_levels": _extract_security_levels(content),
                },
            )
        )
    return evidence_units


def _evidence_content(block: MinerUContentBlock) -> str:
    content = _block_content(block)
    if content:
        return content
    if block.type == "image" and block.img_path:
        return block.img_path
    return ""


def _evidence_type(block: MinerUContentBlock, content: str) -> EvidenceType:
    if block.type == "text":
        return "text"
    if block.type == "table":
        return "table"
    if block.type == "equation":
        return "equation"
    if block.type == "image":
        image_text = " ".join([content, block.img_path or ""])
        if re.search(r"\b(flow|process|sequence|type\s*\d+|io\s*control|dtc)\b|流程|跳转|分支", image_text, re.I):
            return "flowchart"
        return "image"
    return "text"


def _block_role(block: MinerUContentBlock, content: str) -> str:
    """Classify blocks for downstream Graph RAG without changing old fields."""

    if _looks_like_flowchart_body(content):
        return "flowchart_body"
    if block.type == "image" and _evidence_type(block, content) == "flowchart":
        return "flowchart_image"
    if block.type == "table":
        return "table"
    return "section_text"


def _looks_like_flowchart_body(content: str) -> bool:
    lowered = content.lower()
    signals = [
        r"\breq\s*:",
        r"\bres\s*:",
        "positive response",
        "positive respone",
        "negative response",
        "time out",
        "failed",
        "routine aborted",
    ]
    return sum(1 for pattern in signals if re.search(pattern, lowered)) >= 3
