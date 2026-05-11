#!/usr/bin/env python3
"""
Build Stratosyn-style gradebook views from the data/ layout.

Auto-discovers every assignment under data/ (one folder per assignment),
loads metadata.json + reads each period subfolder, and emits:

  output/<assignment-id>/gradebook.html   — per-assignment grading view
  output/master.html                      — master view across all assignments

Both views share a localStorage namespace (stratosyn:gradebook:v1) so
teacher edits in one view immediately surface in the other on reload.
"""

import html
import json
import re
import sys
from pathlib import Path

# Shared parsing lives in _parse.py — import it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _parse import (
    ANSWER_KEY, QUESTION_PROMPTS, BUCKET_ORDER,
    parse_submission, grade_summary, bucket, bucket_breakdown,
    fmt_seconds, last_name_key, slugify, lesson_to_slug,
)

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

# Period chip color mapping. honors-1 takes cyan (the brand accent),
# regular periods cycle through the rest of the palette.
PERIOD_COLORS = {
    ("honors", 1): "cyan",
    ("regular", 2): "gold",
    ("regular", 3): "green",
    ("regular", 4): "purple",
    ("regular", 5): "red",
    ("regular", 6): "pearl",
}
DEFAULT_PERIOD_COLOR = "pearl"

# Short-display labels for known assignments (otherwise auto-derived).
ASSIGNMENT_SHORT_LABELS = {
    "day1-periodic-table": "DAY 1 · PT",
    "day2-electron-shells-element-families": "DAY 2 · ESF",
}
ASSIGNMENT_DEFAULT_COLOR = "cyan"
QUIZ_DEFAULT_COLOR = "gold"  # quizzes show as gold cards to distinguish from worksheets



# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────

def build_periods_registry(records):
    """Group records by (section, period_num) and emit chip metadata."""
    counts = {}
    for r in records:
        key = r["period_key"] or f"{r['section']}-?"
        counts[key] = counts.get(key, 0) + 1

    chips = []
    # Stable order: honors first (P1 HON), then regular periods ascending.
    seen = set()
    def add_chip(section, period_num, count):
        key = f"{section}-{period_num}" if period_num else f"{section}-?"
        if key in seen: return
        seen.add(key)
        is_honors = section == "honors"
        label = f"P{period_num}" if period_num else f"P?"
        if is_honors: label += " HON"
        chips.append({
            "key": key,
            "label": label,
            "section": section,
            "period_num": period_num,
            "count": count,
            "color": PERIOD_COLORS.get((section, period_num), DEFAULT_PERIOD_COLOR),
            "is_honors": is_honors,
            "is_stray": count <= 2 and not is_honors,  # mark tiny groups
        })

    # Honors first (sorted by period_num)
    honors_keys = sorted([k for k in counts if k.startswith("honors-")],
                        key=lambda k: int(k.split("-")[1]) if k.split("-")[1].isdigit() else 999)
    regular_keys = sorted([k for k in counts if k.startswith("regular-")],
                         key=lambda k: int(k.split("-")[1]) if k.split("-")[1].isdigit() else 999)

    for key in honors_keys + regular_keys:
        section, period_str = key.split("-", 1)
        period_num = int(period_str) if period_str.isdigit() else None
        add_chip(section, period_num, counts[key])

    return chips


def discover_assignments():
    """Walk data/ and return one Path per fully organized assignment."""
    if not DATA_DIR.is_dir():
        return []
    return [d for d in sorted(DATA_DIR.iterdir())
            if d.is_dir() and not d.name.startswith("_") and (d / "metadata.json").exists()]


