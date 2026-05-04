"""Tests for vector indexing, dense retrieval, and hybrid merge."""

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.indexing.local_builder import EVIDENCE_UNITS_FILE, KNOWLEDGE_UNITS_FILE, SUMMARY_FILE  # noqa: E402
from diagnostic_platform.indexing.vector_index import build_vector_index  # noqa: E402
from diagnostic_platform.retrieval.embedding import HashingEmbeddingClient  # noqa: E402
from diagnostic_platform.retrieval.local_sparse import query_local_index  # noqa: E402
from diagnostic_platform.retrieval.vector_store import InMemoryVectorStore, VectorRecord  # noqa: E402
from diagnostic_platform.schemas import (  # noqa: E402
    EvidenceUnit,
    KnowledgeUnit,
    RetrievalFilters,
    RetrievalQueryRequest,
    VectorIndexBuildRequest,
)


class VectorIndexRetrievalTest(unittest.TestCase):
    def test_build_vector_index_and_dense_query_with_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = _fixture_index(Path(tmpdir))
            store = InMemoryVectorStore(collection_name="diagnostic_knowledge_fixture")
            embedder = HashingEmbeddingClient(dimension=32)

            result = build_vector_index(
                VectorIndexBuildRequest(
                    index_dir=str(index_dir),
                    collection_name="diagnostic_knowledge_fixture",
                    embedding_model=embedder.model_name,
                    batch_size=2,
                    drop_existing=True,
                ),
                embedding_client=embedder,
                vector_store=store,
            )

            self.assertEqual(result.vector_count, 3)
            self.assertEqual(result.knowledge_unit_count, 2)
            self.assertEqual(result.evidence_unit_count, 1)
            self.assertTrue(result.validation["valid"])
            self.assertEqual(result.validation["checks"]["collection"]["row_count"], 3)
            self.assertTrue(Path(result.manifest_path).exists())
            self.assertEqual(len(store.records), 3)
            self.assertEqual(store.records["ku:doc1_text_0001"].metadata["doc_id"], "doc1")

            response = query_local_index(
                RetrievalQueryRequest(
                    query="Change VMM DID D134",
                    index_dir=str(index_dir),
                    filters=RetrievalFilters(ecu="ZCUD1D01", dids=["D134"]),
                    enable_sparse=False,
                    enable_dense=True,
                    dense_top_k=2,
                    vector_collection="diagnostic_knowledge_fixture",
                    embedding_model=embedder.model_name,
                ),
                embedding_client=embedder,
                vector_store=store,
            )

            self.assertTrue(response.chunks)
            self.assertTrue(any("D134" in chunk.content for chunk in response.chunks))
            self.assertEqual(response.chunks[0].source, "dense")
            self.assertEqual(response.trace["dense"]["collection_name"], "diagnostic_knowledge_fixture")
            self.assertTrue(
                any(chunk.metadata.get("source_evidence_id") == "legacy_ev_doc1_row_1" for chunk in response.chunks)
            )
            self.assertIn("chunk_id=", response.context)

    def test_hybrid_query_merges_sparse_and_dense_by_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = _fixture_index(Path(tmpdir))
            store = InMemoryVectorStore(collection_name="diagnostic_knowledge_fixture")
            embedder = HashingEmbeddingClient(dimension=32)
            build_vector_index(
                VectorIndexBuildRequest(
                    index_dir=str(index_dir),
                    collection_name="diagnostic_knowledge_fixture",
                    embedding_model=embedder.model_name,
                    drop_existing=True,
                ),
                embedding_client=embedder,
                vector_store=store,
            )

            response = query_local_index(
                RetrievalQueryRequest(
                    query="Change VMM D134",
                    index_dir=str(index_dir),
                    filters=RetrievalFilters(ecu="ZCUD1D01", dids=["D134"]),
                    top_k=2,
                    enable_sparse=True,
                    enable_dense=True,
                    dense_top_k=2,
                    hybrid_top_k=2,
                    vector_collection="diagnostic_knowledge_fixture",
                    embedding_model=embedder.model_name,
                ),
                embedding_client=embedder,
                vector_store=store,
            )

            self.assertTrue(response.chunks)
            self.assertEqual(response.chunks[0].chunk_id, "doc1_text_0001")
            self.assertEqual(response.chunks[0].source, "hybrid")
            self.assertEqual(response.chunks[0].metadata["hybrid_sources"], ["dense", "sparse"])
            self.assertTrue(response.trace["hybrid"]["results"])

    def test_in_memory_vector_store_metadata_filter_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            index_dir = _fixture_index(Path(tmpdir))
            store = InMemoryVectorStore(collection_name="diagnostic_knowledge_fixture")
            embedder = HashingEmbeddingClient(dimension=16)
            build_vector_index(
                VectorIndexBuildRequest(
                    index_dir=str(index_dir),
                    collection_name="diagnostic_knowledge_fixture",
                    embedding_model=embedder.model_name,
                    drop_existing=True,
                ),
                embedding_client=embedder,
                vector_store=store,
            )

            query_vector = embedder.embed_texts(["battery voltage"])[0]
            results = store.search(
                query_vector,
                top_k=5,
                filters=RetrievalFilters(module="power", ecu="BCM"),
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].record.source_id, "doc2_text_0001")
            self.assertEqual(results[0].record.metadata["source_path"], "doc2.md")

    def test_vector_store_search_deduplicates_same_source_id(self) -> None:
        store = InMemoryVectorStore(collection_name="diagnostic_knowledge_fixture")
        store.ensure_collection(dimension=2, metric_type="COSINE", drop_existing=True)
        store.upsert(
            [
                VectorRecord(primary_id="ku:shared", source_id="shared", record_type="knowledge_unit", content="a", embedding=[1, 0]),
                VectorRecord(primary_id="ev:shared", source_id="shared", record_type="evidence_unit", content="a", embedding=[1, 0]),
                VectorRecord(primary_id="ku:other", source_id="other", record_type="knowledge_unit", content="b", embedding=[0.5, 0.5]),
            ]
        )

        results = store.search([1, 0], top_k=3)

        self.assertEqual([result.record.source_id for result in results], ["shared", "other"])


