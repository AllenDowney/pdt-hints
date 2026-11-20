"""Build the HINTS records from NCI's codebooks and the Stata data files.

Usage (under the memory cap):
    python build/build_records.py

Reads build/slice.yaml (the cycles) and, for each cycle, the public codebook as text
(data/interim/<dir>/<codebook>: pdftotext -layout of the PDF in the cycle's zip) and the Stata
file. Every variable of each file is built. Writes metadata/releases/<release>/variables.jsonl,
metadata/derived/scale_signature.jsonl and build/report.md.

Where each field comes from (STANDARD.md §3):
- label (Variable Label), universe (Criteria description and Criteria to receive Question),
  codes and labels with their unweighted counts: the codebook (publisher)
- question number and text: rule hints_label_split, from the label ("A1. Have you ever ...")
- the valid range of a numeric item: rule hints_value_listing, where the codebook lists every
  value under one repeated label ("Current age, in years")
- missing types: rule hints_missing_types, from the negative codes' labels
- storage type, availability counts: the data file
- scale signatures: the shared library's rule (pdt.derived)

C3 (coverage), C4 (codes in the data the codebook doesn't list) and C5 (the codebook's counts
against the data) run here and go to build/report.md.
"""

import collections
import json
import re
from pathlib import Path

import pandas as pd
import pyreadstat
import yaml

try:
    from pdt.derived import scale_signatures
except ImportError:
    import os
    import sys
    sys.path.insert(0, str(Path(os.environ.get("PDT", Path(__file__).resolve().parents[2] / "PublicDataToolkit"))
                           / "lib"))
    from pdt.derived import scale_signatures

ROOT = Path(__file__).resolve().parents[1]
INTERIM = ROOT / "data" / "interim"
META = ROOT / "metadata"
DATASET = "hints"
RULE_VERSION = "0.1"
LISTING_MIN = 5          # a label repeated on this many codes is a listing of a numeric item's values

# ---------------------------------------------------------------- the codebook

ROW = re.compile(r"^\s*(?P<code>\S.*?)\s{2,}(?P<label>\S.*?)\s{2,}(?P<n>[\d,]+)\s+[\d.]+\s+[\d,]+\s+[\d,]+\s+[\d.]+\s*$")
NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def code_value(code):
    x = float(code)
    return int(x) if x.is_integer() else x


def parse_codebook(text):
    """{NAME: entry} for every variable entry ("Variable Name: ...") in a HINTS codebook."""
    lines, pages = [], []
    for page, chunk in enumerate(text.split("\f"), start=1):
        for line in chunk.split("\n"):
            lines.append(line)
            pages.append(page)          # the PDF page the line is on
    starts = [i for i, l in enumerate(lines) if l.startswith("Variable Name: ")]
    entries = {}
    for k, i in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        block = lines[i:end]
        e = {"name": lines[i].split(":", 1)[1].strip(), "page": pages[i], "rows": [], "label": "", "criteria": "", "description": ""}
        field = None
        for line in block[1:]:
            s = line.strip()
            m = re.match(r"(Variable Label|Variable Format|Criteria to receive Question|Criteria description):\s*(.*)", s)
            if m:
                field = {"Variable Label": "label", "Variable Format": "format",
                         "Criteria to receive Question": "criteria", "Criteria description": "description"}[m.group(1)]
                e[field] = m.group(2).strip()
                continue
            if s.startswith("Back to Table of Contents"):
                field = "table"
                continue
            if field in ("label", "criteria", "description") and s:
                e[field] += " " + s          # a field wrapped at the page width
                continue
            if field == "table":
                r = ROW.match(line)
                if r and r.group("code") not in ("Text String", "Number"):
                    e["rows"].append({"code": r.group("code").strip(), "label": r.group("label").strip(),
                                      "n": int(r.group("n").replace(",", ""))})
                elif r:
                    e["kind"] = r.group("code")      # "Text String" or "Number": no codes listed
                elif (e["rows"] and s and line.startswith(" " * 10) and not re.fullmatch(r"\d+", s)
                      and not re.search(r"Unweighted|Weighted|Percent|\(Estimated\)", s)):
                    e["rows"][-1]["label"] += ("" if s.startswith("_") else " ") + s   # a label wrapped onto the next line
        entries[e["name"].upper()] = e
    return entries


# ---------------------------------------------------------------- rules

