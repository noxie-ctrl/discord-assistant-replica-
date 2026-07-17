"""
utils/github_tools.py

Shared GitHub-related AI tool implementations, used by BOTH Lucy
(cogs/ai_chat.py, conversational tools) and the new isolated GitHub bot
(cogs/github_chat.py). Extracted out of ai_chat.py (this session) so the
same repo-question logic isn't duplicated across two bots — one source of
truth per the project standard, even though the two bots have separate,
independent tool-calling loops (ai_chat.py's is fused to Lucy's persona/
vent/chat_memory pipeline; this module deliberately isn't fused to either).

Durable caching: get_repo_overview/search_repo_code/read_repo_file all
read-through ghbot_repo_cache (utils/database.py) before hitting the GitHub
API, and write back after a real fetch. This sits UNDERNEATH cachetools'
existing in-memory caching, not alongside it as a second cache — it exists
specifically so a Render free-tier restart (which wipes memory) doesn't
force every repo back through a cold GitHub API fetch.
"""

import logging

import discord

from utils import database as db
from utils import github_client

logger = logging.getLogger("lucy.github_tools")

REPO_CACHE_TTL_SECONDS = 15 * 60  # repo content doesn't change fast enough to need less


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format) — moved out of
# nim_client.GROUNDING_TOOLS (this session) so this module is the single
# source of truth for both the schema AND its execution. nim_client.py
# imports GITHUB_TOOL_SCHEMAS and folds it into GROUNDING_TOOLS unchanged,
# so Lucy's existing tool list is identical to before this refactor.
# ---------------------------------------------------------------------------