def _fixture_index(root: Path) -> Path:
    index_dir = root / "index"
    index_dir.mkdir()
    knowledge_units = [
        KnowledgeUnit(
            chunk_id="doc1_text_0001",
            unit_type="text_chunk",
            content="ZCUD1D01 Change VMM uses DID D134 with 2F service.",
            protocol="UDS",
            module="body",
            ecu="ZCUD1D01",
            service_ids=["2F"],
            dids=["D134"],
            page_idx=3,
            source_path="doc1.md",
        ),
        KnowledgeUnit(
            chunk_id="doc2_text_0001",
            unit_type="text_chunk",
            content="BCM battery voltage check uses DID 1234.",
            protocol="UDS",
            module="power",
            ecu="BCM",
            service_ids=["22"],
            dids=["1234"],
            page_idx=4,
            source_path="doc2.md",
        ),
    ]
    evidence_units = [
        EvidenceUnit(
            evidence_id="ev_doc1_row_1",
            evidence_type="table",
            content="Parameter row: ECU ZCUD1D01 DID D134 request parameter 0x00.",
            doc_id="doc1",
            source_path="doc1.md",
            page_idx=3,
            metadata={
                "module": "body",
                "ecu": "ZCUD1D01",
                "dids": ["D134"],
                "service_ids": ["2F"],
                "source_evidence_id": "legacy_ev_doc1_row_1",
            },
        )
    ]
    _write_jsonl(index_dir / KNOWLEDGE_UNITS_FILE, knowledge_units)
    _write_jsonl(index_dir / EVIDENCE_UNITS_FILE, evidence_units)
    (index_dir / SUMMARY_FILE).write_text(json.dumps({"collection_name": "fixture"}), encoding="utf-8")
    return index_dir


def _write_jsonl(path: Path, models: list) -> None:
    with path.open("w", encoding="utf-8") as file:
        for model in models:
            file.write(json.dumps(model.model_dump(mode="json"), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    unittest.main()
