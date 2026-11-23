
"""Run the scoped Captain coding worker from a dedicated Hatchet workflow."""

from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess

from hatchet_sdk import Hatchet, TTLBasedIdempotencyConfig
from pydantic import BaseModel, ConfigDict


class CodingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lane: str
    repository: str
    head: str
    issue: int
    epoch: int


hatchet = Hatchet()


@hatchet.task(
    name="fo-captain-project", input_validator=CodingRequest, execution_timeout=timedelta(seconds=110),
    idempotency=TTLBasedIdempotencyConfig(
        key_expression="additional_metadata.idempotencyKey", ttl=timedelta(hours=24),
    ),
)
def work(request, _context):
    script = Path(__file__).resolve().with_name("project-worker.py")
    interpreter = Path(os.environ["FO_PROJECT_PYTHON"])
    if not interpreter.is_absolute() or not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError("The pinned local coding interpreter is unavailable")
    result = subprocess.run(
        ["/usr/bin/nice", "-n", "10", str(interpreter), "-I", "-B", str(script)],
        input=request.model_dump_json().encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=100,
    )
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("The scoped project worker returned no candidate")
    return json.loads(result.stdout)


if __name__ == "__main__":
    hatchet.worker("fo-captain-project", slots=1, workflows=[work]).start()
