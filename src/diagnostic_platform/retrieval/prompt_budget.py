"""Prompt budget helpers for evidence-heavy LLM calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from diagnostic_platform.schemas import EvidenceMatch, NodeEvidenceBundle


@dataclass(frozen=True)
class PromptBudgetConfig:
    """Budget policy for converting evidence into prompt context."""

    max_prompt_tokens: int = 4096
    reserved_output_tokens: int = 512
    max_evidence_items: int = 8
    max_graph_paths: int = 8
    chars_per_token: int = 4
    min_score: float = 0.0

    @property
    def evidence_token_budget(self) -> int:
        return max(1, self.max_prompt_tokens - self.reserved_output_tokens)


@dataclass
class PromptBudgetReport:
    """Summary of budget decisions."""

    original_matches: int = 0
    kept_matches: int = 0
    dropped_matches: int = 0
    original_graph_paths: int = 0
    kept_graph_paths: int = 0
    estimated_tokens: int = 0
    budget_tokens: int = 0
    dropped_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptSection:
    """One scored text section that may be placed into an LLM prompt."""

    label: str
    content: str
    score: float = 0.0


def budget_node_bundle(
    bundle: NodeEvidenceBundle,
    config: PromptBudgetConfig | None = None,
) -> tuple[NodeEvidenceBundle, PromptBudgetReport]:
    """Return a copy of a node evidence bundle trimmed to prompt budget."""

    config = config or PromptBudgetConfig()
    kept_matches, report = budget_evidence_matches(bundle.matches, config)
    graph_paths = list(_dedupe(bundle.graph_paths))[: config.max_graph_paths]
    report.original_graph_paths = len(bundle.graph_paths)
    report.kept_graph_paths = len(graph_paths)
    notes = [*bundle.notes, _budget_note(report)]
    return (
        bundle.model_copy(
            update={
                "matches": kept_matches,
                "graph_paths": graph_paths,
                "notes": notes,
            }
        ),
        report,
    )


def budget_evidence_matches(
    matches: list[EvidenceMatch],
    config: PromptBudgetConfig | None = None,
) -> tuple[list[EvidenceMatch], PromptBudgetReport]:
    """Score-sort, dedupe, and trim evidence matches to a token budget."""

    config = config or PromptBudgetConfig()
    report = PromptBudgetReport(original_matches=len(matches), budget_tokens=config.evidence_token_budget)
    kept: list[EvidenceMatch] = []
    seen_evidence_ids: set[str] = set()
    used_tokens = 0

    for match in sorted(matches, key=lambda item: (-item.score, item.evidence.evidence_id)):
        evidence_id = match.evidence.evidence_id
        if evidence_id in seen_evidence_ids:
            _drop(report, "duplicate_evidence")
            continue
        if match.score < config.min_score:
            _drop(report, "below_min_score")
            continue
        if len(kept) >= config.max_evidence_items:
            _drop(report, "max_items")
            continue

        estimate = estimate_tokens(match.evidence.content, chars_per_token=config.chars_per_token)
        if kept and used_tokens + estimate > config.evidence_token_budget:
            _drop(report, "token_budget")
            continue

        kept.append(match)
        seen_evidence_ids.add(evidence_id)
        used_tokens += estimate

    report.kept_matches = len(kept)
    report.dropped_matches = report.original_matches - report.kept_matches
    report.estimated_tokens = used_tokens
    return kept, report


def budget_text_sections(
    sections: list[PromptSection],
    config: PromptBudgetConfig | None = None,
) -> tuple[list[PromptSection], PromptBudgetReport]:
    """Score-sort, dedupe, and trim arbitrary prompt sections to budget."""

    config = config or PromptBudgetConfig()
    report = PromptBudgetReport(original_matches=len(sections), budget_tokens=config.evidence_token_budget)
    kept: list[PromptSection] = []
    seen_labels: set[str] = set()
    seen_content: set[str] = set()
    used_tokens = 0

    for section in sorted(sections, key=lambda item: (-item.score, item.label)):
        content = section.content.strip()
        if not content:
            _drop(report, "empty_section")
            continue
        content_key = _content_key(content)
        if section.label in seen_labels or content_key in seen_content:
            _drop(report, "duplicate_section")
            continue
        if len(kept) >= config.max_evidence_items:
            _drop(report, "max_items")
            continue

        estimate = estimate_tokens(content, chars_per_token=config.chars_per_token)
        remaining_tokens = config.evidence_token_budget - used_tokens
        if estimate > remaining_tokens:
            if remaining_tokens <= 0:
                _drop(report, "token_budget")
                continue
            content = _truncate_to_token_budget(content, remaining_tokens, config.chars_per_token)
            estimate = estimate_tokens(content, chars_per_token=config.chars_per_token)
            _drop(report, "section_truncated")

        kept.append(PromptSection(label=section.label, content=content, score=section.score))
        seen_labels.add(section.label)
        seen_content.add(content_key)
        used_tokens += estimate

    report.kept_matches = len(kept)
    report.dropped_matches = report.original_matches - report.kept_matches
    report.estimated_tokens = used_tokens
    return kept, report


def render_text_sections(sections: list[PromptSection], config: PromptBudgetConfig | None = None) -> str:
    """Render budgeted prompt sections in a compact, auditable format."""

    kept, _ = budget_text_sections(sections, config=config)
    return "\n\n".join(f"[{section.label}]\n{section.content.strip()}" for section in kept)


def render_evidence_context(matches: list[EvidenceMatch], config: PromptBudgetConfig | None = None) -> str:
    """Render budgeted evidence into a compact prompt context."""

    kept, _ = budget_evidence_matches(matches, config=config)
    sections = []
    for index, match in enumerate(kept, start=1):
        evidence = match.evidence
        sections.append(
            "\n".join(
                [
                    f"[{index}] evidence_id={evidence.evidence_id} score={match.score:.4f}",
                    f"type={evidence.evidence_type} doc_id={evidence.doc_id} page={evidence.page_idx}",
                    evidence.content.strip(),
                ]
            )
        )
    return "\n\n".join(sections)


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    """Cheap token estimate without tokenizer dependency."""

    chars_per_token = max(1, chars_per_token)
    return max(1, (len(text) + chars_per_token - 1) // chars_per_token)


def _drop(report: PromptBudgetReport, reason: str) -> None:
    report.dropped_reasons[reason] = report.dropped_reasons.get(reason, 0) + 1


def _budget_note(report: PromptBudgetReport) -> str:
    return (
        "prompt_budget:"
        f"kept={report.kept_matches}/original={report.original_matches},"
        f"tokens={report.estimated_tokens}/{report.budget_tokens},"
        f"dropped={report.dropped_reasons}"
    )


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item not in seen:
            output.append(item)
            seen.add(item)
    return output


def _truncate_to_token_budget(text: str, budget_tokens: int, chars_per_token: int) -> str:
    max_chars = max(1, budget_tokens * max(1, chars_per_token))
    suffix = "\n...[truncated by prompt budget]"
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - len(suffix))].rstrip() + suffix


def _content_key(text: str) -> str:
    return " ".join(text.split())[:512]
