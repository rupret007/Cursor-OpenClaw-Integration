import importlib.util
from datetime import datetime, timezone
import json
import os
import types
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from zoneinfo import ZoneInfo

import stat
import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fo_cycle", ROOT / "first-officer/acceptance/cycle.py")
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)

NOW = 1_763_700_000
FACT = "The lane review is weekly."
CHICAGO = ZoneInfo("America/Chicago")


def _files(root):
    return "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file())


def test_a_fact_stays_unverified_until_its_digest_is_confirmed_and_the_chain_holds(tmp_path):
    root = tmp_path / "cycle"
    cycle.initialize(root)
    assert cycle.recall(root) == "No verified saved fact."
    card = cycle.propose(root, FACT, NOW)
    assert cycle.recall(root) == "No verified saved fact."
    assert FACT not in (root / "audit.log").read_text(encoding="utf-8")
    wrong = "a" * 64
    assert cycle.confirm(root, card["nonce"], wrong, NOW) == {"saved": False, "reason": "digest"}
    assert not (root / "memory.json").exists()
    saved = cycle.confirm(root, card["nonce"], card["digest"], NOW)
    assert saved == {"saved": True, "text": FACT}
    assert FACT not in (root / "audit.log").read_text(encoding="utf-8")
    assert cycle.confirm(root, card["nonce"], card["digest"], NOW)["saved"] is False
    recalled = cycle.recall(root)
    assert recalled == f"Verified fact: {FACT} Approval {card['digest'][:12]}."
    memory = json.loads((root / "memory.json").read_text(encoding="utf-8"))
    assert memory["facts"][0]["text"] == FACT and len(memory["facts"]) == 1
    with pytest.raises(ValueError, match="already verified"):
        cycle.propose(root, FACT, NOW)
    receipt = cycle.run_routine(root, NOW)
    assert receipt["sent"] is False and receipt["called_model"] is False and receipt["changed_repo"] is False
    assert receipt["dry_run"] is True and receipt["routine"] == "lane-review"
    assert cycle.verify(root) == 4
    original = (root / "audit.log").read_text(encoding="utf-8").splitlines()
    flipped = original[:]
    flipped[0] = flipped[0][:-1] + ("0" if flipped[0][-1] != "0" else "1")
    (root / "audit.log").write_text("\n".join(flipped) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="altered"):
        cycle.verify(root)
    removed = original[:1] + original[2:]
    (root / "audit.log").write_text("\n".join(removed) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="altered|binding|audit"):
        cycle.verify(root)
    inserted = original[:1] + [original[0]] + original[1:]
    (root / "audit.log").write_text("\n".join(inserted) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="altered|binding|audit"):
        cycle.verify(root)


def test_expired_secret_and_live_routine_do_not_save_or_send(tmp_path):
    root = tmp_path / "cycle"
    cycle.initialize(root)
    card = cycle.propose(root, FACT, NOW)
    assert cycle.confirm(root, card["nonce"], card["digest"], NOW + cycle.TTL)["saved"] is True
    late = tmp_path / "late"
    cycle.initialize(late)
    late_card = cycle.propose(late, FACT, NOW)
    assert cycle.confirm(late, late_card["nonce"], late_card["digest"], NOW + cycle.TTL + 1) == {
        "saved": False, "reason": "expired",
    }
    assert cycle.recall(late) == "No verified saved fact."
    edited = tmp_path / "edited"
    cycle.initialize(edited)
    edited_card = cycle.propose(edited, FACT, NOW)
    pending_path = edited / "pending.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["text"] = "A different sentence."
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    pending_path.chmod(0o600)
    assert cycle.confirm(edited, edited_card["nonce"], edited_card["digest"], NOW) == {
        "saved": False, "reason": "digest",
    }
    assert cycle.recall(edited) == "No verified saved fact."
    secret = tmp_path / "secret"
    cycle.initialize(secret)
    with pytest.raises(ValueError, match="plain sentence"):
        cycle.propose(secret, "note sk-live-secretvalue", NOW)
    assert "sk-live-secretvalue" not in _files(secret)
    replaced = tmp_path / "replaced"
    cycle.initialize(replaced)
    first = cycle.propose(replaced, FACT, NOW)
    second = cycle.propose(replaced, "The lane review moved to Thursday.", NOW + cycle.TTL + 1)
    assert second["nonce"] != first["nonce"]
    assert cycle.confirm(replaced, second["nonce"], second["digest"], NOW + cycle.TTL + 1)["saved"] is True
    assert "Thursday" in cycle.recall(replaced)
    assert FACT not in cycle.recall(replaced)
    assert not (secret / "memory.json").exists()
    assert not (secret / "pending.json").exists()
    with pytest.raises(ValueError, match="dry run"):
        cycle.run_routine(tmp_path / "untouched", NOW, dry_run=False)
    assert not (tmp_path / "untouched").exists()


