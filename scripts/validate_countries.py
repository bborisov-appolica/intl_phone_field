#!/usr/bin/env python3
"""
Validate and compare country phone metadata from lib/countries.dart against
authoritative data from Google's libphonenumber (via the `phonenumbers` package).

Outputs three CSV files:
  1) current.csv        - data parsed from lib/countries.dart
  2) authoritative.csv  - data derived from phonenumbers + pycountry
  3) diff.csv           - rows where dial_code/min_length/max_length differ
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import phonenumbers
except Exception:
    phonenumbers = None  # type: ignore[assignment]

try:
    import pycountry
except Exception:
    pycountry = None  # type: ignore[assignment]


@dataclass(frozen=True)
class CountryRow:
    code: str
    name: str
    dial_code: str
    min_length: int
    max_length: int


COUNTRY_ENTRY_RE = re.compile(
    r"""
    Country\(
        \s*name:\s*"(?P<name>[^"]+)"\s*,       # Country name
        (?:(?:.|\n)*?)                          # non-greedy up to code
        code:\s*"(?P<code>[A-Z]{2})"\s*,        # ISO alpha-2 code
        (?:(?:.|\n)*?)                          # non-greedy up to dialCode
        dialCode:\s*"(?P<dial>\d+)"\s*,         # Dial code digits
        (?:(?:.|\n)*?)                          # non-greedy up to min/max
        minLength:\s*(?P<min>\d+)\s*,\s*
        maxLength:\s*(?P<max>\d+)\s*,?\s*
    \)""",
    re.VERBOSE | re.DOTALL,
)


def parse_countries_dart(dart_path: Path) -> List[CountryRow]:
    text = dart_path.read_text(encoding="utf-8")
    rows: List[CountryRow] = []
    for m in COUNTRY_ENTRY_RE.finditer(text):
        name = m.group("name").strip()
        code = m.group("code").strip()
        dial = m.group("dial").strip()
        min_len = int(m.group("min"))
        max_len = int(m.group("max"))
        rows.append(
            CountryRow(
                code=code,
                name=name,
                dial_code=dial,
                min_length=min_len,
                max_length=max_len,
            )
        )
    if not rows:
        raise RuntimeError("No Country(...) entries parsed from countries.dart")
    return rows


def write_csv(rows: Iterable[CountryRow], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["code", "name", "dial_code", "min_length", "max_length"],
        )
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "code": r.code,
                    "name": r.name,
                    "dial_code": r.dial_code,
                    "min_length": r.min_length,
                    "max_length": r.max_length,
                }
            )


def get_authoritative_row_for_region(code: str) -> Optional[Tuple[str, int, int]]:
    """
    Returns (dial_code_str, min_length, max_length) for a region code using phonenumbers.
    Falls back from general_desc.possible_length to union of fixed_line/mobile when needed.
    """
    if phonenumbers is None:
        raise RuntimeError(
            "Missing dependencies. Install with: pip install phonenumbers pycountry"
        )
    # Resolve PhoneMetadata class dynamically for compatibility across versions
    PhoneMetadataClass = getattr(phonenumbers, "PhoneMetadata", None)
    if PhoneMetadataClass is None:
        try:
            from phonenumbers.phonemetadata_pb2 import PhoneMetadata as _PBPhoneMetadata  # type: ignore
            PhoneMetadataClass = _PBPhoneMetadata
        except Exception:
            try:
                from phonenumbers.phonemetadata import PhoneMetadata as _ModPhoneMetadata  # type: ignore
                PhoneMetadataClass = _ModPhoneMetadata
            except Exception:
                PhoneMetadataClass = None
    try:
        meta = PhoneMetadataClass.metadata_for_region(code, None) if PhoneMetadataClass else None  # type: ignore[attr-defined]
    except Exception:
        meta = None

    if not meta:
        return None

    dial_code = phonenumbers.country_code_for_region(code)
    if not dial_code or dial_code <= 0:
        return None

    # Primary source: general_desc.possible_length
    lengths: List[int] = list(getattr(meta.general_desc, "possible_length", []) or [])
    # Clean and filter sentinel -1 if present
    lengths = [n for n in lengths if isinstance(n, int) and n > 0]

    # Fallback: union of mobile/fixed_line possible lengths if general is empty
    if not lengths:
        fallback_sets: List[int] = []
        for kind in ("mobile", "fixed_line"):
            desc = getattr(meta, kind, None)
            if desc is None:
                continue
            pl = getattr(desc, "possible_length", None) or []
            fallback_sets.extend([n for n in pl if isinstance(n, int) and n > 0])
        lengths = sorted(set(fallback_sets))

    if not lengths:
        # As a last resort, try global generalDesc pattern length heuristic (rare)
        return str(dial_code), 0, 0

    return str(dial_code), min(lengths), max(lengths)


def resolve_country_name(code: str, fallback_name: str) -> str:
    """
    Resolves a canonical English name using pycountry when possible.
    """
    if pycountry is None:
        return fallback_name
    try:
        country = pycountry.countries.get(alpha_2=code)
        if country is None:
            # pycountry may not include some territories (e.g., XK)
            return fallback_name
        # Prefer common_name or official_name if available
        for attr in ("common_name", "official_name", "name"):
            if hasattr(country, attr):
                value = getattr(country, attr)
                if isinstance(value, str) and value.strip():
                    return value
        return fallback_name
    except Exception:
        return fallback_name


def build_authoritative_rows(current_rows: List[CountryRow]) -> List[CountryRow]:
    result: List[CountryRow] = []
    seen: set[str] = set()
    for r in current_rows:
        if r.code in seen:
            continue
        seen.add(r.code)
        lookup = get_authoritative_row_for_region(r.code)
        if lookup is None:
            # Keep entry but mark unknowns so they appear in diff
            result.append(
                CountryRow(
                    code=r.code,
                    name=resolve_country_name(r.code, r.name),
                    dial_code="",
                    min_length=0,
                    max_length=0,
                )
            )
            continue
        dial_code, min_len, max_len = lookup
        result.append(
            CountryRow(
                code=r.code,
                name=resolve_country_name(r.code, r.name),
                dial_code=dial_code,
                min_length=min_len,
                max_length=max_len,
            )
        )
    return result


def index_by_code(rows: Iterable[CountryRow]) -> Dict[str, CountryRow]:
    return {r.code: r for r in rows}


def build_diff_rows(
    current_rows: List[CountryRow], authoritative_rows: List[CountryRow]
) -> List[Dict[str, str]]:
    current_by_code = index_by_code(current_rows)
    auth_by_code = index_by_code(authoritative_rows)

    diffs: List[Dict[str, str]] = []
    for code, cur in current_by_code.items():
        auth = auth_by_code.get(code)
        if auth is None:
            diffs.append(
                {
                    "code": code,
                    "current_name": cur.name,
                    "authoritative_name": "",
                    "current_dial_code": cur.dial_code,
                    "authoritative_dial_code": "",
                    "current_min_length": str(cur.min_length),
                    "authoritative_min_length": "",
                    "current_max_length": str(cur.max_length),
                    "authoritative_max_length": "",
                    "note": "Missing in authoritative",
                }
            )
            continue

        has_diff = (
            cur.dial_code != auth.dial_code
            or cur.min_length != auth.min_length
            or cur.max_length != auth.max_length
        )
        if has_diff:
            diffs.append(
                {
                    "code": code,
                    "current_name": cur.name,
                    "authoritative_name": auth.name,
                    "current_dial_code": cur.dial_code,
                    "authoritative_dial_code": auth.dial_code,
                    "current_min_length": str(cur.min_length),
                    "authoritative_min_length": str(auth.min_length),
                    "current_max_length": str(cur.max_length),
                    "authoritative_max_length": str(auth.max_length),
                    "note": "",
                }
            )
    return diffs


def write_diff_csv(diff_rows: List[Dict[str, str]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "code",
        "current_name",
        "authoritative_name",
        "current_dial_code",
        "authoritative_dial_code",
        "current_min_length",
        "authoritative_min_length",
        "current_max_length",
        "authoritative_max_length",
        "note",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in diff_rows:
            w.writerow(row)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate lib/countries.dart against libphonenumber."
    )
    parser.add_argument(
        "--dart-file",
        default=str(Path(__file__).resolve().parents[1] / "lib" / "countries.dart"),
        help="Path to lib/countries.dart",
    )
    parser.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parents[1] / "reports"),
        help="Directory where CSVs will be written",
    )
    args = parser.parse_args(argv)

    dart_path = Path(args.dart_file).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    current_csv = out_dir / "current.csv"
    authoritative_csv = out_dir / "authoritative.csv"
    diff_csv = out_dir / "diff.csv"

    current_rows = parse_countries_dart(dart_path)
    write_csv(current_rows, current_csv)

    authoritative_rows = build_authoritative_rows(current_rows)
    write_csv(authoritative_rows, authoritative_csv)

    diffs = build_diff_rows(current_rows, authoritative_rows)
    write_diff_csv(diffs, diff_csv)

    print(f"Wrote: {current_csv}")
    print(f"Wrote: {authoritative_csv}")
    print(f"Wrote: {diff_csv}")
    print(f"Total countries: {len(current_rows)} | Differences: {len(diffs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


