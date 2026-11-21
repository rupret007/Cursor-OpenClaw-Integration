import importlib.util
from ipaddress import IPv6Address
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
    assert scan.BEARER_FIXTURES == frozenset({
        "ANDREA_SYNC_INTERNAL_TOKEN",
        "experience-internal-token",
        "fixture-lane-key",
        "internal-test-token",
    })
    for exact in ("experience-internal-token", "fixture-lane-key", "internal-test-token"):
        assert scan.findings_in_text("Bearer " + exact) == []
        assert [item["rule"] for item in scan.findings_in_text("Bearer " + exact + "s")] == ["credential"]
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


def test_scan_catches_private_and_public_ipv6_addresses_without_printing_them(tmp_path, capsys):
    private = IPv6Address((0xFD << 120) | (42 << 64) | 9)
    public = IPv6Address((0x2004 << 112) | (0xA1 << 80) | 9)
    mapped = "::ffff:" + ADDRESS
    note = tmp_path / "route.txt"
    note.write_text(f"private {private}%route\npublic [{public.exploded}]:443\nmapped {mapped}\n", encoding="utf-8")
    findings, _ = scan.scan_files(tmp_path)
    rendered, code = scan.report({"files": 1, "commits": 0, "logs": 0, "findings": findings})
    assert code == 1
    assert rendered.splitlines()[1:] == [f"address route.txt:{line}" for line in (1, 2, 3)]
    assert all(value not in rendered for value in (str(private), public.exploded, mapped))
    assert scan.findings_in_text("unspecified :: loopback ::1 documentation 2001:db8::9 at 08:30:00") == []
    assert scan.findings_in_text(str(IPv6Address((0xFD00 << 112) | 1))) == [{"rule": "address", "line": 1}]


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


@pytest.mark.parametrize("trailer", [
    f"Signed-off-by: {CREDENTIAL}",
    f"Reviewed-by: Lane <{EMAIL}> {CREDENTIAL}",
    f"Co-authored-by: {CREDENTIAL} <{EMAIL}>",
    f"Acked-by: Lane <{CREDENTIAL}@private.test>",
], ids=["bare", "suffix", "name", "mailbox"])
def test_history_scans_credentials_inside_identity_trailers(tmp_path, trailer):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "demo")
    git("config", "user.name", "First Officer lane")
    git("config", "user.email", "lane@example.invalid")
    git("commit", "--allow-empty", "-qm", "fixture", "-m", trailer)
    findings, count = scan.scan_history(tmp_path)
    assert count == 1
    assert any(item["rule"] == "credential" and item["path"] == "message" for item in findings)
    rendered, code = scan.report({"files": 0, "commits": count, "logs": 0, "findings": findings})
    assert code == 1 and CREDENTIAL not in rendered and EMAIL not in rendered


