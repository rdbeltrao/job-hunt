from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

from jobs import settings
from jobs.adapters import linkedin_guest, personio
from jobs.config import load_companies, load_searches, load_title_keywords, title_matches
from jobs.http import PoliteClient
from jobs.models import Job
from jobs.seen import load_seen, qualify, save_seen
from jobs.store import load_jobs, save_jobs

log = logging.getLogger("jobs")

LOGIN_HINT = (
    "LinkedIn asked for a sign-in or a security checkpoint, so the run stopped.\n"
    "Run `uv run python -m jobs login`, sign in yourself in the browser window that opens, "
    "then run `fetch` again."
)


def dedupe(jobs: list[Job]) -> list[Job]:
    seen: set[tuple[str, str]] = set()
    out: list[Job] = []
    for job in jobs:
        if job.key in seen:
            continue
        seen.add(job.key)
        out.append(job)
    return out


def write_jsonl(jobs: list[Job], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for job in jobs:
            fh.write(json.dumps(job.to_dict(), ensure_ascii=False) + "\n")


SCORE_FIELDS = ("first_seen", "score", "verdict", "strong_matches", "gaps", "red_flags", "pitch", "notes", "scoring")


def carry_over_scores(jobs: list[Job], previous: list[Job]) -> int:
    """Copy scoring results from an earlier jobs.jsonl so a re-fetch does not trigger re-scoring."""
    by_id = {j.job_id: j for j in previous}
    carried = 0
    for job in jobs:
        old = by_id.get(job.job_id)
        if old is None or (old.score is None and not old.verdict):
            continue
        for f in SCORE_FIELDS:
            setattr(job, f, getattr(old, f))
        carried += 1
    return carried


def format_table(jobs: list[Job]) -> str:
    cols = [
        ("source", 9),
        ("posted", 10),
        ("title", 48),
        ("company", 28),
        ("location", 30),
        ("workplace", 9),
        ("seniority", 16),
        ("desc", 5),
        ("score", 5),
        ("verdict", 7),
    ]
    header = "  ".join(name.ljust(width) for name, width in cols)
    lines = [header, "-" * len(header)]
    for j in jobs:
        row = [
            j.source, j.posted_date, j.title, j.company, j.location, j.workplace, j.seniority,
            "yes" if j.description else "no", "" if j.score is None else str(j.score), j.verdict,
        ]
        lines.append("  ".join(_fit(v, w) for v, (_, w) in zip(row, cols)))
    return "\n".join(lines)


def _fit(value: str, width: int) -> str:
    value = value or ""
    return (value[: width - 1] + "…") if len(value) > width else value.ljust(width)


def run_login(args: argparse.Namespace) -> int:
    from jobs.browser import first_page, persistent_context, wait_for_login

    print("Opening LinkedIn in a browser window. Sign in there; this script never types credentials.")
    print(f"Waiting up to {args.timeout // 60} minutes for the login to complete...")
    with persistent_context(headless=False) as context:
        page = first_page(context)
        ok = wait_for_login(page, timeout_s=args.timeout)
    if ok:
        print("Signed in. The session is stored in the browser profile; you can close this now.")
        return 0
    print("Timed out waiting for the login. Nothing was saved; run `login` again.")
    return 1


def run_fetch(args: argparse.Namespace) -> int:
    all_jobs: list[Job] = []
    failures: list[str] = []
    login_required = False

    searches = [s for s in load_searches(Path(args.searches)) if s.enabled]
    if not searches and not any(c.enabled for c in load_companies(Path(args.companies))):
        print(f"fetch: nothing configured. Copy searches.example.yaml to {args.searches} and companies.example.yaml to {args.companies}, then edit them.")
        return 1
    random.shuffle(searches)
    browser_searches = [] if args.skip_linkedin else [s for s in searches if s.source == "linkedin"]
    guest_searches = [s for s in searches if s.source == "linkedin_guest"]
    for s in searches:
        if s.source not in ("linkedin", "linkedin_guest"):
            failures.append(f"search {s.name}: unsupported source {s.source!r}")

    seen_path = Path(args.seen)
    seen = load_seen(seen_path)

    if browser_searches:
        from jobs.adapters import linkedin_browser
        from jobs.browser import LoginRequired, persistent_context

        skip = {sid.split(":", 1)[1] for sid in seen if sid.startswith("linkedin:")}
        dump_dir = Path(args.dump_html) if args.dump_html else None
        try:
            with persistent_context(headless=not args.headed) as context:
                for search in browser_searches:
                    max_pages = args.max_pages if args.max_pages is not None else search.max_pages
                    try:
                        jobs = linkedin_browser.fetch(
                            context,
                            search.url,
                            search.name,
                            max_pages=max_pages,
                            skip_ids=skip,
                            fetch_details=not args.no_details,
                            dump_dir=dump_dir,
                        )
                    except LoginRequired as exc:
                        failures.append(f"linkedin {search.name}: login required (landed on {exc})")
                        login_required = True
                        break
                    except Exception as exc:  # noqa: BLE001 - report and continue with other sources
                        failures.append(f"linkedin {search.name}: {type(exc).__name__}: {exc}")
                        log.exception("linkedin search %s failed", search.name)
                        continue
                    all_jobs.extend(jobs)
                    print(f"[linkedin] {search.name}: {len(jobs)} new jobs", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - browser could not start at all
            failures.append(f"linkedin browser: {type(exc).__name__}: {exc}")
            log.exception("browser session failed")

    client = PoliteClient()
    for search in guest_searches:
        max_pages = args.max_pages if args.max_pages is not None else search.max_pages
        try:
            jobs = linkedin_guest.fetch(search.url, max_pages=max_pages, client=client, fetch_details=not args.no_details)
            for j in jobs:
                j.extra["search"] = search.name
            all_jobs.extend(jobs)
            print(f"[linkedin_guest] {search.name}: {len(jobs)} jobs", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"linkedin_guest {search.name}: {type(exc).__name__}: {exc}")
            log.exception("linkedin_guest search %s failed", search.name)

    from jobs.adapters import ashby, greenhouse

    company_fetchers = {"personio": personio.fetch, "greenhouse": greenhouse.fetch, "ashby": ashby.fetch}
    title_keywords = load_title_keywords(Path(args.companies))
    for company in load_companies(Path(args.companies)):
        if not company.enabled:
            continue
        fetcher = company_fetchers.get(company.ats)
        if fetcher is None:
            failures.append(f"company {company.name}: unsupported ats {company.ats!r} (use one of {', '.join(company_fetchers)})")
            continue
        try:
            jobs = fetcher(company.slug, company.name, client=client)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{company.ats} {company.name} ({company.slug}): {type(exc).__name__}: {exc}")
            log.exception("%s %s failed", company.ats, company.slug)
            continue
        kept = [j for j in jobs if title_matches(j.title, title_keywords)]
        all_jobs.extend(kept)
        print(f"[{company.ats}] {company.name}: {len(kept)} jobs kept, {len(jobs) - len(kept)} dropped by title filter", file=sys.stderr)

    jobs = dedupe(all_jobs)
    out_path = Path(args.out)
    carry_over_scores(jobs, load_jobs(out_path))
    write_jsonl(jobs, out_path)
    if not args.no_details:
        # Only jobs whose details were actually opened count as seen.
        seen.update(qualify(j.source, j.source_id) for j in jobs if j.source == "linkedin" and j.description)
        save_seen(seen, seen_path)

    print(format_table(jobs))
    print(f"\n{len(jobs)} unique jobs ({len(all_jobs) - len(jobs)} duplicates dropped) written to {out_path}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  - {f}")
    if login_required:
        print(f"\n{LOGIN_HINT}")
        return 2
    return 0 if jobs or not failures else 1


PROFILE_PATH = Path("profile.md")


def _load_profile(path: Path) -> str | None:
    if not path.exists():
        print(f"score: profile file {path} not found. Copy profile.example.md to profile.md and fill it in first.")
        return None
    profile = path.read_text(encoding="utf-8").strip()
    if not profile:
        print(f"score: {path} is empty.")
        return None
    return profile


def score_jobs(jobs: list[Job], profile: str, api_key: str, explain_pass: bool, save) -> int:
    """Score every job that still needs it (and explain the promising ones). `save` persists progress.

    Returns 0 on success, 1 when every scored job failed or the scorer is misconfigured.
    """
    from jobs.scoring import explain, jev, llm

    scorer_name = settings.env(settings.SCORER) or "jev"
    if scorer_name not in ("jev", "llm"):
        print(f"score: unknown {settings.SCORER}={scorer_name!r}; use 'jev' or 'llm'.")
        return 1
    llm_model = settings.env(settings.SCORE_MODEL) or llm.MODEL
    client = jev.JevClient(api_key) if scorer_name == "jev" else llm.LlmScorer(api_key, model=llm_model)
    log.info("score: using %s scorer (%s)", scorer_name, getattr(client, "model", ""))
    todo = [j for j in jobs if jev.needs_scoring(j)]
    scored = failed = no_desc = 0
    for job in todo:
        if not job.description.strip():
            jev.mark_no_description(job)
            no_desc += 1
            continue
        try:
            jev.apply_result(job, client.score_job(profile, job))
            scored += 1
        except jev.JevError as exc:
            if jev.is_provider_blocked(exc) and scorer_name == "jev":
                # The account cannot reach the typesafe provider; fall back for the rest of the run.
                print(f"score: Jev unavailable on this OpenRouter account ({exc}); falling back to {llm_model}")
                scorer_name, client = "llm", llm.LlmScorer(api_key, model=llm_model)
                try:
                    jev.apply_result(job, client.score_job(profile, job))
                    scored += 1
                    continue
                except Exception as exc2:  # noqa: BLE001
                    exc = exc2
            failed += 1
            log.warning("score: %s (%s) failed: %s", job.title, job.job_id, exc)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the batch
            failed += 1
            log.warning("score: %s (%s) failed: %s", job.title, job.job_id, exc)
    save()
    print(f"score ({scorer_name}): {scored} scored, {no_desc} without description (verdict maybe), {failed} failed, {len(jobs) - len(todo)} already scored")

    if explain_pass:
        explain_model = settings.env(settings.EXPLAIN_MODEL) or explain.MODEL
        client_ai = explain.make_client(api_key, model=explain_model)
        todo = [j for j in jobs if explain.needs_explanation(j)]
        done = failed = 0
        for job in todo:
            try:
                explain.apply_explanation(job, explain.explain_job(client_ai, profile, job))
                done += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.warning("explain: %s (%s) failed: %s", job.title, job.job_id, exc)
        save()
        print(f"explain: {done} explained (fit >= {explain.criteria.EXPLAIN_THRESHOLD}), {failed} failed")
    return 0 if failed == 0 or scored else 1


def run_score(args: argparse.Namespace) -> int:
    jobs_path = Path(args.jobs)
    jobs = load_jobs(jobs_path)
    if not jobs:
        print(f"score: nothing to score, {jobs_path} is empty or missing")
        return 0
    profile = _load_profile(Path(args.profile))
    if profile is None:
        return 1
    api_key = settings.openrouter_key()
    if api_key is None:
        print(f"score: skipped, {' / '.join(settings.OPENROUTER_API_KEY_NAMES)} is not set (Keychain, .env or shell).")
        return 1
    return score_jobs(jobs, profile, api_key, args.explain, save=lambda: save_jobs(jobs, jobs_path))


def run_rescore(args: argparse.Namespace) -> int:
    """Score rows already in the CSV again, after profile.md or criteria.py changed."""
    from jobs import export

    csv_path = Path(args.csv)
    try:
        rows = export.read_rows(csv_path)
    except export.HeaderMismatch as exc:
        print(f"rescore: refused: {exc}")
        return 1
    if not rows:
        print(f"rescore: nothing to do, {csv_path} is empty or missing")
        return 0
    profile = _load_profile(Path(args.profile))
    if profile is None:
        return 1
    api_key = settings.openrouter_key()
    if api_key is None:
        print(f"rescore: skipped, {' / '.join(settings.OPENROUTER_API_KEY_NAMES)} is not set (Keychain, .env or shell).")
        return 1

    wanted = set(args.job_ids or [])
    selected = [r for r in rows if not wanted or r["job_id"] in wanted]
    if args.status:
        selected = [r for r in selected if r["status"] == args.status]
    pairs = [(row, export.row_to_job(row)) for row in selected]
    jobs = [job for _, job in pairs]
    print(f"rescore: {len(jobs)} of {len(rows)} rows selected")

    def save() -> None:
        for row, job in pairs:
            export.apply_scores(row, job)
        export.write_rows(csv_path, rows)

    return score_jobs(jobs, profile, api_key, args.explain, save=save)


EXPLAIN_FIELDS = ("strong_matches", "gaps", "red_flags", "pitch")


def run_explain(args: argparse.Namespace) -> int:
    """Hand explanations to whoever writes them (a Claude session, a person) and take them back.

    --list writes the jobs that still need an explanation as JSON; --apply reads the same
    shape back with the four fields filled in and writes them into the CSV. No API call.
    """
    import json

    from jobs import export
    from jobs.scoring import criteria

    csv_path = Path(args.csv)
    try:
        rows = export.read_rows(csv_path)
    except export.HeaderMismatch as exc:
        print(f"explain: refused: {exc}")
        return 1
    threshold = args.min_fit if args.min_fit is not None else criteria.EXPLAIN_THRESHOLD

    if args.apply:
        try:
            given = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"explain: cannot read {args.apply}: {exc}")
            return 1
        by_id = {r["job_id"]: r for r in rows}
        applied = skipped = 0
        for item in given if isinstance(given, list) else given.get("jobs", []):
            row = by_id.get(item.get("job_id", ""))
            if row is None:
                skipped += 1
                continue
            for field in EXPLAIN_FIELDS:
                if field in item:
                    row[field] = str(item[field] or "").strip()
            applied += 1
        export.write_rows(csv_path, rows)
        print(f"explain: {applied} rows updated in {csv_path}, {skipped} job_ids not found")
        return 0

    pending = [
        r for r in rows
        if r["score"] and int(r["score"]) >= threshold and not (r["strong_matches"] or r["gaps"] or r["pitch"])
    ]
    profile = Path(args.profile).read_text(encoding="utf-8").strip() if Path(args.profile).exists() else ""
    payload = {
        "profile": profile,
        "instructions": criteria.EXPLAIN_SYSTEM + " " + criteria.EXPLAIN_INSTRUCTIONS,
        "fields": list(EXPLAIN_FIELDS),
        "jobs": [
            {
                "job_id": r["job_id"],
                "title": r["title"],
                "company": r["company"],
                "location": r["location"],
                "workplace": r["workplace"],
                "score": int(r["score"]),
                "verdict": r["verdict"],
                "url": r["url"],
                "description": r["description"][: criteria.MAX_DESCRIPTION_CHARS],
            }
            for r in pending
        ],
    }
    out = Path(args.out) if args.out else None
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"explain: {len(pending)} jobs with fit >= {threshold} need an explanation; written to {out}")
    else:
        print(text)
    return 0


