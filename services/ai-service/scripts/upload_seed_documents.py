import argparse
import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import httpx


def _default_seed_root() -> Path:
    parents = Path(__file__).resolve().parents
    repo_root = parents[3] if len(parents) > 3 else None
    checkout = repo_root / "rag_seed_documents" if repo_root else None
    if checkout is not None and checkout.is_dir():
        return checkout
    return Path("/tmp/rag_seed_documents")


_CATEGORIES: tuple[tuple[str, bool],
                   ...] = (("earnings_call", True), ("analyst_report", True),
                           ("macro_research", False), ("filing",
                                                       True), ("news", True))

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_TRAILING_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})$")


@dataclass(frozen=True)
class SeedFile:
    path: Path
    category: str
    ticker: str | None
    content_date: str | None
    title: str


def _seed_sources(seed_root: Path | None) -> list[tuple[Path, str, bool]]:
    root = seed_root if seed_root is not None else _default_seed_root()
    return [(root / category, category, ticker_from_filename)
            for category, ticker_from_filename in _CATEGORIES]


def seed_metadata(
        path: Path, category: str, ticker_from_filename: bool) -> SeedFile:
    stem = path.stem
    ticker = path.name.split("_")[0].upper() if ticker_from_filename else None
    match = _TRAILING_DATE_RE.search(stem)
    return SeedFile(
        path=path, category=category, ticker=ticker,
        content_date=match.group(1) if match else None, title=stem)


def collect_seed_files(
        wanted_categories: set[str] | None,
        seed_root: Path | None = None) -> tuple[list[SeedFile], list[Path]]:
    files: list[SeedFile] = []
    missing: list[Path] = []
    for directory, category, ticker_from_filename in _seed_sources(seed_root):
        if wanted_categories is not None and category not in wanted_categories:
            continue
        if not directory.is_dir():
            missing.append(directory)
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file():
                files.append(
                    seed_metadata(path, category, ticker_from_filename))
    return files, missing


def _media_type(path: Path) -> str:
    return "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain"


async def upload_system_document(
        client: httpx.AsyncClient, core_url: str,
        seed: SeedFile) -> tuple[str, str]:
    form = {"category": seed.category, "title": seed.title}
    if seed.ticker:
        form["ticker"] = seed.ticker
    if seed.content_date:
        form["content_date"] = seed.content_date
    try:
        with open(seed.path, "rb") as f:
            resp = await client.post(
                f"{core_url.rstrip('/')}/internal/documents/system",
                files={"file": (seed.path.name, f, _media_type(seed.path))},
                data=form)
    except httpx.HTTPError as exc:
        return "failed", f"{type(exc).__name__}: {exc}"
    if resp.status_code >= 400:
        return "failed", f"{resp.status_code} {resp.text[:300]}"
    try:
        body = resp.json()
        payload = body.get("data", body) if isinstance(body, dict) else {}
    except ValueError:
        payload = {}
    if payload.get("duplicate"):
        return "duplicate", f"id={payload.get('id')}"
    return "ok", f"id={payload.get('id')} status={payload.get('status')}"


async def existing_filenames(
        client: httpx.AsyncClient, core_url: str, category: str,
        ticker: str | None) -> set[str] | None:
    params = {"category": category}
    if ticker:
        params["ticker"] = ticker
    try:
        resp = await client.get(
            f"{core_url.rstrip('/')}/internal/documents/filenames",
            params=params)
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        body = body.get("data")
    if not isinstance(body, list):
        return None
    return {str(name) for name in body}


async def _already_stored(
        client: httpx.AsyncClient, core_url: str, seed: SeedFile,
        cache: dict[tuple[str, str | None], set[str] | None]) -> bool:
    key = (seed.category, seed.ticker)
    if key not in cache:
        cache[key] = await existing_filenames(client, core_url, *key)
        if cache[key] is None:
            print(
                f"WARNING could not list existing filenames for "
                f"category={seed.category} ticker={seed.ticker or '-'}; "
                "uploading without the cross-owner check")
    names = cache[key]
    return names is not None and seed.path.name in names


async def run_as_system(
        files: list[SeedFile], *, dry_run: bool,
        client: httpx.AsyncClient | None = None,
        core_url: str | None = None) -> dict[str, int]:
    counts = {
        "uploaded": 0,
        "duplicates": 0,
        "failed": 0,
        "planned": 0,
        "skipped": 0
    }
    owned_client = client is None
    if core_url is None:
        from shared.config import get_settings

        core_url = get_settings().CORE_SERVICE_URL
    if client is None:
        from shared.http_client import build_http_client

        client = build_http_client(timeout=120.0)
    stored: dict[tuple[str, str | None], set[str] | None] = {}
    try:
        for seed in files:
            if await _already_stored(client, core_url, seed, stored):
                counts["skipped"] += 1
                print(
                    f"SKIP {seed.path.name} (already uploaded by another "
                    f"owner)")
                continue
            if dry_run:
                counts["planned"] += 1
                print(
                    f"DRY-RUN {seed.path.name}: category={seed.category} "
                    f"ticker={seed.ticker or '-'} "
                    f"content_date={seed.content_date or '-'}")
                continue
            outcome, detail = await upload_system_document(
                client, core_url, seed)
            if outcome == "ok":
                counts["uploaded"] += 1
                print(f"OK {seed.path.name} -> {detail}")
            elif outcome == "duplicate":
                counts["duplicates"] += 1
                print(f"DUPLICATE {seed.path.name} (already stored, {detail})")
            else:
                counts["failed"] += 1
                print(f"FAILED {seed.path.name}: {detail}")
    finally:
        if owned_client:
            await client.aclose()
    return counts


