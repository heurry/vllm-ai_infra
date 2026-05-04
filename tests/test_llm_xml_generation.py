"""Tests for LLM XML generation contexts, validation, repair, and assembly."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.generation.llm_xml import (  # noqa: E402
    assemble_llm_xml_plan,
    build_node_xml_repair_messages,
    build_xml_generation_context,
    generate_node_xml_with_llm,
    validate_and_repair_node_xml,
)
from diagnostic_platform.renderers.xml_workflow import render_flow_serial_node  # noqa: E402
from diagnostic_platform.schemas import (  # noqa: E402
    EvidenceUnit,
    NodeEvidenceBundle,
    TaskEvidenceCandidate,
    VllmModelConfig,
    XmlTemplateArgContract,
    XmlTemplateClassContract,
    XmlTemplateContractRegistry,
    XmlTemplateEntry,
    XmlTemplateRegistry,
    XmlValidationRequest,
)
from diagnostic_platform.validation.xml_validator import validate_xml  # noqa: E402


class LlmXmlGenerationTest(unittest.TestCase):
    def test_generate_validate_and_assemble_scriptnode(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )

        generation = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient([_valid_response()]))
        checked = validate_and_repair_node_xml(generation, context, max_repair_attempts=0)
        plan = assemble_llm_xml_plan([checked], source_flow_path="flow.xlsx")
        xml = render_flow_serial_node(plan)
        xml_result = validate_xml(XmlValidationRequest(xml=xml, min_script_nodes=1))

        self.assertTrue(checked.valid)
        self.assertFalse(checked.needs_review)
        self.assertEqual(checked.arg_evidence_map["DID"], ["ev_param"])
        self.assertEqual(plan.nodes[0].evidence_ids, ["ev_param"])
        self.assertTrue(xml_result.valid)
        self.assertTrue(any(item.startswith("template:") for item in context.prompt_context_ids))
        self.assertGreater(checked.prompt_estimated_tokens, 0)
        self.assertGreater(checked.output_estimated_tokens, 0)
        self.assertEqual(
            {arg.name: arg.value for arg in checked.raw_llm_args},
            {arg.name: arg.value for arg in checked.post_guardrail_args},
        )

    def test_missing_required_arg_enters_repair(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )
        client = _FakeClient([_repair_response()])
        broken = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient([_missing_required_response()]))

        repaired = validate_and_repair_node_xml(
            broken,
            context,
            client=client,
            max_repair_attempts=1,
        )

        self.assertTrue(repaired.valid)
        self.assertEqual(len(repaired.repair_attempts), 1)
        self.assertEqual(repaired.script.args[1].name, "DID")
        self.assertIn("REQUIRED_ARG_MISSING", client.last_user_message)

    def test_malformed_initial_json_enters_repair(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )
        broken = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient(['{"node_name":']))

        repaired = validate_and_repair_node_xml(
            broken,
            context,
            client=_FakeClient([_repair_response()]),
            max_repair_attempts=1,
        )

        self.assertTrue(repaired.valid)
        self.assertEqual(len(repaired.repair_attempts), 1)
        self.assertTrue(any(issue.code == "LLM_XML_JSON_PARSE_FAILED" for issue in broken.xml_validation_errors))

    def test_complete_xml_is_recovered_from_truncated_json_tail(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )
        response = _valid_response().rsplit('"confidence"', 1)[0]

        generation = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient([response]))
        checked = validate_and_repair_node_xml(generation, context, max_repair_attempts=0)

        self.assertTrue(checked.valid)
        self.assertTrue(any(issue.code == "LLM_XML_JSON_RECOVERED" for issue in checked.xml_validation_errors))
        self.assertEqual(checked.script.class_name, "GEEA30_VMM_Change")

    def test_failed_repair_is_marked_for_review(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )
        broken = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient([_invalid_xml_response()]))

        repaired = validate_and_repair_node_xml(
            broken,
            context,
            client=_FakeClient([_invalid_xml_response()]),
            max_repair_attempts=1,
        )

        self.assertFalse(repaired.valid)
        self.assertTrue(repaired.needs_review)
        self.assertEqual(len(repaired.repair_attempts), 1)
        self.assertTrue(any(issue.code == "XML_PARSE_ERROR" for issue in repaired.xml_validation_errors))

    def test_unknown_evidence_id_is_rejected_for_high_risk_arg(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )
        generation = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient([_unknown_evidence_response()]))

        checked = validate_and_repair_node_xml(generation, context, max_repair_attempts=0)

        self.assertFalse(checked.valid)
        self.assertTrue(any(issue.code == "LLM_XML_ARG_EVIDENCE_UNKNOWN" for issue in checked.xml_validation_errors))

    def test_high_risk_arg_evidence_can_be_inferred_from_context(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )
        response = _valid_response().replace(
            '{"name":"EcuBOMName","value":"ZCUD1D01","evidence_ids":["ev_param"]}',
            '{"name":"EcuBOMName","value":"ZCUD1D01","evidence_ids":[]}',
        ).replace('"EcuBOMName":["ev_param"],', "")
        generation = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient([response]))

        checked = validate_and_repair_node_xml(generation, context, max_repair_attempts=0)

        self.assertTrue(checked.valid)
        self.assertEqual(checked.arg_evidence_map["EcuBOMName"], ["ev_param"])
        self.assertEqual(checked.guardrail_corrections[0].arg_name, "EcuBOMName")
        self.assertEqual(checked.guardrail_corrections[0].correction_source, "evidence_context")

    def test_deterministic_guardrail_corrects_high_risk_arg(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )
        response = _valid_response()
        response = response.replace(
            '{"name":"EcuBOMName","value":"ZCUD1D01","evidence_ids":["ev_param"]}',
            '{"name":"EcuBOMName","value":"ZCUD","evidence_ids":["ev_param"]}',
        )
        response = response.replace(
            '<Arg ArgName=\\"EcuBOMName\\">ZCUD1D01</Arg>',
            '<Arg ArgName=\\"EcuBOMName\\">ZCUD</Arg>',
        )
        generation = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient([response]))

        checked = validate_and_repair_node_xml(generation, context, max_repair_attempts=0)

        self.assertTrue(checked.valid)
        self.assertEqual({arg.name: arg.value for arg in checked.script.args}["EcuBOMName"], "ZCUD1D01")
        self.assertEqual({arg.name: arg.value for arg in checked.raw_llm_args}["EcuBOMName"], "ZCUD")
        self.assertEqual({arg.name: arg.value for arg in checked.post_guardrail_args}["EcuBOMName"], "ZCUD1D01")
        self.assertEqual(checked.guardrail_corrections[0].correction_source, "deterministic_plan")

    def test_base_empty_required_arg_is_allowed_and_classname_is_normalized(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow_with_empty_position(),
            contract_registry=_contract_registry_with_empty_position(),
            order=1,
        )
        generation = generate_node_xml_with_llm(
            context,
            VllmModelConfig(),
            client=_FakeClient([_empty_position_missing_class_response()]),
        )

        checked = validate_and_repair_node_xml(generation, context, max_repair_attempts=0)

        self.assertTrue(checked.valid)
        self.assertEqual(checked.script.class_name, "GEEA30_VMM_Change")
        self.assertEqual({arg.name: arg.value for arg in checked.script.args}["Position"], "")

    def test_repair_request_failure_is_marked_for_review(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )
        broken = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient(['{"node_name":']))

        repaired = validate_and_repair_node_xml(
            broken,
            context,
            client=_RaisingClient(RuntimeError("context window exceeded")),
            max_repair_attempts=1,
        )

        self.assertFalse(repaired.valid)
        self.assertTrue(repaired.needs_review)
        self.assertEqual(len(repaired.repair_attempts), 1)
        self.assertTrue(any("context window exceeded" in issue.message for issue in repaired.xml_validation_errors))

    def test_repair_prompt_clips_raw_response(self) -> None:
        context = build_xml_generation_context(
            _node_bundle(),
            _template_registry(),
            _base_workflow(),
            contract_registry=_contract_registry(),
            order=1,
        )
        broken = generate_node_xml_with_llm(context, VllmModelConfig(), client=_FakeClient(["x" * 5000]))

        messages = build_node_xml_repair_messages(context, broken)

        self.assertIn("...[truncated]", messages[-1].content)
        self.assertLess(len(messages[-1].content), 5000)


class _FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.last_user_message = ""

    def chat(self, messages, extra_body=None, profile=None):
        self.last_user_message = messages[-1].content
        return self.responses.pop(0)


class _RaisingClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def chat(self, messages, extra_body=None, profile=None):
        raise self.exc


def _node_bundle() -> NodeEvidenceBundle:
    evidence = EvidenceUnit(
        evidence_id="ev_param",
        evidence_type="table",
        content="ZCUD1D01 Car Mode Change DID D134 Request Parameter 0x00",
        doc_id="doc",
        source_path="protocol.md",
        page_idx=3,
        metadata={"ecu": "ZCUD1D01", "block_role": "table_row"},
    )
    return NodeEvidenceBundle(
        step_key="step_001",
        node_name="ZCUD1D01_Car_Mode_Change_1",
        template_name="GEEA30_VMM_Change",
        row=1,
        column=1,
        candidates=[
            TaskEvidenceCandidate(
                evidence=evidence,
                score=20.0,
                matched_terms=["car mode change"],
                block_role="table_row",
                parameter_fields={"DID": "D134", "REQUEST_PARAMETER": "0x00"},
                kg_paths=["FlowTask:Car_Mode --uses--> DID:D134"],
            )
        ],
    )


def _template_registry() -> XmlTemplateRegistry:
    xml = (
        '<ScriptNode Name="ZCUD1D01_Car_Mode_Change_1" DeadMS="0" RetryTimes="0" '
        'ScriptType="NORMAL" InteruptStart="false" ClassName="GEEA30_VMM_Change">'
        "<Args><Arg ArgName=\"EcuBOMName\">ZCUD1D01</Arg>"
        "<Arg ArgName=\"DID\">0xD134</Arg>"
        "<Arg ArgName=\"RequestParameter\">0x00</Arg></Args></ScriptNode>"
    )
    entry = XmlTemplateEntry(
        template_id="template.xml::ZCUD1D01_Car_Mode_Change_1",
        node_name="ZCUD1D01_Car_Mode_Change_1",
        class_name="GEEA30_VMM_Change",
        root_tag="ScriptNode",
        source_path="template.xml",
        arg_names=["EcuBOMName", "DID", "RequestParameter"],
        arg_values={"EcuBOMName": "ZCUD1D01", "DID": "0xD134", "RequestParameter": "0x00"},
        xml=xml,
    )
    return XmlTemplateRegistry(
        source_dir=".",
        templates=[entry],
        by_class_name={"GEEA30_VMM_Change": [entry.template_id]},
        by_node_name={"ZCUD1D01_Car_Mode_Change_1": [entry.template_id]},
    )


def _contract_registry() -> XmlTemplateContractRegistry:
    contract = XmlTemplateClassContract(
        class_name="GEEA30_VMM_Change",
        template_count=1,
        required_args=["EcuBOMName", "DID", "RequestParameter"],
        optional_args=[],
        arg_contracts=[
            XmlTemplateArgContract(arg_name="EcuBOMName", observed_count=1, required=True),
            XmlTemplateArgContract(arg_name="DID", observed_count=1, required=True),
            XmlTemplateArgContract(arg_name="RequestParameter", observed_count=1, required=True),
        ],
        node_names=["ZCUD1D01_Car_Mode_Change_1"],
        source_paths=["template.xml"],
    )
    return XmlTemplateContractRegistry(
        class_contracts=[contract],
        by_class_name={"GEEA30_VMM_Change": contract},
    )


def _contract_registry_with_empty_position() -> XmlTemplateContractRegistry:
    contract = XmlTemplateClassContract(
        class_name="GEEA30_VMM_Change",
        template_count=1,
        required_args=["EcuBOMName", "DID", "RequestParameter", "Position"],
        optional_args=[],
        arg_contracts=[
            XmlTemplateArgContract(arg_name="EcuBOMName", observed_count=1, required=True),
            XmlTemplateArgContract(arg_name="DID", observed_count=1, required=True),
            XmlTemplateArgContract(arg_name="RequestParameter", observed_count=1, required=True),
            XmlTemplateArgContract(arg_name="Position", observed_count=1, required=True),
        ],
        node_names=["ZCUD1D01_Car_Mode_Change_1"],
        source_paths=["template.xml"],
    )
    return XmlTemplateContractRegistry(
        class_contracts=[contract],
        by_class_name={"GEEA30_VMM_Change": contract},
    )


def _base_workflow() -> str:
    return (
        '<?xml version="1.0"?><SerialNode Name="MAC_ALL" DeadMS="0"><Serials>'
        '<ScriptNode Name="ZCUD1D01_Car_Mode_Change_1" DeadMS="0" RetryTimes="0" '
        'ScriptType="NORMAL" InteruptStart="false" ClassName="GEEA30_VMM_Change">'
        "<Args><Arg ArgName=\"EcuBOMName\">ZCUD1D01</Arg>"
        "<Arg ArgName=\"DID\">0xD134</Arg>"
        "<Arg ArgName=\"RequestParameter\">0x00</Arg></Args></ScriptNode>"
        "</Serials></SerialNode>"
    )


def _base_workflow_with_empty_position() -> str:
    return (
        '<?xml version="1.0"?><SerialNode Name="MAC_ALL" DeadMS="0"><Serials>'
        '<ScriptNode Name="ZCUD1D01_Car_Mode_Change_1" DeadMS="0" RetryTimes="0" '
        'ScriptType="NORMAL" InteruptStart="false" ClassName="GEEA30_VMM_Change">'
        "<Args><Arg ArgName=\"EcuBOMName\">ZCUD1D01</Arg>"
        "<Arg ArgName=\"DID\">0xD134</Arg>"
        "<Arg ArgName=\"RequestParameter\">0x00</Arg>"
        "<Arg ArgName=\"Position\"></Arg></Args></ScriptNode>"
        "</Serials></SerialNode>"
    )


def _valid_response() -> str:
    return (
        '{"node_name":"ZCUD1D01_Car_Mode_Change_1","class_name":"GEEA30_VMM_Change",'
        '"args":[{"name":"EcuBOMName","value":"ZCUD1D01","evidence_ids":["ev_param"]},'
        '{"name":"DID","value":"0xD134","evidence_ids":["ev_param"]},'
        '{"name":"RequestParameter","value":"0x00","evidence_ids":["ev_param"]}],'
        '"xml":"<?xml version=\\"1.0\\"?><ScriptNode Name=\\"ZCUD1D01_Car_Mode_Change_1\\" '
        'DeadMS=\\"0\\" RetryTimes=\\"0\\" ScriptType=\\"NORMAL\\" InteruptStart=\\"false\\" '
        'ClassName=\\"GEEA30_VMM_Change\\"><Args><Arg ArgName=\\"EcuBOMName\\">ZCUD1D01</Arg>'
        '<Arg ArgName=\\"DID\\">0xD134</Arg><Arg ArgName=\\"RequestParameter\\">0x00</Arg>'
        '</Args></ScriptNode>",'
        '"evidence_map":{"EcuBOMName":["ev_param"],"DID":["ev_param"],"RequestParameter":["ev_param"]},'
        '"confidence":0.91,"needs_review":false,"missing_or_conflict_reason":""}'
    )


def _missing_required_response() -> str:
    return _valid_response().replace('<Arg ArgName=\\"DID\\">0xD134</Arg>', "")


def _repair_response() -> str:
    return _valid_response().replace('"confidence":0.91', '"confidence":0.88')


def _invalid_xml_response() -> str:
    return _valid_response().replace("</ScriptNode>", "")


def _unknown_evidence_response() -> str:
    return _valid_response().replace("ev_param", "ev_unknown")


def _empty_position_missing_class_response() -> str:
    return (
        '{"node_name":"ZCUD1D01_Car_Mode_Change_1","class_name":"GEEA30_VMM_Change",'
        '"args":[{"name":"EcuBOMName","value":"ZCUD1D01","evidence_ids":["ev_param"]},'
        '{"name":"DID","value":"0xD134","evidence_ids":["ev_param"]},'
        '{"name":"RequestParameter","value":"0x00","evidence_ids":["ev_param"]},'
        '{"name":"Position","value":"","evidence_ids":[]}],'
        '"xml":"<ScriptNode Name=\\"ZCUD1D01_Car_Mode_Change_1\\" DeadMS=\\"0\\" RetryTimes=\\"0\\" '
        'ScriptType=\\"NORMAL\\" InteruptStart=\\"false\\"><Args>'
        '<Arg ArgName=\\"EcuBOMName\\">ZCUD1D01</Arg><Arg ArgName=\\"DID\\">0xD134</Arg>'
        '<Arg ArgName=\\"RequestParameter\\">0x00</Arg><Arg ArgName=\\"Position\\"></Arg>'
        '</Args></ScriptNode>",'
        '"evidence_map":{"EcuBOMName":["ev_param"],"DID":["ev_param"],"RequestParameter":["ev_param"]},'
        '"confidence":0.91,"needs_review":false,"missing_or_conflict_reason":""}'
    )


if __name__ == "__main__":
    unittest.main()