def run_status(args: argparse.Namespace) -> int:
    """Show or change the application status of rows in the CSV (the status and notes columns)."""
    from collections import Counter

    from jobs import export

    csv_path = Path(args.csv)
    try:
        rows = export.read_rows(csv_path)
    except export.HeaderMismatch as exc:
        print(f"status: refused: {exc}")
        return 1
    if args.set or args.note:
        if args.set and args.set not in export.STATUSES:
            print(f"status: unknown status {args.set!r}; one of {', '.join(export.STATUSES)}")
            return 1
        if not args.job_ids:
            print("status: --set and --note need at least one job_id")
            return 1
        by_id = {r["job_id"]: r for r in rows}
        missing = [j for j in args.job_ids if j not in by_id]
        if missing:
            print(f"status: job_id not in {csv_path}: {', '.join(missing)}")
            return 1
        for job_id in args.job_ids:
            row = by_id[job_id]
            # --note alone keeps the status and only adds a dated line to the trail.
            status = args.set or row["status"] or export.DEFAULT_STATUS
            export.set_status(row, status, args.note or "", args.date)
            print(f"status: {job_id} -> {status}" + (f" ({args.note})" if args.note and not args.set else ""))
        export.write_rows(csv_path, rows)
        return 0
    counts = Counter(r["status"] for r in rows)
    print("status: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    for r in rows:
        if r["status"] != export.DEFAULT_STATUS and (not args.job_ids or r["job_id"] in args.job_ids):
            last = r["notes"].splitlines()[-1] if r["notes"] else ""
            print(f"  {r['status']:<10} {r['job_id']:<22} {r['company']} - {r['title']}  {last}")
    return 0


def run_network(args: argparse.Namespace) -> int:
    """Weekly LinkedIn connection plan from the CSV, or mark companies as covered (--done)."""
    from jobs import export, network

    done_path = Path(args.done_file)
    done = network.load_done(done_path)
    if args.done:
        network.mark_done(args.done, done, args.date)
        network.save_done(done, done_path)
        print(f"network: marked done: {', '.join(args.done)} (recorded in {done_path})")
        return 0
    csv_path = Path(args.csv)
    try:
        rows = export.read_rows(csv_path)
    except export.HeaderMismatch as exc:
        print(f"network: refused: {exc}")
        return 1
    plans = network.rank_companies(rows, done)
    if args.limit:
        plans = plans[: args.limit]
    roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else None
    text = network.render_plan(plans, roles=roles, quota=args.quota, today=args.date)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"network: {len(plans)} companies in this week's plan; written to {out}")
    else:
        print(text, end="")
    return 0


