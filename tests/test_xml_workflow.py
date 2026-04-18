"""Tests for XML renderer and validator."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.renderers.xml_workflow import render_script_node, render_serial_node
from diagnostic_platform.schemas import (
    XmlArg,
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


if __name__ == "__main__":
    unittest.main()
