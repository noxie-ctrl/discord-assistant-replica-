"""
cogs/github.py

Lets a server admin/mod link a GitHub repo to a channel; Lucy then posts an
update whenever there's new activity on it — new commits pushed to the
default branch, and pull requests being opened/merged/closed. Polling runs
in the background (GITHUB_POLL_INTERVAL_MINUTES, default 5) across every
linked repo on every guild the bot is in.

Commands:
  /githublink   <repo> [channel]   - link a repo (admin/mod only)
  /githubunlink <repo>             - remove a link (admin/mod only)
  /githublinks                     - list repos linked in this server
  /githubdigest [channel]          - set/clear the weekly recap channel (admin/mod only)

v2 (this session) — "Discord as the project hub":
  - AI-written summaries (utils/github_summarizer.py) instead of raw commit
    messages / bare PR titles, so updates are skimmable by non-technical
    teammates too.
  - New PRs get their own Discord thread, so review discussion has a home
    instead of scattering across the main channel.
  - Every posted update is logged to github_activity_log (utils/database.py)
    — this backs both the weekly digest and a new search_github_activity
    tool that lets Lucy answer "what changed in X this week?" in normal
    chat (see nim_client.py GROUNDING_TOOLS + ai_chat.py's dispatcher).
  - Weekly digest: a daily-ticking loop that, once a week, posts one
    consolidated AI recap of everything that happened across every linked
    repo in a guild, to an admin-configured channel.

State (last_commit_sha, last_pr_check_at) lives in the github_links table
so it survives restarts — a fresh deploy doesn't dump a repo's entire
commit history into a channel.
"""

import os
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import database as db
from utils import github_client
from utils import github_summarizer
from utils.permissions import is_admin_or_mod

logger = logging.getLogger("lucy.github")

POLL_INTERVAL_MINUTES = int(os.getenv("GITHUB_POLL_INTERVAL_MINUTES", "5"))
DIGEST_WEEKDAY = int(os.getenv("GITHUB_DIGEST_WEEKDAY", "0"))  # 0 = Monday