GITHUB_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_github_activity",
            "description": (
                "Look up recent commit and pull-request activity from this server's linked "
                "GitHub repos (see /githublink) — use this whenever someone asks what changed, "
                "what shipped, what's been worked on, or the status of a repo/feature, instead "
                "of guessing from memory. Only covers repos actually linked in this server."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": (
                            "Optional: a specific 'owner/repo' to filter to. Omit to search "
                            "across every repo linked in this server."
                        ),
                    },
                    "days": {
                        "type": "integer",
                        "description": "How many days back to look. Defaults to 7.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_repo_overview",
            "description": (
                "Get a high-level overview of a linked GitHub repo — its README and top-level "
                "project structure. Use this for broad questions about a project: what it does, "
                "how it's organized, what tech stack it uses, etc. Only works on repos linked "
                "with /githublink in this server."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "'owner/repo'. Optional if exactly one repo is linked in this server.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_repo_code",
            "description": (
                "Search the actual code of a linked GitHub repo for something specific — a "
                "function name, a config value, how a feature is implemented, etc. Use this "
                "for pointed technical questions ('how is auth handled', 'where's the database "
                "connection set up') rather than get_repo_overview, which is for broad "
                "questions. Returns matching file paths with excerpts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "'owner/repo' to search within."},
                    "query": {
                        "type": "string",
                        "description": "Code search terms — e.g. a function/class name, keyword, or short phrase.",
                    },
                },
                "required": ["repo", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_repo_file",
            "description": (
                "Read the full contents of one specific file from a linked GitHub repo by its "
                "path — use when someone names an exact file ('what's in utils/database.py', "
                "'show me main.py') or after search_repo_code points at a file worth reading "
                "in full."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "'owner/repo'."},
                    "path": {"type": "string", "description": "File path within the repo, e.g. 'utils/database.py'."},
                },
                "required": ["repo", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_context",
            "description": (
                "Get this channel's linked project info — which repo it's about and its "
                "description (see /projectlink) — use this to orient yourself on what the "
                "team in this channel is actually working on before answering a question "
                "that assumes that context."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


# ---------------------------------------------------------------------------
# Shared repo resolution (moved from cogs/ai_chat.py's _resolve_linked_repo)
# ---------------------------------------------------------------------------

async def resolve_linked_repo(guild: discord.Guild, repo_arg: str | None) -> tuple[dict | None, str]:
    """Matches a (possibly omitted) repo argument against this guild's
    linked repos. Returns (link_row_or_None, error_message) — exactly one
    of which is populated, so callers can just check link first."""
    links = await db.list_github_links(guild.id)
    if not links:
        return None, "Error: no GitHub repos are linked in this server (use /githublink first)."

    if repo_arg:
        wanted = repo_arg.strip().lower()
        match = next((l for l in links if l["repo"] == wanted), None)
        if not match:
            available = ", ".join(l["repo"] for l in links)
            return None, f"Error: '{repo_arg}' isn't linked in this server. Linked repos: {available}."
        return match, ""

    if len(links) == 1:
        return links[0], ""

    available = ", ".join(l["repo"] for l in links)
    return None, f"Error: multiple repos are linked — specify one of: {available}."


# ---------------------------------------------------------------------------
# Durable read-through cache helper
# ---------------------------------------------------------------------------

async def _cached_fetch(cache_key: str, fetch_fn) -> str | None:
    """fetch_fn is a zero-arg callable returning an awaitable[str | None]
    (a lambda wrapping an API call, or a small async def). Checks
    ghbot_repo_cache first; on miss, calls fetch_fn, caches a non-None
    result, and returns it. A failed fetch (None) is never cached, so a
    transient GitHub error doesn't get stuck serving 'not found' for 15
    minutes — only real content gets cached."""
    cached = await db.get_repo_cache(cache_key, REPO_CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    result = await fetch_fn()
    if result is not None:
        await db.set_repo_cache(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def search_github_activity(args: dict, guild: discord.Guild) -> str:
    repo = (args.get("repo") or "").strip().lower() or None
    days = args.get("days") or 7
    try:
        days = max(1, min(int(days), 30))
    except (TypeError, ValueError):
        days = 7
    activity = await db.get_recent_github_activity(guild.id, repo=repo, days=days)
    if not activity:
        scope = f"'{repo}'" if repo else "any linked repo"
        return f"No GitHub activity found for {scope} in the last {days} day(s)."
    lines = []
    for item in activity:
        if item["kind"] == "commits":
            lines.append(f"[{item['repo']}] Commits: {item['title']}")
        else:
            lines.append(f"[{item['repo']}] PR #{item['ref']} \"{item['title']}\" — {item['detail'] or 'no summary'}")
    return "\n".join(lines)


async def execute_repo_tool(name: str, args: dict, guild: discord.Guild) -> str:
    repo_arg = (args.get("repo") or "").strip().lower() or None
    link, error = await resolve_linked_repo(guild, repo_arg)
    if not link:
        return error

    owner, repo_name = link["repo"].split("/", 1)
    branch = link["default_branch"] or "main"

    if name == "get_repo_overview":
        readme = await _cached_fetch(
            f"readme:{link['repo']}", lambda: github_client.get_readme(owner, repo_name)
        )

        async def _fetch_tree():
            tree_list = await github_client.get_repo_tree(owner, repo_name, branch)
            return "\n".join(tree_list[:150]) if tree_list else None

        tree = await _cached_fetch(f"tree:{link['repo']}@{branch}", _fetch_tree)

        parts = [f"Repo: {link['repo']} (default branch: {branch})"]
        parts.append("README (may be truncated):\n" + readme[:3000] if readme else "No README found.")
        parts.append("Top-level structure:\n" + tree if tree else "No file tree available.")
        return "\n\n".join(parts)

    if name == "search_repo_code":
        query = (args.get("query") or "").strip()
        if not query:
            return "Error: no search query given."
        try:
            results = await github_client.search_code(owner, repo_name, query)
        except github_client.GitHubError as e:
            return f"Error: {e}"
        if not results:
            return f"No code matches for '{query}' in {link['repo']}."

        chunks = [f"Search results for '{query}' in {link['repo']}:"]
        for r in results:
            chunks.append(f"- {r['path']}")
        # Pull excerpts for the top couple of matches so the model can
        # actually answer, not just list filenames. Cache key matches
        # read_repo_file's below, so a search excerpt and a direct read of
        # the same file share one cache entry.
        for r in results[:2]:
            content = await _cached_fetch(
                f"file:{link['repo']}@{branch}:{r['path']}",
                lambda p=r["path"]: github_client.get_file_content(owner, repo_name, p, branch),
            )
            if content:
                chunks.append(f"\n--- {r['path']} (excerpt) ---\n{content[:2000]}")
        return "\n".join(chunks)

    if name == "read_repo_file":
        path = (args.get("path") or "").strip().lstrip("/")
        if not path:
            return "Error: no file path given."
        content = await _cached_fetch(
            f"file:{link['repo']}@{branch}:{path}",
            lambda: github_client.get_file_content(owner, repo_name, path, branch),
        )
        if content is None:
            return f"Error: couldn't read '{path}' in {link['repo']} — it may not exist, be a directory, or be a binary file."
        return f"{path} in {link['repo']}:\n{content}"

    return f"Error: unknown tool '{name}'."


async def get_project_context(guild: discord.Guild, channel_id: int) -> str:
    project = await db.get_project_link(guild.id, channel_id)
    if not project:
        return (
            "No project is linked to this channel yet — use /projectlink to associate it "
            "with a repo, or just ask about a specific repo by name."
        )
    parts = [f"This channel is linked to project: {project['repo']}"]
    if project.get("description"):
        parts.append(f"Description: {project['description']}")
    return "\n".join(parts)


async def dispatch(name: str, args: dict, guild: discord.Guild, channel_id: int | None = None) -> str:
    """Single entry point both bots' tool loops call for any GitHub-related
    tool."""
    if name == "search_github_activity":
        return await search_github_activity(args, guild)
    if name in ("get_repo_overview", "search_repo_code", "read_repo_file"):
        return await execute_repo_tool(name, args, guild)
    if name == "get_project_context":
        if channel_id is None:
            return "Error: no channel context available for get_project_context."
        return await get_project_context(guild, channel_id)
    return f"Error: unknown tool '{name}'."