def _tls_verify(base_url: str, insecure: bool) -> bool:
    if insecure:
        return False
    parts = urlsplit(base_url)
    return not (parts.scheme == "https" and parts.hostname in _LOOPBACK_HOSTS)


async def _login(
        client: httpx.AsyncClient, base_url: str, email: str,
        password: str) -> str:
    resp = await client.post(
        f"{base_url}/api/v1/auth/login", json={
            "email": email,
            "password": password
        })
    resp.raise_for_status()
    token: str = resp.json()["data"]["access_token"]
    return token


async def _upload_one(
        client: httpx.AsyncClient, base_url: str, token: str, path: Path,
        category: str, ticker: str | None) -> bool:
    with open(path, "rb") as f:
        resp = await client.post(
            f"{base_url}/api/v1/research/documents",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (path.name, f, _media_type(path))}, data={
                "category": category,
                **({
                    "ticker": ticker
                } if ticker else {})
            })
    if resp.status_code >= 400:
        print(f"FAILED {path.name}: {resp.status_code} {resp.text[:300]}")
        return False
    doc = resp.json()["data"]
    print(f"OK {path.name} -> id={doc['id']} status={doc['status']}")
    return True


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://localhost:8443")
    parser.add_argument(
        "--insecure", action="store_true",
        help="Skip TLS certificate verification for a non-loopback host "
        "(loopback hosts are already skipped: self-signed dev certificate).")
    parser.add_argument("--email", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument(
        "--as-system", action="store_true",
        help="Upload as system documents through core's internal route "
        "(run inside the ai-service container; no user account needed).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="With --as-system: only list the files and their metadata.")
    parser.add_argument(
        "--seed-root", default=None,
        help="Directory holding the earnings_call/, analyst_report/, "
        "macro_research/, filing/ and news/ folders (default: the repo-root "
        "rag_seed_documents/ in a checkout, else /tmp/rag_seed_documents).")
    parser.add_argument(
        "--categories", default=None, help="Comma-separated subset of "
        "{earnings_call,analyst_report,macro_research,"
        "filing,news} to upload -- e.g. to re-run only the categories that "
        "were "
        "missing last time without re-uploading (and duplicating) ones that "
        "already succeeded. Default: all 5.")
    args = parser.parse_args(argv)
    wanted_categories = (
        set(args.categories.split(",")) if args.categories else None)

    if args.as_system:
        files, missing = collect_seed_files(
            wanted_categories,
            Path(args.seed_root) if args.seed_root else None)
        for directory in missing:
            print(f"Skipping {directory} (not found)")
        counts = await run_as_system(files, dry_run=args.dry_run)
        if args.dry_run:
            print(
                f"\nDry run: {counts['planned']} files would be uploaded, "
                f"{counts['skipped']} skipped (already uploaded by another "
                f"owner).")
            return 0
        print(
            f"\nDone: {counts['uploaded']} uploaded, {counts['duplicates']} "
            f"duplicates, {counts['skipped']} skipped (already uploaded by "
            f"another owner), {counts['failed']} failed, {len(missing)} "
            f"category "
            "directories skipped (not found).")
        return 1 if counts["failed"] else 0
    if args.dry_run:
        parser.error("--dry-run needs --as-system")
    if not args.email or not args.password:
        parser.error("--email and --password are required without --as-system")

    verify = _tls_verify(args.base_url, args.insecure)
    async with httpx.AsyncClient(timeout=60.0, verify=verify) as client:
        token = await _login(client, args.base_url, args.email, args.password)
        print("Logged in.")

        uploaded, failed, skipped = 0, 0, 0
        seed_root = Path(args.seed_root) if args.seed_root else None
        for directory, category, ticker_from_filename in _seed_sources(
                seed_root):
            if (wanted_categories is not None
                    and category not in wanted_categories):
                continue
            if not directory.is_dir():
                print(f"Skipping {directory} (not found -- check --seed-root)")
                skipped += 1
                continue
            for path in sorted(directory.iterdir()):
                if not path.is_file():
                    continue
                ticker = (
                    path.name.split("_")[0] if ticker_from_filename else None)
                if await _upload_one(
                        client, args.base_url, token, path, category, ticker):
                    uploaded += 1
                else:
                    failed += 1

        print(
            f"\nDone: {uploaded} files uploaded, {failed} failed, {skipped} "
            "category directories skipped (not found).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
