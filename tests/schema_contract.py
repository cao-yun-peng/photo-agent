"""Canonicalize drift operations without hiding column/index/table differences."""

import re


def normalize_drift(rendered: str) -> str:
    body = "\n".join(
        line for line in rendered.splitlines() if not line.lstrip().startswith("#")
    )
    operations = re.split(r"(?m)(?=^    op\.)", body)
    return (
        "\n\n".join(sorted(part.strip() for part in operations if part.strip())) + "\n"
    )
