"""Tests for MinerU normalization."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.normalizer.mineru import normalize_request
from diagnostic_platform.schemas import DocumentMetadata, MinerUContentBlock, NormalizeMinerURequest


class MinerUNormalizerTest(unittest.TestCase):
    def test_normalize_text_block(self) -> None:
        request = NormalizeMinerURequest(
            metadata=DocumentMetadata(
                doc_id="uds_demo_v1",
                protocol="UDS",
                module="security_access",
                ecu="BCM",
                source_path="data/raw/demo.pdf",
            ),
            blocks=[
                MinerUContentBlock(
                    type="text",
                    text="Enter Extended Session with 10 03 before Security Access level 05.",
                    page_idx=0,
                    bbox=[1, 2, 3, 4],
                )
            ],
        )

        units = normalize_request(request)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].unit_type, "text_chunk")
        self.assertIn("10", units[0].service_ids)
        self.assertIn("03", units[0].sessions)
        self.assertIn("05", units[0].security_levels)


if __name__ == "__main__":
    unittest.main()