def run_outreach(args: argparse.Namespace) -> int:
    """Write the outreach pack (job, profile, people searches, message shapes) for one opening as JSON."""
    import json

    from jobs import export, outreach

    csv_path = Path(args.csv)
    try:
        rows = export.read_rows(csv_path)
    except export.HeaderMismatch as exc:
        print(f"outreach: refused: {exc}")
        return 1
    row = next((r for r in rows if r["job_id"] == args.job_id), None)
    if row is None:
        print(f"outreach: job_id not in {csv_path}: {args.job_id}")
        return 1
    profile_path = Path(args.profile)
    profile = profile_path.read_text(encoding="utf-8").strip() if profile_path.exists() else ""
    if not profile:
        print(f"outreach: {profile_path} not found or empty; the pack has no profile section")
    text = json.dumps(outreach.build_pack(row, profile), ensure_ascii=False, indent=1)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"outreach: pack for {row['company']} - {row['title']} written to {out}")
    else:
        print(text)
    return 0


def run_engage(args: argparse.Namespace) -> int:
    """Collect the latest posts of the creators in creators.yaml (--list) or mark posts commented on (--done)."""
    import json

    from jobs import engage

    done_path = Path(args.done_file)
    done = engage.load_done(done_path)
    if args.done:
        engage.mark_done(args.done, done, args.date)
        engage.save_done(done, done_path)
        print(f"engage: marked done: {len(args.done)} posts (recorded in {done_path})")
        return 0
    creators = engage.load_creators(Path(args.creators))
    if not creators:
        print(f"engage: no creators configured. Copy creators.example.yaml to {args.creators} and add 3 to 5 people whose posts you want to comment on.")
        return 1
    from jobs.adapters import linkedin_activity
    from jobs.browser import LoginRequired, persistent_context

    posts = []
    failures: list[str] = []
    dump_dir = Path(args.dump_html) if args.dump_html else None
    try:
        with persistent_context(headless=not args.headed) as context:
            for creator in creators:
                try:
                    found = linkedin_activity.fetch_recent_posts(context, creator.url, max_posts=args.max_posts, dump_dir=dump_dir)
                except LoginRequired as exc:
                    failures.append(f"{creator.name}: login required (landed on {exc})")
                    print(LOGIN_HINT, file=sys.stderr)
                    break
                except Exception as exc:  # noqa: BLE001 - one creator failing must not stop the others
                    failures.append(f"{creator.name}: {type(exc).__name__}: {exc}")
                    log.exception("activity fetch for %s failed", creator.name)
                    continue
                for post in found:
                    post.creator = creator.name
                    post.priority = creator.priority
                posts.extend(found)
                print(f"[engage] {creator.name}: {len(found)} posts", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"browser: {type(exc).__name__}: {exc}")
        log.exception("browser session failed")
    payload = engage.build_pending(posts, done, max_age_days=args.max_age_days)
    payload["failures"] = failures
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"engage: {len(payload['posts'])} posts to consider ({len(payload['skipped_done'])} already commented); written to {out}")
    else:
        print(text)
    for f in failures:
        print(f"engage: failed: {f}", file=sys.stderr)
    return 0 if posts or not failures else 1


