"""
Shared parsing utilities for Stratosyn worksheet submissions.

Imported by both organize.py (file routing) and build_gradebook.py (rendering).
Keeps the regex contract for header fields, period detection, and slug generation
in one place so the two scripts can never drift apart.
"""

import html
import re
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

# Legacy Day-1 defaults. These are kept as a fallback only — the real
# answer key and question prompts for each assignment live in that
# assignment's data/<slug>_<date>/metadata.json under "answer_key" and
# "question_prompts". organize.py auto-extracts question prompts from the
# first submission; the answer key must be filled in by the teacher.
LEGACY_ANSWER_KEY = {"Q1": "A", "Q2": "C", "Q3": "B", "Q4": "C",
                     "Q5": "C", "Q6": "B", "Q7": "C", "Q8": "D"}
LEGACY_QUESTION_PROMPTS = {
    "Q1": "Alphabetical organizing — main problem",
    "Q2": "Mendeleev's repeating pattern — name of table",
    "Q3": "Sodium's atomic number = 11 means",
    "Q4": "Why atomic number beats atomic mass",
    "Q5": "Sodium in Period 3 — what it tells about electrons",
    "Q6": "Li/Na/K in Group 1 — predicted behavior",
    "Q7": "Iron's region of the table",
    "Q8": "Silicon — metal / nonmetal / metalloid",
}

# Backward-compat aliases so existing imports don't break. Prefer the
# explicit per-assignment lookups via metadata.json in new code.
ANSWER_KEY = LEGACY_ANSWER_KEY
QUESTION_PROMPTS = LEGACY_QUESTION_PROMPTS

CHN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}
PERIOD_PATS = (r"Period\s+(\d+)", r"Phase\s+(\d+)", r"Hour\s+(\d+)", r"Block\s+(\d+)")

# Optional manual short-slug map for known lessons.
# Without an entry here, we auto-slug the lesson title.
MANUAL_SLUGS = {
    "What Is The Periodic Table?": "day1-periodic-table",
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t ]+")


# ─────────────────────────────────────────────────────────────────────
# Slug helpers
# ─────────────────────────────────────────────────────────────────────

