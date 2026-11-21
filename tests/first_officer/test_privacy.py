import importlib.util
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fo_privacy", ROOT / "first-officer/privacy/scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

CREDENTIAL = "sk-" + "live-othersecret"
PHONE = "+1" + "3125551212"
ADDRESS = "10" + ".9.8.7"
EMAIL = "ada" + "@private.test"
TOKEN = "ghp_" + "abcdefghijklmnop"


def test_fixture_set_is_frozen_and_only_those_strings_are_ignored():
    assert scan.FIXTURES == frozenset({"sk-" + "live-secretvalue", "sk-" + "abcd1234efgh5678"})
    assert scan.findings_in_text("ask-location task-YYYYMMDD-HHMMSS") == []
    assert scan.findings_in_text("note sk-" + "live-secretvalue") == []
    assert scan.findings_in_text("Bearer ANDREA_SYNC_INTERNAL_TOKEN") == []
    assert scan.findings_in_text("see 8" + ".8.8.8 and 203.0.113.9 and 127.0.0.1") == []
    assert scan.findings_in_text("lane@example.invalid") == []
    assert scan.findings_in_text("+1" + "5555550123") == []
    assert scan.findings_in_text(scan.commit_message("Signed-off-by: Lane <" + EMAIL + ">")) == []
    assert scan.findings_in_text(scan.commit_message("Reach me at " + EMAIL)) == [
        {"rule": "email", "line": 1}]
    planted = scan.findings_in_text("\n".join([CREDENTIAL, PHONE, ADDRESS, EMAIL, TOKEN]))
    assert [item["rule"] for item in planted] == ["credential", "phone", "address", "email", "credential"]


def test_a_planted_secret_fails_and_is_not_printed(tmp_path, capsys):
    note = tmp_path / "note.txt"
    note.write_text(f"prefix {CREDENTIAL} suffix\n{PHONE}\n", encoding="utf-8")
    found = scan._file_findings(tmp_path, note)
    assert [item["rule"] for item in found] == ["credential", "phone"]
    assert all(CREDENTIAL not in item["path"] and PHONE not in item["path"] for item in found)
    rendered, code = scan.report({"files": 1, "commits": 1, "logs": 0, "findings": found})
    assert code == 1 and CREDENTIAL not in rendered and PHONE not in rendered
    assert "credential note.txt:1" in rendered and "phone note.txt:2" in rendered
    named = tmp_path / CREDENTIAL
    named.write_text("clean\n", encoding="utf-8")
    hidden = scan.report({"files": 1, "commits": 1, "logs": 0,
                          "findings": scan._file_findings(tmp_path, named)})[0]
    assert CREDENTIAL not in hidden and "[redacted]" in hidden


def test_history_and_logs_keep_a_removed_secret_and_a_log_line(tmp_path, capsys):
    root = tmp_path / "checkout"
    root.mkdir()
    run = lambda *args: subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    run("init", "-q", "-b", "demo")
    run("config", "user.email", "lane@example.invalid")
    run("config", "user.name", "First Officer lane")
    secret = root / "note.txt"
    secret.write_text(CREDENTIAL + "\n", encoding="utf-8")
    run("add", "note.txt")
    run("commit", "-q", "-m", "fixture")
    run("rm", "-q", "note.txt")
    run("commit", "-q", "-m", "remove fixture")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "lane.log").write_text(PHONE + "\n", encoding="utf-8")
    result = scan.survey(root, logs)
    rules = {(item["rule"], item["path"]) for item in result["findings"]}
    assert ("credential", "note.txt") in rules
    assert ("phone", "lane.log") in rules
    rendered, code = scan.report(result)
    assert code == 1 and CREDENTIAL not in rendered and PHONE not in rendered
    assert scan.main(["--root", str(root), "--logs", str(logs)]) == 1
    captured = capsys.readouterr()
    assert CREDENTIAL not in captured.err and PHONE not in captured.err
    assert "credential" in captured.err and "phone" in captured.err


def test_the_repository_history_and_logs_stay_clean(capsys):
    result = scan.survey(ROOT)
    assert result["findings"] == []
    assert result["files"] > 0 and result["commits"] > 1 and result["logs"] == 0
    assert scan.main([]) == 0
    assert capsys.readouterr().out == (
        f"Secret scan clean. Files {result['files']}. "
        f"History commits {result['commits']}. Logs {result['logs']}.\n"
    )
    with pytest.raises(ValueError, match="live gateway"):
        scan.survey(Path("/opt/openclaw-fo"))


def test_a_binary_file_and_a_shallow_clone_fail_closed(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"\0secret")
    run = lambda *args: subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    run("init", "-q", "-b", "demo")
    run("config", "user.email", "lane@example.invalid")
    run("config", "user.name", "First Officer lane")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    run("add", "README.md")
    run("commit", "-q", "-m", "fixture")
    (root / ".git" / "shallow").write_text("abcdef\n", encoding="utf-8")
    with pytest.raises(ValueError, match="full clone"):
        scan.scan_history(root)
    (root / ".git" / "shallow").unlink()
    findings, _count = scan.scan_files(root)
    assert {"rule": "binary", "path": "blob.bin", "line": 0} in findings
