#!/usr/bin/env python3
"""
Grade Stratosyn-style self-scoring quiz submissions.

Quiz files are different from worksheet files — they have no LESSON header
and instead carry STUDENT/CLASS/DATE/QUIZ/SCORE/TIME plus per-part results
(Part 1 MC, Part 2 Classification, Part 3 Period Placement, Part 4 Ordering).
The student-facing app self-scores, so this script does NOT re-grade —
it parses the embedded scores and produces a rollup HTML view.

USAGE
─────
    python3 grade_quiz.py --honors  <raw-honors-folder>   \\
                          --regular <raw-regular-folder>

    # preview without moving files:
    python3 grade_quiz.py --honors path/ --regular path/ --dry-run

    # specify output roots:
    python3 grade_quiz.py --honors ... --regular ... --data /path --output /path

Outputs
───────
    data/<quiz-slug>_<date>/<pN-section>/<original-filename>
    data/<quiz-slug>_<date>/metadata.json
    output/<quiz-slug>_<date>/gradebook.html

After running this, also run build_gradebook.py to refresh
output/master.html, which includes both worksheets and quizzes
as a unified roster.
"""

import argparse
import html
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data"
DEFAULT_OUTPUT = ROOT / "output"

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")


def strip_tags(s: str) -> str:
    s = TAG_RE.sub("\n", s)
    s = html.unescape(s)
    return s


def slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


# ─────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────

