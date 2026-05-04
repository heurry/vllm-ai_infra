"""Render XML workflow DSL into deterministic XML fragments."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from diagnostic_platform.schemas import (
    RenderedXml,
    XmlGenerationPlan,
    XmlRenderRequest,
    XmlScriptNodePlan,
    XmlSerialNodePlan,
)


def render_xml_request(request: XmlRenderRequest) -> RenderedXml:
    """Render the requested XML node."""

    if request.mode == "script_node":
        if request.script is None:
            raise ValueError("script is required when mode is script_node")
        return RenderedXml(root_tag="ScriptNode", xml=render_script_node(request.script))

    if request.serial is None:
        raise ValueError("serial is required when mode is serial_node")
    return RenderedXml(root_tag="SerialNode", xml=render_serial_node(request.serial))


def render_script_node(plan: XmlScriptNodePlan, include_declaration: bool = True) -> str:
    """Render a ScriptNode XML fragment."""

    element = _script_node_element(plan)
    return _xml_text(element, include_declaration=include_declaration)


def render_serial_node(plan: XmlSerialNodePlan) -> str:
    """Render a SerialNode XML fragment containing ScriptNode children."""

    root = ET.Element(
        "SerialNode",
        {
            "Name": plan.name,
            "DeadMS": str(plan.dead_ms),
        },
    )
    if plan.expression:
        root.set("Expression", plan.expression)

    serials = ET.SubElement(root, "Serials")
    for script in plan.scripts:
        serials.append(_script_node_element(script))
    return _xml_text(root)


def render_flow_serial_node(plan: XmlGenerationPlan) -> str:
    """Render a flow-aware SerialNode.

    Planned nodes with the same ``order`` come from the same Excel column and
    are rendered under one ParallelNode. Columns stay ordered inside the
    SerialNode.
    """

    root = ET.Element(
        "SerialNode",
        {
            "Name": plan.serial.name,
            "DeadMS": str(plan.serial.dead_ms),
        },
    )
    if plan.serial.expression:
        root.set("Expression", plan.serial.expression)

    serials = ET.SubElement(root, "Serials")
    nodes_by_order: dict[int, list] = {}
    for node in plan.nodes:
        nodes_by_order.setdefault(node.order, []).append(node)

    for order in sorted(nodes_by_order):
        planned_nodes = sorted(nodes_by_order[order], key=lambda item: (item.row, item.node_name))
        if len(planned_nodes) == 1:
            serials.append(_script_node_element(planned_nodes[0].script))
            continue

        parallel = ET.SubElement(
            serials,
            "ParallelNode",
            {
                "Name": f"{planned_nodes[0].step_key}_parallel",
                "DeadMS": "0",
            },
        )
        tasks = ET.SubElement(parallel, "Tasks")
        for planned_node in planned_nodes:
            tasks.append(_script_node_element(planned_node.script))

    return _xml_text(root)


def _script_node_element(plan: XmlScriptNodePlan) -> ET.Element:
    attrs = {
        "Name": plan.node_name,
        "DeadMS": str(plan.dead_ms),
        "RetryTimes": str(plan.retry_times),
        "ScriptType": plan.script_type,
        "InteruptStart": str(plan.interupt_start).lower(),
        "ClassName": plan.class_name,
    }
    if plan.expression:
        attrs["Expression"] = plan.expression

    root = ET.Element("ScriptNode", attrs)
    if plan.args:
        args = ET.SubElement(root, "Args")
        for item in plan.args:
            arg = ET.SubElement(args, "Arg", {"ArgName": item.name})
            if item.value is not None:
                arg.text = item.value
    return root


def _xml_text(root: ET.Element, include_declaration: bool = True) -> str:
    _indent_xml(root)
    body = ET.tostring(root, encoding="unicode")
    if include_declaration:
        return '<?xml version="1.0"?>\n' + body + "\n"
    return body


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