def slugify(s: str) -> str:
    """Lowercase, hyphen-separated, alphanumeric-only slug."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def lesson_to_slug(lesson_line: str) -> str:
    """Turn a LESSON header value into a stable assignment slug.

    "7th Grade Science · Day 1 — What Is The Periodic Table?"
        → "day1-periodic-table"   (via MANUAL_SLUGS override)
        → "day1-what-is-the-periodic-table" (auto fallback)
    """
    if not lesson_line:
        return "untitled-assignment"

    # Try to extract title (after em-dash) and day-number prefix
    title = lesson_line
    day_prefix = ""
    if "—" in lesson_line:
        before, after = lesson_line.split("—", 1)
        title = after.strip()
        m = re.search(r"Day\s+(\d+)", before)
        if m:
            day_prefix = f"day{m.group(1)}-"
    else:
        m = re.search(r"Day\s+(\d+)", lesson_line)
        if m:
            day_prefix = f"day{m.group(1)}-"

    # Manual override on raw title
    if title in MANUAL_SLUGS:
        return MANUAL_SLUGS[title]
    return (day_prefix + slugify(title)) or "untitled-assignment"


def period_slug(section: str, period_num) -> str:
    """e.g. ('honors', 1) → 'p1-honors'; (None, None) → 'unknown'."""
    if period_num is None:
        return "unknown"
    return f"p{period_num}-{section or 'unknown'}"


# ─────────────────────────────────────────────────────────────────────
# Tag stripping
# ─────────────────────────────────────────────────────────────────────

def strip_tags(raw: str) -> str:
    """Convert worksheet HTML to a clean line-separated text representation."""
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", raw, flags=re.I)
    s = re.sub(r"</\s*p\s*>", "\n", s, flags=re.I)
    s = TAG_RE.sub("", s)
    s = html.unescape(s)
    out = []
    for line in s.split("\n"):
        line = line.replace(" ", " ")
        line = WS_RE.sub(" ", line).strip()
        if line:
            out.append(line)
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────
# Period detection
# ─────────────────────────────────────────────────────────────────────

def _find_period_num_in(scope: str):
    for pat in PERIOD_PATS:
        m = re.search(pat, scope)
        if m:
            return int(m.group(1))
    m = re.search(r"第([一二三四五六七])", scope)
    if m:
        return CHN_NUM[m.group(1)]
    return None


def parse_period_num(text: str, class_line: str = None):
    """Find the period number, preferring the CLASS line if present.

    Falls back to scanning the first ~1500 chars (header region) so we don't
    accidentally pick up Q5's 'Sodium is in Period 3' line further down.
    """
    if class_line:
        n = _find_period_num_in(class_line)
        if n:
            return n
    return _find_period_num_in(text[:1500])


# ─────────────────────────────────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────────────────────────────────

def parse_time_to_seconds(text: str) -> int:
    m = re.match(r"(?:(\d+)m\s*)?(\d+)s", text.strip())
    if not m:
        return 0
    return (int(m.group(1) or 0)) * 60 + int(m.group(2))


def fmt_seconds(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"


# ─────────────────────────────────────────────────────────────────────
# File parsing
# ─────────────────────────────────────────────────────────────────────

def file_to_student_id(path: Path) -> str:
    """allensofia_36930_text.html → allensofia_36930."""
    stem = path.stem
    if stem.endswith("_text"):
        stem = stem[:-5]
    return stem


def parse_header(text: str) -> dict:
    """Lightweight header extraction — used by organize.py.

    Returns lesson, date, period, period_num, student, plus the optional
    teacher line (for metadata.json)."""
    out = {
        "student": None, "lesson": None, "date": None,
        "period": None, "period_num": None, "teacher": None,
    }
    m = re.search(r"STUDENT\s*:?\s*([^\n]+)", text)
    if m: out["student"] = m.group(1).strip()
    m = re.search(r"LESSON\s*:?\s*([^\n]+)", text)
    if m: out["lesson"] = m.group(1).strip()
    m = re.search(r"DATE\s*:?\s*([^\n]+)", text)
    if m: out["date"] = m.group(1).strip()
    cm = re.search(r"CLASS\s*:?\s*([^\n]+)", text)
    if cm: out["period"] = cm.group(1).strip()
    out["period_num"] = parse_period_num(text, class_line=out["period"])
    m = re.search(r"To:\s*(Ms?\.[^—\n]+|Mr?\.[^—\n]+)", text)
    if m: out["teacher"] = m.group(1).strip()
    return out


def extract_question_prompts(text: str) -> dict:
    """Auto-extract Q<n> prompt text from any one student's submission.

    The prompts are identical across all students of a given worksheet —
    only the chosen letters differ. Pulls the text between "Q<n> ·" and
    the next "Answer:" line, truncated to a short label suitable for
    rendering as a per-question header. Returns {qid: prompt_string}.
    """
    prompts: dict[str, str] = {}
    pat = re.compile(r"(Q\d+)\s*·\s*(.+?)(?:\n|$)", re.S)
    for m in pat.finditer(text):
        qid = m.group(1)
        if qid in prompts:
            continue
        prompt = m.group(2).strip()
        # Strip the "Answer:" tail if it landed in the capture due to no newline
        prompt = re.split(r"\s*Answer:", prompt, maxsplit=1)[0].strip()
        if prompt and not prompt.startswith("Answer"):
            prompts[qid] = prompt
    return prompts


def parse_submission(path: Path, section: str = "honors",
                     answer_key: dict | None = None) -> dict:
    """Full parse — returns the rich record used by build_gradebook.py.

    answer_key: dict mapping Q<n> → correct letter (A/B/C/D). If None or
    empty, MC `correct` flags are set to None (signals "no key available";
    grade_summary will not produce mc_pct in that case).
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = strip_tags(raw)

    rec = {
        "id": file_to_student_id(path),
        "file": path.name,
        "section": section,
        "student": None,
        "period": None,
        "period_num": None,
        "period_key": None,
        "date": None,
        "time_on_task_s": 0,
        "answers": {},
        "games": {},
        "essay": "",
        "session_start": None,
        "submitted_at": None,
        "cards_flipped": [0, 6],
        "cards_list": "",
        "callouts": [0, 4],
        "callouts_list": "",
        "section_times_s": {},
    }

    m = re.search(r"STUDENT\s*:?\s*([^\n]+)", text)
    if m: rec["student"] = m.group(1).strip()
    m = re.search(r"DATE\s*:?\s*([^\n]+)", text)
    if m: rec["date"] = m.group(1).strip()
    m = re.search(r"TIME ON TASK\s*:?\s*([^\n]+)", text)
    if m: rec["time_on_task_s"] = parse_time_to_seconds(m.group(1))

    cm = re.search(r"CLASS\s*:?\s*([^\n]+)", text)
    class_line = cm.group(1).strip() if cm else None
    if class_line:
        rec["period"] = class_line
    period_num = parse_period_num(text, class_line=class_line)
    if period_num:
        rec["period_num"] = period_num
        rec["period_key"] = f"{section}-{period_num}"
        if not rec["period"]:
            rec["period"] = f"Period {period_num}"

    # Determine which QIDs to look for. Prefer the explicit answer_key (so we
    # only score what the teacher has graded). Otherwise probe the file for
    # whatever Q<n> lines exist, so we still capture chosen letters even when
    # no key is available yet.
    qids: list[str] = list(answer_key.keys()) if answer_key else \
        sorted(set(re.findall(r"\b(Q\d+)\s*·", text)),
               key=lambda q: int(q[1:]))
    for qid in qids:
        pat = rf"{qid}\s*·.*?\nAnswer:\s*([A-D])\s*·\s*([^\n]+)"
        m = re.search(pat, text, flags=re.S)
        if not m:
            rec["answers"][qid] = {"chosen": None, "correct": None, "changed_from": None}
            continue
        chosen = m.group(1)
        rest = m.group(2)
        cf = re.search(r"\[changed from\s*([A-D])\]", rest)
        correct = (chosen == answer_key[qid]) if answer_key else None
        rec["answers"][qid] = {
            "chosen": chosen,
            "correct": correct,
            "changed_from": cf.group(1) if cf else None,
        }

    game_blocks = re.split(r"GAME\s+(\d)\s*·\s*", text)
    for i in range(1, len(game_blocks), 2):
        gid = int(game_blocks[i])
        body = game_blocks[i + 1]
        body = re.split(r"\n(?:GAME|────|═══|──── ESSENTIAL)", body, maxsplit=1)[0]
        m_score = re.search(r"Score:\s*(\d+)\s*/\s*(\d+)\s*placed\s*·\s*First-try accuracy:\s*(\d+)\s*/\s*(\d+)", body)
        if m_score:
            placed, total = int(m_score.group(1)), int(m_score.group(2))
            first_try, ft_total = int(m_score.group(3)), int(m_score.group(4))
        else:
            placed = total = first_try = ft_total = 0
        wrongs = re.findall(r"\((\d+)\s*wrong attempt", body)
        wrong_count = sum(int(w) for w in wrongs)
        rec["games"][gid] = {
            "placed": placed, "total": total,
            "first_try": first_try, "first_try_total": ft_total,
            "wrong_attempts": wrong_count,
            "attempts": ft_total + wrong_count,
        }

    m = re.search(r"Response:\s*\n(.+?)(?:\n══|\nENGAGEMENT|\Z)", text, flags=re.S)
    if m:
        essay = m.group(1).strip()
        essay = re.sub(r"\n+(?:[─═]{5,}.*)$", "", essay).strip()
        rec["essay"] = essay

    m = re.search(r"Session started\s*:?\s*([0-9:]+\s*[AP]M)", text)
    if m: rec["session_start"] = m.group(1)
    m = re.search(r"Submitted at\s*:?\s*([0-9:]+\s*[AP]M)", text)
    if m: rec["submitted_at"] = m.group(1)

    m = re.search(r"Element cards flipped\s*:?\s*(\d+)\s*/\s*(\d+)\s*\(([^)]+)\)", text)
    if m:
        rec["cards_flipped"] = [int(m.group(1)), int(m.group(2))]
        rec["cards_list"] = m.group(3).strip()

    m = re.search(r"Atomic callouts explored\s*:?\s*(\d+)\s*/\s*(\d+)\s*\(([^)]+)\)", text)
    if m:
        rec["callouts"] = [int(m.group(1)), int(m.group(2))]
        rec["callouts_list"] = m.group(3).strip()

    for label_key, pretty in [
        ("Element Cards", "element_cards"),
        ("Organizing Problem", "organizing"),
        ("Atomic Number", "atomic_number"),
        ("Periods & Groups", "periods_groups"),
        ("Three Regions", "three_regions"),
        ("Placement Games", "placement_games"),
        ("Essential Question", "essential_question"),
    ]:
        pat = rf"{re.escape(label_key)}\s*:?\s*([0-9ms\s]+)"
        m = re.search(pat, text)
        if m:
            rec["section_times_s"][pretty] = parse_time_to_seconds(m.group(1))

    return rec


