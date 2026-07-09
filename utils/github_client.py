"""
utils/github_client.py

Thin async wrapper around the public GitHub REST API (v3), used by
cogs/github.py to power the "link a repo to a channel" feature.

No dependency beyond aiohttp (already a requirement). Works unauthenticated
(60 requests/hour per IP, shared across ALL linked repos on ALL guilds this
bot instance serves), or authenticated if GITHUB_TOKEN is set in the
environment (5,000 requests/hour) — strongly recommended once more than a
couple of repos are linked. GITHUB_TOKEN only needs public_repo / no scopes
at all for public repos; a fine-grained PAT with read-only "Contents" and
"Pull requests" access is enough for private repos too.
"""

import os
import re
import base64
import logging
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger("lucy.github_client")

API_BASE = "https://api.github.com"

_REPO_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_REPO_SHORT_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$")


class GitHubError(Exception):
    """Raised for anything that isn't a clean 200 from the GitHub API."""


class RepoNotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    pass


def parse_repo(text: str) -> tuple[str, str]:
    """Accepts 'owner/repo', a full/partial github.com URL, or an SSH-style
    URL, and returns (owner, repo). Raises ValueError if it can't be parsed."""
    text = text.strip()
    text = re.sub(r"^git@github\.com:", "https://github.com/", text)

    match = _REPO_URL_RE.match(text) or _REPO_SHORT_RE.match(text)
    if not match:
        raise ValueError(
            f"Couldn't parse '{text}' as a GitHub repo. Use `owner/repo` or a github.com URL."
        )
    owner, repo = match.group(1), match.group(2)
    return owner, repo


def _headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lucy-discord-bot",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    async with session.get(url, params=params, headers=_headers()) as resp:
        if resp.status == 404:
            raise RepoNotFound(url)
        if resp.status == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            raise RateLimited("GitHub API rate limit hit — set GITHUB_TOKEN for a higher limit.")
        if resp.status >= 400:
            body = await resp.text()
            raise GitHubError(f"GitHub API {resp.status} for {url}: {body[:300]}")
        return await resp.json()


async def get_repo_info(owner: str, repo: str) -> dict:
    """Validates the repo exists (and is reachable with current auth) and
    returns a small dict: default_branch, description, html_url, private, stars."""
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _get(session, f"{API_BASE}/repos/{owner}/{repo}")
    return {
        "default_branch": data.get("default_branch") or "main",
        "description": data.get("description") or "",
        "html_url": data.get("html_url") or f"https://github.com/{owner}/{repo}",
        "private": bool(data.get("private")),
        "stars": data.get("stargazers_count", 0),
    }


async def get_latest_commit_sha(owner: str, repo: str, branch: str) -> str | None:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            data = await _get(session, f"{API_BASE}/repos/{owner}/{repo}/commits/{branch}")
        except RepoNotFound:
            return None
    return data.get("sha")


async def get_new_commits(owner: str, repo: str, base_sha: str, head_sha: str, limit: int = 8) -> list[dict]:
    """Commits reachable from head_sha but not base_sha, newest first. Falls
    back to an empty list (rather than raising) if the compare fails — e.g.
    base_sha was force-pushed away — so the caller can just resync quietly."""
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            data = await _get(session, f"{API_BASE}/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}")
        except GitHubError as e:
            logger.warning("Compare failed for %s/%s (%s...%s): %s", owner, repo, base_sha, head_sha, e)
            return []

    commits = data.get("commits", [])[-limit:]
    out = []
    for c in reversed(commits):  # newest first
        commit_info = c.get("commit", {})
        author = commit_info.get("author", {}) or {}
        message = (commit_info.get("message") or "").split("\n", 1)[0]  # first line only
        out.append({
            "sha": c.get("sha", "")[:7],
            "message": message,
            "author": author.get("name") or (c.get("author") or {}).get("login") or "unknown",
            "url": c.get("html_url", ""),
        })
    return out


async def get_pull_request(owner: str, repo: str, number: int) -> dict | None:
    """Full PR details — body/description and diff stats — used for the AI
    summary and size label. Returns None on any failure rather than raising,
    since callers treat this as a best-effort enrichment."""
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            data = await _get(session, f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}")
        except GitHubError as e:
            logger.warning("Failed to fetch PR #%s for %s/%s: %s", number, owner, repo, e)
            return None
    return {
        "body": data.get("body") or "",
        "merged": bool(data.get("merged_at")),
        "additions": data.get("additions", 0),
        "deletions": data.get("deletions", 0),
        "changed_files": data.get("changed_files", 0),
    }


def pr_size_label(additions: int, deletions: int) -> str:
    total = additions + deletions
    if total <= 20:
        return "XS"
    if total <= 100:
        return "S"
    if total <= 400:
        return "M"
    if total <= 1000:
        return "L"
    return "XL"


