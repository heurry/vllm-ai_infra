"""Invoke MinerU and load its structured outputs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from diagnostic_platform.schemas import (
    LoadMinerUOutputRequest,
    MinerUContentBlock,
    MinerUOutputFiles,
    NormalizeMinerURequest,
    ParseDocumentRequest,
    ParseDocumentResult,
)


def parse_document_with_mineru(request: ParseDocumentRequest) -> ParseDocumentResult:
    """Run MinerU CLI synchronously and optionally load its output."""

    command = build_mineru_command(request)
    try:
        completed = subprocess.run(
            command,
            cwd=str(Path.cwd()),
            capture_output=True,
            check=False,
            text=True,
            timeout=request.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"MinerU parse timed out after {request.timeout_seconds}s") from exc
    output_dir = Path(request.output_dir)
    files = discover_mineru_outputs(output_dir)
    normalized = None
    if request.load_output and completed.returncode == 0:
        normalized = load_mineru_output(
            LoadMinerUOutputRequest(
                output_dir=request.output_dir,
                metadata=request.metadata,
            )
        )

    return ParseDocumentResult(
        source_path=request.source_path,
        output_dir=request.output_dir,
        command=command,
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        files=files,
        normalized=normalized,
    )


def build_mineru_command(request: ParseDocumentRequest) -> list[str]:
    """Build the MinerU CLI command without shell interpolation."""

    command = [
        request.mineru_executable,
        "-p",
        request.source_path,
        "-o",
        request.output_dir,
        "-b",
        request.backend,
        "-m",
        request.method,
        "-l",
        request.lang,
        "-s",
        str(request.start_page_id),
        "-f",
        str(request.formula_enable),
        "-t",
        str(request.table_enable),
    ]
    if request.end_page_id is not None:
        command.extend(["-e", str(request.end_page_id)])
    if request.server_url:
        command.extend(["-u", request.server_url])
    if request.device_mode:
        command.extend(["-d", request.device_mode])
    if request.model_source:
        command.extend(["--source", request.model_source])
    return command


def discover_mineru_outputs(output_dir: Path) -> MinerUOutputFiles:
    """Find MinerU files under an output directory."""

    if not output_dir.exists():
        return MinerUOutputFiles()

    markdown_files = sorted(str(path) for path in output_dir.rglob("*.md"))
    content_list_files = sorted(str(path) for path in output_dir.rglob("*_content_list.json"))
    middle_json_files = sorted(str(path) for path in output_dir.rglob("*_middle.json"))
    image_dirs = sorted(str(path) for path in output_dir.rglob("images") if path.is_dir())
    return MinerUOutputFiles(
        markdown_files=markdown_files,
        content_list_files=content_list_files,
        middle_json_files=middle_json_files,
        image_dirs=image_dirs,
    )


def load_mineru_output(request: LoadMinerUOutputRequest) -> NormalizeMinerURequest:
    """Load content_list.json files into the current normalization request shape."""

    output_dir = Path(request.output_dir)
    content_list_paths = [Path(path) for path in request.content_list_paths]
    if not content_list_paths:
        content_list_paths = [Path(path) for path in discover_mineru_outputs(output_dir).content_list_files]

    blocks: list[MinerUContentBlock] = []
    for content_list_path in sorted(content_list_paths):
        blocks.extend(_load_content_list(content_list_path))

    if not blocks and request.include_markdown_fallback:
        blocks.extend(_load_markdown_fallback(output_dir))

    return NormalizeMinerURequest(metadata=request.metadata, blocks=blocks)


def _load_content_list(content_list_path: Path) -> list[MinerUContentBlock]:
    raw = json.loads(content_list_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"MinerU content list must be a list: {content_list_path}")

    blocks: list[MinerUContentBlock] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        block = _content_item_to_block(item, content_list_path)
        if block is not None:
            blocks.append(block)
    return blocks


def _content_item_to_block(item: dict[str, Any], content_list_path: Path) -> MinerUContentBlock | None:
    raw_type = str(item.get("type") or "text")
    block_type = raw_type if raw_type in {"text", "table", "image", "equation"} else "text"
    page_idx = _safe_int(item.get("page_idx"), default=0)
    bbox = _bbox(item.get("bbox"))
    img_path = _resolve_img_path(item.get("img_path"), content_list_path)

    if block_type == "text":
        text = _first_text(item, ["text", "content", "code_body"])
        if not text:
            return None
        return MinerUContentBlock(
            type="text",
            page_idx=page_idx,
            bbox=bbox,
            text=text,
            text_level=_optional_int(item.get("text_level")),
            img_path=img_path,
        )
    if block_type == "table":
        return MinerUContentBlock(
            type="table",
            page_idx=page_idx,
            bbox=bbox,
            table_caption=_string_list(item.get("table_caption")),
            table_footnote=_string_list(item.get("table_footnote")),
            table_body=_first_text(item, ["table_body", "text", "content"]),
            img_path=img_path,
        )
    if block_type == "image":
        return MinerUContentBlock(
            type="image",
            page_idx=page_idx,
            bbox=bbox,
            image_caption=_string_list(item.get("image_caption")),
            image_footnote=_string_list(item.get("image_footnote")),
            img_path=img_path,
        )
    if block_type == "equation":
        text = _first_text(item, ["text", "content"])
        if not text and not img_path:
            return None
        return MinerUContentBlock(
            type="equation",
            page_idx=page_idx,
            bbox=bbox,
            text=text,
            img_path=img_path,
        )
    return None


def _load_markdown_fallback(output_dir: Path) -> list[MinerUContentBlock]:
    blocks: list[MinerUContentBlock] = []
    for index, markdown_path in enumerate(sorted(output_dir.rglob("*.md"))):
        text = markdown_path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            continue
        blocks.append(
            MinerUContentBlock(
                type="text",
                page_idx=index,
                text=text,
                bbox=[],
            )
        )
    return blocks


def _first_text(item: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _bbox(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    bbox: list[int] = []
    for item in value[:4]:
        try:
            bbox.append(int(round(float(item))))
        except (TypeError, ValueError):
            return []
    return bbox if len(bbox) == 4 else []


def _resolve_img_path(value: Any, content_list_path: Path) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    image_path = Path(value)
    if image_path.is_absolute():
        return str(image_path)
    return str((content_list_path.parent / image_path).resolve())


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
