"""Tests for flow.xlsx parsing."""

from pathlib import Path
import sys
import tempfile
import unittest
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.ingestion.flow_excel import parse_flow_xlsx


class FlowExcelTest(unittest.TestCase):
    def test_parse_columns_as_serial_steps_and_rows_as_parallel_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "flow.xlsx"
            _write_minimal_xlsx(path)

            plan = parse_flow_xlsx(path)

        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[0].step_key, "step_001")
        self.assertEqual(plan.steps[0].row, 1)
        self.assertEqual(plan.steps[0].column, 1)
        self.assertEqual(len(plan.steps[0].parallel_nodes), 1)
        self.assertEqual(plan.steps[0].parallel_nodes[0].name, "Car_Mode_Change_1")
        self.assertEqual(plan.steps[0].parallel_nodes[0].template_name, "GEEA30_VMM_Change")
        self.assertEqual(plan.steps[1].column, 2)
        self.assertEqual(len(plan.steps[1].parallel_nodes), 2)
        self.assertEqual(plan.steps[1].parallel_nodes[0].name, "CMD1A1C_DTC_Read")
        self.assertEqual(plan.steps[1].parallel_nodes[1].name, "TCAM1101_DTC_Read")


def _write_minimal_xlsx(path: Path) -> None:
    workbook = """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""
    sheet = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>Car_Mode_Change_1 (GEEA30_VMM_Change)</t></is></c>
      <c r="B1" t="inlineStr"><is><t>CMD1A1C_DTC_Read (DTC_Read_Type1)</t></is></c>
    </row>
    <row r="2">
      <c r="B2" t="inlineStr"><is><t>TCAM1101_DTC_Read (DTC_Read_Type1)</t></is></c>
    </row>
  </sheetData>
</worksheet>
"""
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


if __name__ == "__main__":
    unittest.main()