# Rule hints_missing_types: HINTS's negative codes, typed by their labels. HINTS is
# self-administered (mail and web), so an item left blank is `skipped`.
MISSING = [
    (-1, r"^inapplicable", ["inap"]),
    (-2, r"^question answered", ["inap"]),
    (-4, r"^(unreadable|non-?conforming)", ["uncodeable"]),
    (-5, r"^multiple responses", ["uncodeable"]),
    (-6, r"^missing data \(filter", ["inap", "skipped"]),
    (-7, r"^missing data \(web", ["not_asked"]),
    (-9, r"^missing (data|value)", ["skipped"]),
]
RULE_MISSING = {"origin": "rule", "rule": "hints_missing_types", "rule_version": RULE_VERSION,
                "note": "HINTS's negative codes, typed by their labels: -1 Inapplicable (skipped by design) and -2 "
                        "'Question answered in error (Commission Error)' (answered though the filter excluded the "
                        "respondent; the answer is removed) are inap; -4 unreadable or nonconforming and -5 multiple "
                        "responses are uncodeable; -6 'Filter Missing' (the filter question was left blank, so it isn't "
                        "known whether the item applied) is inap or skipped; -7 'Web partial - Question Never Seen' "
                        "(a web respondent stopped before reaching it) is not_asked; -9 'Not Ascertained' is skipped: "
                        "HINTS is self-administered, so an item left blank was skipped by the respondent."}
RULE_SPLIT = {"origin": "rule", "rule": "hints_label_split", "rule_version": RULE_VERSION,
              "note": "The codebook's Variable Label is the item number and the question ('A1. Have you ever ...'); "
                      "the rule splits them at the first '. '."}
RULE_LISTING = {"origin": "rule", "rule": "hints_value_listing", "rule_version": RULE_VERSION,
                "note": f"The codebook lists every observed value of this numeric item under one label (repeated on "
                        f"{LISTING_MIN} or more codes); the rule keeps the range and drops the listing."}
QNUM = re.compile(r"^(?P<number>[A-Z]{0,2}\d+[a-z]?(?:\d+)?)\.\s+(?P<text>\S.*)$")


def missing_types(code, label):
    for c, pattern, types in MISSING:
        if code == c and re.search(pattern, label, re.I):
            return types
    return None


# ---------------------------------------------------------------- build

def build_record(release, year, entry, col, source, codebook_path, stata_labels=None):
    rec = {"dataset": DATASET, "release": release, "name": entry["name"]}
    label = " ".join(entry["label"].split())
    if label:
        rec["label"] = label
    m = QNUM.match(label)
    fields = {}
    if m:
        rec["questions"] = [{"number": m.group("number"), "text": m.group("text")}]
        fields["questions"] = RULE_SPLIT
    desc, crit = " ".join(entry["description"].split()), " ".join(entry["criteria"].split())
    if desc and desc != "N/A":
        rec["universe"] = desc + (f" ({crit})" if crit and crit != "N/A" else "")
    is_date = pd.api.types.is_datetime64_any_dtype(col)
    is_string = not is_date and not pd.api.types.is_numeric_dtype(col)
    rec["storage_type"] = ("date" if is_date else "string" if is_string else
                           "integer" if (col.dropna() % 1 == 0).all() else "float")

    # The data: counts by code.
    counts = collections.Counter()
    for k, v in col.value_counts(dropna=False).items():
        if pd.isna(k):
            counts["."] += int(v)
        elif is_date:
            counts["date"] += int(v)
        elif is_string:
            s = str(k).strip()
            counts[code_value(s) if NUMBER.match(s) else s] += int(v)
        else:
            counts[code_value(k)] += int(v)
    numeric = [r for r in entry["rows"] if NUMBER.match(r["code"])]
    positive_labels = collections.Counter(r["label"] for r in numeric if code_value(r["code"]) >= 0)
    listing = {lab for lab, n in positive_labels.items() if n >= LISTING_MIN}
    values, listed_codes, report = [], [], []
    for r in numeric:
        code = code_value(r["code"])
        if code >= 0 and r["label"] in listing:
            listed_codes.append(code)
            continue
        item = {"code": code, "label": r["label"], "kind": "valid", "n": r["n"]}
        if code < 0:
            types = missing_types(code, r["label"])
            if types:
                item["kind"], item["missing_types"] = "missing", types
            else:
                item["kind"] = "unresolved"
                item["origin"] = {**RULE_MISSING, "note": f"A negative code whose label the rule doesn't know: "
                                                          f"{r['label']!r}."}
                report.append(("unresolved", f"{release} `{entry['name']}` {code} {r['label']!r}"))
        values.append(item)
    if entry["rows"] and not numeric and stata_labels and not is_string:
        # The codebook prints character codes ("HM", "A"); the Stata file stores numbers, labeled.
        for code, lab in sorted(stata_labels.items()):
            code = code_value(code)
            item = {"code": code, "label": lab, "kind": "valid", "n": counts.get(code, 0)}
            types = missing_types(code, lab) if code < 0 else None
            if types:
                item["kind"], item["missing_types"] = "missing", types
            values.append(item)
        fields["values"] = {"origin": "data_file", "source": source,
                            "note": "The codebook prints these codes as characters ("
                                    + ", ".join(repr(r["code"]) for r in entry["rows"][:4])
                                    + "); the codes and labels are the Stata file's value labels, and n counts the data."}
        numeric = [{"code": str(v["code"]), "label": v["label"], "n": v["n"]} for v in values]
        report.append(("character", f"{release} `{entry['name']}`: codebook codes {[r['code'] for r in entry['rows']]}"
                                    f", Stata labels {dict(sorted(stata_labels.items()))}"))
    if values:
        rec["values"] = values
    if listed_codes:
        rec["valid_range"] = {"min": min(listed_codes), "max": max(listed_codes)}
        fields["valid_range"] = RULE_LISTING
        rec["notes"] = f"The codebook lists each value with the label {sorted(listing)[0]!r}."
    missing = {v["code"] for v in values if v["kind"] != "valid"}

    n = sum(c for code, c in counts.items() if code not in missing and code != "" and code != ".")
    rec["availability"] = [{"wave": year, "n": n}] if n else []
    fields["storage_type"] = {"origin": "rule", "rule": "storage_type_from_values", "rule_version": RULE_VERSION,
                              "source": source}
    fields["availability"] = {"origin": "data_file", "source": source,
                              "note": "n counts the respondents whose code isn't missing."}
    rec["source_location"] = {"source": source, "page": entry["page"], "anchor": entry["name"]}
    rec["provenance"] = {"record": {"origin": "publisher", "source": source}, "fields": fields}
    if missing:
        rec["provenance"]["codes"] = RULE_MISSING

    # C4: codes in the data that the codebook doesn't list (numeric items only).
    if not is_string and not is_date and (entry["rows"] or entry.get("kind") != "Number"):
        known = {v["code"] for v in values} | set(listed_codes)
        extra = sorted((c, k) for c, k in counts.items() if c != "." and c not in known)
        if extra and entry["rows"]:
            report.append(("undocumented", f"{release} `{entry['name']}`: {len(extra)} codes not in the codebook, "
                                           f"e.g. {extra[:5]}"))
    # C5: the codebook's counts against the data's.
    for r in numeric:
        got = counts.get(code_value(r["code"]), 0)
        if got != r["n"]:
            report.append(("counts", f"{release} `{entry['name']}` {r['code']} ({r['label']}): "
                                     f"codebook {r['n']}, data {got}"))
    return rec, report


