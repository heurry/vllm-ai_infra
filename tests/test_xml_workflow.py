"""Tests for XML renderer and validator."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.renderers.xml_workflow import render_flow_serial_node, render_script_node, render_serial_node
from diagnostic_platform.schemas import (
    XmlArg,
    XmlGenerationPlan,
    XmlPlannedNode,
    XmlScriptNodePlan,
    XmlSerialNodePlan,
    XmlValidationRequest,
)
from diagnostic_platform.validation.xml_validator import validate_xml


class XmlWorkflowTest(unittest.TestCase):
    def test_render_and_validate_script_node(self) -> None:
        xml = render_script_node(
            XmlScriptNodePlan(
                node_name="Car_Mode_Change_1",
                class_name="GEEA30_VMM_Change",
                args=[
                    XmlArg(name="SourceAddress", value="0x0E80"),
                    XmlArg(name="ECUAddress", value="0x1D01"),
                    XmlArg(name="DID", value="0xD134"),
                ],
            )
        )

        result = validate_xml(
            XmlValidationRequest(
                xml=xml,
                required_arg_names=["SourceAddress", "ECUAddress", "DID"],
                min_script_nodes=1,
            )
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.stats["root_tag"], "ScriptNode")

    def test_validate_placeholder_failure(self) -> None:
        result = validate_xml(
            XmlValidationRequest(
                xml='<ScriptNode Name="{{STEP_NAME}}" DeadMS="0" RetryTimes="0" ScriptType="NORMAL" InteruptStart="false" ClassName="Demo" />'
            )
        )

        self.assertFalse(result.valid)
        self.assertTrue(any(issue.code == "PLACEHOLDER_REMAINS" for issue in result.issues))

    def test_render_serial_node(self) -> None:
        xml = render_serial_node(
            XmlSerialNodePlan(
                name="MAC_ALL",
                scripts=[
                    XmlScriptNodePlan(
                        node_name="StepA",
                        class_name="ClassA",
                        args=[XmlArg(name="Message", value="hello")],
                    )
                ],
            )
        )

        result = validate_xml(XmlValidationRequest(xml=xml, min_script_nodes=1))
        self.assertTrue(result.valid)
        self.assertEqual(result.stats["root_tag"], "SerialNode")

    def test_render_flow_serial_node_keeps_same_column_parallel(self) -> None:
        first = XmlScriptNodePlan(node_name="StepA", class_name="ClassA")
        second = XmlScriptNodePlan(node_name="StepB", class_name="ClassB")
        third = XmlScriptNodePlan(node_name="StepC", class_name="ClassC")
        xml = render_flow_serial_node(
            XmlGenerationPlan(
                source_flow_path="flow.xlsx",
                serial=XmlSerialNodePlan(name="MAC_ALL", scripts=[first, second, third]),
                nodes=[
                    XmlPlannedNode(
                        step_key="step_001",
                        order=1,
                        row=1,
                        column=1,
                        node_name="StepA",
                        template_name="ClassA",
                        script=first,
                    ),
                    XmlPlannedNode(
                        step_key="step_001",
                        order=1,
                        row=2,
                        column=1,
                        node_name="StepB",
                        template_name="ClassB",
                        script=second,
                    ),
                    XmlPlannedNode(
                        step_key="step_002",
                        order=2,
                        row=2,
                        column=1,
                        node_name="StepC",
                        template_name="ClassC",
                        script=third,
                    ),
                ],
            )
        )

        result = validate_xml(XmlValidationRequest(xml=xml, min_script_nodes=3))
        self.assertTrue(result.valid)
        self.assertEqual(result.stats["parallel_nodes"], 1)
        self.assertIn("<ParallelNode Name=\"step_001_parallel\"", xml)


if __name__ == "__main__":
    unittest.main()
