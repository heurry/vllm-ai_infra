"""Infer ScriptNode ClassName argument contracts from template registries."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path

from diagnostic_platform.schemas import (
    XmlTemplateArgContract,
    XmlTemplateClassContract,
    XmlTemplateContractBuildRequest,
    XmlTemplateContractRegistry,
    XmlTemplateEntry,
    XmlTemplateRegistry,
)


def build_template_contracts(request: XmlTemplateContractBuildRequest) -> XmlTemplateContractRegistry:
    """Build ClassName -> allowed/required Arg contracts from scanned XML templates."""

    registry = _load_registry(request)
    templates_by_class: dict[str, list[XmlTemplateEntry]] = defaultdict(list)
    for template in registry.templates:
        if template.class_name:
            templates_by_class[template.class_name].append(template)

    class_contracts = [
        _build_class_contract(class_name, templates, request.required_observed_ratio)
        for class_name, templates in sorted(templates_by_class.items())
    ]
    contract_registry = XmlTemplateContractRegistry(
        source_registry_path=request.registry_path,
        class_contracts=class_contracts,
        by_class_name={contract.class_name: contract for contract in class_contracts},
    )
    if request.output_path:
        output_path = Path(request.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(contract_registry.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return contract_registry


def _load_registry(request: XmlTemplateContractBuildRequest) -> XmlTemplateRegistry:
    if request.registry is not None:
        return request.registry
    if request.registry_path:
        payload = json.loads(Path(request.registry_path).read_text(encoding="utf-8"))
        return XmlTemplateRegistry.model_validate(payload)
    raise ValueError("Either registry or registry_path is required.")


def _build_class_contract(
    class_name: str,
    templates: list[XmlTemplateEntry],
    required_ratio: float,
) -> XmlTemplateClassContract:
    arg_counts: Counter[str] = Counter()
    node_names: set[str] = set()
    source_paths: set[str] = set()
    for template in templates:
        arg_counts.update(set(template.arg_names))
        if template.node_name:
            node_names.add(template.node_name)
        if template.source_path:
            source_paths.add(template.source_path)

    template_count = len(templates)
    arg_contracts: list[XmlTemplateArgContract] = []
    required_args: list[str] = []
    optional_args: list[str] = []
    threshold_count = template_count * required_ratio
    for arg_name, observed_count in sorted(arg_counts.items()):
        required = observed_count >= threshold_count
        arg_contracts.append(
            XmlTemplateArgContract(
                arg_name=arg_name,
                observed_count=observed_count,
                required=required,
            )
        )
        if required:
            required_args.append(arg_name)
        else:
            optional_args.append(arg_name)

    return XmlTemplateClassContract(
        class_name=class_name,
        template_count=template_count,
        required_args=required_args,
        optional_args=optional_args,
        arg_contracts=arg_contracts,
        node_names=sorted(node_names),
        source_paths=sorted(source_paths),
    )

