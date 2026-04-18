"""Parse flow.xlsx into a structured flow-step plan."""

from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from diagnostic_platform.schemas import FlowNode, FlowStep, FlowStepPlan


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def parse_flow_xlsx(source_path: Path, include_non_flow: bool = False) -> FlowStepPlan:
    """Parse an xlsx flow table into ordered steps.

    Cells are expected to use the common format: ``NodeName(TemplateName)``.
    Cells in the same Excel column are treated as parallel nodes in the same step.
    """

    flow_path = source_path.expanduser().resolve()
    if not flow_path.exists():
        raise FileNotFoundError(f"flow.xlsx not found: {flow_path}")

    columns: OrderedDict[int, list[FlowNode]] = OrderedDict()
    for sheet_name, cells in _parse_workbook(flow_path):
        for row_index, col_index, text in cells:
            node_name, template_name = _parse_cell(text)
            is_flow_node = bool(template_name)
            if not include_non_flow and not is_flow_node:
                continue

            columns.setdefault(col_index, []).append(
                FlowNode(
                    raw=text,
                    name=node_name,
                    template_name=template_name,
                    sheet=sheet_name,
                    row=row_index,
                    column=col_index,
                )
            )

    steps: list[FlowStep] = []
    for order, col_index in enumerate(sorted(columns), start=1):
        steps.append(
            FlowStep(
                step_key=f"step_{order:03d}",
                display_name=f"第{order}步",
                order=order,
                column=col_index,
                parallel_nodes=columns[col_index],
            )
        )

    return FlowStepPlan(source_path=str(flow_path), steps=steps)


def _parse_cell(value: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", value).strip()
    match = re.match(r"^(.*?)\s*\((.*?)\)\s*$", text, flags=re.S)
    if not match:
        return text, ""
    return match.group(1).strip(), match.group(2).strip()


def _parse_workbook(flow_path: Path) -> list[tuple[str, list[tuple[int, int, str]]]]:
    ns = {
        "main": MAIN_NS,
        "pkgrel": PKG_REL_NS,
    }

    with ZipFile(flow_path) as archive:
        shared_strings = _read_shared_strings(archive, ns)
        workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
        rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels_root.findall("pkgrel:Relationship", ns)
        }

        parsed_sheets: list[tuple[str, list[tuple[int, int, str]]]] = []
        sheets = workbook_root.find("main:sheets", ns)
        if sheets is None:
            return parsed_sheets

        for sheet in sheets:
            sheet_name = sheet.attrib["name"]
            rel_id = sheet.attrib[f"{{{OFFICE_REL_NS}}}id"]
            target = rel_map[rel_id]
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = f"xl/{target}"
            cells = _read_sheet_cells(archive, target, shared_strings, ns)
            parsed_sheets.append((sheet_name, cells))
        return parsed_sheets


def _read_shared_strings(archive: ZipFile, ns: dict[str, str]) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []

    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root.findall("main:si", ns):
        values.append("".join(text.text or "" for text in item.iterfind(".//main:t", ns)))
    return values


def _read_sheet_cells(
    archive: ZipFile,
    target: str,
    shared_strings: list[str],
    ns: dict[str, str],
) -> list[tuple[int, int, str]]:
    root = ET.fromstring(archive.read(target))
    cells: list[tuple[int, int, str]] = []
    for row in root.findall(".//main:sheetData/main:row", ns):
        row_index = int(row.attrib["r"])
        for cell in row.findall("main:c", ns):
            ref = cell.attrib.get("r", "")
            col_index = _column_index("".join(char for char in ref if char.isalpha()))
            value = _read_cell_value(cell, shared_strings, ns).strip()
            if value:
                cells.append((row_index, col_index, value))
    return cells


def _read_cell_value(cell: ET.Element, shared_strings: list[str], ns: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "s":
        node = cell.find("main:v", ns)
        if node is not None and node.text is not None:
            return shared_strings[int(node.text)]
        return ""
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.iterfind(".//main:t", ns))

    node = cell.find("main:v", ns)
    return node.text if node is not None and node.text is not None else ""


def _column_index(column_letters: str) -> int:
    index = 0
    for char in column_letters:
        index = index * 26 + ord(char.upper()) - 64
    return index