def test_memory_refuses_scanner_credential_shapes(tmp_path):
    """Credential shapes the checkout scanner enforces cannot be saved or recalled."""
    shapes = (
        "github" + "_pat_" + "A" * 22,
        "Bearer " + "a" * 24,
        "bearer " + "b" * 16,
        "BEARER " + "C" * 20,
        "Bearer " + ("D" * 12 + "8" * 8),
        "AS" + "IA" + "E" * 16,
        "AK" + "IA" + "F" * 16,
        "Bearer " + ("ANDREA_SYNC_INTERNAL_TOKEN"[:-1] + "M"),
        "bearer " + ("ANDREA_SYNC_INTERNAL_TOKEN" + "S"),
        "Bearer " + "ab-cd.ef_ghijklm",
        "BEARER " + ("aa_bb" + "-cc.dd/ee+ff~gg"),
    )
    for index, secret in enumerate(shapes):
        root = tmp_path / f"cycle-{index}"
        cycle.initialize(root)
        with pytest.raises(ValueError, match="plain sentence"):
            cycle.propose(root, f"Note {secret} today.", NOW)
        assert secret not in _files(root)
        assert cycle.recall(root) == "No verified saved fact."
        assert not (root / "pending.json").exists()
        assert not (root / "memory.json").exists()
    allowed = tmp_path / "exact-fixture"
    cycle.initialize(allowed)
    fact = "Remember Bearer " + "ANDREA_SYNC_INTERNAL_TOKEN" + " today."
    card = cycle.propose(allowed, fact, NOW)
    assert card["text"] == fact
    assert cycle.recall(allowed) == "No verified saved fact."


def test_reinitializing_after_deleting_the_log_and_heads_does_not_verify(tmp_path):
    """Deleting the log, key, and heads must not leave a new empty chain that verifies."""
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    assert cycle.verify(root) == 1
    trust = cycle.trust_dir(root)
    witness = cycle.witness_dir(root)
    identity = cycle.identity_dir(root)
    assert (identity / "root.id").is_file()
    for place in (root, root.parent, trust, witness):
        assert place.resolve() != identity.resolve()
        assert place.resolve() not in identity.resolve().parents
        assert identity.resolve() not in place.resolve().parents
    shutil.rmtree(root)
    shutil.rmtree(trust)
    shutil.rmtree(witness)
    with pytest.raises(ValueError, match="reset|owner cannot delete"):
        cycle.initialize(root)
    with pytest.raises(ValueError, match="reset|owner cannot delete|truncated"):
        cycle.verify(root)
    shutil.rmtree(identity)
    anchor = cycle.durable_anchor_path(root)
    anchor.unlink()
    cycle.initialize(root)
    # The owner-writable file can still be deleted when the root-owned seal is not installed.
    assert cycle.verify(root) == 0


def test_total_reset_of_the_four_directories_does_not_verify_empty(tmp_path, monkeypatch):
    """A silent initialize after deleting every owner-writable copy must not verify as empty."""
    monkeypatch.delenv("FO_AUDIT_ANCHOR", raising=False)
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    assert cycle.verify(root) == 1
    anchor = cycle.durable_anchor_path(root)
    trust = cycle.trust_dir(root)
    witness = cycle.witness_dir(root)
    identity = cycle.identity_dir(root)
    assert anchor.is_file()
    for place in (root, trust, witness, identity):
        assert anchor.resolve() != place.resolve()
        assert place.resolve() not in anchor.resolve().parents
        assert anchor.resolve() not in place.resolve().parents
        shutil.rmtree(place)
    assert anchor.is_file()
    total_reset_verifies_empty = False
    try:
        cycle.initialize(root)
        total_reset_verifies_empty = cycle.verify(root) == 0
    except ValueError:
        total_reset_verifies_empty = False
    assert total_reset_verifies_empty is False
    with pytest.raises(ValueError, match="reset|owner cannot delete"):
        cycle.verify(root)


