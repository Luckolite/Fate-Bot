"""Small provider-neutral demonstration of the Organism facade."""

from __future__ import annotations

import json
import os
from pathlib import Path

from organism import Cues, Observation, Organism


def main() -> None:
    # Durable actor memory and authenticated learning use separate secrets.
    identity_key = os.environ.get("ORGANISM_IDENTITY_KEY")
    learning_key = os.environ.get("ORGANISM_LEARNING_KEY")
    owner_key = os.environ.get("ORGANISM_OWNER_KEY")
    engine = Organism(
        Path(__file__).with_name("runtime") / "example-state.json",
        identity_key=identity_key,
        learning_key=learning_key,
        owner_key=owner_key,
    )
    packet = engine.feel(
        Observation(
            event_id="demo-observation-1",
            scope="demo-workspace",
            actor_ref="platform-user-42",
            cues=Cues(
                safety=0.35,
                threat=0.2,
                boundary_pressure=0.65,
                uncertainty=0.55,
                controllability=0.75,
            ),
        )
    )
    print(json.dumps(packet.prompt_payload(), indent=2))
    print(
        json.dumps(packet.responses_input("Please respond to this request."), indent=2)
    )


if __name__ == "__main__":
    main()
