"""Check the results of a real n8n CLI execution of workflow.json."""

import json
import re
import sys
from pathlib import Path


def execution_from_log(path):
    output = Path(path).read_text()
    # n8n can print startup messages alongside its JSON result.
    for match in re.finditer(r"^\{", output, re.MULTILINE):
        try:
            result, _ = json.JSONDecoder().raw_decode(output[match.start():])
        except json.JSONDecodeError:
            continue
        if "resultData" in result.get("data", {}):
            return result["data"]["resultData"]
    raise ValueError("No n8n execution result found")


def check(result):
    if result.get("error"):
        raise ValueError(f"Workflow failed: {result['error']}")
    expected = {
        "Clean sales": {
            "demo-01": ([], True),
            "demo-02": ([], False),
            "demo-10": ([], True),
        },
        "Exceptions for review": {
            "demo-03": (["amount_mismatch"], False),
            "demo-04": (["missing_destination_sale"], False),
            "demo-05": (["unexpected_destination_sale"], False),
            "demo-06": (["duplicate_source_id"], False),
            "demo-07": (["duplicate_destination_id"], False),
            "demo-08": (["consent_mismatch"], False),
            "demo-09": (["currency_mismatch"], False),
        },
    }
    for branch, cases in expected.items():
        runs = result["runData"][branch]
        if len(runs) != 1:
            raise ValueError(f"{branch} executed {len(runs)} times")
        rows = [item["json"] for item in runs[0]["data"]["main"][0]]
        actual = {
            row["sale_id"]: (row["issues"], row["follow_up_allowed"])
            for row in rows
        }
        if len(rows) != len(cases) or actual != cases:
            raise ValueError(f"{branch} returned unexpected results: {actual!r}")
        for row in rows:
            if row["needs_review"] is not bool(row["issues"]):
                raise ValueError(f"Inconsistent review flag for {row['sale_id']}")
    print("Verified 3 clean sales and 7 exceptions. Both opt-out cases block follow-up.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 n8n/verify.py EXECUTION_LOG")
    check(execution_from_log(sys.argv[1]))
