"""Opt-in integration smoke test for a real Milvus service."""

from pathlib import Path
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.indexing.local_builder import EVIDENCE_UNITS_FILE, KNOWLEDGE_UNITS_FILE, SUMMARY_FILE  # noqa: E402
from diagnostic_platform.indexing.vector_index import build_vector_index  # noqa: E402
from diagnostic_platform.retrieval.embedding import HashingEmbeddingClient  # noqa: E402
from diagnostic_platform.retrieval.local_sparse import query_local_index  # noqa: E402
from diagnostic_platform.retrieval.vector_store import MilvusVectorStore  # noqa: E402
from diagnostic_platform.schemas import (  # noqa: E402
    EvidenceUnit,
    KnowledgeUnit,
    RetrievalQueryRequest,
    VectorIndexBuildRequest,
)


@unittest.skipUnless(os.environ.get("RUN_MILVUS_INTEGRATION") == "1", "set RUN_MILVUS_INTEGRATION=1 to use a real Milvus service")
class MilvusIntegrationTest(unittest.TestCase):
    def test_build_and_query_real_milvus_collection(self) -> None:
        uri = os.environ.get("MILVUS_URI", "http://127.0.0.1:19530")
        collection = f"diagnostic_knowledge_smoke_{int(time.time())}"
        store = MilvusVectorStore(uri=uri, collection_name=collection)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                index_dir = _fixture_index(Path(tmpdir))
                embedder = HashingEmbeddingClient(dimension=16)
                result = build_vector_index(
                    VectorIndexBuildRequest(
                        index_dir=str(index_dir),
                        collection_name=collection,
                        milvus_uri=uri,
                        embedding_model=embedder.model_name,
                        drop_existing=True,
                    ),
                    embedding_client=embedder,
                    vector_store=store,
                )
                self.assertTrue(result.validation["valid"])

                response = query_local_index(
                    RetrievalQueryRequest(
                        query="ZCUD1D01 Change VMM D134",
                        index_dir=str(index_dir),
                        enable_dense=True,
                        enable_sparse=True,
                        vector_collection=collection,
                        milvus_uri=uri,
                        embedding_model=embedder.model_name,
                    ),
                    embedding_client=embedder,
                    vector_store=store,
                )
                self.assertTrue(response.chunks)
                self.assertTrue(response.trace["dense"]["results"])
        finally:
            if getattr(store, "client", None) and store.client.has_collection(collection):
                store.client.drop_collection(collection)


def _fixture_index(root: Path) -> Path:
    index_dir = root / "index"
    index_dir.mkdir()
    _write_jsonl(
        index_dir / KNOWLEDGE_UNITS_FILE,
        [
            KnowledgeUnit(
                chunk_id="doc1_text_0001",
                unit_type="text_chunk",
                content="ZCUD1D01 Change VMM DID D134 service 2F.",
                protocol="UDS",
                module="body",
                ecu="ZCUD1D01",
                service_ids=["2F"],
                dids=["D134"],
                page_idx=1,
                source_path="doc1.md",
            )
        ],
    )
    _write_jsonl(
        index_dir / EVIDENCE_UNITS_FILE,
        [
            EvidenceUnit(
                evidence_id="ev_doc1_row_1",
                evidence_type="table",
                content="ZCUD1D01 Change VMM DID D134 request parameter 0x00.",
                doc_id="doc1",
                source_path="doc1.md",
                page_idx=1,
                metadata={"ecu": "ZCUD1D01", "dids": ["D134"], "service_ids": ["2F"]},
            )
        ],
    )
    (index_dir / SUMMARY_FILE).write_text(json.dumps({"collection_name": "smoke"}), encoding="utf-8")
    return index_dir


def _write_jsonl(path: Path, models: list) -> None:
    with path.open("w", encoding="utf-8") as file:
        for model in models:
            file.write(json.dumps(model.model_dump(mode="json"), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
