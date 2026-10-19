"""Frozen initial context policy for evaluator and evolution test fixtures."""

import json
import re
import sys


def main():
    request = json.load(sys.stdin)
    query = set(re.findall(r"[a-z0-9]+", request["query"].lower()))
    ranked = sorted(request["documents"],
                    key=lambda doc: len(query & set(re.findall(r"[a-z0-9]+", doc["text"].lower()))),
                    reverse=True)
    print(json.dumps({"selected_ids": [doc["id"] for doc in ranked[:3]],
                      "instructions": "Use the selected evidence when relevant. Verify commands before execution."}))


if __name__ == "__main__":
    main()
