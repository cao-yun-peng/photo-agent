"""Minimal provider metadata; never retain response text or infer invoice amounts."""


def safe_usage(value):
    if not isinstance(value, dict):
        return None
    allowed = ("input_tokens", "output_tokens", "total_tokens")
    usage = {
        key: value[key]
        for key in allowed
        if type(value.get(key)) is int and value[key] >= 0
    }
    return usage or None


def generation_cost(result):
    simulated = result.model == "mock"
    return {
        "currency": "CNY",
        "scope": "image_generation_only",
        "basis": "mock_zero" if simulated else "configured_estimate_not_invoice",
        "estimated_yuan": result.cost_yuan,
        "actual_yuan": 0 if simulated else None,
        "usage": safe_usage(getattr(result, "usage", None)),
        "unaccounted_stages": ["planning", "verification"],
        "complete": False,
    }
