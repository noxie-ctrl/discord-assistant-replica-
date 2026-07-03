"""
cogs/games.py

v2: all games now feed the shared currency economy, and Guess the Number
supports real competition — challenge another member, or race against Lucy
herself as an AI opponent — instead of just a solo timer game.

  - /tictactoe @opponent          — button-grid, two human players, winner earns coins
  - /guessnumber [opponent] [vs_ai] [max]  — solo, vs a member, or vs Lucy; first to
    guess right wins the pot
  - /rps                           — rock/paper/scissors vs Lucy, buttons
  - /balance [user]                 — check coin balance
  - /leaderboard                    — top balances in the server

Currency payouts are intentionally simple flat amounts, not a full wagering
system — bigger, riskier economy features (betting, shops, etc.) would need
their own spec.
"""

import random
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils import database as db


# Reward amounts, kept simple and flat
PAYOUT = {
    "tictactoe_win": 15,
    "tictactoe_draw": 5,
    "rps_win": 10,
    "rps_draw": 3,
    "guessnumber_solo_win": 15,
    "guessnumber_vs_player_win": 25,
    "guessnumber_vs_ai_win": 20,
}


# ---------------------------------------------------------------------------
# Tic-Tac-Toe
# ---------------------------------------------------------------------------

class TicTacToeButton(discord.ui.Button):
    def __init__(self, x: int, y: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=y)
        self.x = x
        self.y = y

    async def callback(self, interaction: discord.Interaction):
        view: "TicTacToeView" = self.view
        await view.handle_move(interaction, self)


class TicTacToeView(discord.ui.View):
    X, O, EMPTY = "X", "O", None

    def __init__(self, player_x: discord.Member, player_o: discord.Member, guild_id: int):
        super().__init__(timeout=180)
        self.player_x = player_x
        self.player_o = player_o
        self.guild_id = guild_id
        self.current = player_x
        self.board = [[self.EMPTY] * 3 for _ in range(3)]
        for y in range(3):
            for x in range(3):
                self.add_item(TicTacToeButton(x, y))

    def _symbol_for(self, player: discord.Member) -> str:
        return self.X if player.id == self.player_x.id else self.O

    def _check_winner(self) -> str | None:
        lines = []
        lines.extend(self.board)
        lines.extend([[self.board[r][c] for r in range(3)] for c in range(3)])
        lines.append([self.board[i][i] for i in range(3)])
        lines.append([self.board[i][2 - i] for i in range(3)])
        for line in lines:
            if line[0] is not None and line[0] == line[1] == line[2]:
                return line[0]
        if all(cell is not None for row in self.board for cell in row):
            return "draw"
        return None

    async def handle_move(self, interaction: discord.Interaction, button: TicTacToeButton):
        if interaction.user.id != self.current.id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return
        if self.board[button.y][button.x] is not None:
            await interaction.response.send_message("That square's taken.", ephemeral=True)
            return

        symbol = self._symbol_for(self.current)
        self.board[button.y][button.x] = symbol
        button.label = symbol
        button.style = (
            discord.ButtonStyle.danger if symbol == self.X else discord.ButtonStyle.primary
        )
        button.disabled = True

        result = self._check_winner()
        if result is None:
            self.current = self.player_o if self.current.id == self.player_x.id else self.player_x
            await interaction.response.edit_message(
                content=f"{self.current.mention}'s turn ({self._symbol_for(self.current)})",
                view=self,
            )
            return

        for child in self.children:
            child.disabled = True

        if result == "draw":
            await db.record_game_result(self.guild_id, self.player_x.id, "tictactoe", "draw")
            await db.record_game_result(self.guild_id, self.player_o.id, "tictactoe", "draw")
            bal_x = await db.adjust_balance(self.guild_id, self.player_x.id, PAYOUT["tictactoe_draw"])
            bal_o = await db.adjust_balance(self.guild_id, self.player_o.id, PAYOUT["tictactoe_draw"])
            content = f"It's a draw! Both earn {PAYOUT['tictactoe_draw']} coins."
        else:
            winner = self.player_x if result == self.X else self.player_o
            loser = self.player_o if result == self.X else self.player_x
            await db.record_game_result(self.guild_id, winner.id, "tictactoe", "win")
            await db.record_game_result(self.guild_id, loser.id, "tictactoe", "loss")
            new_balance = await db.adjust_balance(self.guild_id, winner.id, PAYOUT["tictactoe_win"])
            content = f"🎉 {winner.mention} wins! +{PAYOUT['tictactoe_win']} coins (balance: {new_balance})."

        await interaction.response.edit_message(content=content, view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ---------------------------------------------------------------------------
# Rock Paper Scissors
# ---------------------------------------------------------------------------

class RPSView(discord.ui.View):
    CHOICES = ["rock", "paper", "scissors"]
    BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

    def __init__(self, player: discord.Member, guild_id: int):
        super().__init__(timeout=30)
        self.player = player
        self.guild_id = guild_id

    async def _resolve(self, interaction: discord.Interaction, user_choice: str):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return

        bot_choice = random.choice(self.CHOICES)
        if user_choice == bot_choice:
            result_text = f"Both picked **{user_choice}** — draw! +{PAYOUT['rps_draw']} coins."
            await db.record_game_result(self.guild_id, self.player.id, "rps", "draw")
            await db.adjust_balance(self.guild_id, self.player.id, PAYOUT["rps_draw"])
        elif self.BEATS[user_choice] == bot_choice:
            new_balance = await db.adjust_balance(self.guild_id, self.player.id, PAYOUT["rps_win"])
            result_text = (
                f"You picked **{user_choice}**, I picked **{bot_choice}** — you win! 🎉 "
                f"+{PAYOUT['rps_win']} coins (balance: {new_balance})."
            )
            await db.record_game_result(self.guild_id, self.player.id, "rps", "win")
        else:
            result_text = f"You picked **{user_choice}**, I picked **{bot_choice}** — I win! 😏"
            await db.record_game_result(self.guild_id, self.player.id, "rps", "loss")

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=result_text, view=self)
        self.stop()

    @discord.ui.button(label="Rock", style=discord.ButtonStyle.secondary, emoji="🪨")
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, "rock")

    @discord.ui.button(label="Paper", style=discord.ButtonStyle.secondary, emoji="📄")
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, "paper")

    @discord.ui.button(label="Scissors", style=discord.ButtonStyle.secondary, emoji="✂️")
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, "scissors")


