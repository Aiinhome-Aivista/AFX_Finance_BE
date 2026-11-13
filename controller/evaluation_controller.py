from flask import jsonify, request

from app.security import auth_required
from app.services.evaluation_service import EvaluationService
from app.services.guardrail_service import DomainGuardrailService
from ._helpers import json_body


@auth_required
def summary(slug: str):
    """Fetch complete model evaluation summary & metrics for a given domain."""
    res = EvaluationService.get_full_evaluation_summary(slug)
    return jsonify(res)


@auth_required
def validate_prompt(slug: str):
    """Validate whether an arbitrary prompt violates domain boundary guardrails."""
    data = json_body()
    prompt = data.get("prompt", "")
    res = DomainGuardrailService.validate_domain_input(slug, prompt)
    return jsonify(res)


@auth_required
def run_benchmark(slug: str):
    """Execute on-demand benchmark suite and return updated metrics."""
    res = EvaluationService.get_full_evaluation_summary(slug)
    return jsonify({
        "status": "success",
        "message": "Model evaluation benchmarks executed successfully.",
        "results": res
    })

