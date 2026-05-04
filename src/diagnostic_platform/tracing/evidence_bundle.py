"""Build deterministic Graph RAG evidence bundles for flow steps."""

from __future__ import annotations

from diagnostic_platform.graph.full_kg import GraphRepository
from diagnostic_platform.retrieval.embedding import EmbeddingClient
from diagnostic_platform.retrieval.graph_rag import (
    extract_parameter_fields,
    retrieve_task_evidence_candidates,
    task_aliases,
)
from diagnostic_platform.retrieval.local_sparse import query_local_index
from diagnostic_platform.retrieval.vector_store import VectorStore
from diagnostic_platform.schemas import (
    BuildStepEvidenceRequest,
    BuildStepEvidenceResponse,
    EvidenceMatch,
    EvidenceUnit,
    FlowNode,
    RetrievedChunk,
    RetrievalFilters,
    RetrievalQueryRequest,
    NodeEvidenceBundle,
    StepEvidenceBundle,
    TaskEvidenceCandidate,
)
from diagnostic_platform.tracing.evidence_chain import (
    build_query_plan,
    build_reference_aware_evidence_bundle,
)


def build_step_evidence_bundles(
    request: BuildStepEvidenceRequest,
    graph_repository: GraphRepository | None = None,
    *,
    embedding_client: EmbeddingClient | None = None,
    vector_store: VectorStore | None = None,
) -> BuildStepEvidenceResponse:
    """Link each flow node to the most relevant evidence units."""

    evidence_by_id = {evidence.evidence_id: evidence for evidence in request.evidence_units}
    bundles: list[StepEvidenceBundle] = []
    for step in request.flow_plan.steps:
        node_bundles: list[NodeEvidenceBundle] = []
        for node in step.parallel_nodes:
            if graph_repository is not None:
                candidates = graph_repository.query_task_candidates(node=node, top_k=request.top_k_per_node)
            else:
                candidates = retrieve_task_evidence_candidates(
                    node=node,
                    evidence_units=request.evidence_units,
                    top_k=request.top_k_per_node,
                )
            retrieval_trace: dict = {}
            if request.enable_dense:
                retrieval_candidates, retrieval_trace = _retrieve_hybrid_candidates(
                    node=node,
                    request=request,
                    evidence_by_id=evidence_by_id,
                    embedding_client=embedding_client,
                    vector_store=vector_store,
                )
                candidates = _merge_graph_and_retrieval_candidates(
                    graph_candidates=candidates,
                    retrieval_candidates=retrieval_candidates,
                    top_k=max(request.top_k_per_node, request.hybrid_top_k),
                )
            graph_paths = _candidate_graph_paths(candidates)
            notes = [] if candidates else ["no_graph_rag_candidate"]
            if request.enable_dense and not retrieval_trace.get("hybrid", {}).get("results"):
                notes.append("no_dense_hybrid_candidate")
            node_bundle = NodeEvidenceBundle(
                step_key=step.step_key,
                node_name=node.name,
                template_name=node.template_name,
                row=node.row,
                column=node.column,
                matches=_matches_from_candidates(candidates),
                candidates=candidates,
                graph_paths=graph_paths,
                retrieval_trace=retrieval_trace,
                notes=notes,
            )
            node_bundle = build_reference_aware_evidence_bundle(
                node_bundle=node_bundle,
                flow_node=node,
                evidence_by_id=evidence_by_id,
                graph_repository=graph_repository,
                max_reference_depth=request.reference_max_depth,
                reference_limit=request.reference_limit,
            )
            node_bundles.append(
                node_bundle.model_copy(update={"matches": _matches_from_candidates(node_bundle.candidates)})
            )

        bundles.append(
            StepEvidenceBundle(
                step_key=step.step_key,
                display_name=step.display_name,
                order=step.order,
                row=step.row,
                column=step.column,
                node_bundles=node_bundles,
            )
        )
    return BuildStepEvidenceResponse(source_flow_path=request.flow_plan.source_path, bundles=bundles)


def _candidate_graph_paths(candidates) -> list[str]:
    paths: list[str] = []
    for candidate in candidates:
        for path in candidate.graph_paths:
            if path and path not in paths:
                paths.append(path)
    return paths


def _matches_from_candidates(candidates: list[TaskEvidenceCandidate]) -> list[EvidenceMatch]:
    return [
        EvidenceMatch(
            evidence=candidate.evidence,
            score=candidate.score,
            matched_terms=candidate.matched_terms,
            source=candidate.retrieval_source,
            metadata={
                "retrieval_rank": candidate.retrieval_rank,
                "retrieval_score": candidate.retrieval_score,
                "retrieval_metadata": candidate.retrieval_metadata,
                "score_breakdown": candidate.score_breakdown,
                "hybrid_sources": candidate.evidence.metadata.get("hybrid_sources") or [],
            },
        )
        for candidate in candidates
    ]


