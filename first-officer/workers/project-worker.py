
"""Produce one local project candidate using the approved literal edit and local model."""

import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import subprocess
import sys
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


HERE = Path(__file__).resolve()
STATE = HERE.parents[2] / "captain-state.py"
if not STATE.is_file():
    STATE = HERE.parents[1] / "segment1/captain-state.py"
primitives = runpy.run_path(str(STATE))
read_private = primitives["read_private"]
private_bytes = primitives["private_bytes"]
private_directory = primitives["private_directory"]
save = primitives["save"]
digest = primitives["digest"]
queue = runpy.run_path(str(HERE.parents[1] / "lanes/queue.py"))
HOME = Path.home()
SHA = re.compile(r"[a-f0-9]{40}")
MODEL = "qwen-local-fast"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args):
        raise ValueError("The local model route redirected")


def complete(prompt):
    root = HOME / ".local/state/openclaw-nuc/litellm"
    raw = private_bytes(root / "litellm.yaml", 16384)
    try:
        config = json.loads(raw)
    except json.JSONDecodeError:
        import yaml

        config = yaml.safe_load(raw)
    entries = config["model_list"]
    policy = read_private(root / "lane-policy.json", 8192)
    if (len(entries) != 1 or entries[0]["model_name"] != MODEL or
            policy["lanes"]["gateway"]["models"] != [MODEL] or
            config["router_settings"]["fallbacks"] or config["router_settings"]["num_retries"] != 0):
        raise ValueError("The no-cost worker route is unavailable")
    params = entries[0]["litellm_params"]
    if params["model"] != "openai/" + MODEL or params["api_base"] != "http://127.0.0.1:41501/v1":
        raise ValueError("The coding model must be the pinned local deployment")
    key = private_bytes(root / "gateway-lane.key", 1024).decode().strip()
    body = {"model": MODEL, "temperature": 0, "max_tokens": 120, "messages": [
        {"role": "user", "content": prompt + "\nReply with only a JSON object with one string field named replacement."},
    ]}
    request = Request("http://127.0.0.1:41502/v1/chat/completions",
                      data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=20) as result:
        raw = result.read(16385)
    if len(raw) > 16384:
        raise ValueError("The model response exceeded its limit")
    message = json.loads(raw)["choices"][0]["message"]["content"]
    payload = json.loads(message)
    if not isinstance(payload, dict) or set(payload) != {"replacement"}:
        raise ValueError("The local model returned an invalid edit")
    return payload["replacement"]


def git(worktree, *args, data=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(HOME), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "OpenClaw Captain", "GIT_COMMITTER_NAME": "OpenClaw Captain",
           "GIT_AUTHOR_EMAIL": "captain@example.invalid", "GIT_COMMITTER_EMAIL": "captain@example.invalid"}
    return subprocess.run(
        ["/usr/bin/nice", "-n", "10", "/usr/bin/git", "-c", "core.hooksPath=/dev/null",
         "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false", "-C", str(worktree), *args],
        env=env, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8, check=True,
    ).stdout


def standalone_plan(root, request, identity):
    if not private_directory(root / "reviews"):
        raise ValueError("The project authority directory is unavailable")
    lease = read_private(root / "lease.json", 4096)
    grant = read_private(root / "project-grant.json", 4096)
    state = read_private(root / "reviews/state.json", 256 * 1024)
    if (lease.get("node") != "nucbox" or lease.get("active") is not True or lease.get("epoch") != request["epoch"] or
            grant.get("worker") != "local-qwen" or type(grant.get("expires")) is not int or
            not int(time.time()) < grant["expires"] <= int(time.time()) + 3600 or
            any(type(grant.get(key)) is not type(value) or grant[key] != value for key, value in identity.items()) or
            not any(job.get("request") == request and job.get("state") in ("reserved", "running", "unresolved")
                    and job.get("worker") == "local-qwen" and job.get("branch") == grant.get("branch")
                    and job.get("planSha256") == grant.get("planSha256") for job in state.get("projectJobs", []))):
        raise ValueError("The exact coding scope is unavailable")
    return grant.get("planSha256")


def broker_plan(request, profile):
    if not profile.is_absolute():
        raise ValueError("The worker needs an absolute broker profile path")
    launcher = runpy.run_path(str(HERE.parents[1] / "standby_launch.py"))
    settings = launcher["profile_from"](profile)
    client = launcher["Client"](settings["hostname"], settings["port"], cert=settings["cert"],
                                key=settings["key"], ca=settings["ca"])
    return client.project_worker_plan(request["epoch"], request)


def load_scope(root, request):
    if not private_directory(root):
        raise ValueError("The project authority directory is unavailable")
    identity = {key: value for key, value in request.items() if key != "lane"}
    profile = os.environ.get("FO_PROJECT_BROKER_PROFILE")
    approved_plan = broker_plan(request, Path(profile)) if profile is not None else standalone_plan(root, request, identity)
    payload = private_bytes(root / "project-worker.json", 8192)
    if digest(payload) != approved_plan:
        raise ValueError("The approved coding plan changed")
    plan = json.loads(payload)
    fields = {"request", "checkout", "file", "before", "prefix", "suffix", "prompt"}
    if not isinstance(plan, dict) or set(plan) != fields or plan["request"] != identity:
        raise ValueError("The operator task does not match this coding scope")
    for key, limit in {"checkout": 512, "file": 160, "before": 1024, "prefix": 512, "suffix": 512, "prompt": 1600}.items():
        if not isinstance(plan[key], str) or len(plan[key]) > limit or "\0" in plan[key]:
            raise ValueError("The operator task exceeds its scope")
    if not plan["before"] or not plan["prompt"]:
        raise ValueError("The operator task needs an exact preimage and instruction")
    return plan


