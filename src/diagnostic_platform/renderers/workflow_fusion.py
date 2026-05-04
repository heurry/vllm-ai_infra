"""Fuse XML intermediate plans into existing workflow XML."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from diagnostic_platform.schemas import (
    WorkflowFusionRequest,
    WorkflowFusionResult,
    XmlArg,
    XmlPlannedNode,
    XmlScriptNodePlan,
)


ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
ET.register_namespace("xsd", "http://www.w3.org/2001/XMLSchema")


def fuse_workflow(request: WorkflowFusionRequest) -> WorkflowFusionResult:
    """Fuse generated ScriptNode plans into a base workflow while preserving structure."""

    base_xml = _load_base_xml(request)
    root = ET.fromstring(base_xml)
    script_nodes = _script_nodes_by_name(root)
    target_parent = _find_insert_parent(root, request.target_serial_name)

    matched_nodes = 0
    updated_nodes = 0
    inserted_nodes = 0
    updated_args = 0
    appended_args = 0
    missing_nodes: list[str] = []
    nodes_to_insert: list[XmlPlannedNode] = []

    for planned_node in _planned_nodes(request):
        script_plan = planned_node.script
        existing = script_nodes.get(script_plan.node_name)
        if existing is None:
            if request.insert_missing_nodes:
                nodes_to_insert.append(planned_node)
            else:
                missing_nodes.append(script_plan.node_name)
            continue

        matched_nodes += 1
        changed, arg_stats = _merge_script_node(existing, script_plan, request)
        if changed:
            updated_nodes += 1
        updated_args += arg_stats["updated"]
        appended_args += arg_stats["appended"]

    if nodes_to_insert:
        inserted_nodes = _append_missing_plan_nodes(target_parent, nodes_to_insert)
        updated_nodes += inserted_nodes

    _indent_xml(root)
    xml = '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    output_path = _write_output(xml, request.output_path)
    return WorkflowFusionResult(
        xml=xml,
        root_tag=_local_tag(root.tag),
        matched_nodes=matched_nodes,
        updated_nodes=updated_nodes,
        inserted_nodes=inserted_nodes,
        missing_nodes=missing_nodes,
        updated_args=updated_args,
        appended_args=appended_args,
        output_path=output_path,
    )


def _planned_nodes(request: WorkflowFusionRequest) -> list[XmlPlannedNode]:
    if request.plan.nodes:
        return sorted(request.plan.nodes, key=lambda item: (item.order, item.row, item.node_name))
    return [
        XmlPlannedNode(
            step_key=f"step_{index:03d}",
            order=index,
            column=index,
            node_name=script.node_name,
            template_name=script.class_name,
            script=script,
        )
        for index, script in enumerate(request.plan.serial.scripts, start=1)
    ]


def _append_missing_plan_nodes(parent: ET.Element, planned_nodes: list[XmlPlannedNode]) -> int:
    inserted = 0
    by_order: dict[int, list[XmlPlannedNode]] = {}
    for node in planned_nodes:
        by_order.setdefault(node.order, []).append(node)

    for order in sorted(by_order):
        nodes = sorted(by_order[order], key=lambda item: (item.row, item.node_name))
        if len(nodes) == 1:
            parent.append(_script_node_element(nodes[0].script))
            inserted += 1
            continue

        parallel = ET.SubElement(
            parent,
            "ParallelNode",
            {
                "Name": f"{nodes[0].step_key}_parallel",
                "DeadMS": "0",
            },
        )
        tasks = ET.SubElement(parallel, "Tasks")
        for node in nodes:
            tasks.append(_script_node_element(node.script))
            inserted += 1
    return inserted


def _load_base_xml(request: WorkflowFusionRequest) -> str:
    if request.base_xml is not None:
        return request.base_xml
    if request.base_xml_path:
        return Path(request.base_xml_path).read_text(encoding="utf-8")
    raise ValueError("Either base_xml or base_xml_path is required.")


def _script_nodes_by_name(root: ET.Element) -> dict[str, ET.Element]:
    nodes: dict[str, ET.Element] = {}
    for element in root.iter():
        if _local_tag(element.tag) != "ScriptNode":
            continue
        name = element.get("Name")
        if name and name not in nodes:
            nodes[name] = element
    return nodes


def _find_insert_parent(root: ET.Element, target_serial_name: str | None) -> ET.Element:
    if target_serial_name:
        for element in root.iter():
            if _local_tag(element.tag) == "SerialNode" and element.get("Name") == target_serial_name:
                serials = _first_child(element, "Serials")
                if serials is not None:
                    return serials
    if _local_tag(root.tag) in {"PhaseNode", "SerialNode"}:
        serials = _first_child(root, "Serials")
        if serials is not None:
            return serials
    return root


def _merge_script_node(
    element: ET.Element,
    script_plan: XmlScriptNodePlan,
    request: WorkflowFusionRequest,
) -> tuple[bool, dict[str, int]]:
    changed = False
    updated_args = 0
    appended_args = 0

    if request.update_class_name and script_plan.class_name and element.get("ClassName") != script_plan.class_name:
        element.set("ClassName", script_plan.class_name)
        changed = True

    args_element = _first_child(element, "Args")
    if args_element is None and script_plan.args:
        args_element = ET.SubElement(element, "Args")
        changed = True

    if args_element is not None:
        existing_args = _args_by_name(args_element)
        for arg in script_plan.args:
            if not arg.name:
                continue
            existing = existing_args.get(arg.name)
            if existing is None:
                if not request.append_missing_args:
                    continue
                args_element.append(_arg_element(arg))
                existing_args[arg.name] = args_element[-1]
                appended_args += 1
                changed = True
                continue

            if arg.value is None:
                continue
            current = (existing.text or "").strip()
            should_update = request.arg_merge_strategy == "overwrite" or not current
            if should_update and current != arg.value:
                existing.text = arg.value
                updated_args += 1
                changed = True

    return changed, {"updated": updated_args, "appended": appended_args}


def _script_node_element(plan: XmlScriptNodePlan) -> ET.Element:
    root = ET.Element(
        "ScriptNode",
        {
            "Name": plan.node_name,
            "DeadMS": str(plan.dead_ms),
            "RetryTimes": str(plan.retry_times),
            "ScriptType": plan.script_type,
            "InteruptStart": str(plan.interupt_start).lower(),
            "ClassName": plan.class_name,
        },
    )
    if plan.expression:
        root.set("Expression", plan.expression)
    if plan.args:
        args = ET.SubElement(root, "Args")
        for arg in plan.args:
            args.append(_arg_element(arg))
    return root


def _arg_element(arg: XmlArg) -> ET.Element:
    element = ET.Element("Arg", {"ArgName": arg.name})
    if arg.value is not None:
        element.text = arg.value
    return element


def _args_by_name(args_element: ET.Element) -> dict[str, ET.Element]:
    args: dict[str, ET.Element] = {}
    for child in list(args_element):
        if _local_tag(child.tag) != "Arg":
            continue
        name = child.get("ArgName")
        if name and name not in args:
            args[name] = child
    return args


def _first_child(element: ET.Element, tag: str) -> ET.Element | None:
    for child in list(element):
        if _local_tag(child.tag) == tag:
            return child
    return None


def _write_output(xml: str, output_path: str | None) -> str | None:
    if not output_path:
        return None
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")
    return str(path)


def _indent_xml(element: ET.Element, level: int = 0) -> None:
    indent = "\n" + "  " * level
    if len(element):
        if not element.text or not element.text.strip():
            element.text = indent + "  "
        for child in element:
            _indent_xml(child, level + 1)
        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = indent
    if level and (not element.tail or not element.tail.strip()):
        element.tail = indent


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