def run_export(args: argparse.Namespace) -> int:
    from jobs import export

    jobs = load_jobs(Path(args.jobs))
    if not jobs:
        print(f"export: nothing to export, {args.jobs} is empty or missing")
        return 0
    csv_path = Path(args.csv)
    try:
        appended, skipped = export.export_jobs(csv_path, jobs)
    except export.HeaderMismatch as exc:
        print(f"export: refused: {exc}. Move that file aside or point --csv elsewhere.")
        return 1
    except OSError as exc:
        print(f"export: failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"export: {appended} rows appended, {skipped} skipped (already in {csv_path})")
    return 0


def run_pipeline(args: argparse.Namespace) -> int:
    """Feed career-ops: append new openings to data/pipeline.md, data/jd-index.tsv and jds/feeder/ in the data root."""
    from datetime import date

    from jobs import pipeline_md

    data_root = getattr(args, "data_root", None) or settings.data_root()
    if not data_root:
        print(f"pipeline: skipped, no data root: pass --data-root or set {settings.DATA_ROOT} to the career-ops data directory.")
        return 1
    jobs = load_jobs(Path(args.jobs))
    if not jobs:
        print(f"pipeline: nothing to feed, {args.jobs} is empty or missing")
        return 0
    root = Path(data_root).expanduser()
    today = getattr(args, "date", None) or date.today().isoformat()
    try:
        result = pipeline_md.export_to_data_root(jobs, root, today)
    except OSError as exc:
        print(f"pipeline: failed: {type(exc).__name__}: {exc}")
        return 1
    print(
        f"pipeline: {result.added} rows added to {root / pipeline_md.PIPELINE_RELATIVE}, "
        f"{result.skipped_existing} already there, {result.written_jds} JD files written"
    )
    return 0


def run_all(args: argparse.Namespace) -> int:
    """fetch, (pipeline when a data root is given), score, export in order; each step runs even when the previous one failed."""
    steps = [("fetch", run_fetch)]
    if getattr(args, "data_root", None):
        steps.append(("pipeline", run_pipeline))
    steps += [("score", run_score), ("export", run_export)]
    results: list[tuple[str, int]] = []
    for name, func in steps:
        print(f"\n=== {name} ===")
        try:
            code = func(args)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s crashed", name)
            print(f"{name}: crashed: {type(exc).__name__}: {exc}")
            code = 1
        results.append((name, code))
    print("\n=== summary ===")
    for name, code in results:
        print(f"  {name}: {'ok' if code == 0 else f'failed or skipped (exit {code})'}")
    return 0 if all(code == 0 for _, code in results) else 1


def _add_fetch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--searches", default="searches.yaml")
    p.add_argument("--companies", default="companies.yaml")
    p.add_argument("--out", default="out/jobs.jsonl")
    p.add_argument("--seen", default="out/seen_ids.json", help="ids already opened in earlier runs; they are skipped")
    p.add_argument("--max-pages", type=int, default=None, help="override max_pages for every LinkedIn search")
    p.add_argument("--no-details", action="store_true", help="skip opening each job (no description, ids not marked seen)")
    p.add_argument("--headed", action="store_true", help="show the browser window while fetching")
    p.add_argument("--skip-linkedin", action="store_true", help="company boards only; leave the LinkedIn browser profile untouched")
    p.add_argument("--dump-html", nargs="?", const="out/debug", default=None, help="save raw list/detail HTML for selector debugging (default dir out/debug)")


def _add_score_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--jobs", default="out/jobs.jsonl")
    p.add_argument("--profile", default=str(PROFILE_PATH))
    p.add_argument("--explain", action="store_true", help="optional API pass: an LLM via OpenRouter writes strong_matches/gaps/red_flags/pitch for fit >= 60 (costs money; `jobs explain` does it without an API)")


def _add_export_args(p: argparse.ArgumentParser) -> None:
    if not any(a.dest == "jobs" for a in p._actions):
        p.add_argument("--jobs", default="out/jobs.jsonl")
    p.add_argument("--csv", default="out/jobs.csv", help="CSV the openings list is appended to (default out/jobs.csv)")


def _add_pipeline_args(p: argparse.ArgumentParser) -> None:
    if not any(a.dest == "jobs" for a in p._actions):
        p.add_argument("--jobs", default="out/jobs.jsonl")
    p.add_argument(
        "--data-root",
        default=settings.data_root(),
        metavar="PATH",
        help=f"career-ops data directory to feed (default from {settings.DATA_ROOT}); writes data/pipeline.md, data/jd-index.tsv and jds/feeder/",
    )
    p.add_argument("--date", default=None, help="first_seen for jobs that have none (default today, ISO 8601)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobs", description="Fetch job postings into normalized records.")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="open a browser so you can sign in to LinkedIn; the session is kept for fetch")
    login.add_argument("--timeout", type=int, default=600, help="seconds to wait for the sign-in (default 600)")
    login.set_defaults(func=run_login)

    fetch = sub.add_parser("fetch", help="run all configured searches and companies")
    _add_fetch_args(fetch)
    fetch.set_defaults(func=run_fetch)

    score = sub.add_parser("score", help="score unscored jobs in out/jobs.jsonl against profile.md (LLM via OpenRouter, or Jev)")
    _add_score_args(score)
    score.set_defaults(func=run_score)

    export = sub.add_parser("export", help="append new jobs to the CSV openings list")
    _add_export_args(export)
    export.set_defaults(func=run_export)

    pipeline = sub.add_parser("pipeline", help="feed career-ops: append new openings from out/jobs.jsonl to the data root's pipeline.md, jd-index.tsv and jds/feeder/")
    _add_pipeline_args(pipeline)
    pipeline.set_defaults(func=run_pipeline)

    status = sub.add_parser("status", help="show rows whose status is not new, or set the status of given job_ids")
    status.add_argument("--csv", default="out/jobs.csv")
    status.add_argument("--set", default=None, metavar="STATUS", help="new status for the given job_ids: new, skipped, draft, applied, replied, interview, offer, rejected, withdrawn")
    status.add_argument("--note", default=None, help="text appended to notes with today's date and the status; without --set the status stays")
    status.add_argument("--date", default=None, help="date for the notes line (default today, ISO 8601)")
    status.add_argument("job_ids", nargs="*", help="rows to change (with --set) or to show")
    status.set_defaults(func=run_status)

    rescore = sub.add_parser("rescore", help="score rows already in the CSV again (after profile.md or criteria.py changed)")
    rescore.add_argument("--csv", default="out/jobs.csv")
    rescore.add_argument("--profile", default=str(PROFILE_PATH))
    rescore.add_argument("--explain", action="store_true", help="also redo strong_matches/gaps/red_flags/pitch for fit >= 60")
    rescore.add_argument("--status", default=None, help="only rows with this status (for example new)")
    rescore.add_argument("job_ids", nargs="*", help="only these job_ids; default every row")
    rescore.set_defaults(func=run_rescore)

    explain = sub.add_parser("explain", help="list jobs that need an explanation as JSON, or apply explanations written elsewhere")
    explain.add_argument("--csv", default="out/jobs.csv")
    explain.add_argument("--profile", default=str(PROFILE_PATH))
    explain.add_argument("--min-fit", type=int, default=None, help="fit threshold (default criteria.EXPLAIN_THRESHOLD)")
    explain.add_argument("--out", default=None, help="with --list: write the JSON here instead of stdout")
    group = explain.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="print or write the pending jobs with the profile and instructions")
    group.add_argument("--apply", metavar="JSON", help="read {jobs: [{job_id, strong_matches, gaps, red_flags, pitch}]} and write it into the CSV")
    explain.set_defaults(func=run_explain)

    network = sub.add_parser("network", help="weekly LinkedIn connection plan from the CSV (companies with apply/maybe openings or applications), or --done to mark companies covered")
    network.add_argument("--csv", default="out/jobs.csv")
    network.add_argument("--done-file", default=str(Path("out/network.json")), help="where covered companies are recorded")
    network.add_argument("--limit", type=int, default=10, help="companies in the plan (default 10); 0 for all")
    network.add_argument("--quota", type=int, default=100, help="invitations per week to spread over the plan (default 100)")
    network.add_argument("--roles", default=None, help="comma-separated role families to search per company (default: EM, Head of Eng, VP Eng, CTO, Technical Recruiter)")
    network.add_argument("--out", default=None, help="write the Markdown plan here instead of stdout")
    network.add_argument("--date", default=None, help="date for the plan heading or the --done record (default today)")
    network.add_argument("--done", nargs="*", metavar="COMPANY", help="mark these companies as covered; they leave the plan until a new opening appears")
    network.set_defaults(func=run_network)

    outreach = sub.add_parser("outreach", help="JSON pack for writing a LinkedIn message about one opening: job, profile, people searches, message shapes")
    outreach.add_argument("job_id", help="row to build the pack for, for example linkedin:4477094289")
    outreach.add_argument("--csv", default="out/jobs.csv")
    outreach.add_argument("--profile", default=str(PROFILE_PATH))
    outreach.add_argument("--out", default=None, help="write the JSON here instead of stdout")
    outreach.set_defaults(func=run_outreach)

    engage = sub.add_parser("engage", help="latest posts of the creators in creators.yaml as JSON, to draft comments elsewhere; --done marks posts commented on")
    engage.add_argument("--creators", default="creators.yaml")
    engage.add_argument("--done-file", default=str(Path("out/engaged.json")), help="where commented post URLs are recorded")
    engage.add_argument("--out", default=None, help="write the JSON here instead of stdout (the scheduled run uses out/engage-pending.json)")
    engage.add_argument("--max-posts", type=int, default=3, help="posts to read per creator (default 3)")
    engage.add_argument("--max-age-days", type=int, default=3, help="ignore posts older than this (default 3)")
    engage.add_argument("--headed", action="store_true", help="show the browser window")
    engage.add_argument("--dump-html", nargs="?", const="out/debug", default=None, help="save each activity page's HTML for selector debugging")
    engage.add_argument("--date", default=None, help="date for the --done record (default today)")
    engage.add_argument("--done", nargs="*", metavar="POST_URL", help="mark these post URLs as commented on")
    engage.set_defaults(func=run_engage)

    run = sub.add_parser("run", help="fetch, score, export in one go (daily run); with --data-root also feeds career-ops right after fetch")
    _add_fetch_args(run)
    _add_score_args(run)
    _add_export_args(run)
    _add_pipeline_args(run)
    run.set_defaults(func=run_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return args.func(args)
