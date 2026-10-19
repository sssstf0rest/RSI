"""Example improvement: ID weighting, stopwords and conservative selection."""

import json
import re
import sys


STOP = set("a an the for to of on in is are how do i my it and with when from use what".split())


def tokens(text):
    return set(re.findall(r"[a-z0-9]+", text.lower())) - STOP


def main():
    request = json.load(sys.stdin)
    query = tokens(request["query"])
    def score(doc):
        words = tokens(doc["text"])
        return 3 * len(query & tokens(doc["id"])) + len(query & words) / max(len(words), 1) ** 0.5
    ranked = sorted(request["documents"], key=score, reverse=True)
    selected = [ranked[0]["id"]] if ranked and score(ranked[0]) > 0 else []
    procedures = []
    for item in request.get("experience", []):
        if query & tokens(str(item.get("applicability", ""))) and item.get("reusable_procedure"):
            procedures.append(str(item["reusable_procedure"])[:1000])
    instructions = "Verify the selected evidence before acting."
    if procedures:
        instructions += " Relevant prior procedures: " + " ".join(procedures[:3])
    print(json.dumps({"selected_ids": selected, "instructions": instructions[:8192]}))


if __name__ == "__main__":
    main()