def load_assignment(asg_dir: Path) -> dict:
    """Read a single assignment folder into the rendering payload shape.

    Reads answer_key + question_prompts from the assignment's metadata.json
    (per-assignment, not global). If answer_key is missing, MC grading is
    skipped — gradebook.html renders a 'no answer key' state instead of
    incorrect colored stats.
    """
    meta = json.loads((asg_dir / "metadata.json").read_text(encoding="utf-8"))
    asg_answer_key = meta.get("answer_key") or None
    asg_question_prompts = meta.get("question_prompts") or {}

    records = []
    for pdir in sorted(asg_dir.iterdir()):
        if not pdir.is_dir():
            continue
        # Period dir name encodes section: 'p3-regular', 'p1-honors', or 'unknown'
        if pdir.name == "unknown":
            section = "regular"
        else:
            parts = pdir.name.split("-", 1)
            section = parts[1] if len(parts) > 1 else "regular"
        for f in sorted(pdir.glob("*_text.html")):
            rec = parse_submission(f, section=section, answer_key=asg_answer_key)
            rec["summary"] = grade_summary(rec)
            records.append(rec)

    # Dedupe + sort
    seen_ids = set()
    deduped = []
    for r in records:
        if r["id"] in seen_ids: continue
        seen_ids.add(r["id"])
        deduped.append(r)
    records = sorted(deduped, key=lambda r: (r["period_num"] or 99, last_name_key(r)))
    n = len(records)
    periods = build_periods_registry(records)

    avg = lambda xs: (round(sum(xs) / len(xs)) if xs else 0)
    avg_or_none = lambda xs: (round(sum(xs) / len(xs)) if xs else None)
    times = sorted(r["time_on_task_s"] for r in records)
    median_time = times[len(times) // 2] if times else 0

    has_key = bool(asg_answer_key)

    q_accuracy: dict = {}
    if has_key:
        for qid in asg_answer_key:
            correct = sum(1 for r in records if r["answers"].get(qid, {}).get("correct"))
            q_accuracy[qid] = {"correct": correct, "total": n,
                               "pct": round(100 * correct / n) if n else 0}

    # Distribution + bucket breakdowns only make sense when composite_pct is real.
    graded_records = [r for r in records if r["summary"]["composite_pct"] is not None]
    distribution = {b: 0 for b in BUCKET_ORDER}
    for r in graded_records:
        distribution[bucket(r["summary"]["composite_pct"])] += 1

    bucket_breakdowns = {
        b: {"qualified": q, "almost": a, "far": f}
        for b in BUCKET_ORDER
        for q, a, f in [bucket_breakdown(b, graded_records)]
    }

    asg_id = meta["assignment_id"]
    short = ASSIGNMENT_SHORT_LABELS.get(
        asg_id.split("_")[0],
        asg_id.replace("-", " ").upper()[:14],
    )

    mc_pcts = [r["summary"]["mc_pct"] for r in records if r["summary"]["mc_pct"] is not None]
    comp_pcts = [r["summary"]["composite_pct"] for r in records if r["summary"]["composite_pct"] is not None]

    return {
        "id": asg_id,
        "title": meta.get("title") or asg_id,
        "lesson": meta.get("lesson") or asg_id,
        "short": short,
        "color": ASSIGNMENT_DEFAULT_COLOR,
        "teacher": meta.get("teacher") or "—",
        "room": "ALA Room 501",
        "date": meta.get("date") or "",
        "period": "All periods" if len({r["period_key"] for r in records}) > 1
                  else (records[0]["period"] if records else ""),
        "n": n,
        "avg_mc": avg_or_none(mc_pcts),
        "avg_games": avg([r["summary"]["games_pct"] for r in records]),
        "avg_composite": avg_or_none(comp_pcts),
        "avg_time_s": avg([r["time_on_task_s"] for r in records]),
        "median_time_s": median_time,
        "answer_key": asg_answer_key or {},
        "question_prompts": asg_question_prompts,
        "q_accuracy": q_accuracy,
        "distribution": distribution,
        "bucket_breakdowns": bucket_breakdowns,
        "periods": periods,
        "records": records,
        "has_answer_key": has_key,
    }


def load_quiz_assignment(asg_dir: Path) -> dict:
    """Read a quiz-* assignment folder into the same payload shape as load_assignment.

    Quizzes have a different on-disk format than worksheets — they self-score
    via the QUIZ header and parts (Part 1 MC, Part 2 Classification,
    Part 3 Period Placement, Part 4 Ordering). We adapt those fields to the
    {summary: {composite_pct, mc_pct, games_pct, ...}} shape that render_master
    expects, so the master view treats quizzes and worksheets uniformly.
    """
    from grade_quiz import parse_quiz  # avoid circular import at module load

    meta = json.loads((asg_dir / "metadata.json").read_text(encoding="utf-8"))

    records = []
    for pdir in sorted(asg_dir.iterdir()):
        if not pdir.is_dir():
            continue
        section = "honors" if "honors" in pdir.name else "regular"
        for f in sorted(pdir.glob("*_text.html")):
            qrec = parse_quiz(f)
            if qrec is None:
                continue

            mc = qrec["parts"]["mc"]
            mc_correct = sum(1 for q in mc if q["correct"])
            mc_total = len(mc) or 1
            mc_pct = round(100 * mc_correct / mc_total)

            cls = qrec["parts"]["classify"]
            pl = qrec["parts"]["placement"]
            games_first_try = (
                sum(1 for c in cls if c["correct"] and "first try" in c["note"].lower())
                + sum(1 for p in pl if p["correct"] and "first try" in p["note"].lower())
            )
            games_total = (len(cls) + len(pl)) or 1
            games_pct = round(100 * games_first_try / games_total)

            student_id = f.stem
            if student_id.endswith("_text"):
                student_id = student_id[:-5]

            period_num = qrec["period"]
            period_key = f"{section}-{period_num}"
            period_label = f"P{period_num}" + (" HON" if section == "honors" else "")

            records.append({
                "id": student_id,
                "student": qrec["student"],
                "section": section,
                "period_num": period_num,
                "period_key": period_key,
                "period": period_label,
                "file": f.name,
                "time_on_task_s": qrec["time_sec"] or 0,
                "answers": {},  # quizzes use a different question schema than ANSWER_KEY
                "summary": {
                    "mc_correct": mc_correct,
                    "mc_total": mc_total,
                    "mc_pct": mc_pct,
                    "games_first_try": games_first_try,
                    "games_first_try_total": games_total,
                    "games_pct": games_pct,
                    "composite_pct": qrec["percent"],  # source of truth from the quiz app
                },
            })

    seen_ids = set()
    deduped = []
    for r in records:
        if r["id"] in seen_ids: continue
        seen_ids.add(r["id"])
        deduped.append(r)
    records = sorted(deduped, key=lambda r: (r["period_num"] or 99, last_name_key(r)))
    n = len(records)
    periods = build_periods_registry(records)

    avg = lambda xs: (round(sum(xs) / len(xs)) if xs else 0)
    times = sorted(r["time_on_task_s"] for r in records if r["time_on_task_s"])
    median_time = times[len(times) // 2] if times else 0

    distribution = {b: 0 for b in BUCKET_ORDER}
    for r in records:
        distribution[bucket(r["summary"]["composite_pct"])] += 1

    bucket_breakdowns = {
        b: {"qualified": q, "almost": a, "far": fa}
        for b in BUCKET_ORDER
        for q, a, fa in [bucket_breakdown(b, records)]
    }

    asg_id = asg_dir.name
    title = meta.get("quiz_title") or asg_id
    date = meta.get("date") or ""
    short = f"QUIZ {date[-5:].replace('-', '/')}" if date else "QUIZ"

    return {
        "id": asg_id,
        "title": title,
        "lesson": title,
        "short": short[:14],
        "color": QUIZ_DEFAULT_COLOR,
        "teacher": "Ms. Madewell",
        "room": "ALA Room 501",
        "date": date,
        "period": "All periods" if len({r["period_key"] for r in records}) > 1
                  else (records[0]["period"] if records else ""),
        "n": n,
        "avg_mc": avg([r["summary"]["mc_pct"] for r in records]),
        "avg_games": avg([r["summary"]["games_pct"] for r in records]),
        "avg_composite": avg([r["summary"]["composite_pct"] for r in records]),
        "avg_time_s": avg([r["time_on_task_s"] for r in records if r["time_on_task_s"]]),
        "median_time_s": median_time,
        "answer_key": {},
        "question_prompts": {},
        "q_accuracy": {},
        "distribution": distribution,
        "bucket_breakdowns": bucket_breakdowns,
        "periods": periods,
        "records": records,
        "is_quiz": True,
    }


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    asg_dirs = discover_assignments()
    if not asg_dirs:
        print(f"No assignments found in {DATA_DIR}. Run organize.py first.")
        return

    print(f"Discovered {len(asg_dirs)} assignment(s) in {DATA_DIR.name}/")
    assignments = []
    for asg_dir in asg_dirs:
        is_quiz = asg_dir.name.startswith("quiz-")
        kind = " (quiz)" if is_quiz else ""
        print(f"\n  Building {asg_dir.name}/{kind}")
        payload = load_quiz_assignment(asg_dir) if is_quiz else load_assignment(asg_dir)
        if payload["n"] == 0:
            print("    (skipped — no records)")
            continue
        for p in payload["periods"]:
            flag = " (stray)" if p["is_stray"] else ""
            print(f"    {p['label']:>10}  ({p['section']:<7})  n={p['count']}{flag}")
        if is_quiz:
            # Per-quiz HTML is written by grade_quiz.py; we only contribute to master.html.
            print(f"    (per-assignment HTML already at output/{asg_dir.name}/gradebook.html)")
        else:
            out_dir = OUTPUT_DIR / asg_dir.name
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "gradebook.html"
            out_path.write_text(render_assignment(payload), encoding="utf-8")
            print(f"    → {out_path.relative_to(PROJECT_ROOT)}  ({payload['n']} students)")
        assignments.append(payload)

    # Master view aggregates across assignments
    full_roster = {}
    for a in assignments:
        for r in a["records"]:
            full_roster.setdefault(r["id"], {
                "id": r["id"], "student": r["student"], "section": r["section"],
                "period_num": r["period_num"], "period_key": r["period_key"],
            })

    all_period_chips = build_periods_registry(list(full_roster.values()))

    primary = max(assignments, key=lambda a: a["n"]) if assignments else None
    master = {
        "teacher": primary["teacher"] if primary else "—",
        "room": primary["room"] if primary else "",
        "period": "All periods",
        "assignments": assignments,
        "periods": all_period_chips,
        "roster": list(full_roster.values()),
    }
    master_path = OUTPUT_DIR / "master.html"
    master_path.write_text(render_master(master), encoding="utf-8")
    print(f"\n  → {master_path.relative_to(PROJECT_ROOT)}  ({len(full_roster)} students across {len(assignments)} assignment(s))")



# ─────────────────────────────────────────────────────────────────────
# RENDERING
# ─────────────────────────────────────────────────────────────────────

def esc(s):
    return html.escape(s or "", quote=True)


def render_assignment(p: dict) -> str:
    has_key = p.get("has_answer_key", bool(p.get("answer_key")))

    # MC + composite stat cards: only meaningful when a key is provided.
    if has_key:
        mc_card = stat_card("AVG MULTIPLE-CHOICE", f"{p['avg_mc']}%",
                            f"{round(p['avg_mc']*8/100, 1)}/8 questions", "green")
        composite_card = stat_card("AVG COMPOSITE", f"{p['avg_composite']}%",
                                   "MC + games blended", "pearl")
    else:
        mc_card = stat_card("AVG MULTIPLE-CHOICE", "—",
                            "answer key not provided", "pearl")
        composite_card = stat_card("AVG COMPOSITE", "—",
                                   "needs answer key", "pearl")

    # Banner for assignments missing an answer key.
    no_key_banner = "" if has_key else """
  <section class="ss-section">
    <div style="border:1px solid var(--gold);background:rgba(245,200,66,.08);border-radius:8px;padding:14px 16px;color:var(--ink);font-family:var(--mono);font-size:13px;line-height:1.5">
      <strong style="color:var(--gold);letter-spacing:.1em">NO ANSWER KEY</strong><br>
      This assignment's <code>metadata.json</code> does not yet contain an <code>answer_key</code> field, so multiple-choice grading and per-question difficulty are not shown. Edit the file at <code>data/{asg_id}/metadata.json</code> to add one — keys are letter values like <code>{{"Q1":"A","Q2":"C",...}}</code>. Question prompts are auto-extracted; you only need to provide the correct letters. Then re-run <code>python3 scripts/build_gradebook.py</code>.
    </div>
  </section>""".replace("{asg_id}", esc(p["id"]))

    # Question Difficulty section: hide entirely without a key.
    if has_key:
        qdiff_section = f"""
  <section class="ss-section">
    {section_head("03", "QUESTION DIFFICULTY")}
    <div class="ss-qdiff" id="ss-qdiff">
      {"".join(qdiff_row(q, p['question_prompts'].get(q, q), p['answer_key'][q], p['q_accuracy'][q], p['n']) for q in p['answer_key'])}
    </div>
  </section>"""
    else:
        qdiff_section = ""

    body = f"""
<header class="ss-header">
  <div class="ss-header-stroke"></div>
  <div class="ss-header-inner">
    <div class="ss-header-title">
      <div class="ss-eyebrow">
        <a href="../master.html" class="ss-back">◂ MASTER</a>
        <span class="ss-eyebrow-sep">·</span>
        <span>STRATOSYN · ASSIGNMENT GRADEBOOK</span>
      </div>
      <h1>{esc(p['lesson'])}</h1>
      <div class="ss-meta">
        <span>{esc(p['period'])}</span><span class="dot"></span>
        <span>{esc(p['teacher'])}</span><span class="dot"></span>
        <span>{esc(p['room'])}</span><span class="dot"></span>
        <span>{esc(p['date'])}</span>
      </div>
    </div>
    <div class="ss-header-mark"><div class="mark-3x3">{mark_3x3()}</div></div>
  </div>
  {period_strip(p['periods'], p['n'])}
</header>

<main class="ss-main">
{no_key_banner}
  <section class="ss-section">
    {section_head("01", "CLASS PULSE", toolbar=persist_toolbar())}
    <div class="ss-stat-grid" id="ss-stat-grid">
      {stat_card("STUDENTS", str(p['n']), "submitted", "cyan")}
      {mc_card}
      {stat_card("AVG GAMES (FIRST-TRY)", f"{p['avg_games']}%", f"{round(p['avg_games']*18/100)}/18 placements", "gold")}
      {composite_card}
      {stat_card("AVG TIME ON TASK", fmt_seconds(p['avg_time_s']), f"median {fmt_seconds(p['median_time_s'])}", "purple")}
      {stat_card("FINAL GRADES", "<span id='ss-final-graded'>0</span>/" + str(p['n']), "<span id='ss-final-status'>none entered</span>", "cyan", value_html=True, sub_html=True)}
    </div>
  </section>

  <section class="ss-section">
    {section_head("02", "SCORE DISTRIBUTION")}
    <div class="ss-dist-grid" id="ss-dist-grid">
      {"".join(dist_cell(label, rng, color,
          p['bucket_breakdowns'][b]['qualified'],
          p['bucket_breakdowns'][b]['almost'],
          p['bucket_breakdowns'][b]['far'],
          p['n'])
        for b, label, rng, color in [
          ("perfect", "PERFECT", "95–100%", "green"),
          ("strong", "STRONG", "85–94%", "cyan"),
          ("passing", "PASSING", "70–84%", "gold"),
          ("shaky", "SHAKY", "50–69%", "purple"),
          ("needs_help", "NEEDS HELP", "&lt; 50%", "red"),
        ])}
    </div>
  </section>
{qdiff_section}

  <section class="ss-section">
    {section_head("04", "GRADEBOOK", toolbar=table_toolbar())}
    <div class="ss-legend">
      <span class="lg"><span class="sq lg filled green"></span> correct</span>
      <span class="lg"><span class="sq lg filled red"></span> incorrect</span>
      <span class="lg"><span class="sq lg outline"></span> first-try miss</span>
      <span class="lg"><span class="sq lg filled pearl"></span> answer changed</span>
      <span class="lg-sep"></span>
      <span class="lg"><i class="status-chip s-graded">GRADED</i></span>
      <span class="lg"><i class="status-chip s-review">REVIEW</i></span>
      <span class="lg"><i class="status-chip s-flagged">FLAGGED</i></span>
      <span class="lg-sep"></span>
      <span class="lg lg-hint">click student row → expand · essay rubric is clickable · final-grade box is editable</span>
    </div>

    <div class="ss-table-wrap">
      <table class="ss-table ss-table-edit">
        <thead>
          <tr>
            <th class="col-rank">#</th>
            <th class="col-student">STUDENT</th>
            <th class="col-mc">MC · 8</th>
            <th class="col-num">SCORE</th>
            <th class="col-game">GAME 1<br><span class="th-sub">Order · 6</span></th>
            <th class="col-game">GAME 2<br><span class="th-sub">Family · 6</span></th>
            <th class="col-game">GAME 3<br><span class="th-sub">Sort · 6</span></th>
            <th class="col-num">FIRST-TRY</th>
            <th class="col-num">AUTO</th>
            <th class="col-rubric">ESSAY<br><span class="th-sub">rubric 0–4</span></th>
            <th class="col-final">FINAL<br><span class="th-sub">teacher</span></th>
            <th class="col-status">STATUS</th>
            <th class="col-time">TIME</th>
          </tr>
        </thead>
        <tbody id="ss-tbody"></tbody>
      </table>
    </div>
  </section>

  <section class="ss-section">
    {section_head("05", "ESSAY RESPONSES")}
    <div class="ss-legend">
      <span class="lg lg-hint">click 1–4 squares to score the essay · scores sync with the gradebook table above</span>
    </div>
    <div id="ss-essays" class="ss-essay-grid"></div>
  </section>

  <footer class="ss-footer">
    <span>Stratosyn · {esc(p['date'])} · {esc(p['period'])} · {p['n']} submissions</span>
  </footer>

  {save_toast()}
</main>
"""
    data_js = json.dumps({"assignment": p, "assignment_id": p["id"]})
    return render_shell(
        title=f"Stratosyn Gradebook · {p['lesson']}",
        body=body,
        scripts=[SHARED_JS, ASSIGNMENT_JS],
        data_var="STRATO",
        data_json=data_js,
    )


def render_master(p: dict) -> str:
    n_students = len(p["roster"])
    n_assignments = len(p["assignments"])

    body = f"""
<header class="ss-header">
  <div class="ss-header-stroke"></div>
  <div class="ss-header-inner">
    <div class="ss-header-title">
      <div class="ss-eyebrow">STRATOSYN · MASTER GRADEBOOK</div>
      <h1>{esc(p['period'])} · {esc(p['teacher'])}</h1>
      <div class="ss-meta">
        <span>{esc(p['room'])}</span><span class="dot"></span>
        <span>{n_students} students</span><span class="dot"></span>
        <span>{n_assignments} {"assignment" if n_assignments == 1 else "assignments"}</span>
      </div>
    </div>
    <div class="ss-header-mark"><div class="mark-3x3">{mark_3x3()}</div></div>
  </div>
  {period_strip(p['periods'], n_students)}
</header>

<main class="ss-main">

  <section class="ss-section">
    {section_head("01", "CLASS PULSE", toolbar=persist_toolbar())}
    <div class="ss-stat-grid" id="ss-master-stats"></div>
  </section>

  <section class="ss-section">
    {section_head("02", "ASSIGNMENTS")}
    <div class="ss-assignment-strip" id="ss-assignment-strip"></div>
  </section>

  <section class="ss-section">
    {section_head("04", "GRADEBOOK", toolbar=master_table_toolbar())}
    <div class="ss-legend">
      <span class="lg"><span class="sq lg filled green"></span> 95–100</span>
      <span class="lg"><span class="sq lg filled cyan"></span> 85–94</span>
      <span class="lg"><span class="sq lg filled gold"></span> 70–84</span>
      <span class="lg"><span class="sq lg filled purple"></span> 50–69</span>
      <span class="lg"><span class="sq lg filled red"></span> &lt; 50</span>
      <span class="lg-sep"></span>
      <span class="lg lg-hint">click any cell → expand inline detail · open icon → full assignment view</span>
    </div>
    <div class="ss-table-wrap">
      <table class="ss-table ss-table-master">
        <thead id="ss-master-thead"></thead>
        <tbody id="ss-master-tbody"></tbody>
        <tfoot id="ss-master-tfoot"></tfoot>
      </table>
    </div>
  </section>

  <footer class="ss-footer">
    <span>Stratosyn · MASTER · {esc(p['period'])} · {n_students} students · {n_assignments} {"assignment" if n_assignments == 1 else "assignments"}</span>
  </footer>

  {save_toast()}
</main>
"""
    data_js = json.dumps(p)
    return render_shell(
        title=f"Stratosyn Master Gradebook · {p['period']}",
        body=body,
        scripts=[SHARED_JS, MASTER_JS],
        data_var="STRATO_MASTER",
        data_json=data_js,
    )


# ─────────────────────────────────────────────────────────────────────
# RENDER HELPERS
# ─────────────────────────────────────────────────────────────────────

def render_shell(title, body, scripts, data_var, data_json):
    js_init = f"window.{data_var} = {data_json};"
    js_blob = "\n".join(scripts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@500;600;700;800&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{STRATOSYN_CSS}</style>
</head>
<body>
{body}
<script>
{js_init}
{js_blob}
</script>
</body>
</html>"""


def section_head(num, title, toolbar=""):
    return f"""<div class="ss-section-head">
  <span class="ss-section-num">{num}</span>
  <h2>{title}</h2>
  <span class="ss-section-rule"></span>
  {toolbar}
</div>"""


def persist_toolbar():
    return """<div class="ss-toolbar">
  <span id="ss-save-indicator" class="ss-save-indicator">SAVED</span>
  <button class="ss-btn" data-action="export">EXPORT JSON</button>
  <button class="ss-btn" data-action="import">IMPORT JSON</button>
  <button class="ss-btn ss-btn-danger" data-action="reset">RESET EDITS</button>
  <input type="file" id="ss-import-file" accept="application/json" style="display:none">
</div>"""


def table_toolbar():
    return """<div class="ss-toolbar">
  <input id="ss-search" type="text" placeholder="Search students…" autocomplete="off">
  <select id="ss-sort">
    <option value="last">Sort: last name</option>
    <option value="final_desc">Sort: final ↓</option>
    <option value="composite_desc">Sort: auto ↓</option>
    <option value="composite_asc">Sort: auto ↑</option>
    <option value="status">Sort: status</option>
    <option value="time_desc">Sort: time ↓</option>
    <option value="time_asc">Sort: time ↑</option>
  </select>
  <select id="ss-filter">
    <option value="">Filter: all</option>
    <option value="needs_grading">Needs grading (no rubric/final)</option>
    <option value="status_review">Status: review</option>
    <option value="status_flagged">Status: flagged</option>
    <option value="status_graded">Status: graded</option>
    <option value="status_none">Status: none</option>
  </select>
</div>"""


def master_table_toolbar():
    return """<div class="ss-toolbar">
  <input id="ss-search" type="text" placeholder="Search students…" autocomplete="off">
  <select id="ss-sort">
    <option value="last">Sort: last name</option>
    <option value="avg_desc">Sort: running avg ↓</option>
    <option value="avg_asc">Sort: running avg ↑</option>
  </select>
</div>"""


def mark_3x3():
    cells = ["filled gold", "filled cyan", "filled pearl",
             "filled green", "outline", "filled red",
             "filled purple", "filled cyan", "filled gold"]
    return "".join(f'<div class="mark-cell {c}"></div>' for c in cells)


def stat_card(label, value, sub, color, value_html=False, sub_html=False):
    val = value if value_html else esc(value)
    sb = sub if sub_html else esc(sub)
    return f"""<div class="ss-stat" data-color="{color}">
  <div class="ss-stat-label">{label}</div>
  <div class="ss-stat-value">{val}</div>
  <div class="ss-stat-sub">{sb}</div>
  <div class="ss-stat-mark"></div>
</div>"""


def dist_cell(label, range_label, color, qualified, almost, far, total):
    """Each distribution card now shows the entire scoped class as 1 square per
    student, color-coded: green = qualified for this bucket, cyan = one band
    away (almost), red = two or more bands away (far)."""
    pct = round(100 * qualified / total) if total else 0
    fill = (
        '<span class="sq md filled green" title="qualified"></span>' * qualified
        + '<span class="sq md filled cyan" title="one bucket away"></span>' * almost
        + '<span class="sq md filled red" title="two or more buckets away"></span>' * far
    )
    return f"""<div class="ss-dist-card" data-color="{color}">
  <div class="dist-head">
    <div class="dist-label">{label}</div>
    <div class="dist-range">{range_label}</div>
  </div>
  <div class="dist-value"><span class="big">{qualified}</span><span class="of">/{total}</span></div>
  <div class="dist-cells dist-cells-md" data-color="{color}">{fill}</div>
  <div class="dist-bar"><div class="dist-bar-fill" style="width:{pct}%"></div></div>
  <div class="dist-breakdown">
    <span class="bd-q"><i class="bd-dot bd-green"></i>{qualified} in</span>
    <span class="bd-a"><i class="bd-dot bd-cyan"></i>{almost} close</span>
    <span class="bd-f"><i class="bd-dot bd-red"></i>{far} far</span>
    <span class="dist-pct">{pct}%</span>
  </div>
</div>"""


def qdiff_row(qid, prompt, key, acc, total):
    correct = acc["correct"]
    wrong = total - correct
    pct = acc["pct"]
    # Row accent (left stripe + numeric color): bucket-based gradient so
    # "this question was hard for the class" reads at a glance.
    accent = "green" if pct >= 90 else "gold" if pct >= 75 else "red"
    # Cell colors: always green=correct, red=wrong — independent of accent.
    # Previously correct cells used the bucket color, so a red-bucketed row
    # rendered every cell red and you couldn't tell who got it right.
    fill = '<span class="sq md filled green"></span>' * correct + \
           '<span class="sq md filled red"></span>' * wrong
    return f"""<div class="qdiff-row" data-color="{accent}">
  <div class="qdiff-id">{qid}</div>
  <div class="qdiff-key">KEY · {key}</div>
  <div class="qdiff-prompt">{esc(prompt)}</div>
  <div class="qdiff-cells">{fill}</div>
  <div class="qdiff-num"><span class="big">{correct}</span><span class="of">/{total}</span><div class="qdiff-wrong">{wrong} wrong</div></div>
  <div class="qdiff-pct">{pct}%</div>
</div>"""


def save_toast():
    return """<div class="ss-toast" id="ss-toast" aria-live="polite"></div>"""


def period_strip(periods, total_n):
    """Renders the period chip selector. ALL chip on the left, then per-period chips."""
    chips = [
        f'<span class="period-chip is-active" data-period="all" data-color="pearl" title="All students">'
        f'<span class="pc-label">ALL</span><span class="count">{total_n}</span>'
        f'</span>'
    ]
    for p in periods:
        hon_tag = '<span class="hon-tag">HON</span>' if p["is_honors"] else ''
        stray = '<span class="stray-tag" title="few students — verify">·</span>' if p["is_stray"] else ''
        chips.append(
            f'<span class="period-chip" data-period="{esc(p["key"])}" data-color="{p["color"]}" '
            f'title="{esc(p.get("section",""))} period {p.get("period_num","?")}">'
            f'<span class="pc-label">{esc(p["label"])}</span>{hon_tag}{stray}<span class="count">{p["count"]}</span>'
            f'</span>'
        )
    return f'<div class="ss-period-strip" id="ss-period-strip">{"".join(chips)}</div>'


# ─────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────

STRATOSYN_CSS = r"""
:root{
  --void:#06080f;
  --void-2:#0a0e1a;
  --void-3:#11162a;
  --void-4:#161c30;
  --line:rgba(184,207,255,.10);
  --line-2:rgba(184,207,255,.18);
  --ink:#e9edf7;
  --ink-2:#b8cfff;
  --ink-3:#8995b8;
  --gold:#f5c842;
  --cyan:#00e5ff;
  --pearl:#b8cfff;
  --green:#3effa0;
  --red:#f87171;
  --purple:#a855f7;
  --display:'Syne',ui-sans-serif,system-ui,sans-serif;
  --mono:'DM Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
}

*{box-sizing:border-box;margin:0;padding:0}
html,body{background:var(--void);color:var(--ink);font-family:var(--mono);font-size:13px;line-height:1.5}
body{
  min-height:100vh;
  background:
    radial-gradient(1200px 600px at 15% -10%,rgba(0,229,255,.06),transparent 60%),
    radial-gradient(900px 500px at 95% 5%,rgba(245,200,66,.05),transparent 60%),
    radial-gradient(800px 600px at 50% 110%,rgba(168,85,247,.05),transparent 60%),
    var(--void);
  background-attachment:fixed;
}
button{font-family:inherit;cursor:pointer;border:none;background:transparent;color:inherit}

/* ───── HEADER ───── */
.ss-header{
  position:relative;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,rgba(10,14,26,.9),rgba(6,8,15,.5));
  backdrop-filter:blur(10px);
}
.ss-header-stroke{
  position:absolute;left:0;right:0;top:0;height:2px;
  background:linear-gradient(90deg,var(--cyan),var(--gold),var(--green),var(--purple),var(--cyan));
  background-size:300% 100%;
  animation:ss-stroke-flow 8s linear infinite;
}
.ss-header-inner{
  display:flex;align-items:center;justify-content:space-between;gap:32px;
  padding:36px 48px 32px;max-width:1700px;margin:0 auto;
}
.ss-eyebrow{
  font-family:var(--mono);font-size:11px;letter-spacing:.32em;color:var(--ink-3);
  margin-bottom:14px;display:flex;align-items:center;gap:10px;
}
.ss-eyebrow-sep{opacity:.5}
.ss-back{
  color:var(--cyan);text-decoration:none;letter-spacing:.32em;
  border:1px solid var(--line-2);padding:4px 10px;border-radius:4px;
  transition:all .15s ease;
}
.ss-back:hover{border-color:var(--cyan);background:rgba(0,229,255,.06)}
.ss-header-title h1{
  font-family:var(--display);font-weight:700;font-size:32px;letter-spacing:-.01em;line-height:1.15;
  background:linear-gradient(90deg,#fff 0%,var(--cyan) 35%,var(--gold) 70%,#fff 100%);
  background-size:200% 100%;
  -webkit-background-clip:text;background-clip:text;color:transparent;
  animation:ss-title-flow 12s ease-in-out infinite;
}
.ss-meta{
  display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:14px;
  font-family:var(--mono);font-size:12px;color:var(--ink-2);
}
.ss-meta .dot{width:4px;height:4px;background:var(--ink-3);border-radius:1px}

.ss-header-mark{padding:6px}
.mark-3x3{display:grid;grid-template-columns:repeat(3,18px);grid-template-rows:repeat(3,18px);gap:5px}
.mark-cell{border-radius:3px;background:var(--void-3);border:1px solid var(--line)}
.mark-cell.filled.gold{background:var(--gold);border-color:var(--gold)}
.mark-cell.filled.cyan{background:var(--cyan);border-color:var(--cyan)}
.mark-cell.filled.pearl{background:var(--pearl);border-color:var(--pearl)}
.mark-cell.filled.green{background:var(--green);border-color:var(--green)}
.mark-cell.filled.red{background:var(--red);border-color:var(--red)}
.mark-cell.filled.purple{background:var(--purple);border-color:var(--purple)}
.mark-cell.outline{background:transparent;border-color:var(--ink-2)}

/* ───── PERIOD STRIP ───── */
.ss-period-strip{
  display:flex;gap:8px;flex-wrap:wrap;align-items:center;
  max-width:1700px;margin:0 auto;padding:18px 48px 0;
}
.period-chip{
  display:inline-flex;align-items:center;gap:8px;
  padding:8px 14px;border-radius:6px;
  background:var(--void-2);border:1px solid var(--line-2);
  font-family:var(--mono);font-size:11px;letter-spacing:.18em;color:var(--ink-2);
  cursor:pointer;transition:all .12s ease;user-select:none;position:relative;
}
.period-chip:hover{border-color:var(--accent);color:var(--ink);transform:translateY(-1px)}
.period-chip.is-active{
  background:linear-gradient(180deg,var(--accent),color-mix(in srgb,var(--accent) 70%,var(--void)));
  color:var(--void);border-color:var(--accent);font-weight:600;
}
.period-chip[data-color="cyan"]{--accent:var(--cyan)}
.period-chip[data-color="gold"]{--accent:var(--gold)}
.period-chip[data-color="green"]{--accent:var(--green)}
.period-chip[data-color="purple"]{--accent:var(--purple)}
.period-chip[data-color="red"]{--accent:var(--red)}
.period-chip[data-color="pearl"]{--accent:var(--pearl)}
.period-chip .pc-label{font-weight:500;letter-spacing:.22em}
.period-chip.is-active .pc-label{font-weight:700}
.period-chip .count{
  font-size:10px;font-family:var(--mono);
  padding:2px 6px;border-radius:3px;
  background:rgba(255,255,255,.06);color:inherit;
  min-width:20px;text-align:center;
}
.period-chip.is-active .count{background:rgba(0,0,0,.18);color:var(--void)}
.period-chip .hon-tag{
  font-size:8px;letter-spacing:.18em;padding:2px 4px;
  background:rgba(0,229,255,.15);color:var(--cyan);border:1px solid var(--cyan);
  border-radius:3px;font-weight:600;
}
.period-chip.is-active .hon-tag{background:var(--void);color:var(--cyan);border-color:var(--void)}
.period-chip .stray-tag{
  width:6px;height:6px;background:var(--gold);border-radius:1px;
}

/* ───── MAIN ───── */
.ss-main{max-width:1700px;margin:0 auto;padding:32px 48px 80px}
.ss-section{margin-top:48px}
.ss-section:first-child{margin-top:32px}
.ss-section-head{display:flex;align-items:center;gap:16px;margin-bottom:20px}
.ss-section-num{
  font-family:var(--mono);font-size:11px;letter-spacing:.2em;color:var(--ink-3);
  border:1px solid var(--line-2);padding:6px 10px;border-radius:4px;
}
.ss-section-head h2{font-family:var(--display);font-weight:600;font-size:18px;letter-spacing:.18em;color:var(--ink)}
.ss-section-rule{flex:1;height:1px;background:linear-gradient(90deg,var(--line-2),transparent)}

/* ───── TOOLBAR ───── */
.ss-toolbar{display:flex;gap:10px;align-items:center}
.ss-toolbar input,.ss-toolbar select{
  background:var(--void-2);color:var(--ink);
  border:1px solid var(--line-2);border-radius:6px;
  padding:8px 12px;font-family:var(--mono);font-size:12px;outline:none;
}
.ss-toolbar input{min-width:200px}
.ss-toolbar input:focus,.ss-toolbar select:focus{border-color:var(--cyan)}

.ss-btn{
  background:var(--void-2);color:var(--ink-2);
  border:1px solid var(--line-2);border-radius:6px;
  padding:8px 14px;font-family:var(--mono);font-size:11px;letter-spacing:.18em;
  transition:all .15s ease;
}
.ss-btn:hover{border-color:var(--cyan);color:var(--ink);background:var(--void-3)}
.ss-btn-danger:hover{border-color:var(--red);color:var(--red)}

.ss-save-indicator{
  font-family:var(--mono);font-size:10px;letter-spacing:.22em;color:var(--ink-3);
  padding:6px 10px;border:1px solid var(--line);border-radius:4px;
  display:inline-flex;align-items:center;gap:6px;
}
.ss-save-indicator::before{
  content:"";width:6px;height:6px;background:var(--ink-3);border-radius:1px;
}
.ss-save-indicator.is-saved::before{background:var(--green)}
.ss-save-indicator.is-saved{color:var(--green);border-color:rgba(62,255,160,.3)}
.ss-save-indicator.is-dirty::before{background:var(--gold);animation:ss-pulse 1.2s ease-in-out infinite}
.ss-save-indicator.is-dirty{color:var(--gold);border-color:rgba(245,200,66,.3)}

/* ───── STATS ───── */
.ss-stat-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:16px}
.ss-stat{
  position:relative;background:var(--void-2);border:1px solid var(--line-2);border-radius:8px;
  padding:18px;min-height:140px;
  display:flex;flex-direction:column;justify-content:space-between;overflow:hidden;
}
.ss-stat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
.ss-stat[data-color="cyan"]{--accent:var(--cyan)}
.ss-stat[data-color="gold"]{--accent:var(--gold)}
.ss-stat[data-color="pearl"]{--accent:var(--pearl)}
.ss-stat[data-color="green"]{--accent:var(--green)}
.ss-stat[data-color="red"]{--accent:var(--red)}
.ss-stat[data-color="purple"]{--accent:var(--purple)}
.ss-stat-label{font-family:var(--mono);font-size:10px;letter-spacing:.22em;color:var(--ink-3)}
.ss-stat-value{
  font-family:var(--display);font-weight:700;font-size:36px;line-height:1;color:var(--accent);margin:8px 0;
}
.ss-stat-sub{font-family:var(--mono);font-size:11px;color:var(--ink-2)}
.ss-stat-mark{position:absolute;right:14px;top:14px;width:14px;height:14px;background:var(--accent);border-radius:3px;opacity:.85}

/* ───── DISTRIBUTION ───── */
.ss-dist-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:16px}
.ss-dist-card{
  background:var(--void-2);border:1px solid var(--line-2);border-radius:8px;
  padding:18px;display:flex;flex-direction:column;gap:14px;position:relative;overflow:hidden;
}
.ss-dist-card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent)}
.ss-dist-card[data-color="cyan"]{--accent:var(--cyan)}
.ss-dist-card[data-color="gold"]{--accent:var(--gold)}
.ss-dist-card[data-color="pearl"]{--accent:var(--pearl)}
.ss-dist-card[data-color="green"]{--accent:var(--green)}
.ss-dist-card[data-color="red"]{--accent:var(--red)}
.ss-dist-card[data-color="purple"]{--accent:var(--purple)}
.dist-head{display:flex;justify-content:space-between;align-items:baseline}
.dist-label{font-family:var(--mono);font-size:11px;letter-spacing:.22em;color:var(--ink)}
.dist-range{font-family:var(--mono);font-size:10px;color:var(--ink-3)}
.dist-value{font-family:var(--display);font-weight:700;color:var(--accent);display:flex;align-items:baseline;gap:2px}
.dist-value .big{font-size:36px;line-height:1}
.dist-value .of{font-size:14px;color:var(--ink-3)}
.dist-cells{display:flex;flex-wrap:wrap;gap:3px;font-size:0;line-height:0}
.dist-cells-md{
  /* Each card reserves the same vertical space so the strip stays aligned
     even when one bucket has 4 students and another has 47.
     Min-height fits ~3 rows of 14px squares + 3px gaps. */
  align-content:flex-start;min-height:54px;
}
.dist-bar{
  height:6px;background:var(--void-3);border:1px solid var(--line);
  border-radius:3px;overflow:hidden;margin-top:4px;
}
.dist-bar-fill{
  height:100%;background:var(--accent);
  border-radius:2px;transition:width .25s ease;
}
.dist-pct{font-family:var(--mono);font-size:11px;color:var(--ink-2);align-self:flex-end}
.dist-breakdown{
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  font-family:var(--mono);font-size:10px;color:var(--ink-3);letter-spacing:.12em;
  margin-top:2px;
}
.dist-breakdown .bd-q,.dist-breakdown .bd-a,.dist-breakdown .bd-f{
  display:inline-flex;align-items:center;gap:5px;
}
.dist-breakdown .bd-q{color:var(--ink-2)}
.dist-breakdown .dist-pct{margin-left:auto;color:var(--accent);font-weight:500}
.bd-dot{
  display:inline-block;width:8px;height:8px;border-radius:2px;
  flex:0 0 auto;
}
.bd-green{background:var(--green)}
.bd-cyan{background:var(--cyan)}
.bd-red{background:var(--red)}

