import importlib.util
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fo_status", ROOT / "first-officer/status/publish.py")
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

NOW = 1_763_700_000
EVIDENCE = {
    "heavy_jobs": 1,
    "lane_counts": {"running": 1, "queued": 2},
    "nodes": {
        "mac-mini": {"load1": 1.5, "load5": 1.25, "load15": 1, "codex_jobs": 1, "test_jobs": 0, "age_s": 5},
        "nucbox": {"load1": 99, "load5": 99, "load15": 99, "codex_jobs": 4, "test_jobs": 4, "age_s": 900},
    },
}


def test_snapshot_matches_the_fleet_reader_and_drops_stale_or_secret_text(tmp_path):
    evidence = {**EVIDENCE, "note": "sk-live-secretvalue"}
    document = status.build(evidence, NOW)
    assert document["v"] == 1 and document["ts"] == NOW
    parsed = datetime.strptime(document["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert int(parsed.timestamp()) == NOW
    assert document["host"] == {"heavy_jobs": 1}
    assert document["lane_counts"] == {"queued": 2, "running": 1}
    assert document["nodes"]["mac-mini"]["load1"] == 1.5
    assert document["nodes"]["nucbox"] == {"missing": True, "stale": True}
    assert "99" not in json.dumps(document)
    assert "sk-live" not in json.dumps(document)
    assert "redacted" in document["alerts"] and "stale" in document["alerts"]
    path = tmp_path / "STATUS.json"
    status.write_snapshot(path, document)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_size <= 3000
    assert "sk-live" not in path.read_text()


def test_unusable_evidence_is_refused():
    with pytest.raises(ValueError, match="not one of the known"):
        status.build({"nodes": {"laptop": {"age_s": 0}}}, NOW)
    with pytest.raises(ValueError, match="heavy-job"):
        status.build({"heavy_jobs": True}, NOW)
    leaked = status.build({"heavy_jobs": 1, "nodes": {
        "mac-mini": {"load1": 1, "load5": 1, "load15": 1, "codex_jobs": 0, "test_jobs": 0,
                     "age_s": 0, "note": "203.0.113.9"}}}, NOW)
    assert "203.0.113.9" not in json.dumps(leaked) and "redacted" in leaked["alerts"]


def test_status_page_is_loopback_get_only_and_escapes_the_snapshot(tmp_path):
    path = tmp_path / "STATUS.json"
    path.write_text('{"v":1,"host":{},"text":"<script>alert(1)</script>"}\n')
    path.chmod(0o600)
    server = status.serve(path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        base = f"http://127.0.0.1:{server.server_address[1]}"
        page = urlopen(base + "/", timeout=2).read().decode()
        assert "<script>" not in page and "&lt;script&gt;" in page
        body = urlopen(base + "/status.json", timeout=2).read()
        assert json.loads(body)["v"] == 1
        with pytest.raises(HTTPError) as denied:
            urlopen(Request(base + "/", method="POST"), timeout=2)
        assert denied.value.code == 405
        path.write_text('{"v":1,"host":{},"token":"sk-live-secretvalue"}\n')
        with pytest.raises(HTTPError) as hidden:
            urlopen(base + "/", timeout=2)
        assert hidden.value.code == 503 and b"sk-live" not in hidden.value.read()
    finally:
        server.shutdown()
        server.server_close()


def _fetch(url):
    try:
        with urlopen(url, timeout=2) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def test_status_refuses_scanner_credential_shapes(tmp_path):
    """build sets a redacted alert and serve returns 503 without the snapshot body."""
    shapes = (
        ("github_pat", "github" + "_pat_" + "A" * 16),
        ("asia", "AS" + "IA" + "B" * 16),
        ("akia", "AK" + "IA" + "C" * 16),
        ("slack", "xox" + "b-" + "d" * 10),
        ("bearer_alpha", "Bearer " + "e" * 16),
        ("bearer_alnum", "bearer " + ("F" * 12 + "8" * 6)),
        ("bearer_same_shape", "BEARER " + ("ANDREA_SYNC_INTERNAL_TOKEN"[:-1] + "M")),
        ("bearer_longer_shape", "Bearer " + ("ANDREA_SYNC_INTERNAL_TOKEN" + "S")),
        ("bearer_mixed", "bearer " + "ab-cd.ef_ghijklm"),
        ("bearer_symbols", "Bearer " + ("aa_bb" + "-cc.dd/ee+ff~gg")),
    )
    for label, secret in shapes:
        document = status.build({"heavy_jobs": 1, "note": secret}, NOW)
        present = secret in json.dumps(document)
        alert = document["alerts"] == ["redacted"]
        print(label, "build", alert, present)
        assert alert is True
        assert present is False
        assert document["host"] == {"heavy_jobs": 1}

    fixture = "Bearer " + "ANDREA_SYNC_INTERNAL_TOKEN"
    exempt = status.build({"heavy_jobs": 1, "note": fixture}, NOW)
    print("fixture", "build", "redacted" in exempt["alerts"])
    assert exempt["alerts"] == []
    assert exempt["host"] == {"heavy_jobs": 1}

    path = tmp_path / "STATUS.json"
    path.write_text('{"v":1,"host":{}}\n', encoding="utf-8")
    server = status.serve(path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for label, secret in shapes:
            path.write_text(json.dumps({"v": 1, "host": {}, "note": secret}) + "\n", encoding="utf-8")
            for route in ("/", "/status.json"):
                code, payload = _fetch(base + route)
                hidden = code == 503 and payload == b"Status unavailable\n"
                print(label, route, code, hidden)
                assert code == 503
                assert hidden is True
        path.write_text(json.dumps({"v": 1, "host": {}, "note": fixture}) + "\n", encoding="utf-8")
        for route in ("/", "/status.json"):
            code, _payload = _fetch(base + route)
            print("fixture", route, code)
            assert code == 200
    finally:
        server.shutdown()
        server.server_close()
