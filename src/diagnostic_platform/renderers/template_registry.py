"""Build a lightweight registry from existing XML workflow templates."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET

from diagnostic_platform.schemas import (
    XmlTemplateEntry,
    XmlTemplateRegistry,
    XmlTemplateRegistryBuildRequest,
)


def build_template_registry(request: XmlTemplateRegistryBuildRequest) -> XmlTemplateRegistry:
    """Scan XML files and extract reusable ScriptNode templates."""

    source_dir = Path(request.source_dir)
    if not source_dir.exists():
        raise FileNotFoundError(f"XML template source directory does not exist: {source_dir}")

    templates: list[XmlTemplateEntry] = []
    for xml_path in sorted(source_dir.rglob("*.xml")):
        if _is_macos_metadata(xml_path):
            continue
        templates.extend(_extract_templates(xml_path, request.include_workflow_files))

    registry = _build_registry(source_dir, templates)
    if request.output_path:
        output_path = Path(request.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(registry.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return registry


def _extract_templates(xml_path: Path, include_workflow_files: bool) -> list[XmlTemplateEntry]:
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return []

    root_tag = _local_tag(root.tag)
    if not include_workflow_files and root_tag not in {"ScriptNode", "TaskNode"}:
        return []

    templates: list[XmlTemplateEntry] = []
    if root_tag == "TaskNode":
        task_entry = _task_node_template(xml_path, root)
        if task_entry is not None:
            templates.append(task_entry)
    for script in root.iter():
        if _local_tag(script.tag) != "ScriptNode":
            continue
        node_name = script.get("Name") or ""
        class_name = script.get("ClassName") or ""
        if not node_name and not class_name:
            continue
        templates.append(
            XmlTemplateEntry(
                template_id=_template_id(xml_path, node_name, class_name),
                node_name=node_name,
                class_name=class_name,
                root_tag=root_tag,
                source_path=str(xml_path),
                arg_names=_arg_names(script),
                arg_values=_arg_values(script),
                attrs={str(key): str(value) for key, value in script.attrib.items()},
                xml=ET.tostring(script, encoding="unicode"),
            )
        )
    return templates


def _task_node_template(xml_path: Path, root: ET.Element) -> XmlTemplateEntry | None:
    class_name = xml_path.stem
    if not class_name:
        return None
    arg_names = _task_arg_names(root)
    return XmlTemplateEntry(
        template_id=_template_id(xml_path, class_name, class_name),
        node_name=class_name,
        class_name=class_name,
        root_tag=_local_tag(root.tag),
        source_path=str(xml_path),
        arg_names=arg_names,
        arg_values={name: "" for name in arg_names},
        attrs={str(key): str(value) for key, value in root.attrib.items()},
        xml=ET.tostring(root, encoding="unicode"),
    )


def _build_registry(source_dir: Path, templates: list[XmlTemplateEntry]) -> XmlTemplateRegistry:
    by_class_name: dict[str, list[str]] = defaultdict(list)
    by_node_name: dict[str, list[str]] = defaultdict(list)
    deduped: dict[str, XmlTemplateEntry] = {}
    for template in templates:
        if template.template_id in deduped:
            continue
        deduped[template.template_id] = template
        if template.class_name:
            by_class_name[template.class_name].append(template.template_id)
        if template.node_name:
            by_node_name[template.node_name].append(template.template_id)

    return XmlTemplateRegistry(
        source_dir=str(source_dir),
        templates=sorted(deduped.values(), key=lambda item: item.template_id),
        by_class_name={key: sorted(value) for key, value in sorted(by_class_name.items())},
        by_node_name={key: sorted(value) for key, value in sorted(by_node_name.items())},
    )


def _arg_names(script: ET.Element) -> list[str]:
    names: list[str] = []
    for element in script.iter():
        if _local_tag(element.tag) != "Arg":
            continue
        name = element.get("ArgName")
        if name and name not in names:
            names.append(name)
    return names


def _arg_values(script: ET.Element) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    for element in script.iter():
        if _local_tag(element.tag) != "Arg":
            continue
        name = element.get("ArgName")
        if name and name not in values:
            values[name] = element.text if element.text is not None else ""
    return values


def _task_arg_names(root: ET.Element) -> list[str]:
    names: list[str] = []
    for element in root.iter():
        values = [*element.attrib.values()]
        if element.text:
            values.append(element.text)
        for value in values:
            for match in re.finditer(r"_argLibrary\.Get\([\"']([^\"']+)[\"']\)", value):
                name = match.group(1)
                if name and name not in names:
                    names.append(name)
    return names


def _template_id(xml_path: Path, node_name: str, class_name: str) -> str:
    name = node_name or class_name or xml_path.stem
    return f"{xml_path.as_posix()}::{name}"


def _is_macos_metadata(path: Path) -> bool:
    return any(part == "__MACOSX" for part in path.parts) or path.name.startswith("._")


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
