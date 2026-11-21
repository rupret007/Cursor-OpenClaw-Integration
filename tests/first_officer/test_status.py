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
