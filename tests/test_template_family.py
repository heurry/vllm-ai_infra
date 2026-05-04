from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.schemas import FlowNode, TemplateFamilyRegistryBuildRequest
from diagnostic_platform.generation.class_xml_prompt import build_class_xml_generation_messages
from diagnostic_platform.schemas import EvidenceUnit, NodeEvidenceBundle, TaskEvidenceCandidate, TemplateFamilyResolution
from diagnostic_platform.template_family.registry_builder import build_template_family_registry
from diagnostic_platform.template_family.resolver import resolve_template_family
from diagnostic_platform.template_family.xml_signature import extract_tasknode_signature, family_id_from_class_name
from diagnostic_platform.validation.tasknode_validator import validate_tasknode_xml
from diagnostic_platform.validation.template_family_validator import validate_tasknode_against_family


class TemplateFamilyTest(unittest.TestCase):
    def test_family_id_generalizes_ecu_specific_classes(self) -> None:
        self.assertEqual(family_id_from_class_name("ADCU1301_DTC_Clear_Type7"), "DTC_Clear_Type7")
        self.assertEqual(family_id_from_class_name("ASDM1401_DTC_Clear_Type7"), "DTC_Clear_Type7")
        self.assertEqual(family_id_from_class_name("DTC_Read_Type1"), "DTC_Read_Type1")

    def test_build_registry_links_xml_signature_and_flowchart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template_dir = root / "templates"
            template_dir.mkdir()
            (template_dir / "ADCU1301_DTC_Clear_Type7.xml").write_text(_tasknode_xml("0xD40292"), encoding="utf-8")
            (template_dir / "ASDM1401_DTC_Clear_Type7.xml").write_text(_tasknode_xml("0xD40392"), encoding="utf-8")
            flowchart = root / "流程图.md"
            flowchart.write_text(
                "2.3.1.3.5.7 DTC Clear Type7(Single DTC NRC Allowed)\n"
                "Req: 14 xx xx xx\nRes: 54\nRetry <= 3\n",
                encoding="utf-8",
            )

            registry = build_template_family_registry(
                TemplateFamilyRegistryBuildRequest(
                    xml_template_dir=str(template_dir),
                    flowchart_markdown_path=str(flowchart),
                    include_xml_examples=True,
                )
            )

        family = registry.by_family_id["DTC_Clear_Type7"]
        self.assertEqual(set(family.template_class_names), {"ADCU1301_DTC_Clear_Type7", "ASDM1401_DTC_Clear_Type7"})
        self.assertEqual(len(family.flowcharts), 1)
        self.assertIn("14", family.merged_signature.service_ids)
        self.assertNotIn("31", family.merged_signature.service_ids)
        self.assertIn("54", family.merged_signature.positive_responses)
        self.assertIn("dtc_hex_values", family.required_evidence)
        self.assertIn("flowchart_description", family.required_evidence)

    def test_resolver_uses_template_name_and_evidence_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template_dir = root / "templates"
            template_dir.mkdir()
            (template_dir / "ASDM1401_DTC_Clear_Type7.xml").write_text(_tasknode_xml("0xD40292"), encoding="utf-8")
            flowchart = root / "流程图.md"
            flowchart.write_text(
                "2.3.1.3.5.7 DTC Clear Type7(Single DTC NRC Allowed)\nReq: 14 xx xx xx\nRes: 54\n",
                encoding="utf-8",
            )
            registry = build_template_family_registry(
                TemplateFamilyRegistryBuildRequest(
                    xml_template_dir=str(template_dir),
                    flowchart_markdown_path=str(flowchart),
                    include_xml_examples=False,
                )
            )
            resolution = resolve_template_family(
                node=FlowNode(
                    raw="ASDM_DTC_Clear(ASDM1401_DTC_Clear_Type7)",
                    name="ASDM_DTC_Clear",
                    template_name="ASDM1401_DTC_Clear_Type7",
                    sheet="flow",
                    row=1,
                    column=1,
                ),
                registry=registry,
                evidence_texts=["Refer to the EOL process-DTC Clear Type7(Single DTC NRC Allowed)"],
            )

        self.assertEqual(resolution.status, "found")
        self.assertEqual(resolution.family_id, "DTC_Clear_Type7")
        self.assertIn("exact_template_class", resolution.match_reasons)

    def test_tasknode_and_family_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_path = root / "ASDM1401_DTC_Clear_Type7.xml"
            xml = _tasknode_xml("0xD40292")
            xml_path.write_text(xml, encoding="utf-8")
            signature = extract_tasknode_signature(xml_path)
            family = build_template_family_registry(
                TemplateFamilyRegistryBuildRequest(
                    xml_template_dir=str(root),
                    flowchart_markdown_path=str(root / "missing.md"),
                    include_xml_examples=False,
                )
            ).by_family_id["DTC_Clear_Type7"]

        self.assertIsNotNone(signature)
        self.assertTrue(validate_tasknode_xml(xml).valid)
        self.assertTrue(validate_tasknode_against_family(xml, family).valid)
        self.assertFalse(validate_tasknode_xml('<ScriptNode Name="N" ClassName="C" />').valid)

    def test_class_xml_prompt_prioritizes_parent_table_context(self) -> None:
        family = build_template_family_registry(
            TemplateFamilyRegistryBuildRequest(
                xml_template_dir=str(_write_template_dir()),
                flowchart_markdown_path="",
                include_xml_examples=False,
            )
        ).by_family_id["DTC_Clear_Type7"]
        parent = TaskEvidenceCandidate(
            evidence=EvidenceUnit(
                evidence_id="parent_section",
                evidence_type="text",
                content="<table><tr><td>HEX</td></tr><tr><td>D40292</td></tr></table>",
                doc_id="ASDM",
                source_path="asdm.md",
                page_idx=0,
                metadata={"module": "ecu_parameter", "block_role": "section_text"},
            ),
            score=80,
            block_role="section_text",
            retrieval_source="parent_context",
        )
        row = TaskEvidenceCandidate(
            evidence=EvidenceUnit(
                evidence_id="row_1",
                evidence_type="table",
                content="HEX: D40292 NRC ALLOWED: 0x31",
                doc_id="ASDM",
                source_path="asdm.md",
                page_idx=0,
                metadata={"module": "ecu_parameter", "block_role": "table_row"},
            ),
            score=81,
            block_role="table_row",
        )
        bundle = NodeEvidenceBundle(
            step_key="step_001",
            node_name="ASDM_DTC_Clear",
            template_name="ASDM1401_DTC_Clear_Type7",
            candidates=[row, parent],
        )
        messages = build_class_xml_generation_messages(
            node_bundle=bundle,
            family_resolution=TemplateFamilyResolution(
                node_name=bundle.node_name,
                template_name=bundle.template_name,
                family_id=family.family_id,
                status="found",
                family=family,
            ),
        )
        payload = __import__("json").loads(messages[2].content.split("\n", 1)[1])
        self.assertEqual(payload["candidate_evidence"][0]["evidence_id"], "parent_section")