def test_history_flags_a_sensitive_pathname_and_a_rename(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    run = lambda *args: subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    run("init", "-q", "-b", "demo")
    run("config", "user.email", "lane@example.invalid")
    run("config", "user.name", "First Officer lane")
    secret = root / EMAIL
    secret.write_text("benign\n", encoding="utf-8")
    run("add", EMAIL)
    run("commit", "-q", "-m", "fixture")
    run("mv", EMAIL, "note.txt")
    run("commit", "-q", "-m", "rename fixture")
    findings, count = scan.scan_history(root)
    assert count == 2
    assert any(item["rule"] == "email" and item["path"] == "[redacted]" for item in findings)
    rendered, code = scan.report({"files": 0, "commits": count, "logs": 0, "findings": findings})
    assert code == 1 and EMAIL not in rendered and "[redacted]" in rendered


def test_history_scans_a_quoted_path_and_the_deleted_credential(tmp_path):
    """Git's C-quoted path form must not hide a removed credential."""
    root = tmp_path / "checkout"
    root.mkdir()
    run = lambda *args: subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    run("init", "-q", "-b", "demo")
    run("config", "user.email", "lane@example.invalid")
    run("config", "user.name", "First Officer lane")
    run("config", "core.quotePath", "true")
    name = "cred" + "\t" + "file.txt"
    secret = root / name
    secret.write_text(CREDENTIAL + "\n", encoding="utf-8")
    run("add", "--", name)
    run("commit", "-q", "-m", "fixture")
    run("rm", "-q", "--", name)
    run("commit", "-q", "-m", "remove fixture")
    findings, count = scan.scan_history(root)
    assert count == 2
    assert any(item["rule"] == "credential" for item in findings)
    rendered, code = scan.report({"files": 0, "commits": count, "logs": 0, "findings": findings})
    assert code == 1 and CREDENTIAL not in rendered


def test_history_scans_a_credential_that_exists_only_in_a_merge(tmp_path):
    """A credential introduced and removed only by merge resolutions is still a finding."""
    root = tmp_path / "checkout"
    root.mkdir()
    run = lambda *args: subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "lane@example.invalid")
    run("config", "user.name", "First Officer lane")
    (root / "note.txt").write_text("base\n", encoding="utf-8")
    run("add", "note.txt")
    run("commit", "-q", "-m", "base")
    run("checkout", "-q", "-b", "left")
    (root / "note.txt").write_text("left\n", encoding="utf-8")
    run("commit", "-aq", "-m", "left")
    run("checkout", "-q", "main")
    run("checkout", "-q", "-b", "right")
    (root / "note.txt").write_text("right\n", encoding="utf-8")
    run("commit", "-aq", "-m", "right")
    run("checkout", "-q", "left")
    conflict = subprocess.run(
        ["git", "-C", str(root), "merge", "--no-commit", "right"], capture_output=True,
    )
    assert conflict.returncode != 0
    (root / "note.txt").write_text(CREDENTIAL + "\n", encoding="utf-8")
    run("add", "note.txt")
    run("commit", "-q", "-m", "merge one")
    run("checkout", "-q", "-b", "side", "main")
    (root / "note.txt").write_text("side\n", encoding="utf-8")
    run("commit", "-aq", "-m", "side")
    run("checkout", "-q", "left")
    again = subprocess.run(
        ["git", "-C", str(root), "merge", "--no-commit", "side"], capture_output=True,
    )
    assert again.returncode != 0
    (root / "note.txt").write_text("clean\n", encoding="utf-8")
    run("add", "note.txt")
    run("commit", "-q", "-m", "merge two")
    findings, count = scan.scan_history(root)
    assert count > 1
    assert any(item["rule"] == "credential" and item["path"] == "note.txt" for item in findings)
    rendered, code = scan.report({"files": 0, "commits": count, "logs": 0, "findings": findings})
    assert code == 1 and CREDENTIAL not in rendered


def test_temporary_session_keys_and_alphabetic_bearers_fail_closed(tmp_path, capsys):
    """Source, history, and a files-only candidate scan report these shapes and do not print them."""
    access_key = "ASIA" + "A" * 16
    token = "a" * 16
    bearer = "Bearer " + token
    assert [item["rule"] for item in scan.findings_in_text(access_key)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text("AWS_ACCESS_KEY_ID=" + access_key)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(bearer)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text("Authorization: " + bearer)] == ["credential"]

    root = tmp_path / "checkout"
    root.mkdir()
    note = root / "note.txt"
    note.write_text(access_key + "\n" + bearer + "\n", encoding="utf-8")
    findings, _count = scan.scan_files(root)
    rendered, code = scan.report({"files": 1, "commits": 0, "logs": 0, "findings": findings})
    assert code == 1 and access_key not in rendered and token not in rendered
    assert "credential note.txt:1" in rendered and "credential note.txt:2" in rendered

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "demo")
    git("config", "user.email", "lane@example.invalid")
    git("config", "user.name", "First Officer lane")
    git("add", "note.txt")
    git("commit", "-q", "-m", "fixture")
    history, commits = scan.scan_history(root)
    assert commits == 1
    assert any(item["rule"] == "credential" and item["path"] == "note.txt" for item in history)
    history_report, history_code = scan.report({"files": 0, "commits": commits, "logs": 0, "findings": history})
    assert history_code == 1 and access_key not in history_report and token not in history_report

    assert scan.main(["--root", str(root), "--files-only"]) == 1
    captured = capsys.readouterr()
    assert access_key not in captured.err and token not in captured.err
    assert "credential note.txt:1" in captured.err and "credential note.txt:2" in captured.err