/* ───── SQUARES ─────
   font-size:0 + line-height:0 on the element itself defeats inherited
   text metrics that can make empty inline-block <i> tags collapse to 0
   width on Chrome/Safari when nested in a flex container.
   flex:0 0 auto pins the size against any flex-shrink. */
.sq{
  display:inline-block;
  width:10px;height:10px;min-width:10px;min-height:10px;
  border-radius:2px;background:var(--void-3);border:1px solid var(--line);
  vertical-align:middle;font-size:0;line-height:0;font-style:normal;
  flex:0 0 auto;box-sizing:border-box;
}
.sq.filled.gold{background:var(--gold);border-color:var(--gold)}
.sq.filled.cyan{background:var(--cyan);border-color:var(--cyan)}
.sq.filled.pearl{background:var(--pearl);border-color:var(--pearl)}
.sq.filled.green{background:var(--green);border-color:var(--green)}
.sq.filled.red{background:var(--red);border-color:var(--red)}
.sq.filled.purple{background:var(--purple);border-color:var(--purple)}
.sq.outline{background:transparent;border-color:var(--ink-2)}
.sq.dim{background:var(--void-3);border-color:var(--line)}
.sq.md{width:14px;height:14px;min-width:14px;min-height:14px;border-radius:3px}
.sq.lg{width:14px;height:14px;min-width:14px;min-height:14px;border-radius:3px}