def _operator_anchor(tmp_path):
    directory = tmp_path.parent / f".fo-operator-anchor-{tmp_path.name}"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    return directory / "anchor.json"


def test_configured_audit_anchor_fails_closed_when_missing_or_mismatched(tmp_path, monkeypatch):
    """A configured anchor outside the copies fails closed when it is missing or wrong."""
    anchor = _operator_anchor(tmp_path)
    monkeypatch.setenv("FO_AUDIT_ANCHOR", str(anchor))
    root = tmp_path / "cycle"
    with pytest.raises(ValueError, match="missing"):
        cycle.initialize(root)
    with pytest.raises(ValueError, match="missing"):
        cycle.verify(root)
    anchor.write_text("", encoding="utf-8")
    anchor.chmod(0o600)
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    assert cycle.verify(root) == 1
    saved = anchor.read_bytes()
    document = json.loads(saved)
    document["digest"] = ("0" if document["digest"][:1] != "0" else "1") + document["digest"][1:]
    anchor.write_text(json.dumps(document), encoding="utf-8")
    anchor.chmod(0o600)
    with pytest.raises(ValueError, match="match"):
        cycle.verify(root)
    anchor.write_bytes(saved)
    anchor.chmod(0o600)
    assert cycle.verify(root) == 1
    for place in (root, cycle.trust_dir(root), cycle.witness_dir(root), cycle.identity_dir(root)):
        assert anchor.resolve() != place.resolve()
        assert place.resolve() not in anchor.resolve().parents
        shutil.rmtree(place)
    total_reset_verifies_empty = False
    try:
        cycle.initialize(root)
        total_reset_verifies_empty = cycle.verify(root) == 0
    except ValueError:
        total_reset_verifies_empty = False
    assert total_reset_verifies_empty is False
    anchor.unlink()
    with pytest.raises(ValueError, match="missing"):
        cycle.initialize(root)
    with pytest.raises(ValueError, match="missing"):
        cycle.verify(root)
    anchor.write_text("", encoding="utf-8")
    anchor.chmod(0o600)
    cycle.initialize(root)
    # The owner-writable configured file can still be replaced when the root-owned seal is not installed.
    assert cycle.verify(root) == 0
    shutil.rmtree(anchor.parent)


def _plant(path, payload, mode):
    path.write_bytes(payload)
    path.chmod(mode)
    if os.geteuid() == 0:
        os.chown(path, 65534, 65534)
    return path