def _retrieve_hybrid_candidates(
    *,
    node: FlowNode,
    request: BuildStepEvidenceRequest,
    evidence_by_id: dict[str, EvidenceUnit],
    embedding_client: EmbeddingClient | None,
    vector_store: VectorStore | None,
) -> tuple[list[TaskEvidenceCandidate], dict]:
    query_plan = build_query_plan(node)
    if not request.index_dir:
        return [], {
            "enabled": False,
            "error": "index_dir_required_for_dense_retrieval",
            "query_plan": query_plan.model_dump(mode="json"),
        }

    candidates_by_id: dict[str, TaskEvidenceCandidate] = {}
    trace: dict = {
        "enabled": True,
        "query": _node_query(node),
        "rewritten_query": _node_query(node),
        "query_plan": query_plan.model_dump(mode="json"),
        "queries": [],
        "sparse": {"enabled": True, "top_k": request.top_k_per_node, "results": []},
        "dense": {"enabled": True, "top_k": request.dense_top_k, "results": []},
        "hybrid": {
            "enabled": True,
            "top_k": request.hybrid_top_k,
            "dense_weight": request.dense_weight,
            "sparse_weight": request.sparse_weight,
            "results": [],
        },
    }

    for query_index, query_spec in enumerate(query_plan.queries, start=1):
        retrieval_request = RetrievalQueryRequest(
            query=query_spec.query,
            index_dir=request.index_dir,
            filters=RetrievalFilters(),
            top_k=request.top_k_per_node,
            enable_sparse=True,
            enable_dense=True,
            dense_top_k=request.dense_top_k,
            hybrid_top_k=request.hybrid_top_k,
            dense_weight=request.dense_weight,
            sparse_weight=request.sparse_weight,
            milvus_uri=request.milvus_uri,
            vector_collection=request.vector_collection,
            embedding_model=request.embedding_model,
            vector_manifest_path=request.vector_manifest_path,
        )
        response = query_local_index(
            retrieval_request,
            embedding_client=embedding_client,
            vector_store=vector_store,
        )
        query_trace = dict(response.trace)
        trace["queries"].append(
            {
                "query_index": query_index,
                "query_type": query_spec.query_type,
                "query": query_spec.query,
                "trace": query_trace,
            }
        )
        _append_trace_results(trace, "sparse", query_trace.get("sparse", {}).get("results") or [], query_spec.query_type)
        _append_trace_results(trace, "dense", query_trace.get("dense", {}).get("results") or [], query_spec.query_type)
        _append_trace_results(trace, "hybrid", query_trace.get("hybrid", {}).get("results") or [], query_spec.query_type)
        if query_trace.get("dense", {}).get("latency_seconds") is not None:
            trace["dense"]["latency_seconds"] = trace["dense"].get("latency_seconds", 0.0) + float(
                query_trace["dense"]["latency_seconds"]
            )

        for rank, chunk in enumerate(response.chunks, start=1):
            candidate = _candidate_from_retrieved_chunk(rank, node, chunk, evidence_by_id)
            candidate = candidate.model_copy(
                update={
                    "retrieval_metadata": {
                        **candidate.retrieval_metadata,
                        "query_type": query_spec.query_type,
                        "query": query_spec.query,
                        "query_index": query_index,
                    },
                    "notes": _dedupe([*candidate.notes, f"query:{query_spec.query_type}"]),
                }
            )
            evidence_id = candidate.evidence.evidence_id
            existing = candidates_by_id.get(evidence_id)
            if existing is None or candidate.score > existing.score:
                candidates_by_id[evidence_id] = candidate
            elif existing is not None:
                candidates_by_id[evidence_id] = existing.model_copy(
                    update={
                        "notes": _dedupe([*existing.notes, f"query:{query_spec.query_type}"]),
                        "retrieval_metadata": {
                            **existing.retrieval_metadata,
                            "additional_query_types": _dedupe(
                                [
                                    *existing.retrieval_metadata.get("additional_query_types", []),
                                    query_spec.query_type,
                                ]
                            ),
                        },
                    }
                )

    if "latency_seconds" in trace["dense"]:
        trace["dense"]["latency_seconds"] = round(float(trace["dense"]["latency_seconds"]), 6)
    candidates = sorted(candidates_by_id.values(), key=lambda item: (-item.score, item.evidence.evidence_id))
    return candidates[: max(request.top_k_per_node, request.hybrid_top_k)], trace


def _node_query(node: FlowNode) -> str:
    parts = [
        node.name,
        node.template_name,
        node.raw,
        node.name.replace("_", " "),
        node.template_name.replace("_", " "),
    ]
    return " ".join(part for part in parts if part).strip()


