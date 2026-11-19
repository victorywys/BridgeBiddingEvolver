"""Test-only process protocol fixture; not a bridge baseline."""

import json
import os
import sys

print(json.dumps({"ok": True, "runtime": {"fixture": True}}), flush=True)
for line in sys.stdin:
    observation = json.loads(line)
    print(json.dumps({"ok": True, "call": "P", "model_family": "pidgin",
                      "seen_fields": sorted(observation),
                      "secret_environment_present": "BRIDGE_TEST_SECRET" in os.environ}), flush=True)
