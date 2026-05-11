#!/usr/bin/env python3
"""
Organize raw worksheet submissions into the production data layout.

Reads HTML worksheet files from one or more raw folders, extracts the
lesson/date/period from each, and routes them into:

    data/<assignment-slug>_<date>/<period-slug>/<original-filename>

One folder per lesson, period — never per submission date. If a student
submits late, their file goes into the existing on-time folder (the
earliest data/<slug>_<date>/ that exists, or the earliest valid date in
the incoming batch if none exists yet). This prevents the same lesson
from showing up as two columns in master.html just because someone
turned it in two days late.

Each assignment folder gets a metadata.json describing the lesson,
teacher, sections, period counts, and an organize timestamp.

USAGE
─────
    python3 organize.py --honors  <raw-honors-folder>  \\
                        --regular <raw-regular-folder>

    # or generic — section auto-detected from CLASS line, or set per-folder:
    python3 organize.py FOLDER [FOLDER ...] [--section honors|regular|auto]

    # preview without moving:
    python3 organize.py --honors path/ --regular path/ --dry-run

    # specify output root (defaults to ../data relative to this script):
    python3 organize.py --honors path/ --regular path/ --output /other/data
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _parse import (
    parse_header, strip_tags, lesson_to_slug, period_slug, slugify,
    extract_question_prompts,
)

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data"


def collect(folder: Path, section: str):
    """Yield (path, parsed_header, section) tuples for every *_text.html file."""
    files = sorted(folder.glob("*_text.html"))
    for f in files:
        text = strip_tags(f.read_text(encoding="utf-8", errors="replace"))
        header = parse_header(text)
        # Section override: if 'auto', leave None for caller to decide
        sec = section if section != "auto" else (None)
        yield f, header, sec


def main():
    ap = argparse.ArgumentParser(
        description="Organize raw worksheet HTMLs into data/<assignment>/<period>/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("folders", nargs="*", type=Path,
                    help="Raw input folders (positional).")
    ap.add_argument("--honors", type=Path,
                    help="Folder of honors-tagged submissions.")
    ap.add_argument("--regular", type=Path,
                    help="Folder of regular-tagged submissions.")
    ap.add_argument("--section", default="auto",
                    choices=["auto", "honors", "regular"],
                    help="Default section for positional folders (default: auto).")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                    help="Destination root (default: ../data).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the routing plan without moving files.")
    args = ap.parse_args()

    # Build the (folder, section) work list
    sources = []
    if args.honors:
        sources.append((args.honors.resolve(), "honors"))
    if args.regular:
        sources.append((args.regular.resolve(), "regular"))
    for f in args.folders:
        sources.append((f.resolve(), args.section))

    if not sources:
        ap.error("No input folders given. Use --honors / --regular or positional folder args.")

    # Validate
    for folder, _ in sources:
        if not folder.is_dir():
            ap.error(f"Folder not found: {folder}")

    output_root: Path = args.output.resolve()
    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Organizing into: {output_root}\n")

    # Walk and route — first pass: extract slug + claimed date for each file.
    # We rewrite the assignment_folder to the canonical date below.
    plan = []  # [(src_path, dest_path, assignment_folder, period_slug, section, header)]
    unsorted = []
    incoming_dates_by_slug: dict[str, set[str]] = {}

    for folder, section in sources:
        print(f"  Reading: {folder}  (section: {section or 'auto'})")
        n = 0
        for f, header, sec in collect(folder, section):
            n += 1
            sec = sec or "regular"  # auto fallback
            if not header.get("lesson"):
                # Route to _unsorted/ so teacher can manually triage.
                dest = output_root / "_unsorted" / f.name
                unsorted.append((f, dest, "no LESSON header in file"))
                plan.append((f, dest, "_unsorted", "(no period)", sec, header))
                continue
            assignment_slug = lesson_to_slug(header["lesson"])
            date_str = (header.get("date") or "unknown-date").replace("/", "-")
            incoming_dates_by_slug.setdefault(assignment_slug, set()).add(date_str)
            # Tentative folder — rewritten in the canonical-date pass below.
            assignment_folder = f"{assignment_slug}_{date_str}"
            psl = period_slug(sec, header.get("period_num"))
            dest_dir = output_root / assignment_folder / psl
            dest = dest_dir / f.name
            plan.append((f, dest, assignment_folder, psl, sec, header))
        print(f"      {n} files inspected")

    # Canonical date resolution: pick exactly ONE folder per lesson slug.
    # Priority: earliest existing data/<slug>_<date>/ on disk, else earliest
    # valid date in the incoming batch. This is what prevents late submissions
    # from creating a separate <slug>_<later-date>/ folder.
    canonical_folder: dict[str, str] = {}
    reroutes: list[tuple[str, str, str]] = []  # (slug, from_date, to_date)
    for slug, dates in incoming_dates_by_slug.items():
        existing = sorted(
            d.name for d in output_root.glob(f"{slug}_*")
            if d.is_dir() and d.name != f"{slug}_unknown-date"
        )
        if existing:
            canonical_folder[slug] = existing[0]
        else:
            valid_dates = sorted(d for d in dates if d != "unknown-date")
            chosen = valid_dates[0] if valid_dates else "unknown-date"
            canonical_folder[slug] = f"{slug}_{chosen}"
        # Record reroutes for any incoming date that differs from the canonical
        _, canon_date = canonical_folder[slug].rsplit("_", 1)
        for d in dates:
            if d != canon_date:
                reroutes.append((slug, d, canon_date))

    if reroutes:
        print(f"\n  REROUTED — {len(reroutes)} late-submission date(s) consolidated:")
        for slug, frm, to in reroutes:
            print(f"    {slug}: {frm} → {to}")

    # Rewrite plan rows whose tentative folder doesn't match the canonical one.
    rewritten = []
    for src, dest, asg, psl, sec, header in plan:
        if asg == "_unsorted":
            rewritten.append((src, dest, asg, psl, sec, header))
            continue
        slug, _ = asg.rsplit("_", 1)
        new_asg = canonical_folder.get(slug, asg)
        if new_asg != asg:
            new_dest = output_root / new_asg / psl / src.name
            rewritten.append((src, new_dest, new_asg, psl, sec, header))
        else:
            rewritten.append((src, dest, asg, psl, sec, header))
    plan = rewritten

    if unsorted:
        print(f"\n  UNSORTED ({len(unsorted)} files routed to _unsorted/):")
        for path, dest, reason in unsorted:
            print(f"    {path.name} — {reason}")

    # Group by destination assignment
    by_assignment = {}
    for src, dest, asg, psl, sec, header in plan:
        by_assignment.setdefault(asg, {"periods": {}, "header": header, "rows": []})
        by_assignment[asg]["periods"].setdefault(psl, 0)
        by_assignment[asg]["periods"][psl] += 1
        by_assignment[asg]["rows"].append((src, dest, psl, sec, header))

    # Print plan
    print(f"\n  PLAN — {len(plan)} files, {len(by_assignment)} assignment(s):")
    for asg, info in sorted(by_assignment.items()):
        print(f"\n    {output_root.name}/{asg}/")
        for psl, count in sorted(info["periods"].items()):
            print(f"      {psl}/  ({count} files)")

    if args.dry_run:
        print("\n  Dry run — no files moved. Re-run without --dry-run to commit.\n")
        return

    # Execute moves
    moved = 0
    collisions = []
    for src, dest, asg, psl, sec, header in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            collisions.append((src, dest))
            continue
        shutil.move(str(src), str(dest))
        moved += 1

    # Write metadata.json per assignment (skip _unsorted).
    # Recount from disk so late-merged folders reflect total submissions,
    # not just files moved in this run.
    for asg, info in by_assignment.items():
        if asg == "_unsorted":
            continue
        asg_dir = output_root / asg
        meta_path = asg_dir / "metadata.json"
        existing_meta = {}
        if meta_path.exists():
            try:
                existing_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing_meta = {}

        # Count files actually on disk per period
        period_counts: dict[str, int] = {}
        for pdir in asg_dir.iterdir():
            if pdir.is_dir():
                n = sum(1 for _ in pdir.glob("*_text.html"))
                if n:
                    period_counts[pdir.name] = n

        # Identifying fields: prefer this batch's header (newest data),
        # fall back to existing metadata for stable identity.
        sample_header = info["rows"][0][4]
        lesson = sample_header.get("lesson") or existing_meta.get("lesson")
        title = (lesson or "").split("—", 1)[-1].strip() or existing_meta.get("title", "")
        teacher = sample_header.get("teacher") or existing_meta.get("teacher")
        # The folder is identified by the canonical (earliest) date — keep that.
        canon_date = asg.rsplit("_", 1)[1] if "_" in asg else None

        # Auto-extract question prompts from one student's submission. The
        # prompts are identical across all students of the same worksheet —
        # only the chosen letters differ. Skip if metadata already has
        # prompts (teacher may have edited them).
        question_prompts = existing_meta.get("question_prompts") or {}
        if not question_prompts:
            sample_path: Path = info["rows"][0][0]
            try:
                sample_text = strip_tags(sample_path.read_text(encoding="utf-8", errors="replace"))
                question_prompts = extract_question_prompts(sample_text)
            except Exception:
                question_prompts = {}

        meta = {
            "assignment_id": asg,
            "lesson": lesson,
            "title": title,
            "date": canon_date,
            "teacher": teacher,
            "total_students": sum(period_counts.values()),
            "period_counts": period_counts,
            # answer_key: filled in by the teacher. Until then, build_gradebook
            # renders a "no answer key" banner instead of misleading colors.
            "answer_key": existing_meta.get("answer_key"),
            "question_prompts": question_prompts,
            "organized_at": datetime.now().isoformat(timespec="seconds"),
        }
        # Preserve audit trail across consolidations.
        history = existing_meta.get("consolidated_runs", [])
        history.append({
            "at": meta["organized_at"],
            "added_files": len(info["rows"]),
        })
        meta["consolidated_runs"] = history
        if "merged_from" in existing_meta:
            meta["merged_from"] = existing_meta["merged_from"]
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # Summary
    print(f"\n  ✓ Moved {moved} files into {output_root.name}/")
    if collisions:
        print(f"  ⚠ {len(collisions)} collisions (files already at destination — left in source):")
        for src, dest in collisions[:10]:
            print(f"      {src.name} → {dest.relative_to(output_root)}")
    print()


if __name__ == "__main__":
    main()