def _candidate_from_retrieved_chunk(
    rank: int,
    node: FlowNode,
    chunk: RetrievedChunk,
    evidence_by_id: dict[str, EvidenceUnit],
) -> TaskEvidenceCandidate:
    evidence = _evidence_from_retrieved_chunk(chunk, evidence_by_id)
    aliases = task_aliases(node)
    fields = extract_parameter_fields(evidence, aliases)
    retrieval_source = str(chunk.source or "dense")
    graph_paths = [
        f"FlowTask:{node.name} --retrieved_by_{retrieval_source}--> Evidence:{evidence.evidence_id}",
    ]
    return TaskEvidenceCandidate(
        evidence=evidence,
        score=chunk.score,
        matched_terms=_matched_query_terms(node, evidence),
        graph_paths=graph_paths,
        score_breakdown={"retrieval_score": chunk.score},
        provenance_refs=[str(chunk.metadata.get("source_path") or "")],
        raw_source_path=evidence.source_path or "",
        block_role=str(evidence.metadata.get("block_role") or evidence.evidence_type),
        parameter_fields=fields,
        flowchart_title=str(fields.get("FLOWCHART_TITLE") or evidence.metadata.get("flowchart_title") or ""),
        template_match=node.template_name,
        retrieval_source=retrieval_source,
        retrieval_rank=rank,
        retrieval_score=chunk.score,
        retrieval_metadata=dict(chunk.metadata),
        notes=[f"retrieved_by_{retrieval_source}"],
    )


def _evidence_from_retrieved_chunk(chunk: RetrievedChunk, evidence_by_id: dict[str, EvidenceUnit]) -> EvidenceUnit:
    metadata = dict(chunk.metadata)
    source_id = str(metadata.get("source_id") or chunk.chunk_id)
    original = evidence_by_id.get(source_id) or evidence_by_id.get(chunk.chunk_id)
    retrieval_metadata = {
        **metadata,
        "retrieval_chunk_id": chunk.chunk_id,
        "retrieval_source": chunk.source,
        "retrieval_score": chunk.score,
    }
    if original is not None:
        return original.model_copy(update={"metadata": {**original.metadata, **retrieval_metadata}})

    return EvidenceUnit(
        evidence_id=source_id or chunk.chunk_id,
        evidence_type=_evidence_type_from_chunk(chunk),
        content=chunk.content,
        doc_id=str(metadata.get("doc_id") or (source_id.split("_", 1)[0] if source_id else "")),
        source_path=str(metadata.get("source_path") or ""),
        page_idx=int(metadata.get("page_idx") or 0),
        bbox=_int_list(metadata.get("bbox")),
        metadata=retrieval_metadata,
    )


def _evidence_type_from_chunk(chunk: RetrievedChunk):
    evidence_type = str(chunk.metadata.get("evidence_type") or "")
    if evidence_type:
        return evidence_type
    unit_type = str(chunk.metadata.get("unit_type") or "")
    if unit_type == "table_chunk":
        return "table"
    if unit_type == "image_region":
        return "image"
    if unit_type == "equation_chunk":
        return "equation"
    if unit_type == "xml_template":
        return "template"
    if unit_type == "rule_chunk":
        return "rule"
    return "text"


def _matched_query_terms(node: FlowNode, evidence: EvidenceUnit) -> list[str]:
    text = f"{evidence.content} {evidence.metadata}".lower()
    matches: list[str] = []
    for value in [node.name, node.template_name, node.raw, node.name.replace("_", " ")]:
        normalized = value.strip().lower()
        if normalized and normalized in text and normalized not in matches:
            matches.append(normalized)
    return matches


