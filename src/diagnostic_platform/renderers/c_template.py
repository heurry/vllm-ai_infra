"""Render the validated DSL into a simple C function template."""

from __future__ import annotations

from diagnostic_platform.schemas import DiagnosticPlan


def render_c_function(plan: DiagnosticPlan) -> str:
    """Render a minimal C function from a diagnostic plan."""

    lines = [
        f"int {plan.function_name}(void)",
        "{",
    ]

    for precondition in plan.preconditions:
        lines.append(f"    /* precondition: {precondition} */")

    for index, step in enumerate(plan.steps, start=1):
        lines.append(f"    /* step {index}: send {step.send}, expect {step.expect} */")

    lines.append(f"    {plan.on_fail}")
    lines.append("}")
    return "\n".join(lines)