/* ───── QUESTION DIFFICULTY ───── */
.ss-qdiff{background:var(--void-2);border:1px solid var(--line-2);border-radius:8px;padding:8px}
.qdiff-row{
  display:grid;grid-template-columns:48px 80px 1fr auto 80px 60px;
  gap:14px;align-items:center;padding:12px 14px;border-radius:6px;
  border-left:3px solid var(--accent);margin:4px 0;background:rgba(255,255,255,.01);
}
.qdiff-row[data-color="green"]{--accent:var(--green)}
.qdiff-row[data-color="gold"]{--accent:var(--gold)}
.qdiff-row[data-color="red"]{--accent:var(--red)}
.qdiff-id{font-family:var(--display);font-weight:700;font-size:18px;color:var(--accent)}
.qdiff-key{font-family:var(--mono);font-size:10px;letter-spacing:.18em;color:var(--ink-3)}
.qdiff-prompt{font-family:var(--mono);font-size:12px;color:var(--ink-2)}
.qdiff-cells{display:flex;gap:4px;justify-content:flex-end;flex-wrap:wrap;max-width:420px;font-size:0;line-height:0;align-content:flex-start}
.qdiff-num{font-family:var(--display);font-weight:700;color:var(--accent);text-align:right}
.qdiff-num .big{font-size:22px}
.qdiff-num .of{font-size:11px;color:var(--ink-3)}
.qdiff-wrong{font-family:var(--mono);font-size:9px;font-weight:500;color:var(--red);letter-spacing:.18em;margin-top:2px;text-align:right}
.qdiff-pct{font-family:var(--mono);font-size:13px;color:var(--ink);text-align:right}

/* ───── LEGEND ───── */
.ss-legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px;
  font-family:var(--mono);font-size:11px;color:var(--ink-2);align-items:center;}
.ss-legend .lg{display:inline-flex;align-items:center;gap:6px}
.ss-legend .lg-sep{width:1px;height:14px;background:var(--line-2)}
.ss-legend .lg-hint{color:var(--ink-3);font-style:italic}

/* ───── TABLE ───── */
.ss-table-wrap{background:var(--void-2);border:1px solid var(--line-2);border-radius:8px;overflow:auto}
.ss-table{width:100%;border-collapse:separate;border-spacing:0;font-family:var(--mono);font-size:12px}
.ss-table th,.ss-table td{
  padding:12px 14px;text-align:left;vertical-align:middle;
  border-bottom:1px solid var(--line);white-space:nowrap;
}
.ss-table thead th{
  position:sticky;top:0;background:var(--void-3);
  font-size:10px;letter-spacing:.2em;color:var(--ink-3);font-weight:500;
  border-bottom:1px solid var(--line-2);z-index:2;
}
.ss-table thead th .th-sub{display:block;font-size:9px;letter-spacing:.16em;color:var(--ink-3);margin-top:2px;text-transform:none}

/* Tinted bucket rows */
.ss-table tbody tr.row-perfect{background:linear-gradient(90deg,rgba(62,255,160,.06),transparent 60%)}
.ss-table tbody tr.row-strong{background:linear-gradient(90deg,rgba(0,229,255,.05),transparent 60%)}
.ss-table tbody tr.row-passing{background:linear-gradient(90deg,rgba(245,200,66,.05),transparent 60%)}
.ss-table tbody tr.row-shaky{background:linear-gradient(90deg,rgba(168,85,247,.06),transparent 60%)}
.ss-table tbody tr.row-needs_help{background:linear-gradient(90deg,rgba(248,113,113,.07),transparent 60%)}
.ss-table tbody tr.row-perfect{box-shadow:inset 3px 0 0 var(--green)}
.ss-table tbody tr.row-strong{box-shadow:inset 3px 0 0 var(--cyan)}
.ss-table tbody tr.row-passing{box-shadow:inset 3px 0 0 var(--gold)}
.ss-table tbody tr.row-shaky{box-shadow:inset 3px 0 0 var(--purple)}
.ss-table tbody tr.row-needs_help{box-shadow:inset 3px 0 0 var(--red)}
.ss-table tbody tr.is-graded{background:linear-gradient(90deg,rgba(62,255,160,.10),rgba(62,255,160,.02) 60%)!important}
.ss-table tbody tr.is-review{background:linear-gradient(90deg,rgba(245,200,66,.10),rgba(245,200,66,.02) 60%)!important}
.ss-table tbody tr.is-flagged{background:linear-gradient(90deg,rgba(248,113,113,.12),rgba(248,113,113,.02) 60%)!important}
.ss-table tbody tr:hover{background-color:rgba(0,229,255,.04)!important}
.ss-table tbody tr.is-expanded{background-color:rgba(0,229,255,.06)!important}

.col-rank{color:var(--ink-3);width:42px;text-align:right!important}
.col-student{min-width:200px;cursor:pointer}
.col-num{text-align:right!important;font-variant-numeric:tabular-nums;font-weight:500}
.col-mc,.col-game{padding-right:6px}
.col-time{text-align:right!important;color:var(--ink-2)}
.col-rubric{text-align:center!important}
.col-final{text-align:center!important;width:90px}
.col-status{text-align:center!important}

.student-name{
  font-family:var(--display);font-weight:600;font-size:14px;color:var(--ink);letter-spacing:.01em;
  display:inline-flex;align-items:center;gap:8px;
}
.student-name .twirl{
  display:inline-block;width:12px;height:12px;color:var(--ink-3);font-size:10px;
  transition:transform .15s ease;
}
.is-expanded .student-name .twirl{transform:rotate(90deg);color:var(--cyan)}
.student-sub{font-family:var(--mono);font-size:10px;color:var(--ink-3);margin-top:2px}

.cell-row{display:inline-flex;gap:3px;align-items:center}
.cell-row .gap{display:inline-block;width:6px}
.score-pill{
  display:inline-block;min-width:48px;text-align:center;
  font-family:var(--mono);font-size:12px;font-weight:500;
  padding:4px 8px;border-radius:4px;
  background:var(--void-3);border:1px solid var(--line-2);color:var(--ink);
}
.score-pill.s-perfect{color:var(--green);border-color:var(--green)}
.score-pill.s-strong{color:var(--cyan);border-color:var(--cyan)}
.score-pill.s-passing{color:var(--gold);border-color:var(--gold)}
.score-pill.s-shaky{color:var(--purple);border-color:var(--purple)}
.score-pill.s-needs_help{color:var(--red);border-color:var(--red)}

.composite-big{font-family:var(--display);font-weight:700;font-size:18px}
.composite-big.s-perfect{color:var(--green)}
.composite-big.s-strong{color:var(--cyan)}
.composite-big.s-passing{color:var(--gold)}
.composite-big.s-shaky{color:var(--purple)}
.composite-big.s-needs_help{color:var(--red)}

/* Rubric squares (clickable 1–4) */
.rubric{display:inline-flex;gap:4px;align-items:center}
.rubric-sq{
  width:18px;height:18px;border-radius:3px;
  background:var(--void-3);border:1px solid var(--line-2);
  cursor:pointer;transition:all .12s ease;display:inline-block;
}
.rubric-sq:hover{border-color:var(--gold);transform:translateY(-1px)}
.rubric-sq.is-on{background:var(--gold);border-color:var(--gold)}
.rubric-sq.is-on.s-1{background:var(--red);border-color:var(--red)}
.rubric-sq.is-on.s-2{background:var(--purple);border-color:var(--purple)}
.rubric-sq.is-on.s-3{background:var(--gold);border-color:var(--gold)}
.rubric-sq.is-on.s-4{background:var(--green);border-color:var(--green)}
.rubric-clear{
  font-family:var(--mono);font-size:9px;color:var(--ink-3);margin-left:6px;cursor:pointer;
  letter-spacing:.18em;border:1px solid transparent;padding:2px 4px;border-radius:3px;
}
.rubric-clear:hover{color:var(--red);border-color:var(--red)}

/* Final-grade input */
.final-input{
  width:64px;text-align:center;
  background:var(--void-3);color:var(--ink);
  border:1px solid var(--line-2);border-radius:4px;
  padding:6px 4px;font-family:var(--mono);font-size:13px;font-weight:500;outline:none;
  font-variant-numeric:tabular-nums;
}
.final-input:focus{border-color:var(--cyan);background:var(--void-4)}
.final-input.is-override{border-color:var(--gold);color:var(--gold)}
.final-input::placeholder{color:var(--ink-3);font-weight:400}

/* Status chips (cyclable) */
.status-chip{
  display:inline-block;min-width:64px;text-align:center;cursor:pointer;
  font-family:var(--mono);font-size:9px;letter-spacing:.18em;font-weight:500;
  padding:4px 8px;border-radius:4px;
  background:var(--void-3);border:1px solid var(--line-2);color:var(--ink-3);
  font-style:normal;transition:all .12s ease;user-select:none;
}
.status-chip:hover{border-color:var(--cyan);color:var(--ink)}
.status-chip.s-graded{background:rgba(62,255,160,.15);border-color:var(--green);color:var(--green)}
.status-chip.s-review{background:rgba(245,200,66,.15);border-color:var(--gold);color:var(--gold)}
.status-chip.s-flagged{background:rgba(248,113,113,.15);border-color:var(--red);color:var(--red)}

/* Expansion row */
.expand-row td{padding:0!important;background:var(--void-3);border-bottom:1px solid var(--line-2)}
.expand-inner{padding:24px 32px;display:grid;grid-template-columns:1fr 360px;gap:32px}
.expand-section h4{
  font-family:var(--mono);font-size:10px;letter-spacing:.22em;color:var(--ink-3);
  margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--line);
}
.expand-essay{
  font-family:var(--mono);font-size:13px;line-height:1.6;color:var(--ink);
  background:var(--void-2);border:1px solid var(--line);border-radius:6px;
  padding:14px 18px;white-space:pre-wrap;
}
.expand-essay.is-empty{color:var(--ink-3);font-style:italic}
.expand-meta{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.meta-cell{
  background:var(--void-2);border:1px solid var(--line);border-radius:6px;padding:10px 14px;
}
.meta-cell .ml{font-family:var(--mono);font-size:9px;letter-spacing:.22em;color:var(--ink-3)}
.meta-cell .mv{font-family:var(--display);font-weight:600;font-size:16px;color:var(--ink);margin-top:4px}
.meta-cell .ms{font-family:var(--mono);font-size:11px;color:var(--ink-2);margin-top:2px}

.section-times{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-top:14px}
.st-row{display:flex;justify-content:space-between;font-size:11px;color:var(--ink-2);
  padding:6px 10px;background:var(--void-2);border:1px solid var(--line);border-radius:4px}
.st-row .st-l{color:var(--ink-3)}

.notes-area{
  width:100%;min-height:120px;
  background:var(--void-2);color:var(--ink);
  border:1px solid var(--line-2);border-radius:6px;
  padding:10px 14px;font-family:var(--mono);font-size:12px;line-height:1.5;outline:none;resize:vertical;
}
.notes-area:focus{border-color:var(--cyan)}
.notes-hint{font-family:var(--mono);font-size:9px;letter-spacing:.18em;color:var(--ink-3);margin-top:6px}
.notes-hint b{color:var(--gold)}

.expand-actions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}

/* ───── ESSAY GRID ───── */
.ss-essay-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:16px}
.essay-card{
  background:var(--void-2);border:1px solid var(--line-2);border-radius:8px;
  padding:18px;position:relative;overflow:hidden;border-left:3px solid var(--accent);
}
.essay-card[data-color="green"]{--accent:var(--green)}
.essay-card[data-color="cyan"]{--accent:var(--cyan)}
.essay-card[data-color="gold"]{--accent:var(--gold)}
.essay-card[data-color="purple"]{--accent:var(--purple)}
.essay-card[data-color="red"]{--accent:var(--red)}
.essay-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;gap:10px}
.essay-name{font-family:var(--display);font-weight:600;font-size:15px;color:var(--ink)}
.essay-stats{font-family:var(--mono);font-size:10px;color:var(--ink-3);letter-spacing:.12em;text-align:right}
.essay-stats b{color:var(--accent);font-weight:500}
.essay-text{
  font-family:var(--mono);font-size:12px;line-height:1.6;color:var(--ink-2);
  white-space:pre-wrap;margin-bottom:14px;
}
.essay-empty{font-style:italic;color:var(--ink-3)}
.essay-rubric-bar{
  display:flex;justify-content:space-between;align-items:center;
  padding-top:12px;border-top:1px solid var(--line);
}
.essay-rubric-label{font-family:var(--mono);font-size:10px;letter-spacing:.22em;color:var(--ink-3)}

