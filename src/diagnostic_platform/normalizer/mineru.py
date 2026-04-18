"""Convert MinerU output into project-specific knowledge units."""

from __future__ import annotations

import re

from diagnostic_platform.schemas import (
    KnowledgeUnit,
    MinerUContentBlock,
    NormalizeMinerURequest,
)

UDS_SERVICE_IDS = {
    "10",
    "11",
    "14",
    "19",
    "22",
    "27",
    "28",
    "2E",
    "2F",
    "31",
    "34",
    "36",
    "37",
    "3E",
    "85",
}


def normalize_request(request: NormalizeMinerURequest) -> list[KnowledgeUnit]:
    """Normalize MinerU blocks into knowledge units."""

    units: list[KnowledgeUnit] = []
    for index, block in enumerate(request.blocks, start=1):
        content = _block_content(block)
        if not content:
            continue

        units.append(
            KnowledgeUnit(
                chunk_id=f"{request.metadata.doc_id}_{index:04d}",
                unit_type=_map_block_type(block),
                content=content,
                protocol=request.metadata.protocol,
                module=request.metadata.module,
                ecu=request.metadata.ecu,
                service_ids=_extract_service_ids(content),
                dids=_extract_dids(content),
                sessions=_extract_sessions(content),
                security_levels=_extract_security_levels(content),
                page_idx=block.page_idx,
                bbox=block.bbox,
                source_path=request.metadata.source_path,
            )
        )
    return units


def _map_block_type(block: MinerUContentBlock) -> str:
    mapping = {
        "text": "text_chunk",
        "table": "table_chunk",
        "image": "image_region",
        "equation": "equation_chunk",
    }
    return mapping[block.type]


def _block_content(block: MinerUContentBlock) -> str:
    if block.type in {"text", "equation"}:
        return (block.text or "").strip()
    if block.type == "table":
        return " ".join(block.table_caption + block.table_footnote).strip()
    if block.type == "image":
        return " ".join(block.image_caption + block.image_footnote).strip()
    return ""


def _extract_service_ids(text: str) -> list[str]:
    matches: list[str] = []
    for token in re.findall(r"\b(?:0x)?([0-9A-Fa-f]{2})\b", text):
        value = token.upper()
        if value in UDS_SERVICE_IDS and value not in matches:
            matches.append(value)
    return matches


def _extract_dids(text: str) -> list[str]:
    dids: list[str] = []
    for token in re.findall(r"\b(?:DID[:\s]*)?([0-9A-Fa-f]{4})\b", text, re.IGNORECASE):
        value = token.upper()
        if value not in dids:
            dids.append(value)
    return dids


def _extract_sessions(text: str) -> list[str]:
    lowered = text.lower()
    sessions: list[str] = []
    keywords = {
        "default session": "01",
        "programming session": "02",
        "extended session": "03",
    }
    for phrase, code in keywords.items():
        if phrase in lowered and code not in sessions:
            sessions.append(code)
    return sessions


def _extract_security_levels(text: str) -> list[str]:
    levels: list[str] = []
    for raw in re.findall(r"(?:security\s*access|level)\s*0?(\d{1,2})", text, re.IGNORECASE):
        value = f"{int(raw):02d}"
        if value not in levels:
            levels.append(value)
    return levels

