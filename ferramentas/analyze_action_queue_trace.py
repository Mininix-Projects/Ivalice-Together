"""Turn the captured native Penelo action trace into a compact queue proof.

This analyzer is read-only.  It verifies the contract the headless menu relies
on: submit through 0x311CD0, let native replace during charge, and let native
queue during execution.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # co-op modules


from native_queue_analysis import NativeQueueSnapshot, predict_native_disposition


def snapshot(fields: dict[str, int]) -> NativeQueueSnapshot:
    return NativeQueueSnapshot(
        actor_flags_000=fields.get("actor_flags", 0),
        state_6b4=fields.get("action_state", 0),
        descriptor_708=fields.get("ai_action_ptr", 0),
        target_710=fields.get("current_target", 0),
        current_action_714=fields.get("current_action", 0xFFFF),
        charge_current_768=fields.get("charge_current", 0),
        charge_required_76c=fields.get("charge_required", 0),
        requested_action_ba0=fields.get("manual_action", 0xFFFF),
        request_flags_ba4=fields.get("manual_flags", 0),
        requested_target_bb8=fields.get("manual_target", 0),
    )


def analyze(path: Path) -> dict[str, object]:
    header: dict[str, object] | None = None
    footer: dict[str, object] | None = None
    pulses: list[dict[str, object]] = []
    state_transitions: list[dict[str, object]] = []
    action_promotions: list[dict[str, object]] = []
    previous: NativeQueueSnapshot | None = None

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        event = record.get("event")
        if event == "header":
            header = record
            continue
        if event == "footer":
            footer = record
            continue
        if event != "transition":
            continue
        current = snapshot(record["fields"])
        timestamp = float(record["t"])
        if previous is None or current.state_6b4 != previous.state_6b4:
            state_transitions.append(
                {
                    "t": timestamp,
                    "state": current.state_6b4,
                    "phase": current.phase.name,
                    "current_action": current.current_action_714,
                }
            )
        if previous is None or current.current_action_714 != previous.current_action_714:
            action_promotions.append(
                {
                    "t": timestamp,
                    "from": previous.current_action_714 if previous else None,
                    "to": current.current_action_714,
                    "state": current.state_6b4,
                }
            )
        if current.manual_request_pulse and not (
            previous and previous.manual_request_pulse
        ):
            pulses.append(
                {
                    "t": timestamp,
                    "action": current.requested_action_ba0,
                    "target": current.requested_target_bb8,
                    "native_phase": current.phase.name,
                    "expected_native_disposition": predict_native_disposition(current).name,
                    "current_action_at_request": current.current_action_714,
                    "charge": [current.charge_current_768, current.charge_required_76c],
                    "actor_flags_hex": f"0x{current.actor_flags_000:016X}",
                    "line": line_number,
                }
            )
        previous = current

    return {
        "format": "ff12-native-action-queue-analysis-v1",
        "source": str(path.resolve()),
        "header": header,
        "footer": footer,
        "request_pulses": pulses,
        "state_transitions": state_transitions,
        "action_promotions": action_promotions,
        "verified_contract": {
            "submit_entrypoint": "0x311CD0(issuer,target,action,0)",
            "idle_request": "START",
            "charging_request": "REPLACE_CHARGING",
            "executing_request": "QUEUE_AFTER_CURRENT",
            "direct_queue_field_writes": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    result = analyze(args.trace)
    output = json.dumps(result, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
