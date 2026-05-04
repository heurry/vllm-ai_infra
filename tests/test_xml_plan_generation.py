"""Tests for XML intermediate plan generation."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.generation.xml_plan import generate_xml_plan
from diagnostic_platform.normalizer.evidence import evidence_from_mineru
from diagnostic_platform.renderers.xml_workflow import render_serial_node
from diagnostic_platform.schemas import (
    BuildStepEvidenceRequest,
    DocumentMetadata,
    FlowNode,
    FlowStep,
    FlowStepPlan,
    MinerUContentBlock,
    NormalizeMinerURequest,
    XmlArg,
    XmlPlanGenerateRequest,
    XmlPlanValidationRequest,
    XmlValidationRequest,
)
from diagnostic_platform.tracing.evidence_bundle import build_step_evidence_bundles
from diagnostic_platform.validation.xml_plan_validator import validate_xml_plan
from diagnostic_platform.validation.xml_validator import validate_xml


class XmlPlanGenerationTest(unittest.TestCase):
    def test_generate_plan_from_step_evidence(self) -> None:
        evidence_response = _build_evidence_response()

        plan = generate_xml_plan(
            XmlPlanGenerateRequest(
                evidence_response=evidence_response,
                default_args=[XmlArg(name="SourceAddress", value="0x0E80")],
            )
        )

        self.assertEqual(len(plan.nodes), 2)
        self.assertEqual(plan.serial.scripts[0].script_type, "START")
        self.assertEqual(plan.serial.scripts[1].script_type, "END")

        first_args = {arg.name: arg for arg in plan.serial.scripts[0].args}
        self.assertEqual(first_args["SourceAddress"].value, "0x0E80")
        self.assertEqual(first_args["EcuBOMName"].value, "ZCUD1D01")
        self.assertEqual(first_args["DID"].value, "0xD134")
        self.assertEqual(first_args["RequestParameter"].value, "0x00")
        self.assertNotIn("ServiceID", first_args)
        self.assertNotIn("Session", first_args)
        self.assertNotIn("SecurityLevel", first_args)
        self.assertTrue(first_args["DID"].evidence_ids)

        second_args = {arg.name: arg for arg in plan.serial.scripts[1].args}
        self.assertEqual(second_args["DID"].value, "0x3101")
        self.assertEqual(second_args["Position"].value, "")
        self.assertIn("empty Position placeholder", " ".join(second_args["Position"].selection_notes))

        plan_validation = validate_xml_plan(
            XmlPlanValidationRequest(
                plan=plan,
                required_arg_names=["EcuBOMName"],
                min_script_nodes=2,
            )
        )
        self.assertTrue(plan_validation.valid)

        xml = render_serial_node(plan.serial)
        xml_validation = validate_xml(
            XmlValidationRequest(
                xml=xml,
                min_script_nodes=2,
                require_script_types=["START", "END"],
            )
        )
        self.assertTrue(xml_validation.valid)

    def test_validate_required_arg_failure(self) -> None:
        evidence_response = _build_evidence_response()
        plan = generate_xml_plan(XmlPlanGenerateRequest(evidence_response=evidence_response))

        result = validate_xml_plan(
            XmlPlanValidationRequest(
                plan=plan,
                required_arg_names=["ECUAddress"],
            )
        )

        self.assertFalse(result.valid)
        self.assertTrue(any(issue.code == "XML_PLAN_REQUIRED_ARG_MISSING" for issue in result.issues))


def _build_evidence_response():
    evidence_units = evidence_from_mineru(
        NormalizeMinerURequest(
            metadata=DocumentMetadata(doc_id="treg_zcud", ecu="ZCUD1D01"),
            blocks=[
                MinerUContentBlock(
                    type="text",
                    page_idx=8,
                    text=(
                        "ZCUD1D01 Car Mode Change uses DID D134 with 2F service "
                        "and Request Parameter 0x00 in extended session and Security Access level 3."
                    ),
                ),
                MinerUContentBlock(
                    type="text",
                    page_idx=9,
                    text="ZCUD1D01 DTC Read Type1 uses 19 service.",
                ),
            ],
        )
    )
    flow_plan = FlowStepPlan(
        source_path="flow.xlsx",
        steps=[
            FlowStep(
                step_key="step_001",
                display_name="Car mode change",
                order=1,
                column=1,
                parallel_nodes=[
                    FlowNode(
                        raw="ZCUD1D01_Car_Mode_Change_1(GEEA30_VMM_Change)",
                        name="ZCUD1D01_Car_Mode_Change_1",
                        template_name="GEEA30_VMM_Change",
                        sheet="Sheet1",
                        row=1,
                        column=1,
                    )
                ],
            ),
            FlowStep(
                step_key="step_002",
                display_name="DTC read",
                order=2,
                column=2,
                parallel_nodes=[
                    FlowNode(
                        raw="ZCUD1D01_DTC_Read_1(DTC_Read_Type1)",
                        name="ZCUD1D01_DTC_Read_1",
                        template_name="DTC_Read_Type1",
                        sheet="Sheet1",
                        row=1,
                        column=2,
                    )
                ],
            ),
        ],
    )
    return build_step_evidence_bundles(
        BuildStepEvidenceRequest(flow_plan=flow_plan, evidence_units=evidence_units, top_k_per_node=2)
    )


if __name__ == "__main__":
    unittest.main()
