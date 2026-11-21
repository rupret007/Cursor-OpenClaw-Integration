import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fo_trace", ROOT / "first-officer/tracing/deliver.py")
deliver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deliver)

HEAD = "3d350baac7636fe068d5be6299991f5e776259b5"
GOOD = {"fo.lane": "review", "fo.head": HEAD, "fo.backend": "restate", "fo.outcome": "queued"}


def test_attributes_keep_only_the_allowlisted_lane_facts():
    assert deliver.clean_attributes(GOOD) == {
        "fo.backend": "restate", "fo.head": HEAD, "fo.lane": "review", "fo.outcome": "queued"}
    assert deliver.clean_attributes({"fo.lane": "review", "fo.head": HEAD})["fo.lane"] == "review"


def test_attributes_reject_secrets_prompts_and_missing_identity():
    for attributes, message in (
        ({"fo.lane": "review"}, "lane and head"),
        ({}, "lane and head"),
        ({**GOOD, "prompt": "hello"}, "allowlist"),
        ({**GOOD, "gen_ai.prompt.0.content": "sk-live-secretvalue"}, "allowlist"),
        ({**GOOD, "fo.lane": "sk-live-secretvalue"}, "not usable"),
        ({**GOOD, "fo.head": HEAD.upper()}, "not usable"),
        ({**GOOD, "fo.backend": "cloud"}, "not usable"),
        ({**GOOD, "fo.outcome": "hidden"}, "not usable"),
        ("review", "lane and head"),
    ):
        with pytest.raises(ValueError, match=message):
            deliver.clean_attributes(attributes)


def test_collector_must_be_loopback_http_with_no_path_or_credential():
    assert deliver.collector_url("http://127.0.0.1:6006") == "http://127.0.0.1:6006"
    assert deliver.collector_url("http://localhost:6006/") == "http://localhost:6006"
    assert deliver.collector_url("http://[::1]:6006") == "http://[::1]:6006"
    for url in (
        "https://127.0.0.1:6006",
        "http://example.com:6006",
        "http://8.8.8.8:6006",
        "https://api.traceloop.com",
        "http://api.traceloop.com",
        "http://user:secret@127.0.0.1:6006",
        "http://127.0.0.1:6006/v1/traces",
        "http://127.0.0.1:6006?x=1",
        "http://127.0.0.1:6006#span",
    ):
        with pytest.raises(ValueError, match="loopback HTTP collector"):
            deliver.collector_url(url)