def test_production_anchor_fails_closed_for_a_non_root_user(tmp_path, monkeypatch):
    """Missing, replaced, unowned, and writable seals fail closed. A non-root user does not skip them."""
    monkeypatch.delenv("FO_AUDIT_ANCHOR", raising=False)
    monkeypatch.delenv("FO_PRODUCTION_AUDIT_ANCHOR", raising=False)
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    assert cycle.verify(root) == 1
    default_anchor = cycle.durable_anchor_path(root)
    payload = tmp_path / "payload"
    assert cycle.main(["--root", str(root), "export-anchor", str(payload)]) == 0
    body = payload.read_text(encoding="utf-8")
    document = json.loads(body)
    assert set(document) == cycle.SEAL_FIELDS
    assert "key" not in document
    key = (root / "audit.key").read_text(encoding="utf-8").strip()
    leaked = key in body
    assert leaked is False
    assert payload.stat().st_mode & 0o077 == 0

    missing = tmp_path / "no-such-anchor"
    monkeypatch.setenv("FO_PRODUCTION_AUDIT_ANCHOR", str(missing))
    with pytest.raises(ValueError, match="missing"):
        cycle.verify(root)
    with pytest.raises(ValueError, match="missing"):
        cycle.initialize(root)

    monkeypatch.setenv("FO_PRODUCTION_AUDIT_ANCHOR", str(_plant(tmp_path / "unowned", payload.read_bytes(), 0o444)))
    with pytest.raises(ValueError, match="not owned by root|not mode-restricted"):
        cycle.verify(root)
    monkeypatch.setenv("FO_PRODUCTION_AUDIT_ANCHOR", str(_plant(tmp_path / "mode600", payload.read_bytes(), 0o600)))
    with pytest.raises(ValueError, match="not owned by root|not mode-restricted"):
        cycle.verify(root)
    monkeypatch.setenv("FO_PRODUCTION_AUDIT_ANCHOR", str(_plant(tmp_path / "writable", payload.read_bytes(), 0o666)))
    with pytest.raises(ValueError, match="not owned by root|not mode-restricted"):
        cycle.verify(root)
    replaced = json.loads(body)
    replaced["digest"] = ("1" if replaced["digest"][:1] == "0" else "0") + replaced["digest"][1:]
    monkeypatch.setenv(
        "FO_PRODUCTION_AUDIT_ANCHOR",
        str(_plant(tmp_path / "replaced", json.dumps(replaced).encode(), 0o444)),
    )
    with pytest.raises(ValueError, match="not owned by root|match|replaced"):
        cycle.verify(root)

    operator = tmp_path.parent / f".fo-operator-anchor-{tmp_path.name}"
    operator.mkdir(mode=0o700, exist_ok=True)
    operator.chmod(0o700)
    local = operator / "anchor.json"
    local.write_text("", encoding="utf-8")
    local.chmod(0o600)
    monkeypatch.setenv("FO_AUDIT_ANCHOR", str(local))
    monkeypatch.setenv("FO_PRODUCTION_AUDIT_ANCHOR", str(missing))
    for place in (root, cycle.trust_dir(root), cycle.witness_dir(root), cycle.identity_dir(root)):
        shutil.rmtree(place)
    if default_anchor.exists():
        default_anchor.unlink()
    with pytest.raises(ValueError, match="missing|match|replaced|not owned by root|not mode-restricted|owner cannot delete"):
        cycle.initialize(root)
    with pytest.raises(ValueError, match="missing|match|replaced|not owned by root|not mode-restricted|owner cannot delete"):
        cycle.verify(root)
    shutil.rmtree(operator)


def root_owned_anchor_cases(tmp_path):
    """Root-owned mode checks. The caller is uid 0 inside a user namespace, or really root."""
    assert os.geteuid() == 0
    os.environ.pop("FO_AUDIT_ANCHOR", None)
    tmp_path = Path(tmp_path)
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    expected = cycle.verify(root)
    payload = tmp_path / "payload"
    cycle.export_anchor(root, payload)
    directory = tmp_path / "anchor-dir"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    os.chown(directory, 0, 0)
    anchor = directory / "audit-anchor"
    anchor.write_bytes(payload.read_bytes())
    os.chmod(anchor, 0o444)
    os.chown(anchor, 0, 0)
    os.environ["FO_PRODUCTION_AUDIT_ANCHOR"] = str(anchor)
    if cycle.verify(root) != expected:
        raise RuntimeError("readable root seal was refused")

    os.chmod(anchor, 0o644)
    document = json.loads(anchor.read_text(encoding="utf-8"))
    document["root"] = "0" * 64 if document["root"][:1] != "0" else "1" * 64
    anchor.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(anchor, 0o444)
    os.chown(anchor, 0, 0)
    try:
        cycle.verify(root)
    except ValueError as error:
        if "match" not in str(error) and "replaced" not in str(error):
            raise
    else:
        raise RuntimeError("replaced seal was accepted")

    os.chmod(anchor, 0o644)
    anchor.write_bytes(payload.read_bytes())
    os.chmod(anchor, 0o600)
    os.chown(anchor, 0, 0)
    try:
        cycle.verify(root)
    except ValueError as error:
        if "mode-restricted" not in str(error):
            raise
    else:
        raise RuntimeError("mode 0600 was accepted")

    os.chmod(anchor, 0o666)
    try:
        cycle.verify(root)
    except ValueError as error:
        if "mode-restricted" not in str(error):
            raise
    else:
        raise RuntimeError("writable seal was accepted")

    os.chmod(anchor, 0o444)
    os.chmod(directory, 0o777)
    try:
        cycle.verify(root)
    except ValueError as error:
        if "mode-restricted" not in str(error):
            raise
    else:
        raise RuntimeError("replaceable directory was accepted")
    os.chmod(directory, 0o755)

    for place in (root, cycle.trust_dir(root), cycle.witness_dir(root), cycle.identity_dir(root)):
        shutil.rmtree(place)
    local = cycle.durable_anchor_path(root)
    if local.exists():
        local.unlink()
    try:
        cycle.initialize(root)
    except ValueError:
        pass
    else:
        raise RuntimeError("reset initialize returned")
    try:
        cycle.verify(root)
    except ValueError:
        pass
    else:
        raise RuntimeError("reset verify returned")
    print("anchor-ok")


