"""Tests for workflow parameter audit reports."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.evaluation.workflow_audit import audit_workflow
from diagnostic_platform.schemas import (
    WorkflowAuditRequest,
    XmlArg,
    XmlGenerationPlan,
    XmlPlannedNode,
    XmlScriptNodePlan,
    XmlSerialNodePlan,
)


class WorkflowAuditTest(unittest.TestCase):
    def test_audit_reports_statuses_and_review_items(self) -> None:
        report = audit_workflow(
            WorkflowAuditRequest(
                plan=_plan(),
                base_xml=_workflow_xml(did="", ecu="ZCUD1D01", request_parameter=None),
                fused_xml=_workflow_xml(did="0xD134", ecu="ZCUD1D01", request_parameter=None),
                required_arg_names=["EcuBOMName", "DID"],
            )
        )

        self.assertTrue(report.valid)
        self.assertEqual(report.summary["review_items"], 0)
        statuses = {item.arg_name: item.status for item in report.nodes[0].args}
        self.assertEqual(statuses["EcuBOMName"], "confirmed_by_plan")
        self.assertEqual(statuses["DID"], "filled_from_plan")
        self.assertEqual(statuses["RequestParameter"], "extra_plan_arg_ignored")
        self.assertFalse(any(issue.code == "WORKFLOW_ARG_REVIEW_REQUIRED" for issue in report.issues))

    def test_audit_detects_plan_conflict_kept_base(self) -> None:
        report = audit_workflow(
            WorkflowAuditRequest(
                plan=_plan(ecu="ZCUD2D02"),
                base_xml=_workflow_xml(did="0xD134", ecu="ZCUD1D01", request_parameter="0x00"),
                fused_xml=_workflow_xml(did="0xD134", ecu="ZCUD1D01", request_parameter="0x00"),
                include_base_only_args=False,
            )
        )

        item = _item_by_arg(report, "EcuBOMName")
        self.assertEqual(item.status, "plan_conflict_kept_base")
        self.assertTrue(item.requires_review)

    def test_audit_missing_fused_node_is_invalid(self) -> None:
        report = audit_workflow(
            WorkflowAuditRequest(
                plan=_plan(node_name="Missing_Node"),
                base_xml=_workflow_xml(did="", ecu="ZCUD1D01", request_parameter=None),
                fused_xml=_workflow_xml(did="", ecu="ZCUD1D01", request_parameter=None),
            )
        )

        self.assertFalse(report.valid)
        self.assertTrue(any(issue.code == "WORKFLOW_NODE_MISSING_IN_FUSED" for issue in report.issues))


def _item_by_arg(report, arg_name: str):
    for item in report.nodes[0].args:
        if item.arg_name == arg_name:
            return item
    raise AssertionError(f"missing audit arg: {arg_name}")


def _plan(node_name: str = "Car_Mode_Change_1", ecu: str = "ZCUD1D01") -> XmlGenerationPlan:
    script = XmlScriptNodePlan(
        node_name=node_name,
        class_name="GEEA30_VMM_Change",
        args=[
            XmlArg(name="EcuBOMName", value=ecu, evidence_ids=["ev_ecu"]),
            XmlArg(name="DID", value="0xD134", evidence_ids=["ev_did"]),
            XmlArg(name="RequestParameter", value="0x00", evidence_ids=["ev_param"]),
        ],
    )
    return XmlGenerationPlan(
        source_flow_path="flow.xlsx",
        serial=XmlSerialNodePlan(scripts=[script]),
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


def _workflow_xml(did: str, ecu: str, request_parameter: str | None) -> str:
    request_arg = ""
    if request_parameter is not None:
        request_arg = f'<Arg ArgName="RequestParameter">{request_parameter}</Arg>'
    return f"""<?xml version="1.0"?>
<PhaseNode Name="FHC" DeadMS="0">
  <Serials>
    <ScriptNode Name="Car_Mode_Change_1" DeadMS="0" RetryTimes="0" ScriptType="NORMAL" InteruptStart="false" ClassName="GEEA30_VMM_Change">
      <Args>
        <Arg ArgName="EcuBOMName">{ecu}</Arg>
        <Arg ArgName="DID">{did}</Arg>
        {request_arg}
      </Args>
    </ScriptNode>
  </Serials>
</PhaseNode>
"""


if __name__ == "__main__":
    unittest.main()