def _tasknode_xml(dtc_hex: str) -> str:
    return f"""<?xml version="1.0"?>
<TaskNode ID="0">
  <MainNodes>
    <MainNode ID="0">
      <AnnNode ID="0">
        <CVNodes>
          <CreateVarNode ID="0" Name="SourceAddress" Type="Bytes" VarDefaultValue="Bytes.Empty" />
          <CreateVarNode ID="1" Name="ECUAddress" Type="Bytes" VarDefaultValue="Bytes.Empty" />
          <CreateVarNode ID="2" Name="ClearDTC" Type="Bytes" VarDefaultValue="Bytes.Empty" />
          <CreateVarNode ID="3" Name="AllowedNRC" Type="Bytes" VarDefaultValue="Bytes.Empty" />
          <CreateVarNode ID="4" Name="RetryCount" Type="int" VarDefaultValue="0" />
        </CVNodes>
      </AnnNode>
      <FlowsNodes>
        <NormalBoxNode ID="1" GotoCatchError="-1" Goto="2">
          <OptNodes>
            <AssignNode ID="0" LeftVar="SourceAddress" RightExpression="Bytes.Parse(_argLibrary.Get(&quot;SourceAddress&quot;))" />
            <AssignNode ID="1" LeftVar="ClearDTC" RightExpression="Bytes.Parse(&quot;{dtc_hex}&quot;)" />
            <AssignNode ID="2" LeftVar="AllowedNRC" RightExpression="Bytes.Parse(&quot;0x31&quot;)" />
            <AssignNode ID="3" LeftVar="RetryCount" RightExpression="3" />
          </OptNodes>
        </NormalBoxNode>
        <NormalBoxNode ID="2" GotoCatchError="-1" Goto="3">
          <OptNodes>
            <CallFunctionNode ID="0" FunctionName="_udsLibrary.SendUDSRawData">
              <InputVars><Param>Bytes.Concat(SourceAddress,ECUAddress,Bytes.Parse(&quot;0x14&quot;),ClearDTC)</Param></InputVars>
            </CallFunctionNode>
          </OptNodes>
        </NormalBoxNode>
        <NormalBoxNode ID="3" GotoCatchError="-1" Goto="0">
          <GotoPairs>
            <GotoPair><Expression>ResponseByteData.Slice(0,1).SequenceEqual(Bytes.Parse("0x54"))</Expression><Goto>0</Goto></GotoPair>
            <GotoPair><Expression>RetryCount &gt; 0</Expression><Goto>4</Goto></GotoPair>
          </GotoPairs>
        </NormalBoxNode>
        <NormalBoxNode ID="4" GotoCatchError="-1" Goto="2">
          <OptNodes><AssignNode ID="0" LeftVar="RetryCount" RightExpression="RetryCount-1" /></OptNodes>
        </NormalBoxNode>
      </FlowsNodes>
    </MainNode>
  </MainNodes>
</TaskNode>
"""


def _write_template_dir() -> Path:
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name)
    # Keep the temporary directory alive for the duration of the test process.
    _TEMP_DIRS.append(tmp)
    (path / "ASDM1401_DTC_Clear_Type7.xml").write_text(_tasknode_xml("0xD40292"), encoding="utf-8")
    return path


_TEMP_DIRS: list[tempfile.TemporaryDirectory] = []


if __name__ == "__main__":
    unittest.main()
