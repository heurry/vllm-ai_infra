"""Deterministic Graph RAG retrieval for diagnostic flow tasks."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import re
from pathlib import Path

from diagnostic_platform.schemas import EvidenceUnit, FlowNode, TaskEvidenceCandidate


FLOWCHART_TITLE_RE = re.compile(
    r"\b(?:(?:IO|Routine)\s+Control\s+Type\s*\d+\s*\([^)]*\)"
    r"|DTC\s+(?:Clear|Read)\s+Type\s*\d+\s*\([^)]*\)"
    r"|Information\s+(?:Write|Check(?:\s+And\s+Record)?)\s+(?:flow\s+)?Type\s*\d+\s*\([^)]*\)"
    r"|Security\s+Access\s+Type\s*\d+\s*\([^)]*\))",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"0x[0-9a-fA-F]+|[A-Za-z]+[A-Za-z0-9]*|[0-9a-fA-F]{2,8}")
ECU_KEYS = {"ADCU", "ASDM", "BCM", "CMD", "CMP", "ECM", "FAH", "HCML", "HCMR", "TCAM", "WPC", "ZCUD"}


def load_markdown_graph_evidence_units(markdown_dir: Path) -> list[EvidenceUnit]:
    """Build section/table-row evidence units from MinerU Markdown files."""

    root = markdown_dir.expanduser()
    if not root.exists():
        return []

    evidence_units: list[EvidenceUnit] = []
    for markdown_path in sorted(root.rglob("*.md")):
        if _is_generated_report(markdown_path):
            continue
        evidence_units.extend(_evidence_from_markdown(markdown_path))
    return evidence_units


def retrieve_task_evidence_candidates(
    node: FlowNode,
    evidence_units: list[EvidenceUnit],
    top_k: int,
) -> list[TaskEvidenceCandidate]:
    """Return graph-ranked parameter evidence for one flow task."""

    aliases = _aliases_for_node(node)
    node_ecu = _primary_ecu_key(node.name, node.template_name)
    candidates: list[TaskEvidenceCandidate] = []

    for evidence in evidence_units:
        role = _block_role(evidence)
        if role == "flowchart_body":
            continue

        fields = extract_parameter_fields(evidence, aliases)
        score, matched_terms = _score_evidence(node, evidence, aliases, node_ecu, role, fields)
        if score <= 0:
            continue

        flowchart_title = _flowchart_title(evidence)
        graph_paths = _candidate_paths(node=node, evidence=evidence, aliases=matched_terms, fields=fields, flowchart_title=flowchart_title)
        candidates.append(
            TaskEvidenceCandidate(
                evidence=evidence,
                score=round(score, 6),
                matched_terms=matched_terms,
                graph_paths=graph_paths,
                block_role=role,
                parameter_fields=fields,
                flowchart_title=flowchart_title,
                template_match=node.template_name,
            )
        )

    candidates.sort(key=lambda item: (-item.score, _role_rank(item.block_role), item.evidence.doc_id, item.evidence.page_idx, item.evidence.evidence_id))
    return candidates[:top_k]


def extract_parameter_fields(evidence: EvidenceUnit, aliases: list[str] | None = None) -> dict[str, str]:
    """Extract structured parameter fields from table-row or section evidence."""

    metadata_fields = evidence.metadata.get("table_fields")
    if isinstance(metadata_fields, dict):
        fields = {str(key): str(value) for key, value in metadata_fields.items() if str(value).strip()}
        flowchart_title = _flowchart_title(evidence)
        if flowchart_title:
            fields["FLOWCHART_TITLE"] = flowchart_title
        reference_text = _reference_text(evidence)
        if reference_text:
            fields["REFERENCE_TEXT"] = reference_text
        return fields

    fields: dict[str, str] = {}
    rows = _html_tables(evidence.content)
    if rows:
        aliases = aliases or []
        fields.update(_best_table_fields(rows, aliases))

    text = _clean_html(evidence.content)
    if "DID" not in fields:
        match = re.search(r"\bDID\b[:\s]*((?:0x)?[0-9A-Fa-f]{4,6})", text, flags=re.IGNORECASE)
        if match:
            fields["DID"] = match.group(1)
    if "RID" not in fields:
        match = re.search(r"\bRID\b[:\s]*((?:0x)?[0-9A-Fa-f]{4,8})", text, flags=re.IGNORECASE)
        if match:
            fields["RID"] = match.group(1)
    if "REQUEST PARAMETER" not in fields:
        match = re.search(
            r"\bREQUEST\s+PARAMETER\b[:\s]*((?:0x)?[0-9A-Fa-f]{2,8}|NA)\b",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            fields["REQUEST PARAMETER"] = match.group(1)
    if "PARAMETER" not in fields:
        match = re.search(r"\bPARAMETER\b[:\s]*((?:0x)?[0-9A-Fa-f]{2,8}|NA)\b", text, flags=re.IGNORECASE)
        if match:
            fields["PARAMETER"] = match.group(1)
    if "DTC" not in fields:
        match = re.search(r"\bDTC\b[:\s]*((?:0x)?[0-9A-Fa-f]{4,8})\b", text, flags=re.IGNORECASE)
        if match:
            fields["DTC"] = match.group(1)

    flowchart_title = _extract_flowchart_title(text)
    if flowchart_title:
        fields["FLOWCHART_TITLE"] = flowchart_title
    reference_text = _extract_reference_text(text)
    if reference_text:
        fields["REFERENCE_TEXT"] = reference_text
    return fields


def task_aliases(node: FlowNode) -> list[str]:
    """Public helper for tests and XML argument extraction."""

    return _aliases_for_node(node)


def _evidence_from_markdown(markdown_path: Path) -> list[EvidenceUnit]:
    text = markdown_path.read_text(encoding="utf-8")
    doc_id = _safe_id(markdown_path.stem)
    metadata = _doc_metadata(markdown_path, doc_id)
    evidence: list[EvidenceUnit] = []

    for section_index, section in enumerate(_sections(text), start=1):
        content = section["content"].strip()
        if not content:
            continue
        role = "flowchart_title" if _is_flowchart_title(section["title"]) else "section_text"
        evidence_content = _flowchart_section_content(section) if role == "flowchart_title" else content
        reference_text = _extract_reference_text(content)
        evidence_id = f"{doc_id}_sec_{section_index:04d}"
        evidence.append(
            EvidenceUnit(
                evidence_id=evidence_id,
                evidence_type="flowchart" if role == "flowchart_title" else "text",
                content=evidence_content,
                doc_id=doc_id,
                source_path=str(markdown_path),
                page_idx=0,
                metadata={
                    **metadata,
                    "block_role": role,
                    "section_title": section["title"],
                    "line_start": str(section["line_start"]),
                    "line_end": str(section["line_end"]),
                    "reference_text": reference_text,
                    "flowchart_body_ocr_ignored": str(role == "flowchart_title"),
                },
            )
        )
        if role != "flowchart_title" and not _looks_like_flowchart_body(content):
            evidence.extend(_table_row_evidence(doc_id, markdown_path, metadata, section, evidence_id))
    return evidence


def _table_row_evidence(
    doc_id: str,
    markdown_path: Path,
    metadata: dict[str, str],
    section: dict[str, str | int],
    parent_evidence_id: str,
) -> list[EvidenceUnit]:
    units: list[EvidenceUnit] = []
    flowchart_title = _extract_flowchart_title(str(section["content"]))
    reference_text = _extract_reference_text(str(section["content"]))
    for table_index, rows in enumerate(_html_tables(str(section["content"])), start=1):
        if len(rows) < 2:
            continue
        headers = [_normalize_header(cell) for cell in rows[0]]
        if not any(headers):
            continue
        for row_index, row in enumerate(rows[1:], start=1):
            fields = {
                header: row[column].strip()
                for column, header in enumerate(headers)
                if header and column < len(row) and row[column].strip()
            }
            if not fields:
                continue
            content = f"{section['title']}\n" + " ".join(f"{key}: {value}" for key, value in fields.items())
            units.append(
                EvidenceUnit(
                    evidence_id=f"{doc_id}_tbl_{section['line_start']}_{table_index}_{row_index}",
                    evidence_type="table",
                    content=content,
                    doc_id=doc_id,
                    source_path=str(markdown_path),
                    page_idx=0,
                    metadata={
                        **metadata,
                        "block_role": "table_row",
                        "section_title": str(section["title"]),
                        "parent_evidence_id": parent_evidence_id,
                        "table_fields": fields,
                        "flowchart_title": flowchart_title,
                        "reference_text": reference_text,
                    },
                )
            )
    return units


def _sections(text: str) -> list[dict[str, str | int]]:
    sections: list[dict[str, str | int]] = []
    title = "document"
    start_line = 1
    buffer: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        heading = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", line)
        if heading:
            if buffer:
                sections.append(
                    {
                        "title": title,
                        "line_start": start_line,
                        "line_end": line_number - 1,
                        "content": "\n".join(buffer),
                    }
                )
            title = heading.group(1).strip()
            start_line = line_number
            buffer = [line]
            continue
        buffer.append(line)

    if buffer:
        sections.append(
            {
                "title": title,
                "line_start": start_line,
                "line_end": len(text.splitlines()) or start_line,
                "content": "\n".join(buffer),
            }
        )
    return sections


def _score_evidence(
    node: FlowNode,
    evidence: EvidenceUnit,
    aliases: list[str],
    node_ecu: str,
    role: str,
    fields: dict[str, str],
) -> tuple[float, list[str]]:
    if _is_noise_evidence(evidence):
        return 0.0, []

    text = _search_text(evidence, fields)
    text_tokens = set(_tokens(text))
    evidence_ecu = str(evidence.metadata.get("ecu") or "").strip()
    score = 0.0
    matched_terms: list[str] = []
    alias_scores: list[float] = []

    for alias in aliases:
        alias_norm = _normalize_text(alias)
        alias_tokens = set(_tokens(alias))
        if not alias_tokens:
            continue
        overlap = alias_tokens.intersection(text_tokens)
        if alias_norm and alias_norm in _normalize_text(text):
            alias_scores.append(14.0)
            _append_unique(matched_terms, alias_norm)
            continue
        ratio = len(overlap) / len(alias_tokens)
        if ratio >= 0.58:
            alias_scores.append(4.0 + 6.0 * ratio + len(overlap))
            _append_unique(matched_terms, alias_norm)

    score += sum(sorted(alias_scores, reverse=True)[:3])

    if node_ecu and evidence_ecu:
        if _ecu_matches_node(evidence_ecu, node_ecu):
            score += 16.0
            _append_unique(matched_terms, node_ecu.lower())
        elif evidence.metadata.get("module") == "ecu_parameter":
            score -= 35.0
    elif node_ecu and _normalize_token(node_ecu) in text_tokens:
        score += 8.0
        _append_unique(matched_terms, node_ecu.lower())

    section_title = str(evidence.metadata.get("section_title") or "")
    if section_title and any(_token_overlap(alias, section_title) >= 0.58 for alias in aliases):
        score += 5.0

    if role == "table_row":
        score += 3.0
    elif role == "section_text":
        score += 2.0
    elif role == "flowchart_title":
        score += 1.0

    if evidence.metadata.get("module") == "ecu_parameter":
        score += 2.0
    if fields:
        score += 2.0
    if _has_core_parameter_fields(fields):
        score += 25.0
    elif role in {"section_text", "table_row"} and not _flowchart_title(evidence):
        score -= 12.0
    flowchart_title = _flowchart_title(evidence)
    if flowchart_title:
        if _flowchart_matches_template(node.template_name, flowchart_title):
            score += 8.0
        elif _template_requires_flowchart_family(node.template_name):
            score -= 35.0
        else:
            score += 1.0
    if node.template_name == "GEEA30_VMM_Change":
        if "type21" in _normalize_text(flowchart_title):
            score += 6.0
        if "type22" in _normalize_text(flowchart_title) or "ignore nrc" in _normalize_text(text):
            score -= 6.0
    if node.template_name and _token_overlap(_template_alias(node.template_name), text) >= 0.58:
        score += 3.0

    return score, matched_terms


def _candidate_paths(
    node: FlowNode,
    evidence: EvidenceUnit,
    aliases: list[str],
    fields: dict[str, str],
    flowchart_title: str,
) -> list[str]:
    source = f"FlowTask:{node.name}"
    target = _role_entity_id(evidence)
    paths = [
        f"{source} --uses_template--> TemplateClass:{node.template_name}",
        f"{source} --supported_by--> {target}",
    ]
    for alias in aliases[:3]:
        paths.append(f"{source} --matches_alias--> AliasTerm:{alias} --supported_by--> {target}")
    field_relation = "row_has_field" if _block_role(evidence) == "table_row" else "section_has_field"
    for key, value in list(fields.items())[:5]:
        paths.append(f"{target} --{field_relation}--> ParameterField:{key}={value}")
    if flowchart_title:
        paths.append(f"{target} --section_refers_flowchart--> FlowchartTitle:{flowchart_title}")
    return _dedupe(paths)


def _role_entity_id(evidence: EvidenceUnit) -> str:
    role = _block_role(evidence)
    if role == "table_row":
        return f"TableRow:{evidence.evidence_id}"
    if role == "flowchart_title":
        return f"FlowchartTitle:{evidence.metadata.get('section_title') or evidence.evidence_id}"
    if role == "flowchart_image":
        return f"FlowchartImage:{evidence.evidence_id}"
    return f"Section:{evidence.evidence_id}"


def _search_text(evidence: EvidenceUnit, fields: dict[str, str]) -> str:
    metadata_parts = [
        str(evidence.metadata.get("section_title") or ""),
        str(evidence.metadata.get("ecu") or ""),
        str(evidence.metadata.get("module") or ""),
        " ".join(f"{key} {value}" for key, value in fields.items()),
    ]
    return " ".join([evidence.content, *metadata_parts])


def _aliases_for_node(node: FlowNode) -> list[str]:
    action = _action_text(node.name)
    template_alias = _template_alias(node.template_name)
    aliases = [
        node.name,
        node.template_name,
        node.raw,
        action,
        template_alias,
        f"{template_alias} {action}",
        f"{action} {template_alias}",
    ]

    lowered = _normalize_text(" ".join(aliases))
    if "vmm" in lowered:
        aliases.extend(["Change VMM", "VMM Change", "Change VMM Type1"])
    if "dtc clear" in lowered:
        aliases.extend(["DTC Clear", node.template_name.replace("_", " ")])
    if "dtc read" in lowered:
        aliases.extend(["DTC Read", node.template_name.replace("_", " "), "Read Snapshot", "No Snapshot"])
    if "immo" in lowered or "immobilizer" in lowered or "secret key" in lowered:
        aliases.extend(
            [
                "IMMO Secret Key Write",
                "Secret Key for Immobilizer Target Write",
                "Secret Key for Immobilizer",
                "Immobilizer Secret Key",
                "TCAM IMMO Secret Key Write Check and Upload",
                "Remote Vehicle Immobilization Secret Key",
            ]
        )
    if "request open hv battery" in lowered:
        aliases.extend(["Request Open HV Battery", "Open HV Battery", "Request BECM to Open HV Battery"])
    return [alias for alias in _dedupe([_clean_alias(alias) for alias in aliases]) if alias]


def _template_alias(template_name: str) -> str:
    normalized = template_name.replace("_", " ").strip()
    normalized = re.sub(r"\bGEEA\s*30\b", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bGEEA30\b", "", normalized, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", normalized).strip()


def _action_text(node_name: str) -> str:
    value = re.sub(r"_\d+$", "", node_name)
    parts = value.split("_")
    if len(parts) > 1 and _looks_like_ecu(parts[0]):
        value = "_".join(parts[1:])
    return value.replace("_", " ")


def _primary_ecu_key(node_name: str, template_name: str) -> str:
    for value in [node_name.split("_", 1)[0], template_name.split("_", 1)[0]]:
        if _looks_like_ecu(value):
            return value.upper()
    return ""


def _looks_like_ecu(value: str) -> bool:
    upper = value.upper()
    if upper.startswith("GEEA"):
        return False
    return upper in ECU_KEYS or any(upper.startswith(prefix) for prefix in ECU_KEYS)


def _ecu_matches_node(evidence_ecu: str, node_ecu: str) -> bool:
    evidence = evidence_ecu.upper()
    node = node_ecu.upper()
    if not evidence or not node:
        return False
    if evidence == node or evidence.startswith(node):
        return True
    return _normalize_token(node) in set(_tokens(evidence))


def _block_role(evidence: EvidenceUnit) -> str:
    role = str(evidence.metadata.get("block_role") or "")
    if role:
        return role
    if _looks_like_flowchart_body(evidence.content):
        return "flowchart_body"
    if evidence.evidence_type == "flowchart":
        return "flowchart_title"
    if evidence.evidence_type == "table":
        return "table_row" if evidence.metadata.get("table_fields") else "table"
    return "section_text"


def _flowchart_title(evidence: EvidenceUnit) -> str:
    metadata_title = str(evidence.metadata.get("flowchart_title") or "")
    if metadata_title:
        return metadata_title
    return _extract_flowchart_title(evidence.content)


def _flowchart_matches_template(template_name: str, flowchart_title: str) -> bool:
    template = _normalize_template_name(template_name)
    title = _normalize_text(flowchart_title)
    if not template or not title:
        return False
    for template_key, parts in _FLOWCHART_TEMPLATE_FAMILIES.items():
        if template_key in template:
            return all(part in title for part in parts)
    if "GEEA30_VMM_CHANGE" in template:
        return "io control" in title and "type21" in title
    return True


def _template_requires_flowchart_family(template_name: str) -> bool:
    template = _normalize_template_name(template_name)
    return any(template_key in template for template_key in _FLOWCHART_TEMPLATE_FAMILIES) or "GEEA30_VMM_CHANGE" in template


def _normalize_template_name(template_name: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", template_name).strip("_").upper()


_FLOWCHART_TEMPLATE_FAMILIES = {
    "DTC_READ_TYPE1": ("dtc read", "type1"),
    "DTC_READ_TYPE2": ("dtc read", "type2"),
    "DTC_CLEAR_TYPE2": ("dtc clear", "type2"),
    "DTC_CLEAR_TYPE7": ("dtc clear", "type7"),
    "INFORMATION_WRITE_TYPE3": ("information write", "type3"),
}


def _reference_text(evidence: EvidenceUnit) -> str:
    metadata_reference = str(evidence.metadata.get("reference_text") or "")
    if metadata_reference:
        return metadata_reference
    return _extract_reference_text(evidence.content)


def _extract_flowchart_title(text: str) -> str:
    match = FLOWCHART_TITLE_RE.search(_clean_html(text))
    if not match:
        return ""
    title = re.sub(r"\s+", " ", match.group(0)).strip()
    title = re.sub(r"\bLevel\s+(\d+)", r"Level\1", title, flags=re.IGNORECASE)
    title = re.sub(r"Type\s+(\d+)", r"Type\1", title, flags=re.IGNORECASE)
    return title


def _extract_reference_text(text: str) -> str:
    cleaned = _clean_html(text)
    candidates: list[str] = []
    patterns = [
        r"\b(?:refer|refers|reference)\s+to\s+(?:the\s+)?(.{3,180}?)(?:[.;]|\n|$)",
        r"\b(?:detail|details)\s+(?:can\s+)?refer\s+to\s+(?:the\s+)?(.{3,180}?)(?:[.;]|\n|$)",
        r"\bcan\s+refer\s+to\s+(?:the\s+)?(.{3,180}?)(?:[.;]|\n|$)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, cleaned, flags=re.IGNORECASE):
            value = _clean_reference(match.group(1))
            if _looks_like_useful_reference(value):
                candidates.append(value)
    return _dedupe(candidates)[0] if candidates else ""


def _clean_reference(value: str) -> str:
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" :：,，-")
    value = re.sub(r"^(?:file|chapter)\s+", "", value, flags=re.IGNORECASE)
    return value[:160].strip()


def _looks_like_useful_reference(value: str) -> bool:
    normalized = _normalize_text(value)
    if len(normalized) < 3:
        return False
    signals = {
        "eol",
        "process",
        "flow",
        "control",
        "routine",
        "dtc",
        "did",
        "write",
        "read",
        "security",
        "type",
        "ecu",
        "treg",
        "parameter",
        "list",
    }
    return bool(signals.intersection(_tokens(value)))


def _is_flowchart_title(title: str) -> bool:
    return bool(FLOWCHART_TITLE_RE.search(title))


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


def _flowchart_section_content(section: dict[str, str | int]) -> str:
    title = str(section["title"])
    image_refs = re.findall(r"!\[[^\]]*\]\([^)]+\)", str(section["content"]))
    lines = [f"# {title}"]
    if image_refs:
        lines.extend(_dedupe(image_refs))
    return "\n".join(lines)


def _is_noise_evidence(evidence: EvidenceUnit) -> bool:
    title = str(evidence.metadata.get("section_title") or "").strip().lower()
    if title in {"document", "contents"}:
        return True
    if any(term in title for term in ["revision history", "requirements change log", "abbreviation list"]):
        return True
    content = _clean_html(evidence.content).lower()
    if "revision date description author" in content:
        return True
    if "requirements change log" in content or "new requirements in this issue" in content:
        return True
    if "document release status" in content and "document no" in content:
        return True
    if "document release status" in content and "page no" in content:
        return True
    return False


def _has_core_parameter_fields(fields: dict[str, str]) -> bool:
    core_keys = {
        "DID",
        "RID",
        "DTC",
        "REQUEST PARAMETER",
        "PARAMETER",
        "SOURCE",
        "TARGET",
        "READ DID",
        "DID FOR IO CONTROL RESULT CHECK",
        "EXPECTED VALUE",
    }
    normalized_keys = {_normalize_header(key) for key in fields}
    return bool(core_keys.intersection(normalized_keys))


def _html_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for match in re.finditer(r"<table\b.*?</table>", text, flags=re.IGNORECASE | re.DOTALL):
        parser = _TableParser()
        parser.feed(match.group(0))
        rows = [[_clean_cell(cell) for cell in row] for row in parser.rows if any(_clean_cell(cell) for cell in row)]
        if rows:
            tables.append(rows)
    return tables


def _best_table_fields(tables: list[list[list[str]]], aliases: list[str]) -> dict[str, str]:
    best_score = -1.0
    best: dict[str, str] = {}
    for rows in tables:
        if len(rows) < 2:
            continue
        headers = [_normalize_header(cell) for cell in rows[0]]
        for row in rows[1:]:
            fields = {
                header: row[index].strip()
                for index, header in enumerate(headers)
                if header and index < len(row) and row[index].strip()
            }
            if not fields:
                continue
            row_text = " ".join(fields.values())
            score = max((_token_overlap(alias, row_text) for alias in aliases), default=0.0)
            if score > best_score:
                best_score = score
                best = fields
    return best


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


def _doc_metadata(path: Path, doc_id: str) -> dict[str, str]:
    return {
        "doc_type": "pdf_protocol",
        "protocol": "UDS",
        "module": _infer_module(path.name),
        "ecu": _infer_ecu(path.name),
        "source": "mineru_markdown",
        "doc_id": doc_id,
    }


def _infer_module(doc_name: str) -> str:
    normalized = doc_name.lower().replace("_", " ")
    if "process-test sequence" in normalized or "process test sequence" in normalized:
        return "eol_process"
    if "general eol test" in normalized:
        return "general_eol_test"
    if "parameter" in normalized:
        return "ecu_parameter"
    return "treg"


def _infer_ecu(doc_name: str) -> str:
    normalized = doc_name.replace(" ", "_")
    match = re.search(r"TREG[-_](.+?)[-_]Parameter", normalized, re.IGNORECASE)
    return match.group(1).strip("_- ") if match else ""


def _is_generated_report(path: Path) -> bool:
    return "markdown_flow_trace_output" in path.as_posix()


def _clean_alias(value: str) -> str:
    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _normalize_text(value: str) -> str:
    return " ".join(_tokens(value))


def _tokens(value: str) -> list[str]:
    return [_normalize_token(match.group(0)) for match in TOKEN_RE.finditer(value) if _normalize_token(match.group(0))]


def _normalize_token(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    if value == "immo":
        return "immobilizer"
    return value


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(_tokens(left))
    if not left_tokens:
        return 0.0
    return len(left_tokens.intersection(_tokens(right))) / len(left_tokens)


def _normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", _clean_cell(value)).strip().upper()


def _clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _clean_html(value: str) -> str:
    value = unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _safe_id(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_.-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value)
    return value.strip("_") or "document"


def _role_rank(role: str) -> int:
    return {"table_row": 0, "section_text": 1, "flowchart_title": 2, "flowchart_image": 3}.get(role, 9)


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
