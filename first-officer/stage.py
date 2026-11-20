#!/usr/bin/env python3
"""Build a private Segment 1 candidate from the verified Round 13 inventory."""

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile


BASELINE = "daf8df40c3b03440d69db6b61a271362b4b442551c7d3648b0b87c6f8b046ecf"
SEGMENT1 = "ed545847333ee1691824cd8e3b0a528882b2b11c7f2479c1f63af9a0e8def456"
SUPPORTED_BASES = (BASELINE, SEGMENT1)
ROLLBACK_SLOT = "4759047e8a16d1691499d55c4b6656882e67bc2275a3297d2283aa9b01538753"
RUNTIME = Path("round6/runtime")
MODULES = ("andrea-ssh.mjs", "captain.mjs", "captain-state.py", "dispatch.mjs",
           "model-router.mjs", "register.mjs", "runner-quota.py", "tools.mjs")
OVERLAY = Path(__file__).resolve().parent / "segment1"
INVENTORY = "SHA256SUMS"
SAFE_NAME = re.compile(r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*")


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def verify_inventory(root, expected):
    inventory = root / INVENTORY
    if not inventory.is_file() or inventory.is_symlink():
        raise ValueError("Missing release inventory")
    payload = inventory.read_bytes()
    if digest(payload) != expected:
        raise ValueError("Release inventory does not match its pin")
    entries = {}
    for line in payload.decode("utf-8").splitlines():
        checksum, separator, name = line.partition("  ")
        path = PurePosixPath(name)
        if separator != "  " or not re.fullmatch(r"[0-9a-f]{64}", checksum) or not SAFE_NAME.fullmatch(name):
            raise ValueError("Invalid release inventory")
        if "." in name.split("/") or ".." in path.parts or name == INVENTORY or name in entries:
            raise ValueError("Unexpected path in release inventory")
        entries[name] = checksum
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("Unexpected file type in release")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != {*entries, INVENTORY}:
        raise ValueError("Release files do not match the inventory")
    for name, checksum in entries.items():
        if digest((root / name).read_bytes()) != checksum:
            raise ValueError("Release file does not match the inventory")


def replace_once(text, original, updated):
    if text.count(original) != 1:
        raise ValueError("Round 13 source did not match the expected edit")
    return text.replace(original, updated, 1)


def write_source(path, text):
    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(mode | stat.S_IWUSR)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(mode)


def wire_dispatch(root):
    conductor = root / RUNTIME / "first-officer-conductor.mjs"
    text = conductor.read_text(encoding="utf-8")
    text = replace_once(text, "import incumbent from '../../first-officer-conductor.mjs';", """\
import incumbent, { statusTopic } from '../../first-officer-conductor.mjs';
import { controlIntent, routeFor } from '../../intent.mjs';
import { redacted } from '../../trace.mjs';
import { createAndreaReader, readPrivateConfig } from './andrea.mjs';
import { registerSegmentOne } from './segment1/register.mjs';""")
    text = replace_once(text, "import { incomingOwner } from './owner.mjs';",
                        "import { incomingOwner, telegramOwner } from './owner.mjs';")
    text = replace_once(text, "    registerMemory(api);", """\
    const memoryCommands = registerMemory(api);
    registerSegmentOne(api, memoryCommands, {
      statusTopic, controlIntent, routeFor, redacted, createAndreaReader, readPrivateConfig,
      incomingOwner, telegramOwner, parseRequest, gateStatus, quotaStatus, githubReply,
    });""")
    write_source(conductor, text)


def wire_memory(root):
    memory = root / RUNTIME / "memory.mjs"
    text = memory.read_text(encoding="utf-8")
    text = replace_once(text, "  const pending = new Map();", "  const pending = new Map();\n  const commands = new Map();")
    text = replace_once(text, "    api.registerCommand({", "    const command = {")
    text = replace_once(text, "\n    });\n  }\n", "\n    };\n    commands.set(kind, command);\n    api.registerCommand(command);\n  }\n")
    ending = "\n  } });\n}\n"
    text = replace_once(text, ending, "\n  } });\n  return commands;\n}\n")
    write_source(memory, text)


def wire_captain(root):
    conductor = root / RUNTIME / "first-officer-conductor.mjs"
    text = conductor.read_text(encoding="utf-8")
    text = replace_once(text, "import { githubReply } from './github.mjs';",
                        "import { get as getGithub, githubReply } from './github.mjs';")
    text = replace_once(text, "parseRequest, gateStatus, quotaStatus, githubReply,",
                        "parseRequest, gateStatus, quotaStatus, githubReply, getGithub,")
    write_source(conductor, text)

    gate = root / "status-replies.mjs"
    text = gate.read_text(encoding="utf-8")
    # The publisher uses sparse counts. A current, explicitly idle lock
    # and a missing running count support the same reading as zero.
    text = replace_once(text, "snapshot.gate_counts?.running === 0",
                        "snapshot.gate_counts && (snapshot.gate_counts.running ?? 0) === 0")
    write_source(gate, text)


def wire_model_router(root):
    conductor = root / "first-officer-conductor.mjs"
    text = conductor.read_text(encoding="utf-8")
    marker = "import { ownerFallbacks } from './round6/runtime/model-failover.mjs';"
    text = replace_once(text, marker, marker + "\nimport { fastProvider, routeOwnerModel } from './round6/runtime/segment1/model-router.mjs';")
    original = """\
      const route = routeFor(event.prompt, event.attachments);
      const configured = api.config?.agents?.defaults?.model?.fallbacks ?? [];
      const selection = {
        ...route,
        fallbacks: [...new Set([...configured.filter((id) => id.startsWith('litellm/gemini-')),
          ...route.fallbacks, ...configured])].filter((id) => id !== `litellm/${route.model}`),
      };
      traceRoute(event, ctx, selection);
      return { modelOverride: selection.model, providerOverride: 'litellm', fallbacksOverride: await ownerFallbacks(api.config, `litellm/${selection.model}`, selection.fallbacks) };"""
    updated = """\
      const selection = await routeOwnerModel(event, api.config, routeFor, ownerFallbacks);
      traceRoute(event, ctx, selection);
      return { modelOverride: selection.model, providerOverride: fastProvider, fallbacksOverride: selection.fallbacks };"""
    write_source(conductor, replace_once(text, original, updated))


def stage_release(baseline, destination):
    baseline = baseline.resolve(strict=True)
    destination = destination.resolve(strict=True)
    if not destination.is_dir() or destination.is_relative_to(baseline):
        raise ValueError("Choose a staging directory outside the release")
    pin = digest((baseline / INVENTORY).read_bytes())
    if pin not in SUPPORTED_BASES:
        raise ValueError("Choose the verified Round 13 or installed Segment 1 bundle")
    verify_inventory(baseline, pin)
    with tempfile.TemporaryDirectory(prefix=".fo-stage-", dir=destination) as temp:
        working = Path(temp) / "bundle"
        shutil.copytree(baseline, working, symlinks=True)
        verify_inventory(working, pin)
        install = working / RUNTIME / "segment1"
        install.mkdir(exist_ok=True)
        for name in MODULES:
            source = (OVERLAY / name).read_text(encoding="utf-8")
            target = install / name
            if target.exists():
                write_source(target, source)
            else:
                target.write_text(source, encoding="utf-8", newline="\n")
        if pin == BASELINE:
            wire_dispatch(working)
            wire_memory(working)
            wire_model_router(working)
        wire_captain(working)
        names = sorted(file.relative_to(working).as_posix() for file in working.rglob("*")
                       if file.is_file() and file.name != INVENTORY)
        manifest = "".join(f"{digest((working / name).read_bytes())}  {name}\n" for name in names).encode("utf-8")
        write_source(working / INVENTORY, manifest.decode("utf-8"))
        release = digest(manifest)
        verify_inventory(working, release)
        candidate = destination / f"first-officer-{release}"
        if candidate.exists() or candidate.is_symlink():
            raise ValueError("Candidate already exists in the staging directory")
        working.chmod(0o700)
        working.rename(candidate)
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="Verified Round 13 or Segment 1 release directory")
    parser.add_argument("staging_directory", type=Path, help="Private directory for the candidate")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(stage_release(args.baseline, args.staging_directory))
    except (OSError, ValueError) as error:
        # OS exceptions can disclose local paths or unexpected filenames.
        print(str(error) if type(error) is ValueError else "Could not read or stage the release", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