def parse_quiz(path: Path) -> dict | None:
    """Parse one quiz file. Returns None if it doesn't look like a quiz."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = strip_tags(raw)
    # collapse blank-line runs but keep newlines for line-based regexes
    text = re.sub(r"\n{2,}", "\n", text)

    if "QUIZ" not in text or "SCORE" not in text:
        return None

    def grab(pat, flags=re.M, group=1):
        m = re.search(pat, text, flags)
        return m.group(group).strip() if m else None

    # All formal header fields are on their own line (e.g. "QUIZ    : ...")
    # Anchored to line-start to avoid matching the title line at the top
    # ("PERIODIC TABLE REVIEW QUIZ: <name>") which also contains "QUIZ:".
    # Separators are permissive: students sometimes submit with the colon
    # missing or spacing collapsed. CLASS also accepts Phase/Hour/Block as
    # aliases for Period (the student-facing app supports these).
    SEP = r"[\s:]+"
    student = grab(rf"^STUDENT{SEP}(.+)$")
    period = grab(rf"^CLASS{SEP}(?:Period|Phase|Hour|Block)\s+(\d+)\b")
    date = grab(rf"^DATE{SEP}(\d{{4}}-\d{{2}}-\d{{2}})")
    quiz_title = grab(rf"^QUIZ{SEP}(.+)$")

    m = re.search(
        rf"^SCORE{SEP}(\d+)\s*/\s*(\d+)\s*·\s*(\d+)%\s*·\s*([A-F][+\-]?)",
        text, re.M,
    )
    if not (student and period and date and quiz_title and m):
        return None

    score = int(m.group(1))
    max_score = int(m.group(2))
    pct = int(m.group(3))
    letter = m.group(4)

    t = re.search(r"TIME\s*:\s*(?:(\d+)\s*m\s*)?(?:(\d+)\s*s)?", text)
    if t and (t.group(1) or t.group(2)):
        mins = int(t.group(1) or 0)
        secs = int(t.group(2) or 0)
        time_sec = mins * 60 + secs
        time_str = (f"{mins}m " if mins else "") + f"{secs}s"
    else:
        time_sec = None
        time_str = "—"

    rec = {
        "filename": path.name,
        "student": student,
        "period": int(period),
        "date": date,
        "quiz_title": quiz_title,
        "score": score,
        "max_score": max_score,
        "percent": pct,
        "letter": letter,
        "time_sec": time_sec,
        "time_str": time_str,
        "parts": parse_parts(text),
    }
    return rec


def parse_parts(text: str) -> dict:
    """Extract per-part results. Resilient to small format drift."""
    parts = {"mc": [], "classify": [], "placement": [], "ordering_raw": ""}

    # --- Part 1: Multiple Choice ---
    # Pattern: "Q1: <prompt>\n  Answer: X · <text>\n  Result: ✓ CORRECT"
    # (or "✗ INCORRECT (correct: Y) [first attempt: Z]"). Lines may be
    # indented in some submissions, so we allow leading whitespace.
    # [^\S\n] matches any whitespace EXCEPT newline (regular space, tab,
    # AND non-breaking space \xa0 which the HTML often injects as indent).
    mc_pat = re.compile(
        r"(Q\d+)\s*:\s*(.+?)\n"
        r"[^\S\n]*Answer\s*:\s*([A-D])\s*·\s*(.+?)\n"
        r"[^\S\n]*Result\s*:\s*([✓✗])\s*(\w+)",
        re.S,
    )
    for m in mc_pat.finditer(text):
        # Bound to Part 1 only — skip if past "Part 1 score:"
        idx_p1_end = text.find("Part 1 score")
        if idx_p1_end != -1 and m.start() > idx_p1_end:
            continue
        parts["mc"].append({
            "qid": m.group(1),
            "prompt": m.group(2).strip(),
            "chosen": m.group(3),
            "answer_text": m.group(4).strip(),
            "correct": m.group(5) == "✓",
            "result": m.group(6),
        })

    # --- Part 2: Element Classification ---
    # "Na  → METAL      — ✓ Correct (first try)"
    p2_block = re.search(r"PART 2.*?Part 2 score", text, re.S)
    if p2_block:
        for line in p2_block.group(0).splitlines():
            m = re.match(
                r"\s*([A-Z][a-z]?)\s+→\s+(\w+)\s+—\s+([✓✗])\s*(.+)",
                line,
            )
            if m:
                parts["classify"].append({
                    "element": m.group(1),
                    "answer": m.group(2),
                    "correct": m.group(3) == "✓",
                    "note": m.group(4).strip(),
                })

    # --- Part 3: Period Placement ---
    # "H   → Period 1 — ✓ Correct (first try)"
    p3_block = re.search(r"PART 3.*?Part 3 score", text, re.S)
    if p3_block:
        for line in p3_block.group(0).splitlines():
            m = re.match(
                r"\s*([A-Z][a-z]?)\s+→\s+Period\s+(\d+)\s+—\s+([✓✗])\s*(.+)",
                line,
            )
            if m:
                parts["placement"].append({
                    "element": m.group(1),
                    "period": int(m.group(2)),
                    "correct": m.group(3) == "✓",
                    "note": m.group(4).strip(),
                })

    # --- Part 4: Ordering & Ranking ---
    # Two subsections (Q23 reactivity, Q24-25 atomic number). Capture raw text;
    # rendering will preserve formatting inside <pre>.
    p4_block = re.search(r"PART 4.*?(?=════|SCORE SUMMARY|$)", text, re.S)
    if p4_block:
        parts["ordering_raw"] = p4_block.group(0).strip()

    return parts


# ─────────────────────────────────────────────────────────────────────
# Routing
# ─────────────────────────────────────────────────────────────────────

def quiz_slug(title: str) -> str:
    """Turn 'Periodic Table Review · Days 1–3' → 'quiz-periodic-table-review-days-1-3'."""
    return "quiz-" + slugify(title)


def collect(folder: Path, section: str):
    """Yield (path, rec, section) tuples for every *_text.html file."""
    for f in sorted(folder.glob("*_text.html")):
        rec = parse_quiz(f)
        yield f, rec, section


def resolve_canonical_assignment_keys(records, data_root: Path) -> tuple[dict, list]:
    """For each quiz slug seen in this batch, pick exactly one assignment key
    (slug_date) — the earliest existing data/<slug>_*/ folder on disk, else
    the earliest valid date in the incoming batch. Mirrors organize.py so late
    quiz makeups land in the on-time folder instead of creating a duplicate.

    Returns (canonical_by_slug, reroutes_list).
    """
    incoming_dates_by_slug: dict[str, set[str]] = {}
    for _src, rec, _sec in records:
        if rec is None:
            continue
        slug = quiz_slug(rec["quiz_title"])
        incoming_dates_by_slug.setdefault(slug, set()).add(rec["date"])

    canonical: dict[str, str] = {}
    reroutes: list[tuple[str, str, str]] = []
    for slug, dates in incoming_dates_by_slug.items():
        existing = sorted(
            d.name for d in data_root.glob(f"{slug}_*")
            if d.is_dir()
        )
        if existing:
            canonical[slug] = existing[0]
        else:
            valid = sorted(d for d in dates if d)
            chosen = valid[0] if valid else "unknown-date"
            canonical[slug] = f"{slug}_{chosen}"
        _, canon_date = canonical[slug].rsplit("_", 1)
        for d in dates:
            if d != canon_date:
                reroutes.append((slug, d, canon_date))

    return canonical, reroutes


def organize(records: list[dict], data_root: Path, dry_run: bool) -> dict:
    """
    Route files into data/<slug>_<date>/<pN-section>/. Returns metadata grouped
    by assignment key for downstream rendering.

    Late submissions for an existing quiz (different date than the canonical
    folder) get auto-routed into the canonical folder — mirrors organize.py.
    """
    canonical, reroutes = resolve_canonical_assignment_keys(records, data_root)
    if reroutes:
        print(f"  REROUTED — {len(reroutes)} late-submission date(s) consolidated:")
        for slug, frm, to in reroutes:
            print(f"    {slug}: {frm} → {to}")
        print()

    grouped: dict[str, dict] = {}

    for src, rec, section in records:
        if rec is None:
            # Should have been filtered earlier; skip defensively.
            continue
        slug = quiz_slug(rec["quiz_title"])
        assignment_key = canonical.get(slug, f"{slug}_{rec['date']}")
        period_dir = f"p{rec['period']}-{section}"
        target_dir = data_root / assignment_key / period_dir
        target = target_dir / src.name

        entry = grouped.setdefault(assignment_key, {
            "slug": slug,
            "date": rec["date"],
            "quiz_title": rec["quiz_title"],
            "max_score": rec["max_score"],
            "periods": {},
        })
        period_entry = entry["periods"].setdefault(period_dir, [])
        rec_with_target = dict(rec)
        rec_with_target["section"] = section
        rec_with_target["period_dir"] = period_dir
        rec_with_target["target_path"] = str(target.relative_to(data_root.parent))
        period_entry.append(rec_with_target)

        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            if target.exists():
                # Collision: preserve as resubmission with today's date suffix.
                stem = src.stem
                suffix = src.suffix
                today = datetime.now().strftime("%Y-%m-%d")
                target = target_dir / f"{stem}__resubmit-{today}{suffix}"
            shutil.move(str(src), str(target))
            rec_with_target["target_path"] = str(target.relative_to(data_root.parent))

    if not dry_run:
        for key, entry in grouped.items():
            asg_dir = data_root / key
            meta_path = asg_dir / "metadata.json"

            existing = {}
            if meta_path.exists():
                try:
                    existing = json.loads(meta_path.read_text())
                except json.JSONDecodeError:
                    existing = {}

            # Recount on disk so late-merged folders reflect total students.
            disk_counts: dict[str, int] = {}
            for pdir in asg_dir.iterdir():
                if pdir.is_dir():
                    n = sum(1 for _ in pdir.glob("*.html"))
                    if n:
                        disk_counts[pdir.name] = n

            canon_date = key.rsplit("_", 1)[1] if "_" in key else entry["date"]

            payload = {
                "slug": entry["slug"],
                "date": canon_date,
                "quiz_title": entry["quiz_title"],
                "max_score": entry["max_score"],
                "organized_at": datetime.now().isoformat(timespec="seconds"),
                "period_counts": disk_counts,
                "total_students": sum(disk_counts.values()),
            }
            history = existing.get("consolidated_runs", [])
            history.append({
                "at": payload["organized_at"],
                "added_files": sum(len(rs) for rs in entry["periods"].values()),
            })
            payload["consolidated_runs"] = history
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(payload, indent=2))

    return grouped


# ─────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────

CSS = """
:root {
  --ok: #1a7f37;
  --bad: #cf222e;
  --muted: #57606a;
  --line: #d0d7de;
  --bg-alt: #f6f8fa;
  --bg-honors: #fff8e7;
}
* { box-sizing: border-box; }
body {
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  margin: 0; padding: 24px; max-width: 1200px; margin: 0 auto;
  color: #1f2328;
}
h1 { margin: 0 0 4px; font-size: 22px; }
.sub { color: var(--muted); margin-bottom: 18px; }
.controls { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
.controls button {
  border: 1px solid var(--line); background: white; padding: 5px 11px;
  border-radius: 6px; cursor: pointer; font-size: 13px;
}
.controls button.active { background: #0969da; color: white; border-color: #0969da; }
table.roster { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 24px; }
table.roster th, table.roster td { padding: 6px 10px; border-bottom: 1px solid var(--line); text-align: left; }
table.roster th { cursor: pointer; user-select: none; background: var(--bg-alt); font-weight: 600; }
table.roster th:hover { background: #eaeef2; }
table.roster tr.honors { background: var(--bg-honors); }
table.roster a { color: #0969da; text-decoration: none; }
table.roster a:hover { text-decoration: underline; }
.score-cell { font-variant-numeric: tabular-nums; font-weight: 600; }
.letter-A { color: var(--ok); }
.letter-B { color: #1f883d; }
.letter-C { color: #9a6700; }
.letter-D, .letter-F { color: var(--bad); }
.student-card {
  border: 1px solid var(--line); border-radius: 8px; padding: 16px 18px;
  margin: 14px 0; background: white;
}
.student-card.honors { background: var(--bg-honors); }
.student-card h3 { margin: 0 0 4px; font-size: 16px; }
.student-card .meta { color: var(--muted); font-size: 12px; margin-bottom: 12px; }
.student-card h4 {
  margin: 14px 0 6px; font-size: 13px; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--muted);
}
.mc-list { padding-left: 18px; margin: 0; }
.mc-list li { margin: 4px 0; }
.mark { display: inline-block; width: 1.2em; font-weight: 600; }
.mark.ok { color: var(--ok); }
.mark.bad { color: var(--bad); }
.q-chose { color: var(--muted); font-size: 12px; margin-left: 6px; }
table.compact { font-size: 12px; border-collapse: collapse; margin: 0; }
table.compact td { padding: 2px 10px 2px 0; vertical-align: top; }
table.compact td.elem { font-weight: 600; min-width: 2em; }
pre.ordering { font: 12px/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  background: var(--bg-alt); padding: 10px 12px; border-radius: 6px;
  white-space: pre-wrap; margin: 0; }
hr.div { border: 0; border-top: 1px solid var(--line); margin: 24px 0; }
.summary-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 8px; margin-bottom: 18px;
}
.stat { border: 1px solid var(--line); border-radius: 6px; padding: 8px 12px; background: var(--bg-alt); }
.stat .n { font-size: 18px; font-weight: 600; }
.stat .lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
"""

JS = """
(function(){
  const tbody = document.querySelector('table.roster tbody');
  if (!tbody) return;
  const rows = Array.from(tbody.querySelectorAll('tr'));

  // Period filter buttons
  document.querySelectorAll('.controls button').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const f = btn.dataset.filter;
      rows.forEach(r => {
        r.style.display = (f === 'all' || r.dataset.period === f) ? '' : 'none';
      });
    });
  });

  // Header sort
  document.querySelectorAll('table.roster th[data-sort]').forEach((th, idx) => {
    let dir = 1;
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      const sorted = rows.slice().sort((a, b) => {
        const av = a.dataset[key] || '';
        const bv = b.dataset[key] || '';
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return (an - bn) * dir;
        return av.localeCompare(bv) * dir;
      });
      sorted.forEach(r => tbody.appendChild(r));
      dir *= -1;
    });
  });
})();
"""


def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def render_student_card(rec: dict) -> str:
    """Full per-question detail block for one student."""
    section_class = "honors" if rec["section"] == "honors" else ""
    student_id = slugify(rec["student"]) + "-" + str(rec["period"])

    out = [f'<section class="student-card {section_class}" id="student-{student_id}">']
    out.append(f'<h3>{esc(rec["student"])}</h3>')
    out.append(
        f'<div class="meta">P{rec["period"]} '
        f'{rec["section"].title()} · {esc(rec["date"])} · '
        f'<strong>{rec["score"]}/{rec["max_score"]}</strong> · '
        f'{rec["percent"]}% · <span class="letter-{rec["letter"][0]}">{esc(rec["letter"])}</span> · '
        f'{esc(rec["time_str"])}</div>'
    )

    parts = rec["parts"]

    # Part 1
    mc = parts["mc"]
    if mc:
        mc_correct = sum(1 for q in mc if q["correct"])
        out.append(f'<h4>Part 1 — Multiple Choice ({mc_correct}/{len(mc)})</h4>')
        out.append('<ol class="mc-list">')
        for q in mc:
            mark_cls = "ok" if q["correct"] else "bad"
            mark_ch = "✓" if q["correct"] else "✗"
            out.append(
                f'<li><span class="mark {mark_cls}">{mark_ch}</span>'
                f'<strong>{esc(q["qid"])}</strong>: {esc(q["prompt"])} '
                f'<span class="q-chose">chose <strong>{esc(q["chosen"])}</strong> — {esc(q["answer_text"])}</span></li>'
            )
        out.append('</ol>')

    # Part 2
    cls = parts["classify"]
    if cls:
        ok_n = sum(1 for c in cls if c["correct"])
        out.append(f'<h4>Part 2 — Element Classification ({ok_n}/{len(cls)})</h4>')
        out.append('<table class="compact">')
        for c in cls:
            mark_cls = "ok" if c["correct"] else "bad"
            mark_ch = "✓" if c["correct"] else "✗"
            out.append(
                f'<tr><td class="elem">{esc(c["element"])}</td>'
                f'<td>{esc(c["answer"])}</td>'
                f'<td><span class="mark {mark_cls}">{mark_ch}</span> {esc(c["note"])}</td></tr>'
            )
        out.append('</table>')

    # Part 3
    pl = parts["placement"]
    if pl:
        ok_n = sum(1 for p in pl if p["correct"])
        out.append(f'<h4>Part 3 — Period Placement ({ok_n}/{len(pl)})</h4>')
        out.append('<table class="compact">')
        for p in pl:
            mark_cls = "ok" if p["correct"] else "bad"
            mark_ch = "✓" if p["correct"] else "✗"
            out.append(
                f'<tr><td class="elem">{esc(p["element"])}</td>'
                f'<td>Period {p["period"]}</td>'
                f'<td><span class="mark {mark_cls}">{mark_ch}</span> {esc(p["note"])}</td></tr>'
            )
        out.append('</table>')

    # Part 4
    if parts["ordering_raw"]:
        out.append('<h4>Part 4 — Ordering & Ranking</h4>')
        out.append(f'<pre class="ordering">{esc(parts["ordering_raw"])}</pre>')

    out.append('</section>')
    return "\n".join(out)


def render_results_html(entry: dict) -> str:
    """Build the per-assignment results.html for one quiz."""
    title = entry["quiz_title"]
    date = entry["date"]
    all_recs = []
    for period_dir, recs in entry["periods"].items():
        all_recs.extend(recs)
    # Sort: section (honors first), period, last-name-first
    def sort_key(r):
        sec_rank = 0 if r["section"] == "honors" else 1
        last = r["student"].split()[-1].lower() if r["student"] else "zzz"
        return (sec_rank, r["period"], last)
    all_recs.sort(key=sort_key)

    # Summary stats
    n = len(all_recs)
    avg_pct = round(sum(r["percent"] for r in all_recs) / n) if n else 0
    avg_time = round(sum(r["time_sec"] for r in all_recs if r["time_sec"]) / n) if n else 0
    avg_time_str = f"{avg_time // 60}m {avg_time % 60}s"
    perfect = sum(1 for r in all_recs if r["percent"] == 100)
    failing = sum(1 for r in all_recs if r["percent"] < 70)

    # Period filter buttons
    by_period = {}
    for r in all_recs:
        by_period.setdefault(r["period_dir"], 0)
        by_period[r["period_dir"]] += 1
    period_buttons = ['<button data-filter="all" class="active">All (' + str(n) + ')</button>']
    for pd in sorted(by_period.keys(), key=lambda k: (0 if "honors" in k else 1, k)):
        label = pd.replace("p", "P").replace("-honors", " Honors").replace("-regular", "")
        period_buttons.append(f'<button data-filter="{pd}">{label} ({by_period[pd]})</button>')

    # Roster table rows
    rows = []
    for r in all_recs:
        section_class = "honors" if r["section"] == "honors" else ""
        student_id = slugify(r["student"]) + "-" + str(r["period"])
        last = r["student"].split()[-1] if r["student"] else ""
        rows.append(
            f'<tr class="{section_class}" data-period="{r["period_dir"]}" '
            f'data-last="{esc(last.lower())}" data-period-num="{r["period"]}" '
            f'data-score="{r["percent"]}" data-time="{r["time_sec"] or 0}">'
            f'<td><a href="#student-{student_id}">{esc(r["student"])}</a></td>'
            f'<td>P{r["period"]} {r["section"].title()}</td>'
            f'<td class="score-cell">{r["score"]}/{r["max_score"]}</td>'
            f'<td class="score-cell">{r["percent"]}%</td>'
            f'<td class="letter-{r["letter"][0]}">{esc(r["letter"])}</td>'
            f'<td>{esc(r["time_str"])}</td>'
            f'</tr>'
        )

    cards = "\n".join(render_student_card(r) for r in all_recs)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{esc(title)} · Results · {esc(date)}</title>
<style>{CSS}</style>
</head>
<body>
<h1>{esc(title)}</h1>
<div class="sub">Quiz results · {esc(date)} · {n} students</div>

<div class="summary-grid">
  <div class="stat"><div class="n">{n}</div><div class="lbl">Students</div></div>
  <div class="stat"><div class="n">{avg_pct}%</div><div class="lbl">Avg Score</div></div>
  <div class="stat"><div class="n">{avg_time_str}</div><div class="lbl">Avg Time</div></div>
  <div class="stat"><div class="n">{perfect}</div><div class="lbl">Perfect (100%)</div></div>
  <div class="stat"><div class="n">{failing}</div><div class="lbl">Below 70%</div></div>
</div>

<div class="controls">
  {' '.join(period_buttons)}
</div>

<table class="roster">
<thead><tr>
  <th data-sort="last">Student</th>
  <th data-sort="period-num">Period</th>
  <th data-sort="score">Score</th>
  <th data-sort="score">%</th>
  <th>Letter</th>
  <th data-sort="time">Time</th>
</tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>

<hr class="div">
<h2>Per-Student Detail</h2>
{cards}

<script>{JS}</script>
</body>
</html>
"""