def _stat(uid, gid, mode, *, size=128, directory=False):
    kind = stat.S_IFDIR if directory else stat.S_IFREG
    return types.SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=kind | mode, st_size=size)


def test_production_seal_metadata_accepts_only_root_owned_service_readable_seals():
    """Uid 0, gid 0, mode 0444, and a 0755 root-owned directory are required."""
    parent = _stat(0, 0, 0o755, directory=True)
    assert cycle.production_seal_metadata_ok(_stat(0, 0, 0o444), parent) is None
    assert cycle.production_seal_metadata_ok(_stat(0, 0, 0o644, size=0), parent) == cycle.ANCHOR_MISMATCH
    assert cycle.production_seal_metadata_ok(_stat(1, 0, 0o444), parent) == cycle.ANCHOR_OWNER
    assert cycle.production_seal_metadata_ok(_stat(0, 1, 0o444), parent) == cycle.ANCHOR_OWNER
    assert cycle.production_seal_metadata_ok(_stat(0, 0, 0o600), parent) == cycle.ANCHOR_MODE
    assert cycle.production_seal_metadata_ok(_stat(0, 0, 0o664), parent) == cycle.ANCHOR_MODE
    assert cycle.production_seal_metadata_ok(_stat(0, 0, 0o666), parent) == cycle.ANCHOR_MODE
    file_ok = _stat(0, 0, 0o444)
    assert cycle.production_seal_metadata_ok(file_ok, _stat(0, 0, 0o777, directory=True)) == cycle.ANCHOR_MODE
    assert cycle.production_seal_metadata_ok(file_ok, _stat(1, 0, 0o755, directory=True)) == cycle.ANCHOR_OWNER
    link = types.SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFLNK | 0o777, st_size=128)
    assert cycle.production_seal_metadata_ok(link, parent) == cycle.ANCHOR_REPLACED


def _can_map_root_user():
    if os.geteuid() == 0:
        return True
    completed = subprocess.run(
        ["unshare", "--user", "--map-root-user", "true"],
        check=False, text=True, capture_output=True, timeout=10,
    )
    return completed.returncode == 0


