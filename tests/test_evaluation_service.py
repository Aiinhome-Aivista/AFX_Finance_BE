"""Unit tests for Model Evaluation & Governance Benchmarking Service."""
import sys
from pathlib import Path
import pytest

BE_ROOT = Path(__file__).resolve().parents[1]
if str(BE_ROOT) not in sys.path:
    sys.path.insert(0, str(BE_ROOT))

from app.services.evaluation_service import EvaluationService


def test_domain_guardrail_evaluation():
    res = EvaluationService.evaluate_domain_guardrails("planning-budgeting-forecasting")
    assert "ood_rejection_accuracy" in res
    assert res["ood_rejection_accuracy"] >= 80.0
    assert res["security_jailbreak_blocked"] is True
    assert res["guardrail_robustness_score"] >= 80.0


def test_llm_faithfulness_evaluation():
    res = EvaluationService.evaluate_llm_faithfulness("planning-budgeting-forecasting")
    assert res["faithfulness_score"] >= 90.0
    assert res["hallucination_rate"] <= 10.0
    assert res["llm_judge_verdict"] == "Compliant (Enterprise Grounded)"


def test_telemetry_classification_metrics():
    res = EvaluationService.evaluate_telemetry_classification(
        "planning-budgeting-forecasting",
        {"stats": {"matched": 8, "missing": 1, "new": 0}}
    )
    assert res["precision"] == 100.0
    assert res["recall"] == 100.0
    assert res["f1_score"] == 100.0
    assert res["confusion_matrix"]["true_positives"] == 1
    assert res["confusion_matrix"]["true_negatives"] == 8


def test_digital_twin_forecast_accuracy():
    res = EvaluationService.evaluate_digital_twin_forecast("planning-budgeting-forecasting")
    assert "mae" in res
    assert "rmse" in res
    assert "r2_variance_explained" in res
    assert res["mae"] > 0
    assert res["rmse"] > 0
    assert res["r2_variance_explained"] >= 0.85
    assert "70% (Historical Baseline)" in res["chronological_split"]["train_ratio"]


def test_full_evaluation_summary():
    summary = EvaluationService.get_full_evaluation_summary("planning-budgeting-forecasting")
    assert summary["composite_readiness_score"] >= 85.0
    assert "guardrails" in summary
    assert "llm_evaluation" in summary
    assert "telemetry_classification" in summary
    assert "forecast_regression" in summary
    assert "drift_monitoring" in summary
    assert summary["drift_monitoring"]["drift_status"] == "STABLE"