def render_master(data_root: Path, output_root: Path) -> str:
    """Build quiz_master.html — directory of all quizzes in data/."""
    rows = []
    for assignment_dir in sorted(data_root.glob("quiz-*")):
        meta_path = assignment_dir / "metadata.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        results_link = f"{assignment_dir.name}/results.html"
        rows.append(
            f'<tr>'
            f'<td><a href="{esc(results_link)}">{esc(meta["quiz_title"])}</a></td>'
            f'<td>{esc(meta["date"])}</td>'
            f'<td>{meta["total_students"]}</td>'
            f'<td>{meta["max_score"]}</td>'
            f'</tr>'
        )
    body = "\n".join(rows) if rows else '<tr><td colspan="4">No quizzes yet.</td></tr>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>All Quizzes — Madewell 7th Science</title>
<style>{CSS}</style>
</head>
<body>
<h1>All Quizzes</h1>
<div class="sub">Cross-quiz rollup · Madewell 7th-grade Life Science</div>
<table class="roster">
<thead><tr><th>Quiz</th><th>Date</th><th>Students</th><th>Max Score</th></tr></thead>
<tbody>{body}</tbody>
</table>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Grade self-scoring quiz submissions and produce HTML rollup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--honors", type=Path, required=True,
                    help="Raw folder containing the honors batch (smaller).")
    ap.add_argument("--regular", type=Path, required=True,
                    help="Raw folder containing the regular batch (larger).")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA,
                    help="Data root (default: ../data).")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                    help="Output root (default: ../output).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and report but do not move files or write HTML.")
    args = ap.parse_args()

    if not args.honors.exists():
        print(f"ERROR: honors folder not found: {args.honors}", file=sys.stderr)
        sys.exit(1)
    if not args.regular.exists():
        print(f"ERROR: regular folder not found: {args.regular}", file=sys.stderr)
        sys.exit(1)

    print(f"{'DRY RUN — ' if args.dry_run else ''}Grading quizzes")
    print(f"  data root:   {args.data}")
    print(f"  output root: {args.output}")
    print()

    records = []
    skipped = []
    for folder, section in [(args.honors, "honors"), (args.regular, "regular")]:
        print(f"  Reading {folder} (section: {section})")
        count_ok = 0
        for path, rec, sec in collect(folder, section):
            if rec is None:
                skipped.append(path.name)
                continue
            records.append((path, rec, sec))
            count_ok += 1
        print(f"      {count_ok} quiz file(s) parsed")
    print()

    if skipped:
        print(f"  SKIPPED ({len(skipped)} unparseable):")
        for n in skipped[:6]:
            print(f"    {n}")
        if len(skipped) > 6:
            print(f"    … and {len(skipped) - 6} more")
        print()

    grouped = organize(records, args.data, args.dry_run)

    if not grouped:
        print("  No assignments to render.")
        return

    print(f"  PLAN — {len(records)} students across {len(grouped)} assignment(s):")
    for key, entry in grouped.items():
        print(f"    data/{key}/   ({entry['quiz_title']})")
        for pd in sorted(entry["periods"].keys(),
                         key=lambda k: (0 if "honors" in k else 1, k)):
            print(f"      {pd}/  ({len(entry['periods'][pd])} students)")
    print()

    if args.dry_run:
        print("  Dry run — no files moved, no HTML written.")
        return

    for key, entry in grouped.items():
        out_dir = args.output / key
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "gradebook.html"
        out_path.write_text(render_results_html(entry), encoding="utf-8")
        print(f"  → {out_path.relative_to(args.output.parent)}")

    print()
    print(f"  ✓ Graded {len(records)} quiz(zes) across {len(grouped)} assignment(s).")
    print(f"  → Now run: python3 scripts/build_gradebook.py")
    print(f"    (refreshes output/master.html with the quiz included)")


if __name__ == "__main__":
    main()