/* ───── ASSIGNMENT STRIP (master view) ───── */
.ss-assignment-strip{display:flex;gap:14px;flex-wrap:wrap}
.assignment-card{
  background:var(--void-2);border:1px solid var(--line-2);border-radius:8px;
  padding:18px;min-width:280px;flex:0 0 auto;position:relative;overflow:hidden;
  border-left:3px solid var(--accent);transition:all .15s ease;
}
.assignment-card[data-color="cyan"]{--accent:var(--cyan)}
.assignment-card[data-color="gold"]{--accent:var(--gold)}
.assignment-card[data-color="green"]{--accent:var(--green)}
.assignment-card[data-color="purple"]{--accent:var(--purple)}
.assignment-card.is-empty{
  border-style:dashed;border-color:var(--line);opacity:.55;
  --accent:var(--ink-3);
}
.assignment-card a.assignment-link{
  position:absolute;inset:0;text-decoration:none;color:transparent;z-index:1;
}
.assignment-card:hover{transform:translateY(-2px);border-color:var(--accent)}
.assignment-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:8px}
.assignment-title{font-family:var(--display);font-weight:600;font-size:15px;color:var(--ink)}
.assignment-meta{font-family:var(--mono);font-size:10px;color:var(--ink-3);letter-spacing:.18em}
.assignment-stats{display:flex;gap:18px;margin-top:10px}
.assignment-stat{display:flex;flex-direction:column;gap:2px}
.assignment-stat .as-l{font-family:var(--mono);font-size:9px;letter-spacing:.22em;color:var(--ink-3)}
.assignment-stat .as-v{font-family:var(--display);font-weight:700;font-size:20px;color:var(--accent)}
.assignment-mini{display:flex;gap:2px;flex-wrap:wrap;margin-top:12px;max-width:240px;font-size:0;line-height:0}
.assignment-mini i,.assignment-mini span{
  display:inline-block;width:8px;height:8px;min-width:8px;min-height:8px;
  border-radius:2px;flex:0 0 auto;box-sizing:border-box;font-style:normal;
}

/* ───── MASTER TABLE ───── */
.ss-table-master th.col-grade,.ss-table-master td.col-grade{
  text-align:center!important;width:120px;
}
.ss-table-master th.col-period,.ss-table-master td.col-period{
  text-align:center!important;width:80px;
}
.period-tag{
  display:inline-block;padding:3px 8px;border-radius:3px;
  font-family:var(--mono);font-size:10px;letter-spacing:.18em;font-weight:600;
  background:var(--accent);color:var(--void);
}
.period-tag[data-color="cyan"]{--accent:var(--cyan)}
.period-tag[data-color="gold"]{--accent:var(--gold)}
.period-tag[data-color="green"]{--accent:var(--green)}
.period-tag[data-color="purple"]{--accent:var(--purple)}
.period-tag[data-color="red"]{--accent:var(--red)}
.period-tag[data-color="pearl"]{--accent:var(--pearl)}
.ss-table-master th.col-student{position:sticky;left:0;z-index:3;background:var(--void-3)}
.ss-table-master td.col-student{position:sticky;left:0;background:var(--void-2);z-index:1}
.ss-table-master tbody tr.row-perfect td.col-student,
.ss-table-master tbody tr.row-strong td.col-student,
.ss-table-master tbody tr.row-passing td.col-student,
.ss-table-master tbody tr.row-shaky td.col-student,
.ss-table-master tbody tr.row-needs_help td.col-student{
  background:var(--void-2);
}
.grade-cell{
  display:inline-block;min-width:78px;text-align:center;cursor:pointer;
  padding:8px 10px;border-radius:4px;
  background:var(--void-3);border:1px solid var(--line-2);
  font-family:var(--display);font-weight:600;font-size:14px;color:var(--ink-2);
  transition:all .12s ease;
}
.grade-cell:hover{border-color:var(--cyan);transform:translateY(-1px)}
.grade-cell.s-perfect{color:var(--green);border-color:var(--green);background:rgba(62,255,160,.08)}
.grade-cell.s-strong{color:var(--cyan);border-color:var(--cyan);background:rgba(0,229,255,.08)}
.grade-cell.s-passing{color:var(--gold);border-color:var(--gold);background:rgba(245,200,66,.08)}
.grade-cell.s-shaky{color:var(--purple);border-color:var(--purple);background:rgba(168,85,247,.08)}
.grade-cell.s-needs_help{color:var(--red);border-color:var(--red);background:rgba(248,113,113,.10)}
.grade-cell.is-empty{color:var(--ink-3);background:var(--void-3);border-style:dashed;border-color:var(--line)}
.grade-cell .gc-tag{display:block;font-family:var(--mono);font-size:8px;letter-spacing:.18em;color:var(--ink-3);margin-top:2px;font-weight:400}
.grade-cell.is-override .gc-tag{color:var(--gold)}

.col-avg .grade-cell{cursor:default}
.col-status-cell{text-align:center!important}

.ss-table-master tfoot td{
  background:var(--void-3);border-top:2px solid var(--line-2);
  font-family:var(--mono);font-size:11px;letter-spacing:.18em;color:var(--ink-2);
  padding:14px;
}
.ss-table-master tfoot td.col-grade{font-family:var(--display);font-weight:700;font-size:14px;color:var(--ink);text-align:center}

.master-expand-inner{padding:24px 32px;display:grid;grid-template-columns:1fr 1fr 360px;gap:24px}

/* ───── SCALE CHIPS (master per-row mini) ───── */
.row-mini{display:inline-flex;gap:2px}
.row-mini i{width:8px;height:8px;border-radius:2px;background:var(--void-3);border:1px solid var(--line)}

/* ───── TOAST ───── */
.ss-toast{
  position:fixed;bottom:24px;right:24px;z-index:9999;
  background:var(--void-3);color:var(--ink);
  border:1px solid var(--cyan);border-radius:6px;
  padding:12px 18px;font-family:var(--mono);font-size:12px;
  opacity:0;transform:translateY(8px);transition:all .2s ease;pointer-events:none;
  max-width:380px;
}
.ss-toast.is-show{opacity:1;transform:translateY(0)}
.ss-toast.is-error{border-color:var(--red);color:var(--red)}

/* ───── FOOTER ───── */
.ss-footer{margin-top:60px;padding-top:24px;border-top:1px solid var(--line);
  text-align:center;font-family:var(--mono);font-size:10px;letter-spacing:.22em;color:var(--ink-3);}

/* ───── ANIMATIONS ───── */
@keyframes ss-stroke-flow{0%{background-position:0% 50%}100%{background-position:300% 50%}}
@keyframes ss-title-flow{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}
@keyframes ss-pulse{0%,100%{opacity:1}50%{opacity:.45}}
@media (prefers-reduced-motion:reduce){
  .ss-header-stroke,.ss-header-title h1,.ss-save-indicator.is-dirty::before{animation:none}
}