def prepare(plan, request):
    checkout, approved = Path(plan["checkout"]), PurePosixPath(plan["file"])
    if (not checkout.is_absolute() or checkout.resolve(strict=True) != checkout or
            not checkout.is_dir() or checkout.stat().st_uid != os.getuid() or
            checkout.stat().st_mode & 0o022 or approved.is_absolute() or not approved.parts or
            any(part.startswith(".") or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part) for part in approved.parts) or
            approved.suffix not in (".mjs", ".js", ".ts", ".py", ".md", ".txt")):
        raise ValueError("The approved checkout or file is invalid")
    remote = git(checkout, "config", "--local", "--get", "remote.origin.url").decode().strip().lower()
    if remote not in {f"https://github.com/{request['repository']}", f"https://github.com/{request['repository']}.git"}:
        raise ValueError("The approved checkout belongs to a different repository")
    file = str(approved)
    record = git(checkout, "ls-tree", request["head"], "--", file).decode().strip()
    mode, kind, remainder = record.split(" ", 2)
    oid, name = remainder.split("\t", 1)
    if mode != "100644" or kind != "blob" or not SHA.fullmatch(oid) or name != file:
        raise ValueError("The approved file is not a regular tracked blob")
    if int(git(checkout, "cat-file", "-s", oid)) > 128 * 1024:
        raise ValueError("The approved file is too large")
    source = git(checkout, "cat-file", "blob", oid).decode()
    if source.count(plan["before"]) != 1:
        raise ValueError("The exact approved preimage is unavailable")
    return checkout, file, source


def finish_candidate(slot, request, plan, result):
    if (not isinstance(result, dict) or set(result) != {"lane", "head", "commit"}
            or result["lane"] != request["lane"] or result["head"] != request["head"]
            or not isinstance(result["commit"], str) or not SHA.fullmatch(result["commit"])):
        raise ValueError("The existing worker receipt has a different identity")
    worktree = slot / "candidate"
    base, commit = result["head"], result["commit"]
    common_dir = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    if (not worktree.is_dir() or worktree.is_symlink()
            or git(worktree, *common_dir) != git(Path(plan["checkout"]), *common_dir)
            or git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip() != b"HEAD"
            or git(worktree, "rev-list", "--parents", "-n", "1", commit).split() != [commit.encode(), base.encode()]):
        raise ValueError("The checkpoint does not belong to the isolated candidate")
    head = git(worktree, "rev-parse", "HEAD").decode().strip()
    if head == base:
        try:
            git(worktree, "update-ref", "HEAD", commit, base, "--no-deref")
        except subprocess.CalledProcessError:
            if git(worktree, "rev-parse", "HEAD").decode().strip() != commit:
                raise
    elif head != commit:
        raise ValueError("The isolated candidate advanced beyond its checkpoint")
    return result


def run(root, request, respond=complete):
    queue["canonical"](request)
    plan = load_scope(root, request)
    home = root / "project-runs"
    home.mkdir(mode=0o700, exist_ok=True)
    if not private_directory(home):
        raise ValueError("The worker run directory is not private")
    slot = home / request["lane"]
    receipt = slot / "candidate.json"
    if slot.exists():
        if not private_directory(slot):
            raise ValueError("The worker slot is not private")
        try:
            previous = read_private(receipt, 4096)
        except FileNotFoundError:
            raise ValueError("A prior worker attempt is unresolved") from None
        return finish_candidate(slot, request, plan, previous)
    checkout, file, source = prepare(plan, request)
    slot.mkdir(mode=0o700)
    worktree = slot / "candidate"
    base = request["head"]
    git(checkout, "worktree", "add", "--detach", "--no-checkout", str(worktree), base)
    git(worktree, "read-tree", base)
    if load_scope(root, request) != plan:
        raise ValueError("The coding scope changed before the model call")
    text = respond(plan["prompt"])
    if not isinstance(text, str) or not 1 <= len(text) <= 300 or not text.isprintable():
        raise ValueError("The local model returned an invalid literal")
    replacement = plan["prefix"] + json.dumps(text) + plan["suffix"]
    edited = source.replace(plan["before"], replacement)
    if edited == source or load_scope(root, request) != plan:
        raise ValueError("The edit or coding scope changed before committing")
    blob = git(worktree, "hash-object", "-w", "--stdin", "--no-filters", data=edited.encode()).decode().strip()
    git(worktree, "update-index", "--add", "--cacheinfo", "100644", blob, file)
    tree = git(worktree, "write-tree").decode().strip()
    commit = git(worktree, "commit-tree", tree, "-p", base, "-m",
                 f"Captain candidate for issue #{request['issue']}").decode().strip()
    if not SHA.fullmatch(commit):
        raise ValueError("The candidate commit could not be verified")
    result = {"lane": request["lane"], "head": base, "commit": commit}
    # Persist the exact commit before advancing HEAD so a restart can settle it once.
    save(receipt, result)
    if load_scope(root, request) != plan:
        raise ValueError("The coding scope changed before finalizing the candidate")
    return finish_candidate(slot, request, plan, result)


def main():
    os.umask(0o077)
    try:
        payload = sys.stdin.buffer.read(4097)
        if len(payload) > 4096:
            raise ValueError("The project request exceeded its limit")
        print(json.dumps(run(HOME / ".local/state/openclaw-captain", json.loads(payload))))
    except Exception:
        print("The scoped coding worker could not produce a candidate.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