def test_root_owned_anchor_mode_and_replacement(tmp_path):
    """The readable root-owned seal is accepted. 0600, group write, and a replaced seal are not."""
    test_production_seal_metadata_accepts_only_root_owned_service_readable_seals()
    if os.geteuid() == 0:
        root_owned_anchor_cases(tmp_path)
        return
    if not _can_map_root_user():
        # GitHub-hosted runners refuse user namespaces. The policy is still
        # tested above, and fail-closed filesystem cases stay in
        # test_production_anchor_fails_closed_for_a_non_root_user.
        return
    completed = subprocess.run(
        ["unshare", "--user", "--map-root-user", sys.executable, "-B", str(Path(__file__).resolve()),
         "--root-anchor-selftest", str(tmp_path)],
        check=False, text=True, capture_output=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "anchor-ok"
    assert "key" not in completed.stdout


def test_a_configured_anchor_inside_an_owner_copy_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "cycle"
    root.mkdir(mode=0o700)
    places = (
        root / "anchor.json",
        tmp_path / "anchor.json",
        cycle.trust_dir(root) / "anchor.json",
        cycle.witness_dir(root) / "anchor.json",
        cycle.identity_dir(root) / "anchor.json",
    )
    for path in places:
        monkeypatch.setenv("FO_AUDIT_ANCHOR", str(path))
        with pytest.raises(ValueError, match="outside"):
            cycle.initialize(root)


def test_a_missing_default_anchor_does_not_verify_a_live_chain(tmp_path, monkeypatch):
    monkeypatch.delenv("FO_AUDIT_ANCHOR", raising=False)
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    cycle.durable_anchor_path(root).unlink()
    with pytest.raises(ValueError, match="missing|match"):
        cycle.verify(root)


def test_deleting_every_owner_writable_audit_copy_is_not_a_silent_pass(tmp_path):
    """Deleting the root and both owner-writable copies must not verify as a fresh chain."""
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    trust = cycle.trust_dir(root)
    witness = cycle.witness_dir(root)
    assert (trust / "audit.head").is_file()
    assert (witness / "audit.head").is_file()
    shutil.rmtree(root)
    shutil.rmtree(trust)
    shutil.rmtree(witness)
    with pytest.raises(ValueError, match="owner cannot delete"):
        cycle.verify(root)
    fresh = tmp_path / "unused-after-reset"
    with pytest.raises(ValueError, match="owner cannot delete"):
        cycle.verify(fresh)
    assert not cycle.trust_dir(fresh).exists()
    assert not cycle.witness_dir(fresh).exists()


@pytest.mark.parametrize("event", ["routine", "propose", "refusal", "confirm"])
def test_a_new_event_cannot_hide_a_complete_audit_reset(tmp_path, event):
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    trust = cycle.trust_dir(root)
    witness = cycle.witness_dir(root)
    shutil.rmtree(root)
    shutil.rmtree(trust)
    shutil.rmtree(witness)
    moment = NOW + 1

    with pytest.raises(ValueError, match="owner cannot delete"):
        if event == "routine":
            cycle.run_routine(root, moment)
        elif event == "propose":
            cycle.propose(root, FACT, moment)
        elif event == "refusal":
            cycle.propose(root, "", moment)
        else:
            cycle.confirm(root, "mem-" + "a" * 16, "a" * 64, moment)

    assert not trust.exists() and not witness.exists()
    assert (root / "audit.log").read_bytes() == b""
    assert not (root / "pending.json").exists()
    assert not (root / "receipt-lane-review.json").exists()
    with pytest.raises(ValueError, match="owner cannot delete"):
        cycle.verify(root)


def test_deleting_the_acceptance_root_and_its_trust_anchor_does_not_verify(tmp_path):
    """Removing the acceptance root and the real trust directory must not verify as fresh."""
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    trust = cycle.trust_dir(root)
    witness = cycle.witness_dir(root)
    assert (trust / "audit.head").is_file()
    assert (witness / "audit.head").is_file()
    assert trust.resolve() not in witness.resolve().parents
    assert witness.resolve() not in trust.resolve().parents
    assert root.resolve() not in witness.resolve().parents
    assert root.parent.resolve() not in witness.resolve().parents
    shutil.rmtree(root)
    shutil.rmtree(trust)
    with pytest.raises(ValueError, match="truncated"):
        cycle.verify(root)
    assert (witness / "audit.head").is_file()
    kept = tmp_path / "kept"
    cycle.initialize(kept)
    cycle.run_routine(kept, NOW)
    shutil.rmtree(cycle.witness_dir(kept))
    with pytest.raises(ValueError, match="checkpoint|truncated"):
        cycle.verify(kept)
    fresh = tmp_path / "unused"
    with pytest.raises(ValueError, match="owner cannot delete"):
        cycle.verify(fresh)
    assert not cycle.witness_dir(fresh).exists()


def test_deleting_the_acceptance_directory_and_its_sibling_does_not_verify(tmp_path):
    """Wiping the acceptance root and the sibling copy must not look like a fresh root."""
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    sibling = root.parent / f".{root.name}.fo-audit-trust"
    shutil.rmtree(root)
    if sibling.exists():
        shutil.rmtree(sibling)
    with pytest.raises(ValueError, match="truncated"):
        cycle.verify(root)
    anchor = cycle.trust_dir(root) / "audit.head"
    assert anchor.is_file()
    assert root.parent.resolve() not in anchor.resolve().parents
    assert root.resolve() not in anchor.resolve().parents


def test_resetting_the_acceptance_directory_does_not_erase_the_trust_anchor(tmp_path):
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    for name in ("audit.log", "audit.head", "audit.key", "receipt-lane-review.json"):
        (root / name).unlink()
    with pytest.raises(ValueError, match="truncated"):
        cycle.verify(root)
    anchor = cycle.trust_dir(root) / "audit.head"
    assert anchor.is_file()
    assert root.resolve() not in anchor.resolve().parents
    assert root.parent.resolve() not in anchor.resolve().parents
    fresh = tmp_path / "fresh"
    with pytest.raises(ValueError, match="owner cannot delete"):
        cycle.verify(fresh)
    assert not cycle.trust_dir(fresh).exists()
    assert not (fresh.parent / f".{fresh.name}.fo-audit-trust").exists()


def test_a_truncated_chain_or_an_orphan_receipt_does_not_verify(tmp_path):
    root = tmp_path / "cycle"
    cycle.initialize(root)
    cycle.run_routine(root, NOW)
    assert cycle.verify(root) == 1
    (root / "audit.log").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="truncated"):
        cycle.verify(root)
    for name in ("audit.head", "audit.key"):
        leftover = root / name
        if leftover.exists():
            leftover.unlink()
    with pytest.raises(ValueError, match="truncated"):
        cycle.verify(root)
    orphan = tmp_path / "orphan"
    orphan.mkdir(mode=0o700)
    receipt = orphan / "receipt-lane-review.json"
    receipt.write_text("{}\n", encoding="utf-8")
    receipt.chmod(0o600)
    with pytest.raises(ValueError, match="receipt"):
        cycle.verify(orphan)

    pending = tmp_path / "pending"
    cycle.initialize(pending)
    cycle.propose(pending, FACT, NOW)
    assert cycle.verify(pending) == 0
    broken = json.loads((pending / "pending.json").read_text(encoding="utf-8"))
    broken["text"] = "A different sentence."
    (pending / "pending.json").write_text(json.dumps(broken), encoding="utf-8")
    (pending / "pending.json").chmod(0o600)
    with pytest.raises(ValueError, match="pending|altered"):
        cycle.verify(pending)


