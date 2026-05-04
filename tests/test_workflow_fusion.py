"""Tests for workflow XML fusion and template registry."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.renderers.template_registry import build_template_registry
from diagnostic_platform.renderers.workflow_fusion import fuse_workflow
from diagnostic_platform.schemas import (
    WorkflowFusionRequest,
    XmlArg,
    XmlGenerationPlan,
    XmlPlanValidationRequest,
    XmlPlannedNode,
    XmlScriptNodePlan,
    XmlSerialNodePlan,
    XmlTemplateRegistryBuildRequest,
    XmlValidationRequest,
)
from diagnostic_platform.validation.xml_plan_validator import validate_xml_plan
from diagnostic_platform.validation.xml_validator import validate_xml


class WorkflowFusionTest(unittest.TestCase):
    def test_fuse_workflow_preserves_existing_non_empty_args_by_default(self) -> None:
        result = fuse_workflow(
            WorkflowFusionRequest(
                base_xml=_base_workflow_xml(),
                plan=_plan(ecu_value="WRONG_ECU"),
            )
        )

        self.assertEqual(result.matched_nodes, 1)
        self.assertEqual(result.updated_args, 1)
        self.assertEqual(result.appended_args, 0)
        self.assertIn("<Arg ArgName=\"EcuBOMName\">ZCUD1D01</Arg>", result.xml)
        self.assertIn("<Arg ArgName=\"DID\">0xD134</Arg>", result.xml)
        self.assertNotIn("RequestParameter", result.xml)

        validation = validate_xml(XmlValidationRequest(xml=result.xml, min_script_nodes=1))
        self.assertTrue(validation.valid)

    def test_fuse_workflow_can_overwrite_existing_args(self) -> None:
        result = fuse_workflow(
            WorkflowFusionRequest(
                base_xml=_base_workflow_xml(),
                plan=_plan(ecu_value="ZCUD2D02"),
                arg_merge_strategy="overwrite",
                update_class_name=True,
            )
        )

        self.assertIn("<Arg ArgName=\"EcuBOMName\">ZCUD2D02</Arg>", result.xml)
        self.assertIn("ClassName=\"GEEA30_VMM_Change\"", result.xml)
        self.assertGreaterEqual(result.updated_args, 2)

    def test_fuse_workflow_can_append_missing_args(self) -> None:
        result = fuse_workflow(
            WorkflowFusionRequest(
                base_xml=_base_workflow_xml(),
                plan=_plan(),
                append_missing_args=True,
            )
        )

        self.assertEqual(result.appended_args, 1)
        self.assertIn("<Arg ArgName=\"RequestParameter\">0x00</Arg>", result.xml)

    def test_fuse_workflow_insert_missing_node(self) -> None:
        plan = _plan(node_name="New_Node")
        result = fuse_workflow(
            WorkflowFusionRequest(
                base_xml=_base_workflow_xml(),
                plan=plan,
                target_serial_name="FHC_ALL",
                insert_missing_nodes=True,
            )
        )

        self.assertEqual(result.inserted_nodes, 1)
        self.assertIn("Name=\"New_Node\"", result.xml)

    def test_validate_xml_plan_requires_arg_evidence(self) -> None:
        result = validate_xml_plan(
            XmlPlanValidationRequest(
                plan=_plan(),
                require_arg_evidence=True,
            )
        )

        self.assertFalse(result.valid)
        self.assertTrue(any(issue.code == "XML_PLAN_ARG_EVIDENCE_MISSING" for issue in result.issues))

    def test_build_template_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir)
            (source_dir / "template.xml").write_text(_base_workflow_xml(), encoding="utf-8")

            registry = build_template_registry(
                XmlTemplateRegistryBuildRequest(source_dir=str(source_dir))
            )

            self.assertTrue(registry.templates)
            self.assertIn("OldClass", registry.by_class_name)
            self.assertIn("Car_Mode_Change_1", registry.by_node_name)

    def test_build_template_registry_extracts_tasknode_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir)
            (source_dir / "ADCU1301_DTC_Clear_Type7.xml").write_text(
                """<?xml version="1.0"?>
<TaskNode ID="0">
  <MainNodes>
    <MainNode ID="0">
      <FlowsNodes>
        <NormalBoxNode ID="1">
          <OptNodes>
            <AssignNode ID="0" LeftVar="SourceAddress" RightExpression="Bytes.Parse(_argLibrary.Get(&quot;SourceAddress&quot;))" />
            <AssignNode ID="1" LeftVar="EcuBOMName" RightExpression="_argLibrary.Get(&quot;EcuBOMName&quot;)" />
          </OptNodes>
        </NormalBoxNode>
      </FlowsNodes>
    </MainNode>
  </MainNodes>
</TaskNode>
""",
                encoding="utf-8",
            )

            registry = build_template_registry(XmlTemplateRegistryBuildRequest(source_dir=str(source_dir)))

            self.assertIn("ADCU1301_DTC_Clear_Type7", registry.by_class_name)
            entry = next(item for item in registry.templates if item.class_name == "ADCU1301_DTC_Clear_Type7")
            self.assertEqual(entry.root_tag, "TaskNode")
            self.assertEqual(entry.arg_names, ["SourceAddress", "EcuBOMName"])
            self.assertIn("NormalBoxNode", entry.xml)


def _plan(node_name: str = "Car_Mode_Change_1", ecu_value: str = "ZCUD1D01") -> XmlGenerationPlan:
    script = XmlScriptNodePlan(
        node_name=node_name,
        class_name="GEEA30_VMM_Change",
        args=[
            XmlArg(name="EcuBOMName", value=ecu_value),
            XmlArg(name="DID", value="0xD134"),
            XmlArg(name="RequestParameter", value="0x00"),
        ],
    )
    return XmlGenerationPlan(
        source_flow_path="flow.xlsx",
        serial=XmlSerialNodePlan(name="MAC_ALL", scripts=[script]),
        nodes=[
            XmlPlannedNode(
                step_key="step_001",
                order=1,
                node_name=node_name,
                template_name="GEEA30_VMM_Change",
                script=script,
            )
        ],
    )


def _base_workflow_xml() -> str:
    return """<?xml version="1.0"?>
<PhaseNode xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="SerialNode" Name="FHC" DeadMS="0">
  <Serials>
    <SerialNode Name="FHC_ALL" DeadMS="0">
      <Serials>
        <ScriptNode Name="Car_Mode_Change_1" DeadMS="0" RetryTimes="0" ScriptType="NORMAL" InteruptStart="false" ClassName="OldClass">
          <Args>
            <Arg ArgName="SourceAddress">0x0E80</Arg>
            <Arg ArgName="EcuBOMName">ZCUD1D01</Arg>
            <Arg ArgName="DID"></Arg>
          </Args>
        </ScriptNode>
      </Serials>
    </SerialNode>
  </Serials>
</PhaseNode>
"""


if __name__ == "__main__":
    unittest.main()
