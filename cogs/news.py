"""
cogs/news.py

Fixes the "top 5 latest global news" hallucination bug — that data was
coming straight from the model's imagination. This cog fetches real, current
headlines from BBC News' public RSS feed (no API key required) and returns
them as an embed. /news also feeds these into chat memory so if someone
follows up with Lucy about "that earthquake story" etc., she's grounded in
what was actually shown, not a guess.
"""

import logging
import xml.etree.ElementTree as ET

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils import database as db

logger = logging.getLogger("lucy.news")

FEEDS = {
    "world": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "top": "http://feeds.bbci.co.uk/news/rss.xml",
    "tech": "http://feeds.bbci.co.uk/news/technology/rss.xml",
    "business": "http://feeds.bbci.co.uk/news/business/rss.xml",
    "sport": "http://feeds.bbci.co.uk/sport/rss.xml",
}


async def _fetch_feed(category: str, limit: int = 5) -> list[dict]:
    url = FEEDS.get(category, FEEDS["top"])
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Feed request failed with status {resp.status}")
            raw = await resp.text()

    root = ET.fromstring(raw)
    items = []
    for item in root.findall(".//item")[:limit]:
        title = item.findtext("title", default="").strip()
        link = item.findtext("link", default="").strip()
        description = (item.findtext("description", default="") or "").strip()
        items.append({"title": title, "link": link, "description": description})
    return items


class News(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="news", description="Get real, current headlines")
    @app_commands.describe(category="Which section to pull headlines from")
    @app_commands.choices(category=[
        app_commands.Choice(name="Top stories", value="top"),
        app_commands.Choice(name="World", value="world"),
        app_commands.Choice(name="Technology", value="tech"),
        app_commands.Choice(name="Business", value="business"),
        app_commands.Choice(name="Sport", value="sport"),
    ])
    async def news(self, interaction: discord.Interaction, category: app_commands.Choice[str] = None):
        await interaction.response.defer()
        cat_value = category.value if category else "top"

        try:
            items = await _fetch_feed(cat_value)
        except Exception:
            logger.exception("Failed to fetch news feed for category=%s", cat_value)
            await interaction.followup.send(
                "Couldn't reach the news feed just now — try again in a bit."
            )
            return

        if not items:
            await interaction.followup.send("No headlines came back, oddly. Try again shortly.")
            return

        embed = discord.Embed(
            title=f"📰 {cat_value.title()} headlines",
            color=discord.Color.dark_gold(),
        )
        for i, item in enumerate(items, start=1):
            value = item["link"] if item["link"] else "\u200b"
            embed.add_field(name=f"{i}. {item['title']}", value=value, inline=False)

        await interaction.followup.send(embed=embed)

        # Ground any follow-up chat with what was actually shown
        if interaction.guild:
            summary = "; ".join(item["title"] for item in items)
            await db.add_chat_message(
                interaction.guild.id,
                interaction.channel_id,
                None,
                None,
                "assistant",
                f"[Just showed the user real {cat_value} headlines via /news: {summary}]",
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(News(bot))