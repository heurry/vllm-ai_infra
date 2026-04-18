"""Tests for MinerU ingestion integration."""

from pathlib import Path
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.ingestion.mineru_client import (
    build_mineru_command,
    load_mineru_output,
    parse_document_with_mineru,
)
from diagnostic_platform.normalizer.mineru import normalize_request
from diagnostic_platform.schemas import DocumentMetadata, LoadMinerUOutputRequest, ParseDocumentRequest


class MinerUIngestionTest(unittest.TestCase):
    def test_load_content_list_to_normalize_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "mineru_out"
            parse_dir = output_dir / "sample" / "auto"
            parse_dir.mkdir(parents=True)
            (parse_dir / "images").mkdir()
            (parse_dir / "sample_content_list.json").write_text(
                json.dumps(
                    [
                        {
                            "type": "text",
                            "text": "Enter extended session with 10 03.",
                            "text_level": 1,
                            "bbox": [1, 2, 3, 4],
                            "page_idx": 0,
                        },
                        {
                            "type": "table",
                            "table_caption": ["Parameter table"],
                            "table_body": "<table><tr><td>DID</td><td>D134</td></tr></table>",
                            "bbox": [10.2, 20.8, 30, 40],
                            "page_idx": 1,
                        },
                        {
                            "type": "image",
                            "img_path": "images/flow.jpg",
                            "image_caption": ["Flowchart branch to DTC read."],
                            "bbox": [5, 6, 7, 8],
                            "page_idx": 2,
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            request = LoadMinerUOutputRequest(
                output_dir=str(output_dir),
                metadata=DocumentMetadata(doc_id="sample", ecu="ZCUD1D01", source_path="sample.pdf"),
            )
            normalized = load_mineru_output(request)

            self.assertEqual(len(normalized.blocks), 3)
            self.assertEqual(normalized.blocks[1].table_body, "<table><tr><td>DID</td><td>D134</td></tr></table>")
            self.assertTrue(normalized.blocks[2].img_path.endswith("/images/flow.jpg"))

            units = normalize_request(normalized)
            self.assertIn("03", units[0].sessions)
            self.assertIn("D134", units[1].dids)

    def test_markdown_fallback_when_content_list_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "mineru_out"
            output_dir.mkdir()
            (output_dir / "sample.md").write_text("# Sample\n\nDID D134 request 22.", encoding="utf-8")

            normalized = load_mineru_output(
                LoadMinerUOutputRequest(
                    output_dir=str(output_dir),
                    metadata=DocumentMetadata(doc_id="sample"),
                )
            )

            self.assertEqual(len(normalized.blocks), 1)
            self.assertIn("D134", normalized.blocks[0].text or "")

    def test_build_command_includes_core_options(self) -> None:
        request = ParseDocumentRequest(
            source_path="/tmp/input.pdf",
            output_dir="/tmp/out",
            metadata=DocumentMetadata(doc_id="input"),
            mineru_executable="/opt/bin/mineru",
            backend="pipeline",
            method="ocr",
            lang="en",
            end_page_id=2,
            device_mode="cuda:0",
            model_source="local",
        )

        command = build_mineru_command(request)

        self.assertEqual(command[:5], ["/opt/bin/mineru", "-p", "/tmp/input.pdf", "-o", "/tmp/out"])
        self.assertIn("ocr", command)
        self.assertIn("cuda:0", command)
        self.assertIn("local", command)

    def test_parse_document_with_fake_mineru_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            fake_mineru = temp_path / "fake_mineru"
            fake_mineru.write_text(
                """#!/bin/sh
out=""
input=""
method="auto"
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|--output) shift; out="$1" ;;
    -p|--path) shift; input="$1" ;;
    -m|--method) shift; method="$1" ;;
  esac
  shift
done
stem="$(basename "$input" .pdf)"
parse_dir="$out/$stem/$method"
mkdir -p "$parse_dir"
printf '# parsed\\n' > "$parse_dir/$stem.md"
printf '[{"type":"text","text":"DID D134 request 22.","bbox":[1,2,3,4],"page_idx":0}]' > "$parse_dir/${stem}_content_list.json"
""",
                encoding="utf-8",
            )
            os.chmod(fake_mineru, 0o755)
            pdf_path = temp_path / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            output_dir = temp_path / "out"

            result = parse_document_with_mineru(
                ParseDocumentRequest(
                    source_path=str(pdf_path),
                    output_dir=str(output_dir),
                    metadata=DocumentMetadata(doc_id="sample"),
                    mineru_executable=str(fake_mineru),
                    timeout_seconds=10,
                )
            )

            self.assertEqual(result.return_code, 0)
            self.assertEqual(len(result.files.content_list_files), 1)
            self.assertIsNotNone(result.normalized)
            self.assertEqual(result.normalized.blocks[0].text, "DID D134 request 22.")


if __name__ == "__main__":
    unittest.main()