def test_fine_grained_pats_and_lowercase_bearers_fail_closed(tmp_path, capsys):
    """Source, history, and a files-only candidate scan report these shapes and do not print them."""
    pat = "github" + "_pat_" + "A" * 40
    lowered_token = "b" * 24
    lowered = "bearer " + lowered_token
    mixed = "BeArEr " + "c" * 16
    access_key = "AS" + "IA" + "A" * 16
    alphabetic = "Bearer " + "a" * 24
    assert [item["rule"] for item in scan.findings_in_text(pat)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text("token=" + pat)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(lowered)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text("Authorization: " + lowered)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(mixed)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(access_key)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(alphabetic)] == ["credential"]
    assert scan.findings_in_text("bearer ANDREA_SYNC_INTERNAL_TOKEN") == []

    root = tmp_path / "checkout"
    root.mkdir()
    note = root / "note.txt"
    note.write_text("\n".join([pat, lowered, access_key, alphabetic]) + "\n", encoding="utf-8")
    findings, _count = scan.scan_files(root)
    rendered, code = scan.report({"files": 1, "commits": 0, "logs": 0, "findings": findings})
    assert code == 1
    for secret in (pat, lowered_token, lowered, access_key, alphabetic):
        assert secret not in rendered
    for line in (1, 2, 3, 4):
        assert f"credential note.txt:{line}" in rendered

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "demo")
    git("config", "user.email", "lane@example.invalid")
    git("config", "user.name", "First Officer lane")
    git("add", "note.txt")
    git("commit", "-q", "-m", "fixture")
    history, commits = scan.scan_history(root)
    assert commits == 1
    assert any(item["rule"] == "credential" and item["path"] == "note.txt" for item in history)
    history_report, history_code = scan.report({"files": 0, "commits": commits, "logs": 0, "findings": history})
    assert history_code == 1
    for secret in (pat, lowered_token, lowered, access_key, alphabetic):
        assert secret not in history_report

    assert scan.main(["--root", str(root), "--files-only"]) == 1
    captured = capsys.readouterr()
    for secret in (pat, lowered_token, lowered, access_key, alphabetic):
        assert secret not in captured.err and secret not in captured.out
    for line in (1, 2, 3, 4):
        assert f"credential note.txt:{line}" in captured.err


