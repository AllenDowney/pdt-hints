"""Links between consecutive HINTS cycles, by rule hints_same_name_identical.

Usage:
    python build/build_links.py

Reads metadata/releases/<release>/variables.jsonl for the releases in build/slice.yaml, in
order, and writes metadata/links.jsonl and build/links_report.md.

NCI's merge code for HINTS cycles "will match up variables that have the same name and format"
(each cycle's Survey Overview & Data Analysis Recommendations, "Merging HINTS Survey
Iterations"). The rule links a variable to the next cycle's variable of the same name
(compared case-insensitively, as SAS does) only when nothing about the item changed:
- the same question text, label (once the item number, "A1.", is removed) and universe, after
  normalizing case, whitespace, dashes and quotes;
- the same valid codes with the same labels, or, for a numeric item whose codebook lists its
  values, the same listing label;
- missing codes may differ in wording ("Missing data" vs "Missing data (Not Ascertained)") or in
  which of them occur, but a code can't be valid in one cycle and missing in the other.

Relations: same_item, plus labels_changed when the variable label differs only by its item
number, and renamed when the names differ in case. Every other pair is a candidate only: a link
for a changed item needs the publisher's statement (STANDARD.md §5.2), and HINTS's documentation
doesn't list item changes between cycles. Candidates go to build/links_report.md.
"""

import collections
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "metadata"
RULE_VERSION = "0.1"
QNUM = re.compile(r"^[A-Z]{0,2}\d+[a-z]?(?:\d+)?\.\s+")
OVERVIEW = {
    "hints6": ("HINTS 6 Survey Overview & Data Analysis Recommendations, 'Merging HINTS Survey Iterations'",
               "hints6-stata"),
    "hints7": ("HINTS 7 Survey Overview Data Analysis Recommendations, 'Merging HINTS Survey Iterations', p. 53",
               "hints7-stata"),
}
QUOTE = ("Next, the two files are merged into one. It will match up variables that have the same name and "
         "format and create a merged data file")


def norm(text):
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[–—-]+", "-", text)
    return " ".join(text.lower().split())


def strip_number(label):
    return QNUM.sub("", label or "")


def question(rec):
    qs = rec.get("questions") or []
    return norm(qs[0]["text"]) if qs else norm(strip_number(rec.get("label")))


def compare(a, b):
    """The reasons a and b aren't identical items, or [] if they are."""
    why = []
    if question(a) != question(b):
        why.append("question text")
    if norm(strip_number(a.get("label"))) != norm(strip_number(b.get("label"))):
        why.append("label")
    if norm(a.get("universe")) != norm(b.get("universe")):
        why.append("universe")
    if a.get("storage_type") != b.get("storage_type"):
        why.append("storage type")
    va = {v["code"]: v for v in a.get("values", [])}
    vb = {v["code"]: v for v in b.get("values", [])}
    valid_a = {c: norm(v["label"]) for c, v in va.items() if v["kind"] == "valid"}
    valid_b = {c: norm(v["label"]) for c, v in vb.items() if v["kind"] == "valid"}
    if valid_a != valid_b:
        why.append("valid codes")
    for c in set(va) & set(vb):
        if (va[c]["kind"] == "valid") != (vb[c]["kind"] == "valid"):
            why.append(f"code {c} valid in one cycle only")
    if ("valid_range" in a) != ("valid_range" in b) or norm(a.get("notes")) != norm(b.get("notes")):
        why.append("value listing")
    return why


def main():
    spec = yaml.safe_load((ROOT / "build" / "slice.yaml").read_text())
    releases = list(spec["releases"])
    recs = {rel: {r["name"].upper(): r for r in map(json.loads, open(META / "releases" / rel / "variables.jsonl"))}
            for rel in releases}
    links, candidates, counts = [], collections.defaultdict(list), []
    for ra, rb in zip(releases, releases[1:]):
        common = sorted(set(recs[ra]) & set(recs[rb]))
        n = 0
        citation, source = OVERVIEW[rb]
        for key in common:
            a, b = recs[ra][key], recs[rb][key]
            why = compare(a, b)
            if why:
                candidates[(ra, rb)].append((a["name"], why))
                continue
            relations = ["same_item"]
            if a["name"] != b["name"]:
                relations.append("renamed")
            if norm(a.get("label")) != norm(b.get("label")):
                relations.append("labels_changed")
            links.append({
                "from": {"release": ra, "name": a["name"]}, "to": {"release": rb, "name": b["name"]},
                "relations": relations,
                "origin": {"origin": "rule", "rule": "hints_same_name_identical", "rule_version": RULE_VERSION,
                           "source": source,
                           "note": "Same name (case-insensitive), question text, label without its item number, "
                                   "universe, and valid codes and labels in both cycles; missing codes differ at most "
                                   "in wording or occurrence. NCI's merge code matches HINTS variables by name."},
                "evidence": [{"citation": citation, "quote": QUOTE}],
            })
            n += 1
        counts.append(f"- {ra} -> {rb}: {len(common)} names in both, {n} linked, "
                      f"{len(candidates[(ra, rb)])} candidates; {len(set(recs[ra]) - set(recs[rb]))} only in {ra}, "
                      f"{len(set(recs[rb]) - set(recs[ra]))} only in {rb}")
    with open(META / "links.jsonl", "w", encoding="utf-8") as f:
        for link in links:
            f.write(json.dumps(link, ensure_ascii=False) + "\n")
    lines = ["# Links report: hints", "", "Generated by `build/build_links.py`. Do not edit.", "", *counts, "",
             "## Candidates: same name, but something changed", "",
             "Not linked: a link for a changed item needs the publisher's statement (STANDARD.md §5.2). "
             "`pdt diff` shows what changed.", ""]
    for (ra, rb), items in candidates.items():
        by = collections.Counter(tuple(w) for _, w in items)
        lines += [f"### {ra} -> {rb}", "", *[f"- {', '.join(k)}: {n}" for k, n in by.most_common()], "",
                  "Names: " + ", ".join(f"`{name}`" for name, _ in items), ""]
    (ROOT / "build" / "links_report.md").write_text("\n".join(lines))
    print(f"{len(links)} links; see build/links_report.md")


if __name__ == "__main__":
    main()