def test_schedule_is_the_next_chicago_morning_and_two_confirms_save_once(tmp_path):
    morning = datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)
    same_day = datetime(2026, 11, 20, 7, tzinfo=CHICAGO)
    assert cycle.next_run(int(morning.timestamp())) == int(same_day.timestamp())
    afternoon = datetime(2026, 11, 20, 14, tzinfo=timezone.utc)
    next_day = datetime(2026, 11, 21, 7, tzinfo=CHICAGO)
    assert cycle.next_run(int(afternoon.timestamp())) == int(next_day.timestamp())
    exact = datetime(2026, 11, 20, 13, tzinfo=timezone.utc)
    assert cycle.next_run(int(exact.timestamp())) == int(next_day.timestamp())

    root = tmp_path / "cycle"
    cycle.initialize(root)
    card = cycle.propose(root, FACT, NOW)
    results = []

    def confirm_once():
        results.append(cycle.confirm(root, card["nonce"], card["digest"], NOW))

    threads = [threading.Thread(target=confirm_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert sorted(result["saved"] for result in results) == [False, True]
    assert len(json.loads((root / "memory.json").read_text(encoding="utf-8"))["facts"]) == 1
    cycle.verify(root)


def test_initialize_preserves_an_existing_pending_only_approval(tmp_path):
    source = tmp_path / "source"
    cycle.initialize(source)
    card = cycle.propose(source, FACT, NOW)
    legacy = tmp_path / "legacy"
    legacy.mkdir(mode=0o700)
    shutil.copy2(source / "pending.json", legacy / "pending.json")

    cycle.initialize(legacy)

    assert cycle.verify(legacy) == 0
    assert cycle.confirm(legacy, card["nonce"], card["digest"], NOW) == {"saved": True, "text": FACT}
    assert cycle.verify(legacy) == 1


def test_command_prints_the_verified_fact_and_the_dry_run(tmp_path):
    root = tmp_path / "cycle"
    assert cycle.main(["--root", str(root), "init"]) == 0
    assert cycle.verify(root) == 0
    assert cycle.main(["--root", str(root), "init"]) == 1
    assert cycle.main(["--root", str(root), "propose", FACT]) == 0
    pending = json.loads((root / "pending.json").read_text(encoding="utf-8"))
    assert cycle.main(["--root", str(root), "confirm", pending["nonce"], pending["digest"]]) == 0
    assert FACT in cycle.recall(root)
    assert cycle.main(["--root", str(root), "routine"]) == 0
    assert cycle.main(["--root", str(root), "verify"]) == 0
    receipt = json.loads((root / "receipt-lane-review.json").read_text(encoding="utf-8"))
    assert receipt["sent"] is False
    source = Path(cycle.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source and "urllib" not in source


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--root-anchor-selftest":
    root_owned_anchor_cases(Path(sys.argv[2]))
