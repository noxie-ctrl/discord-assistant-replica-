"""
utils/github_summarizer.py

Turns raw GitHub API data into short, plain-English blurbs for cogs/github.py
to post — "what actually changed" instead of a wall of raw commit messages
or a bare PR title. Same cheap-background-task pattern as
summarize_user_notes() in nim_client.py: try Groq first (fast, free,
plenty for a couple of sentences), fall back to NIM if Groq isn't
configured or fails, and fall back to a plain non-AI rendering if both are
unavailable so the feature never goes silent just because a summarizer call
failed.
"""

import logging

from utils import groq_client
from utils import nim_client

logger = logging.getLogger("lucy.github_summarizer")

_SYSTEM = (
    "You write extremely short, plain-English summaries of software changes for a "
    "Discord channel of teammates, some non-technical. 1-2 sentences, no preamble, "
    "no markdown headers, no bullet points unless there are genuinely 3+ distinct "
    "changes. Say what changed and why it matters, not a restatement of the commit "
    "message. Never invent details that aren't in the input."
)


async def _summarize(user_content: str, max_tokens: int = 120) -> str | None:
    """Returns None (rather than raising) on total failure, so callers can
    fall back to a non-AI rendering instead of dropping the update."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_content},
    ]
    if groq_client.is_configured():
        try:
            return (await groq_client.call_groq(
                messages, model=groq_client.MODEL_FAST, max_tokens=max_tokens, temperature=0.3,
            )).strip()
        except Exception as e:
            logger.warning("Groq summarization failed, falling back to NIM: %s", e)

    try:
        return (await nim_client.call_nim(messages, max_tokens=max_tokens, temperature=0.3)).strip()
    except Exception as e:
        logger.warning("NIM summarization also failed, skipping AI summary: %s", e)
        return None


async def summarize_commits(repo: str, branch: str, commits: list[dict]) -> str | None:
    lines = "\n".join(f"- {c['message']} (by {c['author']})" for c in commits)
    prompt = (
        f"Repo: {repo} (branch: {branch})\n"
        f"New commits, oldest to newest:\n{lines}\n\n"
        "Summarize what this batch of commits actually accomplishes."
    )
    return await _summarize(prompt)


async def summarize_pr(repo: str, title: str, body: str, additions: int, deletions: int,
                          changed_files: int) -> str | None:
    body = (body or "").strip()[:1500]  # cap so a huge PR description doesn't blow the token budget
    prompt = (
        f"Repo: {repo}\n"
        f"PR title: {title}\n"
        f"PR description: {body or '(no description given)'}\n"
        f"Diff size: +{additions}/-{deletions} across {changed_files} file(s)\n\n"
        "Summarize what this PR does in plain English."
    )
    return await _summarize(prompt)


async def summarize_digest(guild_name: str, activity: list[dict]) -> str | None:
    """activity is a list of github_activity_log rows (dicts) covering the
    past week, most recent first. Produces one consolidated write-up across
    every repo, not a per-repo breakdown, since the point of the digest is
    a single skimmable "what happened this week" post."""
    lines = []
    for item in activity:
        label = item["title"] if item["kind"] == "commits" else f"PR: {item['title']}"
        lines.append(f"- [{item['repo']}] {label}")
    body = "\n".join(lines)

    prompt = (
        f"Server: {guild_name}\n"
        f"All GitHub activity across linked repos in the last 7 days:\n{body}\n\n"
        "Write a short weekly recap (3-6 sentences, or a few short bullet points if there "
        "are clearly distinct workstreams) a teammate could read in 10 seconds to catch up "
        "on what shipped this week. Group related items together rather than listing every "
        "single one. Don't restate the repo list mechanically — write it like a real update."
    )
    return await _summarize(prompt, max_tokens=280)