/* ───── RESPONSIVE ───── */
@media (max-width:1400px){
  .ss-stat-grid{grid-template-columns:repeat(3,1fr)}
}
@media (max-width:1100px){
  .expand-inner{grid-template-columns:1fr}
  .master-expand-inner{grid-template-columns:1fr}
}
@media (max-width:900px){
  .ss-header-inner{flex-direction:column;align-items:flex-start;padding:24px}
  .ss-main{padding:24px}
  .ss-stat-grid{grid-template-columns:repeat(2,1fr)}
  .ss-dist-grid{grid-template-columns:repeat(2,1fr)}
}
"""

# ─────────────────────────────────────────────────────────────────────
# JS — SHARED
# ─────────────────────────────────────────────────────────────────────

SHARED_JS = r"""
const SS = {
  STORAGE_KEY: 'stratosyn:gradebook:v1',

  bucket(pct){
    if(pct >= 95) return 'perfect';
    if(pct >= 85) return 'strong';
    if(pct >= 70) return 'passing';
    if(pct >= 50) return 'shaky';
    return 'needs_help';
  },
  bucketColor(b){
    return ({perfect:'green', strong:'cyan', passing:'gold', shaky:'purple', needs_help:'red'})[b];
  },
  fmtSeconds(s){
    if(s == null) return '—';
    if(s < 60) return s + 's';
    return Math.floor(s/60) + 'm ' + (s%60) + 's';
  },
  esc(s){
    return (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  },

  // ── Persistence ──
  loadStore(){
    try {
      const raw = localStorage.getItem(SS.STORAGE_KEY);
      if(!raw) return { assignments: {} };
      const parsed = JSON.parse(raw);
      if(!parsed.assignments) parsed.assignments = {};
      return parsed;
    } catch(e){
      console.warn('failed to load store', e);
      return { assignments: {} };
    }
  },
  saveStore(store){
    try {
      localStorage.setItem(SS.STORAGE_KEY, JSON.stringify(store));
      SS._setSavedIndicator();
      return true;
    } catch(e){
      console.warn('failed to save store', e);
      SS.toast('Could not save — storage may be full', 'error');
      return false;
    }
  },
  defaultEdit(){
    return { essay_rubric: 0, final_grade: null, notes: '', status: null, updated_at: null };
  },
  getEdit(assignmentId, studentId){
    const store = SS.loadStore();
    const a = store.assignments[assignmentId];
    if(!a || !a.grades || !a.grades[studentId]) return SS.defaultEdit();
    return Object.assign(SS.defaultEdit(), a.grades[studentId]);
  },
  getAllEdits(assignmentId){
    const store = SS.loadStore();
    const a = store.assignments[assignmentId];
    if(!a || !a.grades) return {};
    return a.grades;
  },
  setEdit(assignmentId, studentId, patch, meta){
    const store = SS.loadStore();
    if(!store.assignments[assignmentId]) store.assignments[assignmentId] = { meta: meta || {}, grades: {} };
    if(meta) store.assignments[assignmentId].meta = Object.assign(store.assignments[assignmentId].meta || {}, meta);
    const cur = store.assignments[assignmentId].grades[studentId] || SS.defaultEdit();
    const next = Object.assign({}, cur, patch, { updated_at: Date.now() });
    store.assignments[assignmentId].grades[studentId] = next;
    SS.saveStore(store);
    return next;
  },
  resetAssignmentEdits(assignmentId){
    const store = SS.loadStore();
    if(store.assignments[assignmentId]) {
      delete store.assignments[assignmentId];
      SS.saveStore(store);
    }
  },
  resetAllEdits(){
    localStorage.removeItem(SS.STORAGE_KEY);
    SS._setSavedIndicator(true);
  },

  // ── Final grade resolution ──
  // suggestion: composite by default; if rubric set, weight: 50% MC + 30% games + 20% essay.
  suggestedFinal(rec, edit){
    const s = rec.summary;
    if(edit && edit.essay_rubric > 0){
      const essayPct = (edit.essay_rubric / 4) * 100;
      return Math.round(0.5 * s.mc_pct + 0.3 * s.games_pct + 0.2 * essayPct);
    }
    return s.composite_pct;
  },
  effectiveFinal(rec, edit){
    if(edit && edit.final_grade != null && edit.final_grade !== '') return Number(edit.final_grade);
    return SS.suggestedFinal(rec, edit);
  },

  // ── UI helpers ──
  _setSavedIndicator(immediate){
    const el = document.getElementById('ss-save-indicator');
    if(!el) return;
    el.classList.remove('is-dirty');
    el.classList.add('is-saved');
    el.textContent = 'SAVED';
    clearTimeout(SS._savedT);
    if(!immediate) {
      SS._savedT = setTimeout(() => { el.classList.remove('is-saved'); el.textContent = 'IDLE'; }, 1800);
    }
  },
  _setDirtyIndicator(){
    const el = document.getElementById('ss-save-indicator');
    if(!el) return;
    el.classList.remove('is-saved');
    el.classList.add('is-dirty');
    el.textContent = 'SAVING…';
  },
  toast(msg, kind){
    const el = document.getElementById('ss-toast');
    if(!el) { console.log(msg); return; }
    el.textContent = msg;
    el.classList.remove('is-error');
    if(kind === 'error') el.classList.add('is-error');
    el.classList.add('is-show');
    clearTimeout(SS._toastT);
    SS._toastT = setTimeout(() => el.classList.remove('is-show'), 2400);
  },

  // ── Export / Import ──
  exportJSON(){
    const store = SS.loadStore();
    const blob = new Blob([JSON.stringify(store, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g,'-').slice(0,19);
    a.href = url;
    a.download = `stratosyn-gradebook-${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    SS.toast('Exported gradebook JSON');
  },
  importJSON(file, onDone){
    const reader = new FileReader();
    reader.onload = e => {
      try {
        const data = JSON.parse(e.target.result);
        if(!data.assignments) throw new Error('Invalid file — missing "assignments"');
        // Merge instead of replace
        const cur = SS.loadStore();
        Object.entries(data.assignments).forEach(([aid, a]) => {
          if(!cur.assignments[aid]) cur.assignments[aid] = { meta: a.meta || {}, grades: {} };
          if(a.meta) cur.assignments[aid].meta = Object.assign(cur.assignments[aid].meta || {}, a.meta);
          if(a.grades) cur.assignments[aid].grades = Object.assign(cur.assignments[aid].grades || {}, a.grades);
        });
        SS.saveStore(cur);
        SS.toast('Imported and merged successfully');
        if(onDone) onDone();
      } catch(err) {
        SS.toast('Import failed: ' + err.message, 'error');
      }
    };
    reader.readAsText(file);
  },

  // Wire the persistence toolbar (export/import/reset)
  wirePersistToolbar(opts){
    const onReset = (opts && opts.onReset) || (() => location.reload());
    document.querySelectorAll('[data-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        const a = btn.getAttribute('data-action');
        if(a === 'export') return SS.exportJSON();
        if(a === 'import') return document.getElementById('ss-import-file').click();
        if(a === 'reset'){
          if(!confirm('Reset all teacher edits (rubric, final, notes, status) for this view? This cannot be undone.')) return;
          if(opts && opts.onResetThis) opts.onResetThis(); else SS.resetAllEdits();
          onReset();
        }
      });
    });
    const f = document.getElementById('ss-import-file');
    if(f){
      f.addEventListener('change', e => {
        const file = e.target.files[0];
        if(file) SS.importJSON(file, onReset);
        f.value = '';
      });
    }
    SS._setSavedIndicator();
  },
};

// ── Period filter (shared) ──────────────────────────────────────
const PeriodFilter = {
  current: 'all',
  init(onChange){
    // Read URL hash
    const m = location.hash.match(/p=([^&]+)/);
    if(m) PeriodFilter.current = decodeURIComponent(m[1]);
    PeriodFilter._syncChips();
    document.getElementById('ss-period-strip')?.addEventListener('click', e => {
      const chip = e.target.closest('.period-chip');
      if(!chip) return;
      PeriodFilter.set(chip.getAttribute('data-period'), onChange);
    });
    window.addEventListener('hashchange', () => {
      const m2 = location.hash.match(/p=([^&]+)/);
      const next = m2 ? decodeURIComponent(m2[1]) : 'all';
      if(next !== PeriodFilter.current){
        PeriodFilter.current = next;
        PeriodFilter._syncChips();
        if(onChange) onChange();
      }
    });
  },
  set(period, onChange){
    PeriodFilter.current = period || 'all';
    if(period === 'all') history.replaceState(null, '', location.pathname + location.search);
    else history.replaceState(null, '', location.pathname + location.search + '#p=' + encodeURIComponent(period));
    PeriodFilter._syncChips();
    if(onChange) onChange();
  },
  _syncChips(){
    document.querySelectorAll('#ss-period-strip .period-chip').forEach(c => {
      c.classList.toggle('is-active', c.getAttribute('data-period') === PeriodFilter.current);
    });
  },
  matches(rec){
    if(PeriodFilter.current === 'all') return true;
    return rec.period_key === PeriodFilter.current;
  },
  matchesStudent(s){
    if(PeriodFilter.current === 'all') return true;
    return s.period_key === PeriodFilter.current;
  },
  // Look up the period's display label (used in subtitles).
  currentLabel(periods){
    if(PeriodFilter.current === 'all') return 'ALL PERIODS';
    const p = (periods || []).find(x => x.key === PeriodFilter.current);
    return p ? p.label : PeriodFilter.current.toUpperCase();
  },
  currentColor(periods){
    if(PeriodFilter.current === 'all') return 'pearl';
    const p = (periods || []).find(x => x.key === PeriodFilter.current);
    return p ? p.color : 'pearl';
  },
};

// Cycle: null → graded → review → flagged → null
const STATUS_CYCLE = [null, 'graded', 'review', 'flagged'];
function nextStatus(s){
  const i = STATUS_CYCLE.indexOf(s);
  return STATUS_CYCLE[(i + 1) % STATUS_CYCLE.length];
}
function statusLabel(s){
  return ({graded:'GRADED', review:'REVIEW', flagged:'FLAGGED'})[s] || 'UNGRADED';
}
function statusClass(s){
  return s ? ('s-' + s) : '';
}
"""

# ─────────────────────────────────────────────────────────────────────
# JS — ASSIGNMENT VIEW
# ─────────────────────────────────────────────────────────────────────

ASSIGNMENT_JS = r"""
(function(){
  const A = window.STRATO.assignment;
  const AID = window.STRATO.assignment_id;
  const META_FOR_STORE = {
    id: A.id, title: A.title, lesson: A.lesson, short: A.short,
    color: A.color, date: A.date, period: A.period, teacher: A.teacher, room: A.room,
    n: A.n, roster: A.records.map(r => ({ id: r.id, student: r.student })),
    composite_by_student: Object.fromEntries(A.records.map(r => [r.id, r.summary.composite_pct])),
  };

  // Ensure meta is registered in localStorage (for master view to find this assignment)
  (function registerMeta(){
    const store = SS.loadStore();
    if(!store.assignments[AID]) store.assignments[AID] = { meta: META_FOR_STORE, grades: {} };
    else store.assignments[AID].meta = Object.assign(store.assignments[AID].meta || {}, META_FOR_STORE);
    SS.saveStore(store);
  })();

  function mcCells(rec){
    return ['Q1','Q2','Q3','Q4','Q5','Q6','Q7','Q8'].map(q => {
      const a = rec.answers[q] || {};
      let cls = 'sq lg ';
      if(a.changed_from) cls += 'filled pearl';
      else if(a.correct) cls += 'filled green';
      else cls += 'filled red';
      const title = q+' · chose '+(a.chosen||'—')+' · key '+A.answer_key[q]+
                    (a.changed_from ? ' · changed from '+a.changed_from : '')+
                    ' · '+(a.correct?'correct':'incorrect');
      return `<span class="${cls}" title="${SS.esc(title)}"></span>`;
    }).join('');
  }
  function gameCells(g){
    if(!g) return '';
    const cells = [];
    for(let i=0;i<g.first_try;i++) cells.push('<span class="sq lg filled green"></span>');
    for(let i=0;i<(g.first_try_total - g.first_try);i++) cells.push('<span class="sq lg outline"></span>');
    for(let i=0;i<(g.total - g.first_try_total);i++) cells.push('<span class="sq lg dim"></span>');
    return `<span class="cell-row" title="${g.first_try}/${g.first_try_total} first-try · ${g.placed}/${g.total} placed">${cells.join('')}</span>`;
  }
  function rubricMarkup(score){
    let html = '<div class="rubric" data-rubric>';
    for(let i=1;i<=4;i++){
      const on = score >= i ? 'is-on s-' + score : '';
      html += `<i class="rubric-sq ${on}" data-score="${i}" title="Score ${i}/4"></i>`;
    }
    html += '<span class="rubric-clear" data-clear title="Clear rubric">×</span></div>';
    return html;
  }

  function renderRow(rec, idx){
    const edit = SS.getEdit(AID, rec.id);
    const s = rec.summary;
    const final = SS.effectiveFinal(rec, edit);
    const suggested = SS.suggestedFinal(rec, edit);
    const isOverride = edit.final_grade != null && edit.final_grade !== '';
    const b = SS.bucket(final);
    const statusCls = edit.status ? ('is-' + edit.status) : '';

    return `<tr class="row-${b} ${statusCls}" data-id="${rec.id}" data-name="${SS.esc((rec.student||'').toLowerCase())}">
      <td class="col-rank">${idx+1}</td>
      <td class="col-student" data-toggle="expand">
        <div class="student-name"><span class="twirl">▸</span>${SS.esc(rec.student||rec.file)}</div>
        <div class="student-sub">${SS.esc(rec.session_start||'')} → ${SS.esc(rec.submitted_at||'')}</div>
      </td>
      <td class="col-mc"><span class="cell-row">${mcCells(rec)}</span></td>
      <td class="col-num"><span class="score-pill s-${SS.bucket(s.mc_pct)}">${s.mc_correct}/${s.mc_total}</span></td>
      <td class="col-game">${gameCells(rec.games[1])}</td>
      <td class="col-game">${gameCells(rec.games[2])}</td>
      <td class="col-game">${gameCells(rec.games[3])}</td>
      <td class="col-num"><span class="score-pill s-${SS.bucket(s.games_pct)}">${s.games_first_try}/${s.games_first_try_total}</span></td>
      <td class="col-num"><span class="composite-big s-${SS.bucket(s.composite_pct)}">${s.composite_pct}%</span></td>
      <td class="col-rubric">${rubricMarkup(edit.essay_rubric)}</td>
      <td class="col-final">
        <input type="number" class="final-input ${isOverride ? 'is-override':''}" min="0" max="100" step="1"
               value="${isOverride ? edit.final_grade : ''}" placeholder="${suggested}" data-final
               title="Suggested ${suggested}% — type to override">
      </td>
      <td class="col-status">
        <i class="status-chip ${statusClass(edit.status)}" data-status>${statusLabel(edit.status)}</i>
      </td>
      <td class="col-time">${SS.fmtSeconds(rec.time_on_task_s)}</td>
    </tr>`;
  }

  function renderExpansion(rec){
    const edit = SS.getEdit(AID, rec.id);
    const sectionTimes = rec.section_times_s || {};
    const stRows = [
      ['Element Cards', sectionTimes.element_cards],
      ['Organizing Problem', sectionTimes.organizing],
      ['Atomic Number', sectionTimes.atomic_number],
      ['Periods & Groups', sectionTimes.periods_groups],
      ['Three Regions', sectionTimes.three_regions],
      ['Placement Games', sectionTimes.placement_games],
      ['Essential Question', sectionTimes.essential_question],
    ].map(([l, v]) => `<div class="st-row"><span class="st-l">${l}</span><span>${SS.fmtSeconds(v)}</span></div>`).join('');

    const essayHTML = rec.essay
      ? `<div class="expand-essay">${SS.esc(rec.essay)}</div>`
      : `<div class="expand-essay is-empty">— no response submitted —</div>`;

    return `<tr class="expand-row" data-expand-for="${rec.id}"><td colspan="13"><div class="expand-inner">
      <div class="expand-section">
        <h4>ESSENTIAL QUESTION RESPONSE · ${(rec.essay||'').trim().split(/\s+/).filter(Boolean).length} WORDS</h4>
        ${essayHTML}
        <div class="expand-meta">
          <div class="meta-cell"><div class="ml">SESSION</div><div class="mv">${SS.esc(rec.session_start||'—')} → ${SS.esc(rec.submitted_at||'—')}</div><div class="ms">${SS.fmtSeconds(rec.time_on_task_s)} on task</div></div>
          <div class="meta-cell"><div class="ml">ENGAGEMENT</div><div class="mv">${rec.cards_flipped[0]}/${rec.cards_flipped[1]} cards · ${rec.callouts[0]}/${rec.callouts[1]} callouts</div><div class="ms">${SS.esc(rec.cards_list || '—')}</div></div>
        </div>
        <div class="section-times">${stRows}</div>
      </div>
      <div class="expand-section">
        <h4>TEACHER NOTES</h4>
        <textarea class="notes-area" data-notes placeholder="Comments, follow-up actions, parent contact reminders…">${SS.esc(edit.notes||'')}</textarea>
        <div class="notes-hint">auto-saves on blur · use <b>FINAL</b> column to override the suggested grade · click <b>STATUS</b> chip to cycle</div>
        <div class="expand-actions">
          <button class="ss-btn" data-quick="graded">MARK GRADED</button>
          <button class="ss-btn" data-quick="review">MARK REVIEW</button>
          <button class="ss-btn ss-btn-danger" data-quick="flagged">FLAG</button>
          <button class="ss-btn" data-quick="clear">CLEAR STATUS</button>
        </div>
      </div>
    </div></td></tr>`;
  }

  function renderEssayCard(rec){
    const edit = SS.getEdit(AID, rec.id);
    const final = SS.effectiveFinal(rec, edit);
    const b = SS.bucket(final);
    const color = SS.bucketColor(b);
    const words = (rec.essay||'').trim().split(/\s+/).filter(Boolean).length;
    const body = rec.essay ? SS.esc(rec.essay) : '<span class="essay-empty">— no response submitted —</span>';
    return `<div class="essay-card" data-color="${color}" data-id="${rec.id}">
      <div class="essay-head">
        <div class="essay-name">${SS.esc(rec.student||rec.file)}</div>
        <div class="essay-stats"><b>${words}</b> WORDS<br><b>FINAL ${final}%</b></div>
      </div>
      <div class="essay-text">${body}</div>
      <div class="essay-rubric-bar">
        <span class="essay-rubric-label">ESSAY RUBRIC</span>
        ${rubricMarkup(edit.essay_rubric)}
      </div>
    </div>`;
  }

  function getFiltered(){
    const q = (document.getElementById('ss-search').value || '').toLowerCase().trim();
    const sortKey = document.getElementById('ss-sort').value;
    const filter = document.getElementById('ss-filter').value;
    let rows = A.records.slice().filter(r => PeriodFilter.matches(r));
    if(q) rows = rows.filter(r => (r.student||'').toLowerCase().includes(q));
    if(filter){
      rows = rows.filter(r => {
        const e = SS.getEdit(AID, r.id);
        if(filter === 'needs_grading') return !e.essay_rubric && e.final_grade == null;
        if(filter === 'status_review') return e.status === 'review';
        if(filter === 'status_flagged') return e.status === 'flagged';
        if(filter === 'status_graded') return e.status === 'graded';
        if(filter === 'status_none') return !e.status;
        return true;
      });
    }
    const lastName = r => {
      const parts = (r.student||r.file).split(/\s+/);
      return (parts[parts.length-1] || '').toLowerCase();
    };
    const finalOf = r => SS.effectiveFinal(r, SS.getEdit(AID, r.id));
    const statusRank = r => ({flagged:0, review:1, graded:2}[SS.getEdit(AID, r.id).status] ?? 3);
    switch(sortKey){
      case 'last': rows.sort((a,b)=>lastName(a).localeCompare(lastName(b))); break;
      case 'final_desc': rows.sort((a,b)=>finalOf(b)-finalOf(a)); break;
      case 'composite_desc': rows.sort((a,b)=>b.summary.composite_pct-a.summary.composite_pct); break;
      case 'composite_asc': rows.sort((a,b)=>a.summary.composite_pct-b.summary.composite_pct); break;
      case 'status': rows.sort((a,b)=>statusRank(a)-statusRank(b)); break;
      case 'time_desc': rows.sort((a,b)=>b.time_on_task_s-a.time_on_task_s); break;
      case 'time_asc': rows.sort((a,b)=>a.time_on_task_s-b.time_on_task_s); break;
    }
    return rows;
  }

  let expandedId = null;

  function renderTable(){
    const tbody = document.getElementById('ss-tbody');
    const rows = getFiltered();
    let html = '';
    rows.forEach((r, i) => {
      html += renderRow(r, i);
      if(expandedId === r.id) {
        // Re-mark the row as expanded so twirl/highlight applies
        html = html.replace(`data-id="${r.id}"`, `data-id="${r.id}" class-marker`);
        html += renderExpansion(r);
      }
    });
    tbody.innerHTML = html;
    // Apply expanded class to the right row
    if(expandedId) {
      const tr = tbody.querySelector(`tr[data-id="${expandedId}"]:not(.expand-row)`);
      if(tr) tr.classList.add('is-expanded');
    }
  }

  function renderEssays(){
    const grid = document.getElementById('ss-essays');
    grid.innerHTML = getFiltered().map(renderEssayCard).join('');
  }

  function getScopedRecords(){
    return A.records.filter(r => PeriodFilter.matches(r));
  }

  function fmtSecs(s){ return SS.fmtSeconds(s); }
  function avg(arr){ return arr.length ? Math.round(arr.reduce((a,b)=>a+b,0) / arr.length) : 0; }

  function renderStatGrid(){
    const recs = getScopedRecords();
    const n = recs.length;
    const periodLabel = PeriodFilter.currentLabel(A.periods);
    const periodColor = PeriodFilter.currentColor(A.periods);
    const aMc = avg(recs.map(r => r.summary.mc_pct));
    const aGames = avg(recs.map(r => r.summary.games_pct));
    const aComp = avg(recs.map(r => r.summary.composite_pct));
    const aTime = avg(recs.map(r => r.time_on_task_s));
    const times = recs.map(r => r.time_on_task_s).sort((a,b)=>a-b);
    const medTime = times.length ? times[Math.floor(times.length/2)] : 0;

    const all = SS.getAllEdits(AID);
    const graded = recs.filter(r => (all[r.id]||{}).status === 'graded').length;
    const touched = recs.filter(r => {
      const e = all[r.id];
      return e && (e.final_grade != null && e.final_grade !== '' || e.essay_rubric > 0 || e.status);
    }).length;

    const grid = document.getElementById('ss-stat-grid');
    grid.innerHTML = `
      ${statCard('STUDENTS', String(n), periodLabel, periodColor)}
      ${statCard('AVG MULTIPLE-CHOICE', aMc + '%', `${(aMc*8/100).toFixed(1)}/8 questions`, 'green')}
      ${statCard('AVG GAMES (FIRST-TRY)', aGames + '%', `${Math.round(aGames*18/100)}/18 placements`, 'gold')}
      ${statCard('AVG COMPOSITE', aComp + '%', 'MC + games blended', 'pearl')}
      ${statCard('AVG TIME ON TASK', fmtSecs(aTime), `median ${fmtSecs(medTime)}`, 'purple')}
      ${statCard('FINAL GRADES', `${graded}/${n}`, touched === 0 ? 'none entered' : `${touched} touched`, 'cyan')}
    `;
  }

  function statCard(label, value, sub, color){
    return `<div class="ss-stat" data-color="${color}">
      <div class="ss-stat-label">${label}</div>
      <div class="ss-stat-value">${value}</div>
      <div class="ss-stat-sub">${SS.esc(sub)}</div>
      <div class="ss-stat-mark"></div>
    </div>`;
  }

  const BUCKET_ORDER = ['perfect', 'strong', 'passing', 'shaky', 'needs_help'];
  function bucketBreakdown(thisBucket, recs){
    const thisIdx = BUCKET_ORDER.indexOf(thisBucket);
    let q=0, a=0, f=0;
    recs.forEach(r => {
      const diff = Math.abs(BUCKET_ORDER.indexOf(SS.bucket(r.summary.composite_pct)) - thisIdx);
      if(diff === 0) q++;
      else if(diff === 1) a++;
      else f++;
    });
    return {q, a, f};
  }

  function renderDistribution(){
    const recs = getScopedRecords();
    const n = recs.length;
    const cells = [
      {bucket:'perfect',    label:'PERFECT',    range:'95–100%', color:'green'},
      {bucket:'strong',     label:'STRONG',     range:'85–94%',  color:'cyan'},
      {bucket:'passing',    label:'PASSING',    range:'70–84%',  color:'gold'},
      {bucket:'shaky',      label:'SHAKY',      range:'50–69%',  color:'purple'},
      {bucket:'needs_help', label:'NEEDS HELP', range:'&lt; 50%', color:'red'},
    ];
    const grid = document.getElementById('ss-dist-grid');
    grid.innerHTML = cells.map(c => {
      const {q, a, f} = bucketBreakdown(c.bucket, recs);
      const pct = n ? Math.round(100 * q / n) : 0;
      const greens = '<span class="sq md filled green" title="qualified"></span>'.repeat(q);
      const cyans  = '<span class="sq md filled cyan" title="one bucket away"></span>'.repeat(a);
      const reds   = '<span class="sq md filled red" title="two or more buckets away"></span>'.repeat(f);
      return `<div class="ss-dist-card" data-color="${c.color}">
        <div class="dist-head">
          <div class="dist-label">${c.label}</div>
          <div class="dist-range">${c.range}</div>
        </div>
        <div class="dist-value"><span class="big">${q}</span><span class="of">/${n}</span></div>
        <div class="dist-cells dist-cells-md" data-color="${c.color}">${greens}${cyans}${reds}</div>
        <div class="dist-bar"><div class="dist-bar-fill" style="width:${pct}%"></div></div>
        <div class="dist-breakdown">
          <span class="bd-q"><i class="bd-dot bd-green"></i>${q} in</span>
          <span class="bd-a"><i class="bd-dot bd-cyan"></i>${a} close</span>
          <span class="bd-f"><i class="bd-dot bd-red"></i>${f} far</span>
          <span class="dist-pct">${pct}%</span>
        </div>
      </div>`;
    }).join('');
  }

  function renderQDiff(){
    const recs = getScopedRecords();
    const n = recs.length;
    const order = Object.keys(A.answer_key);
    const html = order.map(qid => {
      const correct = recs.filter(r => (r.answers[qid]||{}).correct).length;
      const wrong = Math.max(0, n - correct);
      const pct = n ? Math.round(100 * correct / n) : 0;
      // Row accent uses the bucket; cell colors are stable green/red so the
      // correct-vs-wrong histogram stays legible even on red-bucketed rows.
      const accent = pct >= 90 ? 'green' : pct >= 75 ? 'gold' : 'red';
      const filled = '<span class="sq md filled green"></span>'.repeat(correct);
      const wrongCells = '<span class="sq md filled red"></span>'.repeat(wrong);
      return `<div class="qdiff-row" data-color="${accent}">
        <div class="qdiff-id">${qid}</div>
        <div class="qdiff-key">KEY · ${A.answer_key[qid]}</div>
        <div class="qdiff-prompt">${SS.esc(A.question_prompts[qid] || '')}</div>
        <div class="qdiff-cells">${filled}${wrongCells}</div>
        <div class="qdiff-num"><span class="big">${correct}</span><span class="of">/${n}</span><div class="qdiff-wrong">${wrong} wrong</div></div>
        <div class="qdiff-pct">${pct}%</div>
      </div>`;
    }).join('');
    document.getElementById('ss-qdiff').innerHTML = html;
  }

  function renderAll(){
    renderStatGrid();
    renderDistribution();
    renderQDiff();
    renderTable();
    renderEssays();
  }

  // ── Event delegation ──
  document.addEventListener('click', e => {
    const tgt = e.target;

    // Toggle expand on student name
    const studentCell = tgt.closest('td[data-toggle="expand"]');
    if(studentCell){
      const tr = studentCell.closest('tr');
      const id = tr.getAttribute('data-id');
      expandedId = (expandedId === id) ? null : id;
      renderTable();
      return;
    }

    // Rubric square click
    const rubricSq = tgt.closest('.rubric-sq');
    if(rubricSq){
      const tr = rubricSq.closest('tr, .essay-card');
      const id = tr.getAttribute('data-id');
      const score = Number(rubricSq.getAttribute('data-score'));
      SS._setDirtyIndicator();
      const cur = SS.getEdit(AID, id).essay_rubric;
      // Toggle off if same score clicked twice
      const next = (cur === score) ? 0 : score;
      SS.setEdit(AID, id, { essay_rubric: next }, META_FOR_STORE);
      renderAll();
      return;
    }
    const rubricClear = tgt.closest('.rubric-clear');
    if(rubricClear){
      const tr = rubricClear.closest('tr, .essay-card');
      const id = tr.getAttribute('data-id');
      SS._setDirtyIndicator();
      SS.setEdit(AID, id, { essay_rubric: 0 }, META_FOR_STORE);
      renderAll();
      return;
    }

    // Status chip cycle
    const status = tgt.closest('.status-chip[data-status]');
    if(status){
      const tr = status.closest('tr');
      const id = tr.getAttribute('data-id');
      const cur = SS.getEdit(AID, id).status;
      SS._setDirtyIndicator();
      SS.setEdit(AID, id, { status: nextStatus(cur) }, META_FOR_STORE);
      renderAll();
      return;
    }

    // Quick status buttons in expansion
    const quick = tgt.closest('[data-quick]');
    if(quick){
      const tr = quick.closest('.expand-row');
      const id = tr.getAttribute('data-expand-for');
      const action = quick.getAttribute('data-quick');
      SS._setDirtyIndicator();
      SS.setEdit(AID, id, { status: action === 'clear' ? null : action }, META_FOR_STORE);
      renderAll();
      return;
    }
  });

  // Final-grade input changes
  document.addEventListener('change', e => {
    const inp = e.target.closest('input[data-final]');
    if(inp){
      const tr = inp.closest('tr');
      const id = tr.getAttribute('data-id');
      let v = inp.value.trim();
      let val = null;
      if(v !== ''){
        const n = Number(v);
        if(isNaN(n) || n < 0 || n > 100){
          SS.toast('Final grade must be 0–100', 'error');
          inp.value = '';
          return;
        }
        val = n;
      }
      SS._setDirtyIndicator();
      SS.setEdit(AID, id, { final_grade: val }, META_FOR_STORE);
      renderAll();
    }
  });

  // Notes auto-save on blur
  document.addEventListener('focusout', e => {
    const ta = e.target.closest('textarea[data-notes]');
    if(ta){
      const tr = ta.closest('.expand-row');
      const id = tr.getAttribute('data-expand-for');
      SS._setDirtyIndicator();
      SS.setEdit(AID, id, { notes: ta.value }, META_FOR_STORE);
      renderHeaderStats();
    }
  });

  document.getElementById('ss-search').addEventListener('input', renderAll);
  document.getElementById('ss-sort').addEventListener('change', renderAll);
  document.getElementById('ss-filter').addEventListener('change', renderAll);

  PeriodFilter.init(renderAll);

  SS.wirePersistToolbar({
    onResetThis: () => SS.resetAssignmentEdits(AID),
    onReset: () => location.reload(),
  });

  renderAll();
})();
"""

# ─────────────────────────────────────────────────────────────────────
# JS — MASTER VIEW
# ─────────────────────────────────────────────────────────────────────

MASTER_JS = r"""
(function(){
  const M = window.STRATO_MASTER;
  const FULL_ROSTER = M.roster;
  // Roster scoped by period filter — recomputed each render via getRoster()
  function getRoster(){ return FULL_ROSTER.filter(s => PeriodFilter.matchesStudent(s)); }
  // Backwards-compat alias used by render code below.
  const ROSTER = FULL_ROSTER;
  // Build assignment list — combine baked-in payload + any extras stored in localStorage
  function getAssignments(){
    const fromPayload = (M.assignments || []).map(a => ({
      id: a.id, title: a.title, lesson: a.lesson, short: a.short, color: a.color,
      date: a.date, period: a.period,
      records: a.records, // full records baked in
      composite_by_student: Object.fromEntries(a.records.map(r => [r.id, r.summary.composite_pct])),
      summaries_by_student: Object.fromEntries(a.records.map(r => [r.id, r])),
    }));
    // Future: pick up assignments from localStorage that aren't in payload
    const store = SS.loadStore();
    Object.entries(store.assignments || {}).forEach(([aid, s]) => {
      if(!fromPayload.find(a => a.id === aid) && s.meta){
        fromPayload.push({
          id: aid, title: s.meta.title || aid, short: s.meta.short || aid,
          color: s.meta.color || 'cyan', date: s.meta.date || '', period: s.meta.period || '',
          records: [],
          composite_by_student: s.meta.composite_by_student || {},
          summaries_by_student: {},
        });
      }
    });
    return fromPayload;
  }

  function studentEdit(aid, sid){ return SS.getEdit(aid, sid); }
  function studentFinal(a, sid){
    const rec = a.summaries_by_student[sid];
    const edit = studentEdit(a.id, sid);
    if(rec) return SS.effectiveFinal(rec, edit);
    if(edit.final_grade != null && edit.final_grade !== '') return Number(edit.final_grade);
    return a.composite_by_student[sid];
  }
  function hasGrade(a, sid){
    if(a.composite_by_student[sid] != null) return true;
    const e = studentEdit(a.id, sid);
    return e.final_grade != null && e.final_grade !== '';
  }

  function renderHeaderStats(){
    const assignments = getAssignments();
    const roster = getRoster();
    const n = roster.length;
    const periodLabel = PeriodFilter.currentLabel(M.periods);
    // Per-student running average
    const studentAvgs = roster.map(s => {
      const grades = assignments.map(a => hasGrade(a, s.id) ? studentFinal(a, s.id) : null).filter(g => g != null);
      return grades.length ? Math.round(grades.reduce((a,b)=>a+b,0) / grades.length) : null;
    });
    const validAvgs = studentAvgs.filter(g => g != null);
    const classAvg = validAvgs.length ? Math.round(validAvgs.reduce((a,b)=>a+b,0)/validAvgs.length) : 0;
    const dist = {perfect:0, strong:0, passing:0, shaky:0, needs_help:0};
    validAvgs.forEach(g => dist[SS.bucket(g)]++);
    let totalGraded = 0;
    assignments.forEach(a => {
      roster.forEach(s => {
        const e = studentEdit(a.id, s.id);
        if(e.status === 'graded') totalGraded++;
      });
    });
    const totalCells = assignments.length * n;
    const stats = [
      { label: 'STUDENTS', val: String(n), sub: periodLabel, color: 'cyan' },
      { label: 'ASSIGNMENTS', val: String(assignments.length), sub: 'tracked', color: 'gold' },
      { label: 'CLASS AVG', val: classAvg + '%', sub: `over ${validAvgs.length} grades`, color: 'green' },
      { label: 'GRADED CELLS', val: `${totalGraded}/${totalCells}`, sub: 'marked GRADED', color: 'pearl' },
      { label: 'TOP PERFORMERS', val: String(dist.perfect), sub: '95+ running avg', color: 'green' },
      { label: 'AT-RISK', val: String(dist.needs_help + dist.shaky), sub: 'under 70 avg', color: 'red' },
    ];
    document.getElementById('ss-master-stats').innerHTML = stats.map(s =>
      `<div class="ss-stat" data-color="${s.color}">
        <div class="ss-stat-label">${s.label}</div>
        <div class="ss-stat-value">${s.val}</div>
        <div class="ss-stat-sub">${s.sub}</div>
        <div class="ss-stat-mark"></div>
      </div>`
    ).join('');
  }

  function renderAssignmentStrip(){
    const assignments = getAssignments();
    const roster = getRoster();
    const periodLabel = PeriodFilter.currentLabel(M.periods);
    const strip = document.getElementById('ss-assignment-strip');
    let html = '';
    assignments.forEach(a => {
      const grades = roster.map(s => hasGrade(a, s.id) ? studentFinal(a, s.id) : null).filter(g => g != null);
      const avg = grades.length ? Math.round(grades.reduce((x,y)=>x+y,0)/grades.length) : 0;
      const dist = {perfect:0, strong:0, passing:0, shaky:0, needs_help:0};
      grades.forEach(g => dist[SS.bucket(g)]++);
      const stripCells = roster.map(s => {
        if(!hasGrade(a, s.id)) return '<span class="am-cell" style="background:var(--void-3);border:1px solid var(--line)"></span>';
        const b = SS.bucket(studentFinal(a, s.id));
        const c = SS.bucketColor(b);
        return `<span class="am-cell" style="background:var(--${c});border:1px solid var(--${c})"></span>`;
      }).join('');
      const periodHash = PeriodFilter.current !== 'all' ? '#p=' + encodeURIComponent(PeriodFilter.current) : '';
      const linkHref = a.id ? `${a.id}/gradebook.html${periodHash}` : '#';
      html += `<div class="assignment-card" data-color="${a.color}">
        <a class="assignment-link" href="${linkHref}" title="Open assignment view"></a>
        <div class="assignment-head">
          <div class="assignment-title">${SS.esc(a.title)}</div>
          <div class="assignment-meta">${SS.esc(a.date)} · ${periodLabel}</div>
        </div>
        <div class="assignment-stats">
          <div class="assignment-stat"><span class="as-l">SUBMITTED</span><span class="as-v">${grades.length}/${roster.length}</span></div>
          <div class="assignment-stat"><span class="as-l">CLASS AVG</span><span class="as-v">${avg}%</span></div>
          <div class="assignment-stat"><span class="as-l">PERFECT</span><span class="as-v">${dist.perfect}</span></div>
        </div>
        <div class="assignment-mini">${stripCells}</div>
      </div>`;
    });
    // Empty assignment placeholder card
    html += `<div class="assignment-card is-empty">
      <div class="assignment-head"><div class="assignment-title">+ NEW ASSIGNMENT</div><div class="assignment-meta">PLACEHOLDER</div></div>
      <div class="assignment-stats">
        <div class="assignment-stat"><span class="as-l">SUBMITTED</span><span class="as-v">—</span></div>
        <div class="assignment-stat"><span class="as-l">CLASS AVG</span><span class="as-v">—</span></div>
      </div>
      <div class="assignment-mini">${'<span class="am-cell"></span>'.repeat(roster.length)}</div>
    </div>`;
    strip.innerHTML = html;
  }

  let expandedKey = null; // "studentId|assignmentId"

  function renderTable(){
    const assignments = getAssignments();
    const q = (document.getElementById('ss-search').value || '').toLowerCase().trim();
    const sortKey = document.getElementById('ss-sort').value;
    const roster = getRoster();

    let students = roster.slice();
    if(q) students = students.filter(s => (s.student||'').toLowerCase().includes(q));

    const studentAvgFn = s => {
      const grades = assignments.map(a => hasGrade(a, s.id) ? studentFinal(a, s.id) : null).filter(g => g != null);
      return grades.length ? Math.round(grades.reduce((x,y)=>x+y,0)/grades.length) : null;
    };
    const lastName = s => {
      const parts = (s.student||s.id).split(/\s+/);
      return (parts[parts.length-1] || '').toLowerCase();
    };
    if(sortKey === 'avg_desc') students.sort((a,b) => (studentAvgFn(b)||0) - (studentAvgFn(a)||0));
    else if(sortKey === 'avg_asc') students.sort((a,b) => (studentAvgFn(a)||0) - (studentAvgFn(b)||0));
    else students.sort((a,b) => lastName(a).localeCompare(lastName(b)));

    // Header
    const thead = document.getElementById('ss-master-thead');
    thead.innerHTML = `<tr>
      <th class="col-rank">#</th>
      <th class="col-student">STUDENT</th>
      <th class="col-period">PERIOD</th>
      ${assignments.map(a => `<th class="col-grade" title="${SS.esc(a.lesson||a.title)}"><span style="color:var(--${a.color})">${SS.esc(a.short)}</span><br><span class="th-sub">${SS.esc(a.date)}</span></th>`).join('')}
      <th class="col-grade col-avg">RUNNING AVG</th>
    </tr>`;

    // Body
    const tbody = document.getElementById('ss-master-tbody');
    let html = '';
    students.forEach((s, i) => {
      const avg = studentAvgFn(s);
      const avgB = avg != null ? SS.bucket(avg) : '';
      const rowB = avgB || 'strong';
      const periodMeta = (M.periods || []).find(p => p.key === s.period_key);
      const pColor = periodMeta ? periodMeta.color : 'pearl';
      const pLabel = periodMeta ? periodMeta.label : (s.period_num ? 'P' + s.period_num : 'P?');
      html += `<tr class="row-${rowB}" data-sid="${s.id}" data-name="${SS.esc((s.student||'').toLowerCase())}">
        <td class="col-rank">${i+1}</td>
        <td class="col-student">
          <div class="student-name">${SS.esc(s.student)}</div>
        </td>
        <td class="col-period"><span class="period-tag" data-color="${pColor}">${SS.esc(pLabel)}</span></td>
        ${assignments.map(a => {
          if(!hasGrade(a, s.id)){
            return `<td class="col-grade"><span class="grade-cell is-empty" data-sid="${s.id}" data-aid="${a.id}">—</span></td>`;
          }
          const g = studentFinal(a, s.id);
          const b = SS.bucket(g);
          const e = studentEdit(a.id, s.id);
          const isOverride = e.final_grade != null && e.final_grade !== '';
          const tag = isOverride ? 'OVERRIDE' : (e.essay_rubric > 0 ? 'WITH ESSAY' : 'AUTO');
          const expCls = (expandedKey === s.id + '|' + a.id) ? ' is-expanded' : '';
          return `<td class="col-grade"><span class="grade-cell s-${b} ${isOverride ? 'is-override':''}${expCls}" data-sid="${s.id}" data-aid="${a.id}" title="Click to expand">${g}%<span class="gc-tag">${tag}</span></span></td>`;
        }).join('')}
        <td class="col-grade col-avg">${avg != null ? `<span class="grade-cell s-${SS.bucket(avg)}">${avg}%</span>` : `<span class="grade-cell is-empty">—</span>`}</td>
      </tr>`;

      // Expansion row if any cell in this student row is expanded
      if(expandedKey && expandedKey.startsWith(s.id + '|')){
        const aid = expandedKey.split('|')[1];
        const a = assignments.find(x => x.id === aid);
        if(a) html += renderMasterExpansion(s, a);
      }
    });
    tbody.innerHTML = html;

    // Footer: per-assignment class avg (scoped to current period filter)
    const tfoot = document.getElementById('ss-master-tfoot');
    tfoot.innerHTML = `<tr>
      <td></td>
      <td>CLASS AVG</td>
      <td>${PeriodFilter.currentLabel(M.periods)}</td>
      ${assignments.map(a => {
        const grades = roster.map(s => hasGrade(a, s.id) ? studentFinal(a, s.id) : null).filter(g => g != null);
        const av = grades.length ? Math.round(grades.reduce((x,y)=>x+y,0)/grades.length) : null;
        return `<td class="col-grade">${av != null ? av + '%' : '—'}</td>`;
      }).join('')}
      <td class="col-grade"></td>
    </tr>`;
  }

  function renderMasterExpansion(s, a){
    const rec = a.summaries_by_student[s.id];
    if(!rec) return `<tr class="expand-row"><td colspan="99"><div class="master-expand-inner"><div class="expand-section"><h4>NO DATA</h4><div class="expand-essay is-empty">No submission data baked into this view for ${SS.esc(s.student)} on ${SS.esc(a.title)}.</div></div></div></td></tr>`;
    const edit = SS.getEdit(a.id, s.id);
    const final = SS.effectiveFinal(rec, edit);
    const suggested = SS.suggestedFinal(rec, edit);
    const isOverride = edit.final_grade != null && edit.final_grade !== '';
    const colspan = 4 + (M.assignments || []).length;
    let rubricMarkup = '<div class="rubric" data-rubric>';
    for(let i=1;i<=4;i++){
      const on = edit.essay_rubric >= i ? ('is-on s-' + edit.essay_rubric) : '';
      rubricMarkup += `<i class="rubric-sq ${on}" data-score="${i}" title="Score ${i}/4"></i>`;
    }
    rubricMarkup += '<span class="rubric-clear" data-clear title="Clear">×</span></div>';

    return `<tr class="expand-row" data-expand-key="${s.id}|${a.id}"><td colspan="${colspan}"><div class="master-expand-inner">
      <div class="expand-section">
        <h4>${SS.esc(a.title)} · ${SS.esc(s.student)}</h4>
        <div class="expand-meta" style="grid-template-columns:1fr 1fr 1fr">
          <div class="meta-cell"><div class="ml">MULTIPLE-CHOICE</div><div class="mv">${rec.summary.mc_correct}/${rec.summary.mc_total}</div><div class="ms">${rec.summary.mc_pct}%</div></div>
          <div class="meta-cell"><div class="ml">GAMES (FIRST-TRY)</div><div class="mv">${rec.summary.games_first_try}/${rec.summary.games_first_try_total}</div><div class="ms">${rec.summary.games_pct}%</div></div>
          <div class="meta-cell"><div class="ml">TIME ON TASK</div><div class="mv">${SS.fmtSeconds(rec.time_on_task_s)}</div><div class="ms">${SS.esc(rec.session_start||'')} → ${SS.esc(rec.submitted_at||'')}</div></div>
        </div>
        <h4 style="margin-top:18px">ESSAY · ${(rec.essay||'').trim().split(/\s+/).filter(Boolean).length} WORDS</h4>
        ${rec.essay ? `<div class="expand-essay">${SS.esc(rec.essay)}</div>` : `<div class="expand-essay is-empty">— no response submitted —</div>`}
      </div>
      <div class="expand-section">
        <h4>QUICK GRADE</h4>
        <div class="meta-cell"><div class="ml">AUTO COMPOSITE</div><div class="mv">${rec.summary.composite_pct}%</div><div class="ms">50% MC + 50% games</div></div>
        <div class="meta-cell" style="margin-top:10px"><div class="ml">SUGGESTED FINAL</div><div class="mv">${suggested}%</div><div class="ms">${edit.essay_rubric > 0 ? '50/30/20 with essay rubric' : 'no rubric — = composite'}</div></div>
        <div class="meta-cell" style="margin-top:10px"><div class="ml">FINAL (CURRENT)</div><div class="mv" style="color:var(--${SS.bucketColor(SS.bucket(final))})">${final}% ${isOverride ? '· OVERRIDE' : ''}</div><div class="ms">essay rubric: ${edit.essay_rubric || 'unscored'} · status: ${statusLabel(edit.status)}</div></div>
        <div style="margin-top:14px;display:flex;justify-content:space-between;align-items:center">
          <span class="essay-rubric-label">ESSAY RUBRIC</span>
          ${rubricMarkup}
        </div>
        <div style="margin-top:14px">
          <label class="ml">FINAL OVERRIDE (0–100)</label>
          <input type="number" class="final-input ${isOverride ? 'is-override':''}" min="0" max="100" step="1" value="${isOverride ? edit.final_grade : ''}" placeholder="${suggested}" data-final-master style="width:100%;text-align:left;margin-top:6px">
        </div>
      </div>
      <div class="expand-section">
        <h4>STATUS · NOTES</h4>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <i class="status-chip ${statusClass(edit.status)}" data-status-master>${statusLabel(edit.status)}</i>
          <button class="ss-btn" data-quick-master="graded">GRADED</button>
          <button class="ss-btn" data-quick-master="review">REVIEW</button>
          <button class="ss-btn ss-btn-danger" data-quick-master="flagged">FLAG</button>
          <button class="ss-btn" data-quick-master="clear">CLEAR</button>
        </div>
        <textarea class="notes-area" data-notes-master placeholder="Comments, follow-up actions, parent contact reminders…" style="margin-top:14px">${SS.esc(edit.notes||'')}</textarea>
        <div class="expand-actions">
          ${a.id ? `<a href="${a.id}/gradebook.html" class="ss-btn">OPEN FULL ASSIGNMENT VIEW ▸</a>` : ''}
          <button class="ss-btn" data-collapse>COLLAPSE</button>
        </div>
      </div>
    </div></td></tr>`;
  }

  function renderAll(){
    renderHeaderStats();
    renderAssignmentStrip();
    renderTable();
  }

  // ── Events ──
  document.addEventListener('click', e => {
    const t = e.target;

    // Expand a grade cell
    const cell = t.closest('.grade-cell[data-sid][data-aid]');
    if(cell && !cell.classList.contains('is-empty')){
      const sid = cell.getAttribute('data-sid');
      const aid = cell.getAttribute('data-aid');
      const key = sid + '|' + aid;
      expandedKey = (expandedKey === key) ? null : key;
      renderTable();
      return;
    }

    if(t.closest('[data-collapse]')){ expandedKey = null; renderTable(); return; }

    // Rubric in master expansion
    const rs = t.closest('.rubric-sq');
    if(rs){
      const tr = rs.closest('.expand-row');
      if(!tr) return;
      const [sid, aid] = tr.getAttribute('data-expand-key').split('|');
      const score = Number(rs.getAttribute('data-score'));
      const cur = SS.getEdit(aid, sid).essay_rubric;
      SS._setDirtyIndicator();
      SS.setEdit(aid, sid, { essay_rubric: cur === score ? 0 : score });
      renderAll();
      return;
    }
    const rc = t.closest('.rubric-clear');
    if(rc){
      const tr = rc.closest('.expand-row');
      if(!tr) return;
      const [sid, aid] = tr.getAttribute('data-expand-key').split('|');
      SS._setDirtyIndicator();
      SS.setEdit(aid, sid, { essay_rubric: 0 });
      renderAll();
      return;
    }
    // Status chip in master
    const sc = t.closest('[data-status-master]');
    if(sc){
      const tr = sc.closest('.expand-row');
      if(!tr) return;
      const [sid, aid] = tr.getAttribute('data-expand-key').split('|');
      const cur = SS.getEdit(aid, sid).status;
      SS._setDirtyIndicator();
      SS.setEdit(aid, sid, { status: nextStatus(cur) });
      renderAll();
      return;
    }
    const qm = t.closest('[data-quick-master]');
    if(qm){
      const tr = qm.closest('.expand-row');
      if(!tr) return;
      const [sid, aid] = tr.getAttribute('data-expand-key').split('|');
      const action = qm.getAttribute('data-quick-master');
      SS._setDirtyIndicator();
      SS.setEdit(aid, sid, { status: action === 'clear' ? null : action });
      renderAll();
      return;
    }
  });

  document.addEventListener('change', e => {
    const inp = e.target.closest('input[data-final-master]');
    if(inp){
      const tr = inp.closest('.expand-row');
      if(!tr) return;
      const [sid, aid] = tr.getAttribute('data-expand-key').split('|');
      let v = inp.value.trim();
      let val = null;
      if(v !== ''){
        const n = Number(v);
        if(isNaN(n) || n < 0 || n > 100){ SS.toast('Final must be 0–100', 'error'); inp.value=''; return; }
        val = n;
      }
      SS._setDirtyIndicator();
      SS.setEdit(aid, sid, { final_grade: val });
      renderAll();
    }
  });

  document.addEventListener('focusout', e => {
    const ta = e.target.closest('textarea[data-notes-master]');
    if(ta){
      const tr = ta.closest('.expand-row');
      if(!tr) return;
      const [sid, aid] = tr.getAttribute('data-expand-key').split('|');
      SS._setDirtyIndicator();
      SS.setEdit(aid, sid, { notes: ta.value });
      renderHeaderStats();
    }
  });

  document.getElementById('ss-search').addEventListener('input', renderTable);
  document.getElementById('ss-sort').addEventListener('change', renderTable);

  PeriodFilter.init(renderAll);

  SS.wirePersistToolbar({
    onResetThis: () => SS.resetAllEdits(),
    onReset: () => location.reload(),
  });

  renderAll();
})();
"""

if __name__ == "__main__":
    main()