def test_uppercase_bearer_credentials_fail_closed(tmp_path, capsys):
    """Source, history, and a files-only scan catch uppercase bearer tokens and do not print them."""
    upper_alpha = "A" * 24
    upper_alnum = "A" * 12 + "7" * 8
    upper_shape = "ABCD_EFGH_IJKL_1234"
    lower_alpha = "a" * 16
    mixed_token = "c" * 16
    pat = "github" + "_pat_" + "A" * 22
    access_key = "AS" + "IA" + "B" * 16
    fixture = "ANDREA_SYNC_INTERNAL_TOKEN"
    bearers = {
        "upper_alpha": "BEARER " + upper_alpha,
        "upper_alnum": "Bearer " + upper_alnum,
        "upper_shape": "bearer " + upper_shape,
        "lower_alpha": "bearer " + lower_alpha,
        "mixed_scheme": "BeArEr " + mixed_token,
        "upper_scheme_lower_token": "BEARER " + lower_alpha,
    }
    assert [item["rule"] for item in scan.findings_in_text(bearers["upper_alpha"])] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(bearers["upper_alnum"])] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(bearers["upper_shape"])] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(bearers["lower_alpha"])] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(bearers["mixed_scheme"])] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(bearers["upper_scheme_lower_token"])] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(pat)] == ["credential"]
    assert [item["rule"] for item in scan.findings_in_text(access_key)] == ["credential"]
    assert scan.findings_in_text("Bearer " + fixture) == []
    assert scan.findings_in_text("bearer " + fixture) == []
    assert [item["rule"] for item in scan.findings_in_text("Bearer " + fixture + "9")] == ["credential"]

    lines = [
        bearers["upper_alpha"],
        bearers["upper_alnum"],
        bearers["upper_shape"],
        bearers["lower_alpha"],
        pat,
        access_key,
        bearers["mixed_scheme"],
        "Bearer " + fixture,
    ]
    secrets = (upper_alpha, upper_alnum, upper_shape, lower_alpha, mixed_token, pat, access_key)
    root = tmp_path / "checkout"
    root.mkdir()
    note = root / "note.txt"
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    findings, _count = scan.scan_files(root)
    rendered, code = scan.report({"files": 1, "commits": 0, "logs": 0, "findings": findings})
    assert code == 1
    for secret in secrets:
        assert secret not in rendered
    for line in (1, 2, 3, 4, 5, 6, 7):
        assert f"credential note.txt:{line}" in rendered
    assert "credential note.txt:8" not in rendered

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "demo")
    git("config", "user.email", "lane@example.invalid")
    git("config", "user.name", "First Officer lane")
    git("add", "note.txt")
    git("commit", "-q", "-m", "fixture")
    history, commits = scan.scan_history(root)
    assert commits == 1
    assert any(item["rule"] == "credential" and item["path"] == "note.txt" for item in history)
    history_report, history_code = scan.report({"files": 0, "commits": commits, "logs": 0, "findings": history})
    assert history_code == 1
    for secret in secrets:
        assert secret not in history_report

    assert scan.main(["--root", str(root), "--files-only"]) == 1
    captured = capsys.readouterr()
    for secret in secrets:
        assert secret not in captured.err and secret not in captured.out
    for line in (1, 2, 3, 4, 5, 6, 7):
        assert f"credential note.txt:{line}" in captured.err
    assert "credential note.txt:8" not in captured.err


def test_punctuated_bearer_tokens_fail_closed(tmp_path, capsys):
    """A non-digit bearer with punctuation is a credential. Only exact fixtures are exempt."""
    fixture = "ANDREA_SYNC_INTERNAL_TOKEN"
    same_shape = fixture[:-1] + "M"
    longer_shape = fixture + "S"
    lower_fixture = fixture.lower()
    mixed = "ab-cd.ef_ghijklm"
    symbols = "aa_bb" + "-cc.dd/ee+ff~gg"
    pure = "a" * 16
    alnum = "A" * 12 + "7" * 8
    listed = "internal-test-token"
    near = listed + "s"
    short = "a-b.c" * 3
    bearers = {
        "same_shape": "Bearer " + same_shape,
        "longer_shape": "bearer " + longer_shape,
        "lower_fixture": "Bearer " + lower_fixture,
        "mixed": "Bearer " + mixed,
        "symbols": "BEARER " + symbols,
        "pure": "bearer " + pure,
        "alnum": "Bearer " + alnum,
        "near": "bearer " + near,
        "fixture": "Bearer " + fixture,
        "listed": "bearer " + listed,
        "short": "Bearer " + short,
    }
    for name in ("same_shape", "longer_shape", "lower_fixture", "mixed", "symbols", "pure", "alnum", "near"):
        assert [item["rule"] for item in scan.findings_in_text(bearers[name])] == ["credential"]
        assert scan.enforced_secret.search(bearers[name]) is True
    for name in ("fixture", "listed", "short"):
        assert scan.findings_in_text(bearers[name]) == []
        assert scan.enforced_secret.search(bearers[name]) is None
    assert scan.enforced_secret.sub("[redacted]", bearers["same_shape"]) == "[redacted]"
    assert scan.enforced_secret.sub("[redacted]", bearers["mixed"]) == "[redacted]"
    assert scan.enforced_secret.sub("[redacted]", bearers["fixture"]) == bearers["fixture"]

    lines = [
        bearers["same_shape"],
        bearers["longer_shape"],
        bearers["lower_fixture"],
        bearers["mixed"],
        bearers["symbols"],
        bearers["pure"],
        bearers["alnum"],
        bearers["near"],
        bearers["fixture"],
        bearers["listed"],
        bearers["short"],
    ]
    secrets = (same_shape, longer_shape, lower_fixture, mixed, symbols, pure, alnum, near)
    root = tmp_path / "checkout"
    root.mkdir()
    note = root / "note.txt"
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    findings, _count = scan.scan_files(root)
    rendered, code = scan.report({"files": 1, "commits": 0, "logs": 0, "findings": findings})
    assert code == 1
    for secret in secrets:
        assert secret not in rendered
    for line in (1, 2, 3, 4, 5, 6, 7, 8):
        assert f"credential note.txt:{line}" in rendered
    for line in (9, 10, 11):
        assert f"credential note.txt:{line}" not in rendered

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "demo")
    git("config", "user.email", "lane@example.invalid")
    git("config", "user.name", "First Officer lane")
    git("add", "note.txt")
    git("commit", "-q", "-m", "fixture")
    history, commits = scan.scan_history(root)
    assert commits == 1
    assert any(item["rule"] == "credential" and item["path"] == "note.txt" for item in history)
    history_report, history_code = scan.report({"files": 0, "commits": commits, "logs": 0, "findings": history})
    assert history_code == 1
    for secret in secrets:
        assert secret not in history_report

    assert scan.main(["--root", str(root), "--files-only"]) == 1
    captured = capsys.readouterr()
    for secret in secrets:
        assert secret not in captured.err and secret not in captured.out
    for line in (1, 2, 3, 4, 5, 6, 7, 8):
        assert f"credential note.txt:{line}" in captured.err
    for line in (9, 10, 11):
        assert f"credential note.txt:{line}" not in captured.err


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


