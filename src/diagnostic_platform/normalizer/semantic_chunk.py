"""Build semantic RAG chunks while preserving MinerU layout provenance."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import json
import re

from diagnostic_platform.normalizer.mineru import (
    _block_content,
    _extract_dids,
    _extract_security_levels,
    _extract_service_ids,
    _extract_sessions,
)
from diagnostic_platform.schemas import (
    EvidenceUnit,
    KnowledgeUnit,
    MinerUContentBlock,
    NormalizeMinerURequest,
    SemanticChunk,
    SourceBlockRef,
)


TEXT_VERTICAL_GAP_THRESHOLD = 28


def semantic_chunks_from_mineru(request: NormalizeMinerURequest) -> list[SemanticChunk]:
    """Create semantic chunks from MinerU content_list blocks.

    This keeps MinerU blocks as provenance anchors, but creates RAG-friendly
    paragraph/table-row chunks instead of treating every layout block as final
    retrieval context.
    """

    chunks: list[SemanticChunk] = []
    text_buffer: list[tuple[int, MinerUContentBlock, str]] = []

    def flush_text() -> None:
        nonlocal text_buffer
        if not text_buffer:
            return
        chunks.append(_paragraph_chunk(request, text_buffer, len(chunks) + 1))
        text_buffer = []

    for index, block in enumerate(request.blocks, start=1):
        content = _semantic_block_content(block)
        if not content:
            continue
        if block.type in {"text", "equation"}:
            if _should_start_new_paragraph(text_buffer, block, content):
                flush_text()
            text_buffer.append((index, block, content))
            continue

        flush_text()
        if block.type == "table":
            chunks.extend(_table_chunks(request, index, block, content, len(chunks) + 1))
        elif block.type == "image":
            chunks.append(_image_chunk(request, index, block, content, len(chunks) + 1))

    flush_text()
    return chunks


def semantic_chunks_from_evidence_units(evidence_units: list[EvidenceUnit]) -> list[SemanticChunk]:
    """Convert existing Markdown-derived evidence units into semantic chunks."""

    chunks: list[SemanticChunk] = []
    for index, evidence in enumerate(evidence_units, start=1):
        metadata = evidence.metadata
        block_role = str(metadata.get("block_role") or evidence.evidence_type)
        chunk_type = _chunk_type_from_evidence(evidence)
        bbox = _int_list(metadata.get("bbox_union") or evidence.bbox)
        source_block_ids = _string_list(metadata.get("source_block_ids"))
        chunks.append(
            SemanticChunk(
                chunk_id=f"md_{evidence.evidence_id}",
                chunk_type=chunk_type,
                content=evidence.content,
                doc_id=evidence.doc_id,
                protocol=str(metadata.get("protocol") or "UDS"),
                module=str(metadata.get("module") or ""),
                ecu=str(metadata.get("ecu") or ""),
                source="mineru_markdown",
                source_path=evidence.source_path or "",
                page_idx=evidence.page_idx,
                bbox=bbox,
                source_block_ids=source_block_ids,
                parent_chunk_id=str(metadata.get("parent_evidence_id") or ""),
                section_title=str(metadata.get("section_title") or ""),
                table_fields={str(key): str(value) for key, value in (metadata.get("table_fields") or {}).items()}
                if isinstance(metadata.get("table_fields"), dict)
                else {},
                flowchart_title=str(metadata.get("flowchart_title") or ""),
                reference_text=str(metadata.get("reference_text") or ""),
                img_path=str(metadata.get("img_path") or ""),
                metadata={
                    **metadata,
                    "semantic_chunk_index": index,
                    "source_evidence_id": evidence.evidence_id,
                    "chunk_type": chunk_type,
                    "block_role": block_role,
                },
            )
        )
    return chunks


def knowledge_units_from_semantic_chunks(chunks: list[SemanticChunk]) -> list[KnowledgeUnit]:
    """Convert semantic chunks into retrieval-oriented knowledge units."""

    units: list[KnowledgeUnit] = []
    for chunk in chunks:
        content = chunk.content.strip()
        if not content:
            continue
        units.append(
            KnowledgeUnit(
                chunk_id=chunk.chunk_id,
                unit_type=_knowledge_unit_type(chunk),
                content=content,
                protocol=chunk.protocol,
                module=chunk.module or None,
                ecu=chunk.ecu or None,
                service_ids=_extract_service_ids(content),
                dids=_extract_dids(content),
                sessions=_extract_sessions(content),
                security_levels=_extract_security_levels(content),
                page_idx=chunk.page_idx,
                bbox=chunk.bbox,
                source_path=chunk.source_path,
            )
        )
    return units


def evidence_units_from_semantic_chunks(chunks: list[SemanticChunk]) -> list[EvidenceUnit]:
    """Convert semantic chunks into traceable evidence units."""

    evidence_units: list[EvidenceUnit] = []
    for chunk in chunks:
        content = chunk.content.strip()
        if not content:
            continue
        evidence_units.append(
            EvidenceUnit(
                evidence_id=chunk.chunk_id,
                evidence_type=_evidence_type(chunk),
                content=content,
                doc_id=chunk.doc_id,
                source_path=chunk.source_path,
                page_idx=chunk.page_idx,
                bbox=chunk.bbox,
                metadata={
                    **chunk.metadata,
                    "doc_type": str(chunk.metadata.get("doc_type") or "pdf_protocol"),
                    "protocol": chunk.protocol,
                    "module": chunk.module,
                    "ecu": chunk.ecu,
                    "source": chunk.source,
                    "chunk_type": chunk.chunk_type,
                    "block_role": _block_role_from_chunk(chunk),
                    "source_block_ids": chunk.source_block_ids,
                    "bbox_union": chunk.bbox,
                    "source_blocks_json": json.dumps(
                        [block.model_dump(mode="json") for block in chunk.source_blocks],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "parent_evidence_id": chunk.parent_chunk_id,
                    "section_title": chunk.section_title,
                    "table_fields": chunk.table_fields,
                    "flowchart_title": chunk.flowchart_title,
                    "reference_text": chunk.reference_text,
                    "img_path": chunk.img_path,
                    "service_ids": _extract_service_ids(content),
                    "dids": _extract_dids(content),
                    "sessions": _extract_sessions(content),
                    "security_levels": _extract_security_levels(content),
                },
            )
        )
    return evidence_units


def _paragraph_chunk(
    request: NormalizeMinerURequest,
    items: list[tuple[int, MinerUContentBlock, str]],
    ordinal: int,
) -> SemanticChunk:
    indexes = [index for index, _block, _content in items]
    blocks = [block for _index, block, _content in items]
    content = _clean_text(" ".join(content for _index, _block, content in items))
    source_blocks = [_source_block_ref(request, index, block) for index, block, _content in items]
    return SemanticChunk(
        chunk_id=f"{request.metadata.doc_id}_sem_{ordinal:04d}",
        chunk_type="equation" if all(block.type == "equation" for block in blocks) else "paragraph",
        content=content,
        doc_id=request.metadata.doc_id,
        protocol=request.metadata.protocol,
        module=request.metadata.module or "",
        ecu=request.metadata.ecu or "",
        source="mineru_content_list",
        source_path=request.metadata.source_path or "",
        page_idx=blocks[0].page_idx,
        bbox=_bbox_union([block.bbox for block in blocks]),
        source_block_ids=[_source_block_id(request, index) for index in indexes],
        source_blocks=source_blocks,
        section_title=_infer_section_title(content),
        metadata={
            "doc_type": request.metadata.doc_type,
            "source": request.metadata.source or "mineru",
            "block_role": "paragraph",
            "text_block_count": len(items),
        },
    )


def _table_chunks(
    request: NormalizeMinerURequest,
    block_index: int,
    block: MinerUContentBlock,
    content: str,
    ordinal: int,
) -> list[SemanticChunk]:
    source_block = _source_block_ref(request, block_index, block)
    parent_id = f"{request.metadata.doc_id}_sem_{ordinal:04d}"
    chunks = [
        SemanticChunk(
            chunk_id=parent_id,
            chunk_type="table",
            content=content,
            doc_id=request.metadata.doc_id,
            protocol=request.metadata.protocol,
            module=request.metadata.module or "",
            ecu=request.metadata.ecu or "",
            source="mineru_content_list",
            source_path=request.metadata.source_path or "",
            page_idx=block.page_idx,
            bbox=block.bbox,
            source_block_ids=[source_block.block_id],
            source_blocks=[source_block],
            img_path=block.img_path or "",
            metadata={
                "doc_type": request.metadata.doc_type,
                "source": request.metadata.source or "mineru",
                "block_role": "table",
            },
        )
    ]
    rows = _html_tables(block.table_body or "")
    for table_index, table in enumerate(rows, start=1):
        if len(table) < 2:
            continue
        headers = [_normalize_header(cell) for cell in table[0]]
        for row_index, row in enumerate(table[1:], start=1):
            fields = {
                header: row[column].strip()
                for column, header in enumerate(headers)
                if header and column < len(row) and row[column].strip()
            }
            if not fields:
                continue
            row_content = " ".join(f"{key}: {value}" for key, value in fields.items())
            chunks.append(
                SemanticChunk(
                    chunk_id=f"{request.metadata.doc_id}_sem_{ordinal:04d}_tbl_{table_index}_{row_index}",
                    chunk_type="table_row",
                    content=row_content,
                    doc_id=request.metadata.doc_id,
                    protocol=request.metadata.protocol,
                    module=request.metadata.module or "",
                    ecu=request.metadata.ecu or "",
                    source="mineru_content_list",
                    source_path=request.metadata.source_path or "",
                    page_idx=block.page_idx,
                    bbox=block.bbox,
                    source_block_ids=[source_block.block_id],
                    source_blocks=[source_block],
                    parent_chunk_id=parent_id,
                    table_fields=fields,
                    img_path=block.img_path or "",
                    metadata={
                        "doc_type": request.metadata.doc_type,
                        "source": request.metadata.source or "mineru",
                        "block_role": "table_row",
                        "row_index": row_index,
                        "table_index": table_index,
                    },
                )
            )
    return chunks


def _image_chunk(
    request: NormalizeMinerURequest,
    block_index: int,
    block: MinerUContentBlock,
    content: str,
    ordinal: int,
) -> SemanticChunk:
    chunk_type = "flowchart" if _looks_like_flowchart(content, block.img_path or "") else "image"
    source_block = _source_block_ref(request, block_index, block)
    return SemanticChunk(
        chunk_id=f"{request.metadata.doc_id}_sem_{ordinal:04d}",
        chunk_type=chunk_type,
        content=content,
        doc_id=request.metadata.doc_id,
        protocol=request.metadata.protocol,
        module=request.metadata.module or "",
        ecu=request.metadata.ecu or "",
        source="mineru_content_list",
        source_path=request.metadata.source_path or "",
        page_idx=block.page_idx,
        bbox=block.bbox,
        source_block_ids=[source_block.block_id],
        source_blocks=[source_block],
        flowchart_title=_extract_flowchart_title(content),
        img_path=block.img_path or "",
        metadata={
            "doc_type": request.metadata.doc_type,
            "source": request.metadata.source or "mineru",
            "block_role": "flowchart_image" if chunk_type == "flowchart" else "image",
        },
    )


def _semantic_block_content(block: MinerUContentBlock) -> str:
    content = _block_content(block)
    if content:
        return content
    if block.type == "image" and block.img_path:
        return block.img_path
    return ""


def _should_start_new_paragraph(
    buffer: list[tuple[int, MinerUContentBlock, str]],
    block: MinerUContentBlock,
    content: str,
) -> bool:
    if not buffer:
        return False
    _last_index, last_block, last_content = buffer[-1]
    if block.page_idx != last_block.page_idx:
        return True
    if block.type != last_block.type:
        return True
    if _looks_like_heading(content) and not _looks_like_heading(last_content):
        return True
    if _vertical_gap(last_block.bbox, block.bbox) > TEXT_VERTICAL_GAP_THRESHOLD:
        return True
    return False


def _source_block_ref(request: NormalizeMinerURequest, index: int, block: MinerUContentBlock) -> SourceBlockRef:
    return SourceBlockRef(
        block_id=_source_block_id(request, index),
        block_type=block.type,
        page_idx=block.page_idx,
        bbox=block.bbox,
        img_path=block.img_path or "",
    )


def _source_block_id(request: NormalizeMinerURequest, index: int) -> str:
    return f"{request.metadata.doc_id}_block_{index:04d}"


def _knowledge_unit_type(chunk: SemanticChunk) -> str:
    if chunk.chunk_type in {"table", "table_row"}:
        return "table_chunk"
    if chunk.chunk_type in {"image", "flowchart"}:
        return "image_region"
    if chunk.chunk_type == "equation":
        return "equation_chunk"
    return "text_chunk"


def _evidence_type(chunk: SemanticChunk) -> str:
    if chunk.chunk_type in {"table", "table_row"}:
        return "table"
    if chunk.chunk_type == "flowchart":
        return "flowchart"
    if chunk.chunk_type == "image":
        return "image"
    if chunk.chunk_type == "equation":
        return "equation"
    return "text"


def _block_role_from_chunk(chunk: SemanticChunk) -> str:
    if chunk.chunk_type == "table_row":
        return "table_row"
    if chunk.chunk_type == "table":
        return "table"
    if chunk.chunk_type == "flowchart":
        return "flowchart_image"
    if chunk.chunk_type == "image":
        return "image"
    return "section_text" if chunk.chunk_type == "section" else "paragraph"


def _chunk_type_from_evidence(evidence: EvidenceUnit) -> str:
    role = str(evidence.metadata.get("block_role") or "")
    if role == "table_row":
        return "table_row"
    if role == "flowchart_title" or evidence.evidence_type == "flowchart":
        return "flowchart"
    if evidence.evidence_type == "table":
        return "table"
    if evidence.evidence_type == "image":
        return "image"
    if evidence.evidence_type == "equation":
        return "equation"
    return "section"


def _bbox_union(values: list[list[int]]) -> list[int]:
    boxes = [box for box in values if len(box) >= 4]
    if not boxes:
        return []
    return [
        min(int(box[0]) for box in boxes),
        min(int(box[1]) for box in boxes),
        max(int(box[2]) for box in boxes),
        max(int(box[3]) for box in boxes),
    ]


def _vertical_gap(left: list[int], right: list[int]) -> int:
    if len(left) < 4 or len(right) < 4:
        return 0
    return max(0, int(right[1]) - int(left[3]))


def _looks_like_heading(content: str) -> bool:
    return bool(re.match(r"^\s*(?:#{1,6}\s+)?\d+(?:\.\d+){1,8}\s+\S+", content))


def _infer_section_title(content: str) -> str:
    first = content.strip().splitlines()[0] if content.strip() else ""
    return first[:160] if _looks_like_heading(first) else ""


def _extract_flowchart_title(content: str) -> str:
    match = re.search(
        r"\b(?:(?:IO|Routine)\s+Control\s+Type\s*\d+\s*\([^)]*\)"
        r"|DTC\s+(?:Clear|Read)\s+Type\s*\d+\s*\([^)]*\)"
        r"|Information\s+(?:Write|Check(?:\s+And\s+Record)?)\s+(?:flow\s+)?Type\s*\d+\s*\([^)]*\)"
        r"|Security\s+Access\s+Type\s*\d+\s*\([^)]*\))",
        content,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else ""


def _looks_like_flowchart(content: str, img_path: str) -> bool:
    text = f"{content} {img_path}".lower()
    signals = ["flow", "process", "sequence", "type", "io control", "dtc", "流程", "跳转", "分支"]
    return any(signal in text for signal in signals)


def _clean_text(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", _clean_text(value)).strip().upper()


def _int_list(value) -> list[int]:
    if isinstance(value, list):
        result: list[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result
    return []


def _string_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "tr":
            self._current_row = []
        elif tag.lower() in {"td", "th"}:
            self._current_cell = []

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._current_row is not None and self._current_cell is not None:
            self._current_row.append("".join(self._current_cell))
            self._current_cell = None
        elif tag.lower() == "tr" and self._current_row is not None:
            self.rows.append(self._current_row)
            self._current_row = None


def _html_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for match in re.finditer(r"<table\b.*?</table>", text, flags=re.IGNORECASE | re.DOTALL):
        parser = _TableParser()
        parser.feed(match.group(0))
        rows = [[_clean_text(cell) for cell in row] for row in parser.rows if any(_clean_text(cell) for cell in row)]
        if rows:
            tables.append(rows)
    return tables