# ---------------------------------------------------------------------------
# Guess the Number — solo, vs another member, or vs Lucy (AI)
# ---------------------------------------------------------------------------

class GuessNumberGame:
    def __init__(self, max_value: int, allowed_player_ids: set[int], vs_ai: bool):
        self.target = random.randint(1, max_value)
        self.max_value = max_value
        self.attempts: dict[int, int] = {pid: 0 for pid in allowed_player_ids}
        self.allowed_player_ids = allowed_player_ids
        self.vs_ai = vs_ai
        self.finished = False
        self.ai_task: asyncio.Task | None = None


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_guess_games: dict[int, GuessNumberGame] = {}

    # -- Tic Tac Toe --------------------------------------------------------

    @app_commands.command(name="tictactoe", description="Challenge someone to Tic-Tac-Toe")
    @app_commands.describe(opponent="Who do you want to play against?")
    async def tictactoe(self, interaction: discord.Interaction, opponent: discord.Member):
        if opponent.bot:
            await interaction.response.send_message("Pick a human opponent.", ephemeral=True)
            return
        if opponent.id == interaction.user.id:
            await interaction.response.send_message("You can't play yourself.", ephemeral=True)
            return

        view = TicTacToeView(interaction.user, opponent, interaction.guild_id)
        await interaction.response.send_message(
            f"{interaction.user.mention} (X) vs {opponent.mention} (O) — {interaction.user.mention}'s turn (X)",
            view=view,
        )

    # -- Rock Paper Scissors --------------------------------------------------

    @app_commands.command(name="rps", description="Play Rock Paper Scissors against Lucy")
    async def rps(self, interaction: discord.Interaction):
        view = RPSView(interaction.user, interaction.guild_id)
        await interaction.response.send_message("Pick your move:", view=view)

    # -- Guess the Number -----------------------------------------------------

    @app_commands.command(name="guessnumber", description="Guess the number — solo, vs a member, or vs Lucy")
    @app_commands.describe(
        opponent="Optional: race against this member",
        vs_ai="Race against Lucy herself instead of/alongside a member",
        max_value="Upper bound of the range (default 100)",
    )
    async def guessnumber(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
        vs_ai: bool = False,
        max_value: app_commands.Range[int, 10, 1000] = 100,
    ):
        if interaction.channel_id in self.active_guess_games:
            await interaction.response.send_message(
                "There's already a guessing game running in this channel.", ephemeral=True
            )
            return
        if opponent and opponent.bot:
            await interaction.response.send_message("Pick a human opponent (or use vs_ai for Lucy).", ephemeral=True)
            return
        if opponent and opponent.id == interaction.user.id:
            await interaction.response.send_message("You can't race yourself.", ephemeral=True)
            return

        player_ids = {interaction.user.id}
        if opponent:
            player_ids.add(opponent.id)

        game = GuessNumberGame(max_value, player_ids, vs_ai)
        self.active_guess_games[interaction.channel_id] = game

        if opponent and vs_ai:
            blurb = f"{interaction.user.mention} vs {opponent.mention} vs **me** — first correct guess wins!"
        elif opponent:
            blurb = f"{interaction.user.mention} vs {opponent.mention} — first correct guess wins!"
        elif vs_ai:
            blurb = f"{interaction.user.mention} vs **me** — first correct guess wins!"
        else:
            blurb = f"{interaction.user.mention}, race the clock!"

        await interaction.response.send_message(
            f"I'm thinking of a number between 1 and {max_value}. {blurb} "
            "Type your guesses right here in chat! 60 seconds on the clock."
        )

        if vs_ai:
            game.ai_task = asyncio.create_task(
                self._ai_guess_loop(interaction.channel_id, interaction.guild_id, interaction.channel)
            )

        await asyncio.sleep(60)
        if self.active_guess_games.get(interaction.channel_id) is game and not game.finished:
            game.finished = True
            del self.active_guess_games[interaction.channel_id]
            if game.ai_task:
                game.ai_task.cancel()
            for pid in player_ids:
                await db.record_game_result(interaction.guild_id, pid, "guessnumber", "loss")
            await interaction.followup.send(f"⏰ Time's up! Nobody got it — it was **{game.target}**.")

    async def _ai_guess_loop(self, channel_id: int, guild_id: int, channel: discord.abc.Messageable):
        """Lucy plays along with a binary-search-ish strategy, imperfect and
        paced like an actual player rather than instant-solving it."""
        game = self.active_guess_games.get(channel_id)
        if game is None:
            return
        low, high = 1, game.max_value
        try:
            while not game.finished:
                await asyncio.sleep(random.uniform(4, 9))
                game = self.active_guess_games.get(channel_id)
                if game is None or game.finished:
                    return
                guess = random.randint(low, high) if random.random() < 0.15 else (low + high) // 2
                guess = max(1, min(game.max_value, guess))

                if guess == game.target:
                    game.finished = True
                    self.active_guess_games.pop(channel_id, None)
                    await db.record_game_result(guild_id, self.bot.user.id, "guessnumber", "win")
                    for pid in game.allowed_player_ids:
                        await db.record_game_result(guild_id, pid, "guessnumber", "loss")
                    await channel.send(f"Lucy guesses **{guess}**... that's it. I win this round. 😏")
                    return
                elif guess < game.target:
                    low = guess + 1
                    await channel.send(f"Lucy guesses **{guess}** — too low, going higher.")
                else:
                    high = guess - 1
                    await channel.send(f"Lucy guesses **{guess}** — too high, going lower.")
        except asyncio.CancelledError:
            return

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        game = self.active_guess_games.get(message.channel.id)
        if game is None or game.finished or message.author.id not in game.allowed_player_ids:
            return
        if not message.content.strip().lstrip("-").isdigit():
            return

        guess = int(message.content.strip())
        game.attempts[message.author.id] += 1

        if guess == game.target:
            game.finished = True
            del self.active_guess_games[message.channel.id]
            if game.ai_task:
                game.ai_task.cancel()

            winner_id = message.author.id
            others = game.allowed_player_ids - {winner_id}
            had_real_opponent = bool(others) or game.vs_ai

            await db.record_game_result(message.guild.id, winner_id, "guessnumber", "win")
            for pid in others:
                await db.record_game_result(message.guild.id, pid, "guessnumber", "loss")

            payout_key = "guessnumber_vs_player_win" if others else (
                "guessnumber_vs_ai_win" if game.vs_ai else "guessnumber_solo_win"
            )
            new_balance = await db.adjust_balance(message.guild.id, winner_id, PAYOUT[payout_key])

            await message.reply(
                f"🎯 Got it in {game.attempts[winner_id]} guesses! The number was **{game.target}**. "
                f"+{PAYOUT[payout_key]} coins (balance: {new_balance}).",
                mention_author=False,
            )
        elif guess < game.target:
            await message.reply("Higher ⬆️", mention_author=False)
        else:
            await message.reply("Lower ⬇️", mention_author=False)

    # -- Economy ------------------------------------------------------------

    @app_commands.command(name="balance", description="Check a coin balance")
    @app_commands.describe(user="Whose balance to check (defaults to you)")
    async def balance(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        bal = await db.get_balance(interaction.guild_id, target.id)
        await interaction.response.send_message(f"💰 {target.display_name} has **{bal}** coins.")

    @app_commands.command(name="leaderboard", description="Top coin balances in this server")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await db.get_leaderboard(interaction.guild_id, limit=10)
        if not rows:
            await interaction.response.send_message("Nobody's earned any coins yet.")
            return
        lines = []
        for i, row in enumerate(rows, start=1):
            member = interaction.guild.get_member(row["user_id"])
            name = member.display_name if member else f"User {row['user_id']}"
            lines.append(f"**{i}.** {name} — {row['balance']} coins")
        embed = discord.Embed(
            title="🏆 Coin leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    # -- Stats ------------------------------------------------------------

    @app_commands.command(name="gamestats", description="See your mini-game record")
    @app_commands.describe(user="Whose stats to check (defaults to you)")
    async def gamestats(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        games = ["tictactoe", "rps", "guessnumber"]
        lines = []
        for g in games:
            stats = await db.get_game_stats(interaction.guild_id, target.id, g)
            lines.append(f"**{g}** — W:{stats['wins']} L:{stats['losses']} D:{stats['draws']}")
        embed = discord.Embed(
            title=f"{target.display_name}'s game stats",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))