def test_a_directory_symlink_is_rejected_and_named_trees_are_scanned(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("benign\n", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    findings, count = scan.scan_files(root)
    assert findings == [{"rule": "symlink", "path": "linked", "line": 0}]
    assert count == 0

    planted = root / "data"
    planted.mkdir()
    (planted / "note.txt").write_text(CREDENTIAL + "\n", encoding="utf-8")
    logs = root / "artifacts" / "logs"
    logs.mkdir(parents=True)
    (logs / "lane.log").write_text(PHONE + "\n", encoding="utf-8")
    findings, count = scan.scan_files(root)
    rules = {(item["rule"], item["path"]) for item in findings}
    assert ("symlink", "linked") in rules
    assert ("credential", "data/note.txt") in rules
    assert ("phone", "artifacts/logs/lane.log") in rules
    assert count == 2
    rendered, code = scan.report({"files": count, "commits": 0, "logs": 1, "findings": findings})
    assert code == 1 and CREDENTIAL not in rendered and PHONE not in rendered
    log_findings, log_count = scan.default_logs(root)
    assert log_count == 1
    assert log_findings == [{"rule": "symlink", "path": "linked", "line": 0}]

    external = tmp_path / "external-logs"
    external.mkdir()
    (external / "linked").symlink_to(outside, target_is_directory=True)
    (external / "lane.log").write_text("clean\n", encoding="utf-8")
    external_findings, external_count = scan.scan_logs(external)
    assert {"rule": "symlink", "path": "linked", "line": 0} in external_findings
    assert external_count == 1


def test_only_the_documented_local_database_skips_binary_contents(tmp_path):
    root = tmp_path / "checkout"
    (root / "data").mkdir(parents=True)
    documented = root / "data" / "andrea_sync.db"
    documented.write_bytes(b"\0local")
    findings, _count = scan.scan_files(root)
    assert findings == []
    other = root / "data" / "other.db"
    other.write_bytes(b"\0local")
    findings, _count = scan.scan_files(root)
    assert {"rule": "binary", "path": "data/other.db", "line": 0} in findings
    documented.write_text(CREDENTIAL + "\n", encoding="utf-8")
    findings, _count = scan.scan_files(root)
    assert ("credential", "data/andrea_sync.db") in {(item["rule"], item["path"]) for item in findings}
    rendered = scan.report({"files": 1, "commits": 0, "logs": 0, "findings": findings})[0]
    assert CREDENTIAL not in rendered


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
