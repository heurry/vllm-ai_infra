"""Extract reusable structural signatures from TaskNode XML templates."""

from __future__ import annotations

from pathlib import Path
import re
from xml.etree import ElementTree as ET

from diagnostic_platform.schemas import XmlTaskNodeSignature


REQUEST_SERVICE_IDS = {"10", "11", "14", "19", "22", "27", "28", "2E", "2F", "31", "34", "36", "37", "85"}
POSITIVE_RESPONSE_IDS = {"50", "51", "54", "59", "62", "67", "68", "6E", "6F", "71", "74", "76", "77", "C5"}


def extract_tasknode_signature(xml_path: str | Path) -> XmlTaskNodeSignature | None:
    """Parse one XML file and return a TaskNode signature, or None for non-TaskNode roots."""

    path = Path(xml_path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    root_tag = _local_tag(root.tag)
    if root_tag != "TaskNode":
        return None

    all_text = _element_text_blob(root)
    hex_literals = _hex_literals(all_text)
    request_literals = _request_literals_from_send_calls(root)
    if not request_literals:
        request_literals = _request_literals(hex_literals)
    response_literals = _response_literals(hex_literals)
    return XmlTaskNodeSignature(
        root_tag=root_tag,
        source_path=str(path),
        class_name=path.stem,
        create_vars=_create_vars(root),
        arg_names=_arg_names(all_text),
        function_names=_function_names(root),
        assigned_vars=_assigned_vars(root),
        service_ids=_service_ids(request_literals),
        positive_responses=_service_ids(response_literals),
        request_literals=request_literals,
        response_literals=response_literals,
        hex_literals=hex_literals,
        logic_markers=_logic_markers(root, all_text),
        tag_counts=_tag_counts(root),
    )


def family_id_from_class_name(class_name: str) -> str:
    """Generalize a concrete ClassName into a reusable template family id."""

    value = re.sub(r"[^0-9A-Za-z]+", "_", class_name or "").strip("_")
    patterns = [
        r"(DTC_Clear_Type\d+)$",
        r"(DTC_Read_Type\d+)$",
        r"(IO_Control_Type\d+)$",
        r"(Routine_Control_Type\d+)$",
        r"(Information_Read_Type\d+)$",
        r"(Information_Write_Type\d+)$",
        r"(Request_Open_HV_Battery)$",
        r"(VMM_Change)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return _canonical_family_id(match.group(1))
    return value or "unknown_template_family"


def normalize_alias(value: str) -> str:
    """Normalize aliases for robust string matching."""

    return re.sub(r"[^0-9A-Z]+", " ", str(value or "").upper()).strip()


def _canonical_family_id(value: str) -> str:
    parts = [part for part in re.split(r"[_\s]+", value) if part]
    canonical = []
    for part in parts:
        if re.fullmatch(r"type\d+", part, flags=re.IGNORECASE):
            canonical.append("Type" + re.sub(r"[^0-9]", "", part))
        elif part.upper() in {"DTC", "IO", "VMM", "HV"}:
            canonical.append(part.upper())
        else:
            canonical.append(part[:1].upper() + part[1:])
    return "_".join(canonical)


def _create_vars(root: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for element in root.iter():
        if _local_tag(element.tag) != "CreateVarNode":
            continue
        name = element.get("Name") or ""
        if name:
            values[name] = element.get("Type") or ""
    return dict(sorted(values.items()))


def _arg_names(text: str) -> list[str]:
    names = []
    for match in re.finditer(r"_argLibrary\.Get\([\"']([^\"']+)[\"']\)", text):
        name = match.group(1)
        if name and name not in names:
            names.append(name)
    return names


def _function_names(root: ET.Element) -> list[str]:
    names: list[str] = []
    for element in root.iter():
        if _local_tag(element.tag) != "CallFunctionNode":
            continue
        name = element.get("FunctionName") or ""
        if name and name not in names:
            names.append(name)
    return sorted(names)


def _assigned_vars(root: ET.Element) -> list[str]:
    names: list[str] = []
    for element in root.iter():
        if _local_tag(element.tag) != "AssignNode":
            continue
        name = element.get("LeftVar") or ""
        if name and name not in names:
            names.append(name)
    return sorted(names)


def _hex_literals(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"0x[0-9A-Fa-f]+", text):
        value = match.group(0).upper()
        if value not in values:
            values.append(value)
    return values


def _request_literals(hex_literals: list[str]) -> list[str]:
    return [value for value in hex_literals if _first_byte(value) in REQUEST_SERVICE_IDS]


def _request_literals_from_send_calls(root: ET.Element) -> list[str]:
    values: list[str] = []
    for element in root.iter():
        if _local_tag(element.tag) != "CallFunctionNode":
            continue
        if "SendUDSRawData" not in str(element.get("FunctionName") or ""):
            continue
        for literal in _hex_literals(_element_text_blob(element)):
            if _first_byte(literal) in REQUEST_SERVICE_IDS and literal not in values:
                values.append(literal)
    return values


def _response_literals(hex_literals: list[str]) -> list[str]:
    return [value for value in hex_literals if _first_byte(value) in POSITIVE_RESPONSE_IDS]


def _service_ids(literals: list[str]) -> list[str]:
    values: list[str] = []
    for literal in literals:
        service_id = _first_byte(literal)
        if service_id and service_id not in values:
            values.append(service_id)
    return values


def _first_byte(hex_value: str) -> str:
    value = hex_value.upper().removeprefix("0X")
    return value[:2] if len(value) >= 2 else ""


def _logic_markers(root: ET.Element, text: str) -> list[str]:
    markers = set()
    if "RetryCount" in text:
        markers.add("retry_count")
    if "RetryCount-1" in text.replace(" ", ""):
        markers.add("retry_decrement")
    if "AllowedNRC" in text:
        markers.add("allowed_nrc")
    if "ClearDTC" in text:
        markers.add("clear_dtc_loop")
    if "DID" in text:
        markers.add("did_based")
    if "RequestParameter" in text:
        markers.add("request_parameter")
    if "L3DefaultSecurityValue" in text or "2703" in text or "2704" in text:
        markers.add("security_access")
    if "1003" in text:
        markers.add("extended_session")
    if any(_local_tag(element.tag) == "GotoPair" for element in root.iter()):
        markers.add("branching")
    if "errorLibrary.SetFault" in text:
        markers.add("fault_handling")
    return sorted(markers)


def _tag_counts(root: ET.Element) -> dict[str, int]:
    counts: dict[str, int] = {}
    for element in root.iter():
        tag = _local_tag(element.tag)
        counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items()))


def _element_text_blob(root: ET.Element) -> str:
    parts: list[str] = []
    for element in root.iter():
        parts.append(_local_tag(element.tag))
        parts.extend(str(value) for value in element.attrib.values())
        if element.text:
            parts.append(element.text)
        if element.tail:
            parts.append(element.tail)
    return "\n".join(parts)


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
