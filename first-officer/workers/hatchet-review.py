"""Run only the reviewed, model-free Captain CI task on Hatchet."""
import json
import os
from pathlib import Path
import subprocess
from datetime import timedelta

from hatchet_sdk import Hatchet, TTLBasedIdempotencyConfig
from pydantic import BaseModel


class Review(BaseModel):
    lane: str
    head: str
    pull_request: int


hatchet = Hatchet()


@hatchet.task(
    name="fo-captain-review",
    input_validator=Review,
    execution_timeout=timedelta(seconds=25),
    idempotency=TTLBasedIdempotencyConfig(
        key_expression="additional_metadata.idempotencyKey", ttl=timedelta(hours=24),
    ),
)
def verify(review, _context):
    script = Path(os.environ["FO_CAPTAIN_REVIEW_SCRIPT"])
    if not script.is_absolute() or script.name != "review-worker.mjs":
        raise ValueError("Expected the pinned Captain review script")
    result = subprocess.run(
        ["node", str(script), review.lane, review.head, str(review.pull_request)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=22,
    )
    if result.returncode:
        raise ValueError("Captain could not verify CI for the exact queued head")
    return json.loads(result.stdout)


if __name__ == "__main__":
    hatchet.worker("fo-captain-review", slots=1, workflows=[verify]).start()