def main():
    spec = yaml.safe_load((ROOT / "build" / "slice.yaml").read_text())
    report, coverage, all_records = [], [], []
    for release, rel in spec["releases"].items():
        cb_path = f"{rel['dir']}/{rel['codebook']}"
        entries = parse_codebook((INTERIM / cb_path).read_text())
        try:
            frame, meta = pyreadstat.read_dta(str(INTERIM / rel["dir"] / rel["data"]))
            encoding = "utf-8"
        except UnicodeDecodeError:          # an older file in Windows-1252 (0x92 is a curly apostrophe)
            frame, meta = pyreadstat.read_dta(str(INTERIM / rel["dir"] / rel["data"]), encoding="cp1252")
            encoding = "cp1252"
        cols = {c.upper(): c for c in frame.columns}
        coverage.append(f"- {release}: {len(frame):,} rows, {len(cols)} columns, {len(entries)} codebook entries, read as {encoding}; "
                        f"in the data only: {sorted(set(cols) - set(entries)) or 'none'}; "
                        f"in the codebook only: {sorted(set(entries) - set(cols)) or 'none'}")
        recs = []
        for name, entry in entries.items():
            if name not in cols:
                continue
            rec, rep = build_record(release, rel["year"], entry, frame[cols[name]], rel["source"], cb_path,
                                    meta.variable_value_labels.get(cols[name]))
            recs.append(rec)
            report += rep
        write_jsonl(META / "releases" / release / "variables.jsonl", recs)
        all_records += recs
        del frame
    write_jsonl(META / "derived" / "scale_signature.jsonl", scale_signatures(all_records, per_release=True))
    write_report(all_records, coverage, report)
    print(f"{len(all_records)} records; see build/report.md")


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_report(records, coverage, report):
    by = collections.defaultdict(list)
    for kind, line in report:
        by[kind].append(line)
    per = collections.Counter(r["release"] for r in records)
    lines = ["# Build report: hints", "", "Generated by `build/build_records.py`. Do not edit.", "",
             *[f"- {rel}: {n} records" for rel, n in sorted(per.items())],
             "", "## C3: coverage", "", *coverage, ""]
    for kind, title in [("character", "Codes from the Stata labels, where the codebook prints character codes"),
                        ("unresolved", "Negative codes whose labels the rule doesn't know"),
                        ("undocumented", "C4: codes in the data that the codebook doesn't list"),
                        ("counts", "C5: codebook counts vs the data")]:
        items = by[kind]
        lines += [f"## {title}", "", *([f"- {x}" for x in items[:80]] or ["None."])]
        if len(items) > 80:
            lines.append(f"- ... and {len(items) - 80} more")
        lines.append("")
    (ROOT / "build" / "report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
