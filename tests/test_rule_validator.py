"""Tests for the diagnostic rule validator."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from diagnostic_platform.schemas import DiagnosticPlan, DiagnosticStep
from diagnostic_platform.validation.rules import validate_plan


class RuleValidatorTest(unittest.TestCase):
    def test_valid_security_access_sequence(self) -> None:
        plan = DiagnosticPlan(
            function_name="SecurityAccessType1",
            preconditions=["EnterExtSession"],
            steps=[
                DiagnosticStep(send="10 03", expect="50 03"),
                DiagnosticStep(send="27 01", expect="67 01"),
                DiagnosticStep(send="27 02 12 34", expect="67 02"),
            ],
        )

        result = validate_plan(plan)
        self.assertTrue(result.valid)

    def test_invalid_security_pair(self) -> None:
        plan = DiagnosticPlan(
            function_name="SecurityAccessBroken",
            steps=[
                DiagnosticStep(send="10 03", expect="50 03"),
                DiagnosticStep(send="27 02 12 34", expect="67 02"),
            ],
        )

        result = validate_plan(plan)
        self.assertFalse(result.valid)
        self.assertTrue(any(issue.code == "SECURITY_PAIR_MISSING" for issue in result.issues))


if __name__ == "__main__":
    unittest.main()