# ─────────────────────────────────────────────────────────────────────
# Grading helpers
# ─────────────────────────────────────────────────────────────────────

def grade_summary(rec: dict) -> dict:
    """Compute the per-student summary.

    If no answer key was applied (every answer has correct=None), mc_pct is
    None and composite collapses to games_pct alone (or None if no games
    either). This lets gradebook.html show a 'no answer key' state instead
    of misleading zeroes.
    """
    has_key = any(q["correct"] is not None for q in rec["answers"].values())

    if has_key:
        mc_correct = sum(1 for q in rec["answers"].values() if q["correct"])
        mc_total = sum(1 for q in rec["answers"].values() if q["correct"] is not None)
        mc_pct = round(100 * mc_correct / mc_total) if mc_total else 0
    else:
        mc_correct = 0
        mc_total = len(rec["answers"]) or 0
        mc_pct = None

    ft = sum(g["first_try"] for g in rec["games"].values())
    ft_total = sum(g["first_try_total"] for g in rec["games"].values())
    games_pct = round(100 * ft / ft_total) if ft_total else 0

    placed = sum(g["placed"] for g in rec["games"].values())
    placed_total = sum(g["total"] for g in rec["games"].values())

    if mc_pct is not None and ft_total:
        composite = round(0.5 * mc_pct + 0.5 * games_pct)
    elif mc_pct is not None:
        composite = mc_pct
    elif ft_total:
        composite = games_pct  # graceful: no MC key, but games provide a number
    else:
        composite = None

    return {
        "mc_correct": mc_correct, "mc_total": mc_total, "mc_pct": mc_pct,
        "games_first_try": ft, "games_first_try_total": ft_total, "games_pct": games_pct,
        "games_placed": placed, "games_placed_total": placed_total,
        "composite_pct": composite,
        "essay_words": len(rec["essay"].split()) if rec["essay"] else 0,
        "has_answer_key": has_key,
    }


def bucket(pct):
    if pct >= 95: return "perfect"
    if pct >= 85: return "strong"
    if pct >= 70: return "passing"
    if pct >= 50: return "shaky"
    return "needs_help"


BUCKET_ORDER = ["perfect", "strong", "passing", "shaky", "needs_help"]


def bucket_breakdown(this_bucket, records):
    """For a given bucket, count students by relationship:
       qualified (in this bucket), almost (one band away), far (>=2 bands)."""
    this_idx = BUCKET_ORDER.index(this_bucket)
    qualified = almost = far = 0
    for r in records:
        b = bucket(r["summary"]["composite_pct"])
        diff = abs(BUCKET_ORDER.index(b) - this_idx)
        if diff == 0: qualified += 1
        elif diff == 1: almost += 1
        else: far += 1
    return qualified, almost, far


def last_name_key(r):
    n = r.get("student") or r.get("file") or ""
    parts = n.split()
    return (parts[-1] if parts else n).lower()
