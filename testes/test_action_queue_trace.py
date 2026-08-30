from __future__ import annotations

import unittest
from pathlib import Path

from analyze_action_queue_trace import analyze


HERE = Path(__file__).resolve().parent.parent / "artefatos"


class NativeActionQueueTraceTests(unittest.TestCase):
    def test_captured_trace_proves_start_replace_and_queue(self):
        result = analyze(HERE / "native_action_queue_live.jsonl")
        pulses = result["request_pulses"]
        self.assertEqual([pulse["action"] for pulse in pulses], [0x96, 0x52, 0x52])
        self.assertEqual(
            [pulse["expected_native_disposition"] for pulse in pulses],
            ["START", "REPLACE_CHARGING", "QUEUE_AFTER_CURRENT"],
        )
        self.assertFalse(result["verified_contract"]["direct_queue_field_writes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
