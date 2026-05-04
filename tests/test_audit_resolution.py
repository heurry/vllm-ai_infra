"""Tests for template contracts and audit resolution."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.evaluation.workflow_audit import audit_workflow
from diagnostic_platform.resolution.audit_resolver import resolve_workflow_audit
from diagnostic_platform.resolution.template_contract import build_template_contracts
from diagnostic_platform.schemas import (
    WorkflowAuditRequest,
    WorkflowResolveAuditRequest,
    XmlArg,
    XmlGenerationPlan,
    XmlPlannedNode,
    XmlScriptNodePlan,
    XmlSerialNodePlan,
    XmlTemplateContractBuildRequest,
    XmlTemplateEntry,
    XmlTemplateRegistry,
)


class AuditResolutionTest(unittest.TestCase):
    def test_build_template_contracts(self) -> None:
        contracts = build_template_contracts(XmlTemplateContractBuildRequest(registry=_registry()))

        contract = contracts.by_class_name["GEEA30_VMM_Change"]
        self.assertEqual(contract.template_count, 2)
        self.assertIn("EcuBOMName", contract.required_args)
        self.assertIn("DID", contract.required_args)
        self.assertIn("RequestParameter", contract.optional_args)

    def test_resolve_audit_with_contracts(self) -> None:
        audit_report = audit_workflow(
            WorkflowAuditRequest(
                plan=_plan(),
                base_xml=_workflow_xml(ecu="ZCUD1D01"),
                fused_xml=_workflow_xml(ecu="ZCUD1D01"),
                include_base_only_args=False,
            )
        )
        contracts = build_template_contracts(XmlTemplateContractBuildRequest(registry=_registry()))

        resolution = resolve_workflow_audit(
            WorkflowResolveAuditRequest(
                audit_report=audit_report,
                contract_registry=contracts,
            )
        )

        actions = {f"{item.arg_name}:{item.audit_status}": item.action for item in resolution.decisions}
        reviews = {item.arg_name: item.requires_review for item in resolution.decisions}
        self.assertEqual(actions["EcuBOMName:plan_conflict_kept_base"], "keep_base")
        self.assertTrue(reviews["EcuBOMName"])
        self.assertEqual(actions["RequestParameter:extra_plan_arg_ignored"], "ignore_plan_arg")
        self.assertEqual(actions["ServiceID:extra_plan_arg_ignored"], "ignore_plan_arg")
        self.assertFalse(reviews["RequestParameter"])
        self.assertFalse(reviews["ServiceID"])
        self.assertEqual(resolution.summary["review_required"], 1)


def _registry() -> XmlTemplateRegistry:
    return XmlTemplateRegistry(
        source_dir="templates",
        templates=[
            XmlTemplateEntry(
                template_id="t1",
                node_name="Car_Mode_Change_1",
                class_name="GEEA30_VMM_Change",
                root_tag="ScriptNode",
                source_path="t1.xml",
                arg_names=["EcuBOMName", "DID", "RequestParameter"],
            ),
            XmlTemplateEntry(
                template_id="t2",
                node_name="Usage_Mode_Change_1",
                class_name="GEEA30_VMM_Change",
                root_tag="ScriptNode",
                source_path="t2.xml",
                arg_names=["EcuBOMName", "DID"],
            ),
        ],
    )


def _plan() -> XmlGenerationPlan:
    script = XmlScriptNodePlan(
        node_name="Car_Mode_Change_1",
        class_name="GEEA30_VMM_Change",
        args=[
            XmlArg(name="EcuBOMName", value="ECM1610", evidence_ids=["ev_ecu"]),
            XmlArg(name="DID", value="0xD134", evidence_ids=["ev_did"]),
            XmlArg(name="RequestParameter", value="0x00", evidence_ids=["ev_param"]),
            XmlArg(name="ServiceID", value="0x2F", evidence_ids=["ev_service"]),
        ],
    )
    return XmlGenerationPlan(
        source_flow_path="flow.xlsx",
        serial=XmlSerialNodePlan(scripts=[script]),
        nodes=[
            XmlPlannedNode(
                step_key="step_001",
                order=1,
                node_name="Car_Mode_Change_1",
                template_name="GEEA30_VMM_Change",
                script=script,
            )
        ],
    )


def _workflow_xml(ecu: str) -> str:
    return f"""<?xml version="1.0"?>
<PhaseNode Name="FHC" DeadMS="0">
  <Serials>
    <ScriptNode Name="Car_Mode_Change_1" DeadMS="0" RetryTimes="0" ScriptType="NORMAL" InteruptStart="false" ClassName="GEEA30_VMM_Change">
      <Args>
        <Arg ArgName="EcuBOMName">{ecu}</Arg>
        <Arg ArgName="DID">0xD134</Arg>
      </Args>
    </ScriptNode>
  </Serials>
</PhaseNode>
"""


if __name__ == "__main__":
    unittest.main()