COMMIT_COLOR = discord.Color.dark_grey()
DIGEST_COLOR = discord.Color.gold()
PR_COLORS = {
    "opened": discord.Color.green(),
    "merged": discord.Color.purple(),
    "closed": discord.Color.red(),
}
PR_VERBS = {"opened": "opened", "merged": "merged", "closed": "closed (not merged)"}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class GitHub(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self._poll_loop.start()
        self._digest_loop.start()

    async def cog_unload(self):
        self._poll_loop.cancel()
        self._digest_loop.cancel()

    # -----------------------------------------------------------------
    # Commands
    # -----------------------------------------------------------------

    @app_commands.command(name="githublink", description="Link a GitHub repo — get updates on commits and PRs here")
    @app_commands.describe(
        repo="owner/repo, or a github.com URL",
        channel="Where to post updates (defaults to this channel)",
    )
    @is_admin_or_mod()
    async def githublink(self, interaction: discord.Interaction, repo: str, channel: discord.TextChannel = None):
        await interaction.response.defer()
        target_channel = channel or interaction.channel

        try:
            owner, name = github_client.parse_repo(repo)
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}")
            return

        repo_key = f"{owner}/{name}".lower()

        try:
            info = await github_client.get_repo_info(owner, name)
        except github_client.RepoNotFound:
            await interaction.followup.send(
                f"❌ Couldn't find `{owner}/{name}` — check the name, or if it's private make sure "
                f"`GITHUB_TOKEN` (with access to it) is configured."
            )
            return
        except github_client.RateLimited as e:
            await interaction.followup.send(f"⏳ {e}")
            return
        except github_client.GitHubError:
            logger.exception("Failed to fetch repo info for %s", repo_key)
            await interaction.followup.send("❌ GitHub API error while looking up that repo — try again shortly.")
            return

        # Baseline the commit cursor at HEAD so linking doesn't immediately
        # dump the repo's whole recent history into the channel.
        head_sha = await github_client.get_latest_commit_sha(owner, name, info["default_branch"])

        await db.add_github_link(
            interaction.guild_id, repo_key, target_channel.id, interaction.user.id,
            default_branch=info["default_branch"], last_commit_sha=head_sha,
        )

        embed = discord.Embed(
            title=f"🔗 Linked {owner}/{name}",
            description=info["description"] or "\u200b",
            url=info["html_url"],
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Updates in", value=target_channel.mention)
        embed.add_field(name="Default branch", value=info["default_branch"])
        if info["private"]:
            embed.add_field(name="Note", value="This repo is private — make sure `GITHUB_TOKEN` has access.", inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="githubunlink", description="Stop tracking a linked GitHub repo")
    @app_commands.describe(repo="owner/repo of the linked repo to remove")
    @is_admin_or_mod()
    async def githubunlink(self, interaction: discord.Interaction, repo: str):
        try:
            owner, name = github_client.parse_repo(repo)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        repo_key = f"{owner}/{name}".lower()
        removed = await db.remove_github_link(interaction.guild_id, repo_key)
        if removed:
            await interaction.response.send_message(f"🗑️ Unlinked `{repo_key}`.")
        else:
            await interaction.response.send_message(f"`{repo_key}` wasn't linked in this server.", ephemeral=True)

    @app_commands.command(name="githublinks", description="List GitHub repos linked in this server")
    async def githublinks(self, interaction: discord.Interaction):
        links = await db.list_github_links(interaction.guild_id)
        if not links:
            await interaction.response.send_message("No repos linked yet — use `/githublink` to add one.")
            return

        lines = []
        for link in links:
            channel = interaction.guild.get_channel(link["channel_id"])
            where = channel.mention if channel else f"channel {link['channel_id']} (missing)"
            lines.append(f"• **{link['repo']}** → {where}")

        embed = discord.Embed(
            title="🔗 Linked GitHub repos",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="githubdigest", description="Set (or clear) the weekly GitHub recap channel")
    @app_commands.describe(channel="Where to post the weekly recap — omit to turn the digest off")
    @is_admin_or_mod()
    async def githubdigest(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        await db.update_guild_setting(
            interaction.guild_id,
            github_digest_channel_id=channel.id if channel else None,
        )
        if channel:
            await interaction.response.send_message(
                f"📅 Weekly GitHub recap will post in {channel.mention} (next {WEEKDAY_NAMES[DIGEST_WEEKDAY]})."
            )
        else:
            await interaction.response.send_message("📅 Weekly GitHub recap turned off.")

    # -----------------------------------------------------------------
    # Background polling — commits & PRs
    # -----------------------------------------------------------------

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def _poll_loop(self):
        try:
            await self._check_all_links()
        except Exception:
            logger.exception("GitHub poll loop failed")

    @_poll_loop.before_loop
    async def _before_poll_loop(self):
        await self.bot.wait_until_ready()

    async def _check_all_links(self):
        links = await db.get_all_github_links()
        for link in links:
            try:
                await self._check_link(link)
            except github_client.RateLimited as e:
                logger.warning("GitHub rate limited mid-poll, stopping this cycle early: %s", e)
                return
            except Exception:
                logger.exception("Failed checking github link %s for guild %s", link["repo"], link["guild_id"])

    async def _check_link(self, link: dict):
        guild = self.bot.get_guild(link["guild_id"])
        if not guild:
            return
        channel = guild.get_channel(link["channel_id"])
        if not channel:
            return

        owner, name = link["repo"].split("/", 1)

        if link.get("notify_commits", True):
            await self._check_commits(link, owner, name, channel)
        if link.get("notify_prs", True):
            await self._check_prs(link, owner, name, channel)

    async def _check_commits(self, link: dict, owner: str, name: str, channel: discord.TextChannel):
        branch = link["default_branch"] or "main"
        head_sha = await github_client.get_latest_commit_sha(owner, name, branch)
        if not head_sha:
            return

        old_sha = link["last_commit_sha"]
        if old_sha == head_sha:
            return
        if not old_sha:
            # First time we've ever checked this link (shouldn't normally
            # happen — /githublink baselines it — but covers edge cases).
            await db.update_github_link_state(link["guild_id"], link["repo"], last_commit_sha=head_sha)
            return

        commits = await github_client.get_new_commits(owner, name, old_sha, head_sha)
        if not commits:
            # Compare failed (e.g. force-push) — resync quietly rather than spam.
            await db.update_github_link_state(link["guild_id"], link["repo"], last_commit_sha=head_sha)
            return

        summary = await github_summarizer.summarize_commits(link["repo"], branch, commits)

        embed = discord.Embed(
            title=f"📦 {len(commits)} new commit{'s' if len(commits) != 1 else ''} on {link['repo']}@{branch}",
            color=COMMIT_COLOR,
            url=f"https://github.com/{owner}/{name}/commits/{branch}",
        )
        if summary:
            embed.description = f"**{summary}**"
        raw_lines = [f"[`{c['sha']}`]({c['url']}) {c['message']} — *{c['author']}*" for c in commits[:8]]
        embed.add_field(name="Commits", value="\n".join(raw_lines), inline=False)

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Missing permission to post in channel %s for repo %s", channel.id, link["repo"])

        await db.log_github_activity(
            link["guild_id"], link["repo"], "commits", head_sha[:7],
            title=summary or f"{len(commits)} commit(s) pushed",
            detail="; ".join(c["message"] for c in commits),
            author=commits[0]["author"] if commits else None,
            url=f"https://github.com/{owner}/{name}/commits/{branch}",
        )
        await db.update_github_link_state(link["guild_id"], link["repo"], last_commit_sha=head_sha)

    async def _check_prs(self, link: dict, owner: str, name: str, channel: discord.TextChannel):
        since = link["last_pr_check_at"]
        now = discord.utils.utcnow()
        events = await github_client.get_recent_pull_events(owner, name, since)

        for event in events:
            summary = None
            size = None
            if event["type"] == "opened":
                pr_details = await github_client.get_pull_request(owner, name, event["number"])
                if pr_details:
                    size = github_client.pr_size_label(pr_details["additions"], pr_details["deletions"])
                    summary = await github_summarizer.summarize_pr(
                        link["repo"], event["title"], pr_details["body"],
                        pr_details["additions"], pr_details["deletions"], pr_details["changed_files"],
                    )

            title = f"🔀 PR #{event['number']} {PR_VERBS.get(event['type'], event['type'])} on {link['repo']}"
            if size:
                title += f" ({size})"
            embed = discord.Embed(
                title=title,
                description=summary or event["title"],
                url=event["url"],
                color=PR_COLORS.get(event["type"], discord.Color.greyple()),
            )
            if summary:
                embed.add_field(name="Title", value=event["title"], inline=False)
            embed.set_footer(text=f"by {event['user']}")

            sent_message = None
            try:
                sent_message = await channel.send(embed=embed)
            except discord.Forbidden:
                logger.warning("Missing permission to post in channel %s for repo %s", channel.id, link["repo"])
                break

            if event["type"] == "opened" and sent_message:
                try:
                    await sent_message.create_thread(
                        name=f"PR #{event['number']}: {event['title'][:80]}",
                        auto_archive_duration=1440,
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning("Couldn't create PR thread in %s: %s", channel.id, e)

            await db.log_github_activity(
                link["guild_id"], link["repo"], "pr", str(event["number"]),
                title=event["title"], detail=summary, author=event["user"], url=event["url"],
            )

        await db.update_github_link_state(link["guild_id"], link["repo"], last_pr_check_at=now)

    # -----------------------------------------------------------------
    # Weekly digest
    # -----------------------------------------------------------------

    @tasks.loop(hours=24)
    async def _digest_loop(self):
        try:
            await self._maybe_send_digests()
        except Exception:
            logger.exception("GitHub weekly digest loop failed")

    @_digest_loop.before_loop
    async def _before_digest_loop(self):
        await self.bot.wait_until_ready()

    async def _maybe_send_digests(self):
        if datetime.now(timezone.utc).weekday() != DIGEST_WEEKDAY:
            return
        due = await db.get_guilds_due_for_digest()
        for settings in due:
            try:
                await self._send_digest(settings)
            except Exception:
                logger.exception("Failed sending GitHub digest for guild %s", settings["guild_id"])

    async def _send_digest(self, settings: dict):
        guild = self.bot.get_guild(settings["guild_id"])
        if not guild:
            return
        channel = guild.get_channel(settings["github_digest_channel_id"])
        if not channel:
            return

        activity = await db.get_recent_github_activity(guild.id, days=7, limit=100)
        if not activity:
            await db.mark_digest_sent(guild.id)  # nothing happened — don't retry daily until next week
            return

        summary = await github_summarizer.summarize_digest(guild.name, activity)
        commit_batches = sum(1 for a in activity if a["kind"] == "commits")
        pr_events = sum(1 for a in activity if a["kind"] == "pr")
        repos = sorted({a["repo"] for a in activity})

        embed = discord.Embed(
            title="📅 Weekly GitHub recap",
            description=summary or "No AI summary available this week — see the raw activity below.",
            color=DIGEST_COLOR,
        )
        embed.add_field(name="Repos", value=", ".join(repos), inline=False)
        embed.add_field(name="Commit batches", value=str(commit_batches))
        embed.add_field(name="PR events", value=str(pr_events))

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Missing permission to post digest in channel %s for guild %s", channel.id, guild.id)

        await db.mark_digest_sent(guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(GitHub(bot))