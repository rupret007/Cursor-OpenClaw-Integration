import importlib.util
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import threading
from zoneinfo import ZoneInfo

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
