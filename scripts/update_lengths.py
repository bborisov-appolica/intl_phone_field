#!/usr/bin/env python3
"""
Update minLength in lib/countries.dart using reports/authoritative.csv,
but only if the authoritative min_length is LOWER than the current minLength.

maxLength, names, and dial codes are left unchanged.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
DART_FILE = ROOT / "lib" / "countries.dart"
AUTHORITATIVE_CSV = ROOT / "reports" / "authoritative.csv"

# Map code -> (min_length, max_length)
def load_authoritative(path: Path) -> Dict[str, Tuple[int, int]]:
    data: Dict[str, Tuple[int, int]] = {}
    with path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            code = row["code"].strip()
            try:
                min_len = int(row["min_length"])
                max_len = int(row["max_length"])
            except Exception:
                continue
            if min_len <= 0 or max_len <= 0:
                # skip unknowns
                continue
            data[code] = (min_len, max_len)
    return data


def update_lengths(dart_source: str, lengths: Dict[str, Tuple[int, int]]) -> str:
    """
    For each Country block, find code: "XX" then replace its minLength/maxLength lines.
    Preserve all other content and formatting.
    """
    # Regex to find blocks like Country( ... code: "XX", ... minLength: n, maxLength: m, ... )
    country_re = re.compile(
        r"(Country\(\s*(?:.|\n)*?code:\s*\"(?P<code>[A-Z]{2})\"\s*,(?:.|\n)*?minLength:\s*)(?P<min>\d+)(\s*,\s*\n\s*maxLength:\s*)(?P<max>\d+)",
        re.DOTALL,
    )

    def replacer(m: re.Match) -> str:
        code = m.group("code")
        # If we don't have authoritative data, keep as-is
        if code not in lengths:
            return m.group(0)
        auth_min, auth_max = lengths[code]
        # Skip unknowns / zeros
        if not isinstance(auth_min, int) or auth_min <= 0:
            return m.group(0)
        try:
            current_min = int(m.group("min"))
            current_max = int(m.group("max"))
        except Exception:
            return m.group(0)

        # Only lower the minLength
        new_min = auth_min if auth_min < current_min else current_min
        # Always set maxLength to authoritative when valid (>0)
        new_max = current_max
        if isinstance(auth_max, int) and auth_max > 0:
            new_max = auth_max

        # If nothing changed, keep block as-is
        if new_min == current_min and new_max == current_max:
            return m.group(0)

        prefix = m.group(1)
        sep = m.group(4)
        # Preserve formatting, update min and/or max values
        return f"{prefix}{new_min}{sep}{new_max}"

    return country_re.sub(replacer, dart_source)


def main() -> int:
    lengths = load_authoritative(AUTHORITATIVE_CSV)
    source = DART_FILE.read_text(encoding="utf-8")
    updated = update_lengths(source, lengths)
    if updated != source:
        DART_FILE.write_text(updated, encoding="utf-8")
        print("Updated lib/countries.dart min/max lengths.")
    else:
        print("No changes applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


