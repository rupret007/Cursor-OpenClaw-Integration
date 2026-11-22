import asyncio
import os
import sqlite3
import time
from datetime import timedelta

from hatchet_sdk import Hatchet, TTLBasedIdempotencyConfig
from pydantic import BaseModel


class Lane(BaseModel):
    lane: str
    head: str
    pull_request: int

hatchet = Hatchet()


@hatchet.task(
    name="queue-pilot", input_validator=Lane, retries=2,
    idempotency=TTLBasedIdempotencyConfig(
        key_expression="additional_metadata.idempotencyKey", ttl=timedelta(minutes=5),
    ),
)
async def execute(item, ctx):
    started = int(time.time() * 1000)
    if item.lane == "slow":
        await asyncio.sleep(1.5)
    # The INSERT and its uniqueness constraint commit together.
    with sqlite3.connect(os.environ["PILOT_EFFECTS"]) as db:
        db.execute("INSERT OR IGNORE INTO effects VALUES (?, ?, ?, ?, ?)",
                   (item.lane, item.head, item.pull_request, started, int(time.time() * 1000)))
    return {"done": True}


if __name__ == "__main__":
    hatchet.worker("queue-pilot", slots=1, workflows=[execute]).start()
