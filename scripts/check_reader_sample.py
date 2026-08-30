#!/usr/bin/env python3
"""Check a random catalog sample through the real browser reader."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

_RESULT = re.compile(r"^### Result\s*\n(.*?)\n### Ran", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True, slots=True)
class Candidate:
    public_id: str
    source_format: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    public_id: str
    source_format: str
    status: str
    duration_ms: int
    title: str = ""
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Randomly sample visible FB2/EPUB catalog entries and open each one "
            "through the browser reader."
        ),
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/sopds.sqlite3"),
        help="SOPDS SQLite database (default: data/sopds.sqlite3)",
    )
    parser.add_argument("--browser", choices=("chromium", "firefox"), default="chromium")
    parser.add_argument("--timeout", type=float, default=30.0, help="Seconds allowed per book")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Books checked sequentially in each playwright-cli call",
    )
    parser.add_argument("--playwright-cli", default="playwright-cli")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> str:
    if args.count < 1:
        raise ValueError("--count must be at least 1")
    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    if args.timeout <= 0:
        raise ValueError("timeouts must be positive")
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--base-url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("--base-url must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("--base-url must identify the server root without a query or fragment")
    return args.base_url.rstrip("/")


def database_uri(database: Path) -> str:
    if not database.is_file():
        raise RuntimeError(f"Database does not exist: {database}")
    return f"{database.resolve().as_uri()}?mode=ro"


def active_generation(database: Path) -> int | None:
    with sqlite3.connect(database_uri(database), uri=True) as connection:
        row = connection.execute(
            "SELECT active_generation_id FROM catalog_state WHERE id = 1"
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def collect_sample(database: Path, count: int) -> tuple[list[Candidate], int, int]:
    query = """
        FROM book AS b
        JOIN archive AS a ON a.id = b.archive_id
        WHERE b.generation_id = ?
          AND a.generation_id = b.generation_id
          AND b.hidden = 0
          AND a.available = 1
          AND lower(b.original_format) IN ('fb2', 'epub')
          AND (b.series_id IS NULL OR EXISTS (
              SELECT 1 FROM series AS s
              WHERE s.id = b.series_id AND s.generation_id = b.generation_id
          ))
    """
    with sqlite3.connect(database_uri(database), uri=True) as connection:
        connection.execute("BEGIN")
        generation = connection.execute(
            "SELECT active_generation_id FROM catalog_state WHERE id = 1"
        ).fetchone()
        if not generation or generation[0] is None:
            raise RuntimeError("Catalog has no active generation")
        generation_id = int(generation[0])
        observed_count = int(
            connection.execute(
                f"SELECT count(*) {query}",
                (generation_id,),
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"SELECT b.public_id, lower(b.original_format) {query} ORDER BY random() LIMIT ?",
            (generation_id, count),
        ).fetchall()
    sample = [Candidate(public_id=str(row[0]), source_format=str(row[1])) for row in rows]
    return sample, observed_count, generation_id


def playwright_code(
    candidates: list[Candidate],
    *,
    base_url: str,
    timeout_ms: int,
) -> str:
    books = [
        {
            "public_id": candidate.public_id,
            "source_format": candidate.source_format,
            "url": f"{base_url}/books/{quote(candidate.public_id, safe='')}/read",
        }
        for candidate in candidates
    ]
    return f"""async page => {{
    const books = {json.dumps(books, ensure_ascii=False)}
    const timeout = {timeout_ms}
    const results = []
    for (const book of books) {{
        const started = Date.now()
        const pageErrors = []
        const onPageError = error => pageErrors.push(String(error?.message ?? error))
        page.on('pageerror', onPageError)
        let title = ''
        let error = ''
        try {{
            const response = await page.goto(book.url, {{ waitUntil: 'domcontentloaded', timeout }})
            if (!response) throw new Error('Navigation returned no HTTP response')
            if (!response.ok()) throw new Error(`Reader returned HTTP ${{response.status()}}`)
            title = await page.title()
            await page.waitForFunction(() => {{
                const reader = document.querySelector('[data-reader-state="reader"]')
                const failure = document.querySelector('[data-reader-state="error"]')
                return reader && !reader.hidden || failure && !failure.hidden
            }}, null, {{ timeout }})
            const failure = page.locator('[data-reader-state="error"]:not([hidden])')
            if (await failure.count()) {{
                const message = await failure.locator('[data-reader-error-message]').textContent()
                throw new Error(`Reader error: ${{message?.trim() || 'unknown failure'}}`)
            }}
            await page.waitForFunction(() => {{
                const view = document.querySelector('foliate-view')
                const contents = view?.renderer?.getContents?.() ?? []
                return Boolean(view?.book && contents.some(item => item.doc?.body))
            }}, null, {{ timeout }})
            await page.waitForTimeout(100)
            if (pageErrors.length) throw new Error(`Page error: ${{pageErrors.join('; ')}}`)
        }} catch (reason) {{
            const message = String(reason?.message ?? reason)
            if (page.isClosed() || /browser.*closed|context.*closed|page.*closed|page crashed|connection closed/i.test(message))
                throw reason
            error = message
        }} finally {{
            page.off('pageerror', onPageError)
        }}
        results.push({{
            public_id: book.public_id,
            source_format: book.source_format,
            status: error ? 'failed' : 'passed',
            duration_ms: Date.now() - started,
            title,
            error,
        }})
    }}
    return results
}}"""


def run_cli(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "NO_COLOR": "1"}
    return subprocess.run(  # noqa: S603 - executable is explicitly selected by the operator.
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )


def parse_playwright_result(output: str) -> list[CheckResult]:
    match = _RESULT.search(output)
    if not match:
        raise RuntimeError(f"Could not parse playwright-cli result:\n{output[-2000:]}")
    payload: Any = json.loads(match.group(1))
    if not isinstance(payload, list):
        raise RuntimeError("playwright-cli returned a non-list result")
    results: list[CheckResult] = []
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("playwright-cli returned an invalid result item")
        results.append(
            CheckResult(
                public_id=str(item.get("public_id", "")),
                source_format=str(item.get("source_format", "")),
                status=str(item.get("status", "failed")),
                duration_ms=int(item.get("duration_ms", 0)),
                title=str(item.get("title", "")),
                error=str(item.get("error", "")),
            )
        )
    return results


def start_browser(
    prefix: list[str],
    *,
    base_url: str,
    browser: str,
    timeout: float,
) -> str | None:
    try:
        completed = run_cli(
            [*prefix, "open", base_url, f"--browser={browser}"],
            timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired:
        return "timed out while starting playwright-cli"
    if completed.returncode:
        return (completed.stderr or completed.stdout).strip()[-1000:]
    return None


def check_batch(
    prefix: list[str],
    candidates: list[Candidate],
    *,
    base_url: str,
    timeout: float,
) -> list[CheckResult]:
    code = playwright_code(candidates, base_url=base_url, timeout_ms=round(timeout * 1000))
    command_timeout = (timeout * 2 + 5) * len(candidates) + 30
    completed = run_cli([*prefix, "run-code", code], timeout=command_timeout)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-1000:]
        raise RuntimeError(f"playwright-cli failed: {detail}")
    results = parse_playwright_result(completed.stdout)
    expected = [(item.public_id, item.source_format) for item in candidates]
    actual = [(item.public_id, item.source_format) for item in results]
    if actual != expected:
        raise RuntimeError("playwright-cli returned mismatched book results")
    return results


def failed_results(candidates: list[Candidate], error: str) -> list[CheckResult]:
    return [
        CheckResult(
            public_id=candidate.public_id,
            source_format=candidate.source_format,
            status="failed",
            duration_ms=0,
            error=error,
        )
        for candidate in candidates
    ]


def write_report(
    path: Path,
    *,
    base_url: str,
    browser: str,
    database: Path,
    generation_id: int,
    requested_count: int,
    observed_count: int,
    sampled_count: int,
    started_at: str,
    results: list[CheckResult],
    interrupted: bool = False,
    catalog_changed: bool = False,
) -> None:
    failed = sum(result.status != "passed" for result in results)
    report = {
        "version": 1,
        "started_at": started_at,
        "updated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "browser": browser,
        "database": str(database),
        "generation_id": generation_id,
        "requested_count": requested_count,
        "eligible_books_seen": observed_count,
        "sampled_count": sampled_count,
        "checked_count": len(results),
        "passed_count": len(results) - failed,
        "failed_count": failed,
        "interrupted": interrupted,
        "catalog_changed": catalog_changed,
        "results": [asdict(result) for result in results],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    try:
        base_url = validate_args(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    executable = shutil.which(args.playwright_cli)
    if not executable:
        print(f"error: executable not found: {args.playwright_cli}", file=sys.stderr)
        return 2

    started_at = datetime.now(UTC).isoformat()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or Path(f"reader-sample-{timestamp}-{args.browser}.json")
    print(f"Selecting up to {args.count} books from {args.database}", flush=True)
    try:
        sample, observed_count, generation_id = collect_sample(args.database, args.count)
    except Exception as error:
        print(f"error: database sample failed: {error}", file=sys.stderr)
        return 2
    if not sample:
        print("error: catalog contains no visible FB2/EPUB books", file=sys.stderr)
        return 2
    if len(sample) < args.count:
        print(
            f"Catalog has only {len(sample)} eligible books; checking all of them",
            file=sys.stderr,
        )

    session_number = 0
    prefix = [executable, f"-s=reader-sample-{os.getpid()}-{session_number}"]
    start_error = start_browser(
        prefix, base_url=base_url, browser=args.browser, timeout=args.timeout
    )
    if start_error:
        print(f"error: could not start browser: {start_error}", file=sys.stderr)
        return 2

    def restart_browser() -> str | None:
        nonlocal session_number, prefix
        with suppress(subprocess.TimeoutExpired):
            run_cli([*prefix, "close"], timeout=30)
        session_number += 1
        prefix = [executable, f"-s=reader-sample-{os.getpid()}-{session_number}"]
        return start_browser(prefix, base_url=base_url, browser=args.browser, timeout=args.timeout)

    results: list[CheckResult] = []
    interrupted = False
    catalog_changed = False
    try:
        for offset in range(0, len(sample), args.batch_size):
            try:
                catalog_changed = active_generation(args.database) != generation_id
            except Exception as error:
                catalog_changed = True
                print(f"error: could not verify active catalog: {error}", file=sys.stderr)
            if catalog_changed:
                print("error: active catalog changed during the check", file=sys.stderr)
                break
            batch = sample[offset : offset + args.batch_size]
            try:
                batch_results = check_batch(prefix, batch, base_url=base_url, timeout=args.timeout)
            except Exception as first_error:
                print(
                    f"playwright-cli session failed; retrying books individually: {first_error}",
                    file=sys.stderr,
                )
                restart_error = restart_browser()
                if restart_error:
                    batch_results = failed_results(
                        batch, f"browser restart failed: {restart_error}"
                    )
                else:
                    batch_results = []
                    for index, candidate in enumerate(batch):
                        candidate_result: CheckResult | None = None
                        candidate_error = "reader check failed"
                        for _attempt in range(2):
                            try:
                                candidate_result = check_batch(
                                    prefix,
                                    [candidate],
                                    base_url=base_url,
                                    timeout=args.timeout,
                                )[0]
                                break
                            except Exception as error:
                                candidate_error = str(error)
                                restart_error = restart_browser()
                                if restart_error:
                                    candidate_error = f"browser restart failed: {restart_error}"
                                    break
                        if candidate_result:
                            batch_results.append(candidate_result)
                        else:
                            batch_results.extend(failed_results([candidate], candidate_error))
                        if restart_error:
                            batch_results.extend(
                                failed_results(batch[index + 1 :], candidate_error)
                            )
                            break
            results.extend(batch_results)
            for number, result in enumerate(batch_results, start=offset + 1):
                marker = "PASS" if result.status == "passed" else "FAIL"
                message = f"{number:4}/{len(sample)} {marker} {result.public_id}"
                if result.error:
                    message += f" — {result.error}"
                print(message, flush=True)
            try:
                catalog_changed = active_generation(args.database) != generation_id
            except Exception as error:
                catalog_changed = True
                print(f"error: could not verify active catalog: {error}", file=sys.stderr)
            if catalog_changed:
                print("error: active catalog changed during the check", file=sys.stderr)
            write_report(
                output,
                base_url=base_url,
                browser=args.browser,
                database=args.database,
                generation_id=generation_id,
                requested_count=args.count,
                observed_count=observed_count,
                sampled_count=len(sample),
                started_at=started_at,
                results=results,
                catalog_changed=catalog_changed,
            )
            if catalog_changed:
                break
    except KeyboardInterrupt:
        interrupted = True
        print("Interrupted; preserving partial report", file=sys.stderr)
    finally:
        with suppress(subprocess.TimeoutExpired):
            run_cli([*prefix, "close"], timeout=30)
        write_report(
            output,
            base_url=base_url,
            browser=args.browser,
            database=args.database,
            generation_id=generation_id,
            requested_count=args.count,
            observed_count=observed_count,
            sampled_count=len(sample),
            started_at=started_at,
            results=results,
            interrupted=interrupted,
            catalog_changed=catalog_changed,
        )

    failed = sum(result.status != "passed" for result in results)
    print(f"Report: {output}")
    print(f"Checked {len(results)}: {len(results) - failed} passed, {failed} failed")
    if interrupted:
        return 130
    if catalog_changed:
        return 2
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