async def get_recent_pull_events(owner: str, repo: str, since: datetime, limit: int = 10) -> list[dict]:
    """Pull request activity (opened, merged, closed-without-merge) updated
    since the given UTC datetime. Uses the /issues endpoint (which supports
    `since`) filtered down to items that are actually PRs, then resolves
    closed ones against /pulls/{number} to distinguish merged vs closed."""
    since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    timeout = aiohttp.ClientTimeout(total=10)
    events: list[dict] = []

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            issues = await _get(
                session,
                f"{API_BASE}/repos/{owner}/{repo}/issues",
                params={"since": since_iso, "state": "all", "sort": "updated",
                        "direction": "asc", "per_page": str(limit * 2)},
            )
        except GitHubError as e:
            logger.warning("Issue/PR listing failed for %s/%s: %s", owner, repo, e)
            return []

        prs = [i for i in issues if "pull_request" in i][-limit:]

        for pr in prs:
            number = pr["number"]
            created_at = _parse_ts(pr.get("created_at"))
            closed_at = _parse_ts(pr.get("closed_at"))

            if created_at and created_at > since:
                events.append({
                    "type": "opened", "number": number, "title": pr.get("title", ""),
                    "url": pr.get("html_url", ""), "user": (pr.get("user") or {}).get("login", "someone"),
                })
                continue  # don't also report it as closed in the same cycle

            if pr.get("state") == "closed" and closed_at and closed_at > since:
                try:
                    full = await _get(session, f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}")
                    merged = bool(full.get("merged_at"))
                except GitHubError:
                    merged = False
                events.append({
                    "type": "merged" if merged else "closed", "number": number,
                    "title": pr.get("title", ""), "url": pr.get("html_url", ""),
                    "user": (pr.get("user") or {}).get("login", "someone"),
                })

    return events


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Codebase browsing — powers "ask Lucy about the repo" (see
# get_repo_overview / search_repo_code / read_repo_file tools in
# nim_client.py, dispatched from cogs/ai_chat.py).
# ---------------------------------------------------------------------------

MAX_FILE_CHARS = 12000     # keeps a single file read within a sane token budget
MAX_TREE_ENTRIES = 400     # cap on how many paths get_repo_tree returns
NOISE_DIR_PREFIXES = (
    "node_modules/", ".git/", "dist/", "build/", "__pycache__/", ".venv/", "venv/",
    ".next/", "target/", "vendor/", ".pytest_cache/",
)
NOISE_FILE_SUFFIXES = (".lock", ".min.js", ".map", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2")


async def get_readme(owner: str, repo: str) -> str | None:
    """Decoded README text (any of the names GitHub recognizes), or None if
    the repo has none / it can't be fetched."""
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            data = await _get(session, f"{API_BASE}/repos/{owner}/{repo}/readme")
        except GitHubError:
            return None
    return _decode_content(data)


async def get_repo_tree(owner: str, repo: str, branch: str) -> list[str]:
    """Every file path in the repo at `branch` (recursive git tree), minus
    obvious noise directories/files, capped to MAX_TREE_ENTRIES. Used to
    give the model a sense of project layout for broad questions."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            data = await _get(
                session, f"{API_BASE}/repos/{owner}/{repo}/git/trees/{branch}",
                params={"recursive": "1"},
            )
        except GitHubError as e:
            logger.warning("Failed to fetch tree for %s/%s@%s: %s", owner, repo, branch, e)
            return []

    paths = []
    for entry in data.get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry.get("path", "")
        if any(path.startswith(p) for p in NOISE_DIR_PREFIXES):
            continue
        if any(path.endswith(s) for s in NOISE_FILE_SUFFIXES):
            continue
        paths.append(path)
        if len(paths) >= MAX_TREE_ENTRIES:
            break
    return paths


async def get_file_content(owner: str, repo: str, path: str, ref: str) -> str | None:
    """Decoded text content of a single file at `path`, truncated to
    MAX_FILE_CHARS. Returns None if the file doesn't exist, is binary, or
    is too large to be worth returning."""
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            data = await _get(
                session, f"{API_BASE}/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
            )
        except GitHubError as e:
            logger.warning("Failed to fetch file %s in %s/%s: %s", path, owner, repo, e)
            return None

    if isinstance(data, list):
        return None  # path was a directory, not a file
    return _decode_content(data)


def _decode_content(data: dict) -> str | None:
    if data.get("encoding") != "base64" or "content" not in data:
        return None
    try:
        raw = base64.b64decode(data["content"])
        text = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None  # binary file
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + "\n... (truncated)"
    return text


async def search_code(owner: str, repo: str, query: str, limit: int = 5) -> list[dict]:
    """GitHub code search, scoped to one repo. Requires GITHUB_TOKEN — the
    search API rejects unauthenticated requests entirely, so this raises
    GitHubError with a clear message if no token is configured, which
    callers should surface to the user rather than silently returning
    nothing."""
    if not os.getenv("GITHUB_TOKEN", "").strip():
        raise GitHubError(
            "Code search needs a GITHUB_TOKEN configured (GitHub's search API doesn't allow "
            "unauthenticated requests) — the repo link and update features still work without one."
        )

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _get(
            session, f"{API_BASE}/search/code",
            params={"q": f"{query} repo:{owner}/{repo}", "per_page": str(limit)},
        )

    results = []
    for item in data.get("items", [])[:limit]:
        results.append({"path": item.get("path", ""), "url": item.get("html_url", "")})
    return results