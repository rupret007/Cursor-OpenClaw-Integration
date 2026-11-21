import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fo_rollback", ROOT / "first-officer/pilot/rollback_drill.py")
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def test_staged_restore_returns_the_prior_pin_and_records_the_time(tmp_path, monkeypatch):
    installer = drill._installer()
    called = []
    monkeypatch.setattr(installer, "systemctl", lambda *args, **kwargs: called.append(args))
    # The drill loads its own module instance, so patch the function it will call.
    monkeypatch.setattr(drill, "_installer", lambda: installer)
    root = tmp_path / "rollback"
    receipt = drill.drill(root)
    assert receipt["restored"] is True and receipt["gateway_touched"] is False
    assert receipt["previous"] == installer.BASELINE and receipt["release"] == drill.RELEASE
    assert type(receipt["elapsed_us"]) is int and receipt["elapsed_us"] >= 0
    pin = (root / "pin.conf").read_text(encoding="utf-8")
    assert installer.BASELINE in pin and drill.RELEASE not in pin
    config = json.loads((root / "openclaw.json").read_text(encoding="utf-8"))
    assert installer.BASELINE in config["plugins"]["load"]["paths"][0]
    assert drill.RELEASE not in config["plugins"]["load"]["paths"][0]
    saved = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    assert saved["elapsed_us"] == receipt["elapsed_us"]
    assert called == []
    assert drill.main(["--root", str(tmp_path / "again")]) == 0


def test_a_changed_staged_pin_is_not_restored_and_live_paths_are_refused(tmp_path):
    installer = drill._installer()
    original_config, original_pin, candidate_config, candidate_pin = drill._pair(installer)
    root = tmp_path / "changed"
    root.mkdir()
    config = root / "openclaw.json"
    pin = root / "pin.conf"
    drill._write(config, candidate_config)
    drill._write(pin, candidate_pin + b"\n")
    with pytest.raises(ValueError, match="not the candidate"):
        drill.restore(config, pin, original_config, original_pin, candidate_config, candidate_pin)
    assert pin.read_bytes() == candidate_pin + b"\n"
    with pytest.raises(ValueError, match="staged copy"):
        drill.drill(Path("/opt/openclaw-fo/drill"))
