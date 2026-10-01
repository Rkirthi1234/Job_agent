"""Manual Monster diagnostic -- see what Browser Use sees. READ-ONLY.

Opens Monster's search page in the persistent Browser Use session (the
same MonsterJobSource._create_session() the job source uses), then prints
the page title, URL, job-link count, any restriction/CAPTCHA detection and
the first few extracted job cards. With --detail it also opens the FIRST
job's page and prints what it can read (description length, Apply href).

It never clicks, never types, never fills a form, never submits an
application. If Monster shows a CAPTCHA / verification / restriction /
login wall, it reports the blocker and stops -- it does not try to get
past it.

Usage (from the repo root, venv active):
    python scripts/monster_diagnostic.py
    python scripts/monster_diagnostic.py --keywords "python developer" --location "Austin, TX"
    python scripts/monster_diagnostic.py --detail
    python scripts/monster_diagnostic.py --keep-open    # leave the window open to look at it
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations.job_sources.monster import MonsterJobSource  # noqa: E402
from app.schemas.job_search import JobSearchRequest  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Monster diagnostic via Browser Use.")
    parser.add_argument("--keywords", default="python developer")
    parser.add_argument("--location", default="")
    parser.add_argument("--limit", type=int, default=5, help="max cards to print (default 5)")
    parser.add_argument("--detail", action="store_true", help="also open the first job's page (read-only)")
    parser.add_argument("--keep-open", action="store_true", help="wait for Enter before closing the browser")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    source = MonsterJobSource(fetch_descriptions=False)
    request = JobSearchRequest(keywords=args.keywords or None, location=args.location or None, limit=args.limit)
    url = source._build_search_url(request)

    print(f"Profile dir : {source._user_data_dir}")
    print(f"Headless    : {source._headless}")
    print(f"Search URL  : {url}")

    session = source._create_session()
    exit_code = 0
    try:
        await session.start()
        page = await source._goto(session, url)
        appeared = await source._wait_for_results(page)
        title = await source._page_title(page)
        blocked = await source._block_reason(page)
        jobs = await source._extract_jobs(page, args.limit)

        print(f"Page title  : {title!r}")
        print(f"Job links appeared within wait: {appeared}")
        print(f"Blocker     : {blocked or 'none detected'}")
        print(f"Cards parsed: {len(jobs)}")
        for i, job in enumerate(jobs, 1):
            print(f"  [{i}] {job.get('title')!r} | {job.get('company')!r} | {job.get('location')!r}")
            print(f"      url={job['url']}")
            print(f"      external_job_id={job.get('external_job_id')} salary={job.get('salary')!r} "
                  f"type={job.get('job_type')!r} posted={job.get('posted')!r}")

        if blocked and not jobs:
            print("\nBLOCKER: Monster is restricting this browser. Stopping. Nothing was bypassed.")
            exit_code = 2
        elif args.detail and jobs:
            first = jobs[0]
            print(f"\nOpening first job page (read-only): {first['url']}")
            job_page = await source._goto(session, first["url"])
            detail = await source._wait_for_detail(job_page)
            job_blocked = await source._block_reason(job_page)
            description = detail.get("description")
            print(f"  Blocker         : {job_blocked or 'none detected'}")
            print(f"  Description     : {len(description)} chars" if description else "  Description     : not found")
            if description:
                print(f"  Description head: {description[:200]!r}")
            print(f"  Company (page)  : {detail.get('company')!r}")
            print(f"  Location (page) : {detail.get('location')!r}")
            print(f"  Posted (page)   : {detail.get('posted')!r}")
            print(f"  Job type (page) : {detail.get('job_type')!r}")
            print(f"  Apply href      : {detail.get('application_url')!r}  (read only, not clicked)")
            if job_blocked:
                print("\nBLOCKER on job page. Stopping. Nothing was bypassed.")
                exit_code = 2

        if args.keep_open:
            await asyncio.to_thread(input, "\nBrowser left open for manual inspection. Press Enter to close... ")
    finally:
        try:
            await session.kill()
        except Exception as exc:  # cleanup only
            print(f"(browser cleanup failed: {exc})")
    return exit_code


def main() -> None:
    sys.exit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