def _merge_graph_and_retrieval_candidates(
    *,
    graph_candidates: list[TaskEvidenceCandidate],
    retrieval_candidates: list[TaskEvidenceCandidate],
    top_k: int,
) -> list[TaskEvidenceCandidate]:
    if not retrieval_candidates:
        return graph_candidates[:top_k]
    if not graph_candidates:
        return retrieval_candidates[:top_k]

    graph_scores = _normalized_candidate_scores(graph_candidates)
    retrieval_scores = _normalized_candidate_scores(retrieval_candidates)
    by_id: dict[str, TaskEvidenceCandidate] = {}
    order: dict[str, tuple[int, int]] = {}

    for index, candidate in enumerate(graph_candidates, start=1):
        evidence_id = candidate.evidence.evidence_id
        by_id[evidence_id] = candidate.model_copy(
            update={
                "retrieval_source": candidate.retrieval_source or "graph",
                "score_breakdown": {
                    **candidate.score_breakdown,
                    "graph_score": candidate.score,
                    "graph_score_normalized": graph_scores.get(evidence_id, 0.0),
                },
            }
        )
        order[evidence_id] = (index, 0)

    for index, candidate in enumerate(retrieval_candidates, start=1):
        evidence_id = candidate.evidence.evidence_id
        existing = by_id.get(evidence_id)
        if existing is None:
            by_id[evidence_id] = candidate
            order[evidence_id] = (10_000, index)
            continue
        by_id[evidence_id] = _merge_candidate(existing, candidate)
        order[evidence_id] = (order.get(evidence_id, (10_000, index))[0], index)

    merged: list[TaskEvidenceCandidate] = []
    for evidence_id, candidate in by_id.items():
        graph_norm = float(candidate.score_breakdown.get("graph_score_normalized") or 0.0)
        retrieval_norm = retrieval_scores.get(evidence_id, 0.0)
        score = round(100.0 * ((0.65 * graph_norm) + (0.35 * retrieval_norm)), 6)
        if graph_norm <= 0 and retrieval_norm <= 0:
            score = candidate.score
        merged.append(
            candidate.model_copy(
                update={
                    "score": score,
                    "score_breakdown": {
                        **candidate.score_breakdown,
                        "retrieval_score_normalized": retrieval_norm,
                        "hybrid_candidate_score": score,
                    },
                }
            )
        )

    merged.sort(key=lambda item: (-item.score, order.get(item.evidence.evidence_id, (10_000, 10_000)), item.evidence.evidence_id))
    return merged[:top_k]


def _merge_candidate(graph_candidate: TaskEvidenceCandidate, retrieval_candidate: TaskEvidenceCandidate) -> TaskEvidenceCandidate:
    source_parts = _dedupe(
        [
            graph_candidate.retrieval_source or "graph",
            retrieval_candidate.retrieval_source or "dense",
        ]
    )
    evidence_metadata = {
        **graph_candidate.evidence.metadata,
        "hybrid_sources": source_parts,
        "retrieval_metadata_json": _metadata_json(retrieval_candidate.retrieval_metadata),
    }
    return graph_candidate.model_copy(
        update={
            "evidence": graph_candidate.evidence.model_copy(update={"metadata": evidence_metadata}),
            "matched_terms": _dedupe([*graph_candidate.matched_terms, *retrieval_candidate.matched_terms]),
            "graph_paths": _dedupe([*graph_candidate.graph_paths, *retrieval_candidate.graph_paths]),
            "kg_paths": _dedupe([*graph_candidate.kg_paths, *retrieval_candidate.kg_paths]),
            "provenance_refs": _dedupe([*graph_candidate.provenance_refs, *retrieval_candidate.provenance_refs]),
            "score_breakdown": {
                **graph_candidate.score_breakdown,
                "graph_score": graph_candidate.score,
                "retrieval_score": retrieval_candidate.retrieval_score or retrieval_candidate.score,
            },
            "retrieval_source": "hybrid" if len(source_parts) > 1 else source_parts[0],
            "retrieval_rank": retrieval_candidate.retrieval_rank,
            "retrieval_score": retrieval_candidate.retrieval_score,
            "retrieval_metadata": retrieval_candidate.retrieval_metadata,
            "notes": _dedupe([*graph_candidate.notes, *retrieval_candidate.notes]),
        }
    )


def _normalized_candidate_scores(candidates: list[TaskEvidenceCandidate]) -> dict[str, float]:
    if not candidates:
        return {}
    values = [candidate.score for candidate in candidates]
    min_score = min(values)
    max_score = max(values)
    if max_score == min_score:
        return {candidate.evidence.evidence_id: 1.0 for candidate in candidates}
    return {
        candidate.evidence.evidence_id: (candidate.score - min_score) / (max_score - min_score)
        for candidate in candidates
    }


def _int_list(value) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[int] = []
        for item in value:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result
    text = str(value)
    result = []
    for part in text.replace(",", " ").split():
        try:
            result.append(int(float(part)))
        except ValueError:
            continue
    return result


def _metadata_json(value: dict) -> str:
    try:
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _append_trace_results(trace: dict, source: str, results: list[dict], query_type: str) -> None:
    existing_ids = {
        (
            str(item.get("chunk_id") or ""),
            str((item.get("metadata") or {}).get("source_id") or ""),
            query_type,
        )
        for item in trace.get(source, {}).get("results") or []
    }
    for item in results:
        payload = dict(item)
        payload["query_type"] = query_type
        marker = (
            str(payload.get("chunk_id") or ""),
            str((payload.get("metadata") or {}).get("source_id") or ""),
            query_type,
        )
        if marker in existing_ids:
            continue
        existing_ids.add(marker)
        trace.setdefault(source, {}).setdefault("results", []).append(payload)


def _dedupe(values) -> list:
    seen = set()
    result = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
