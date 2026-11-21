import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "fo_litellm_configure", ROOT / "first-officer/litellm/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


PLAN = {
    "fallbacks": {"primary": ["fallback"]},
    "lanes": {"review": {
        "models": ["primary", "fallback"], "max_budget": 1, "budget_duration": "1d",
    }},
}


def catalog(*rows):
    return {
        "model_list": list(rows),
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "database_url": "os.environ/DATABASE_URL",
        },
    }


def row(alias, input_price, output_price, *, where="litellm_params"):
    prices = {}
    if input_price is not None:
        prices["input_cost_per_token"] = input_price
    if output_price is not None:
        prices["output_cost_per_token"] = output_price
    return {"model_name": alias, where: prices}


def test_a_routed_model_needs_finite_nonnegative_prices_and_one_charge():
    rendered = configure.candidate_config(catalog(
        row("primary", 0.001, 0.002),
        row("fallback", 0, 0.002, where="model_info"),
    ), PLAN)
    assert rendered["litellm_settings"]["block_requests_for_models_without_pricing"] is True

    agreed = row("primary", 0.001, 0.002)
    agreed["model_info"] = {"input_cost_per_token": 0.001, "output_cost_per_token": 0.002}
    configure.candidate_config(catalog(agreed, row("fallback", 0.1, 0.1)), PLAN)
    conflict = row("primary", 0.001, 0.002)
    conflict["model_info"] = {"input_cost_per_token": 0.003, "output_cost_per_token": 0.002}
    with pytest.raises(ValueError, match="pricing"):
        configure.candidate_config(catalog(conflict, row("fallback", 0.1, 0.1)), PLAN)

    for primary in (
        row("primary", -1, 0.1),
        row("primary", float("nan"), 0.1),
        row("primary", float("inf"), 0.1),
        row("primary", "0.1", 0.1),
        row("primary", True, 0.1),
        row("primary", 0, 0),
        row("primary", 0.1, None),
    ):
        with pytest.raises(ValueError, match="pricing"):
            configure.candidate_config(catalog(primary, row("fallback", 0.1, 0.1)), PLAN)
