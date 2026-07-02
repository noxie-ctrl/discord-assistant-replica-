"""
cogs/games.py

Lightweight, no-dependency mini-games to keep the server active:
  - /tictactoe @opponent   — button-grid, two human players
  - /guessnumber [max]     — bot picks 1..max (default 100), you type guesses
  - /rps                   — rock/paper/scissors vs Lucy, buttons

Results are recorded to game_stats via utils.database so /gamestats can show
a simple leaderboard-style summary per user.
"""

import random
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils import database as db


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
        lines.extend(self.board)  # rows
        lines.extend([[self.board[r][c] for r in range(3)] for c in range(3)])  # cols
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
            content = "It's a draw!"
        else:
            winner = self.player_x if result == self.X else self.player_o
            loser = self.player_o if result == self.X else self.player_x
            await db.record_game_result(self.guild_id, winner.id, "tictactoe", "win")
            await db.record_game_result(self.guild_id, loser.id, "tictactoe", "loss")
            content = f"🎉 {winner.mention} wins!"

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
            result_text = f"Both picked **{user_choice}** — draw!"
            await db.record_game_result(self.guild_id, self.player.id, "rps", "draw")
        elif self.BEATS[user_choice] == bot_choice:
            result_text = f"You picked **{user_choice}**, I picked **{bot_choice}** — you win! 🎉"
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
# Guess the Number
# ---------------------------------------------------------------------------

class GuessNumberGame:
    __slots__ = ("target", "max_value", "attempts", "player_id")

    def __init__(self, max_value: int, player_id: int):
        self.target = random.randint(1, max_value)
        self.max_value = max_value
        self.attempts = 0
        self.player_id = player_id


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # channel_id -> GuessNumberGame
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

    @app_commands.command(name="guessnumber", description="Guess Lucy's number — type your guesses in chat")
    @app_commands.describe(max_value="Upper bound of the range (default 100)")
    async def guessnumber(self, interaction: discord.Interaction, max_value: app_commands.Range[int, 10, 1000] = 100):
        if interaction.channel_id in self.active_guess_games:
            await interaction.response.send_message(
                "There's already a guessing game running in this channel.", ephemeral=True
            )
            return

        game = GuessNumberGame(max_value, interaction.user.id)
        self.active_guess_games[interaction.channel_id] = game
        await interaction.response.send_message(
            f"I'm thinking of a number between 1 and {max_value}, {interaction.user.mention}. "
            "Type your guesses right here in chat! You've got 60 seconds."
        )

        await asyncio.sleep(60)
        # Clean up if nobody finished it
        if self.active_guess_games.get(interaction.channel_id) is game:
            del self.active_guess_games[interaction.channel_id]
            await db.record_game_result(interaction.guild_id, game.player_id, "guessnumber", "loss")
            await interaction.followup.send(
                f"⏰ Time's up! The number was **{game.target}**."
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        game = self.active_guess_games.get(message.channel.id)
        if game is None or message.author.id != game.player_id:
            return
        if not message.content.strip().lstrip("-").isdigit():
            return

        guess = int(message.content.strip())
        game.attempts += 1

        if guess == game.target:
            del self.active_guess_games[message.channel.id]
            await db.record_game_result(message.guild.id, game.player_id, "guessnumber", "win")
            await message.reply(
                f"🎯 Got it in {game.attempts} guesses! The number was **{game.target}**.",
                mention_author=False,
            )
        elif guess < game.target:
            await message.reply("Higher ⬆️", mention_author=False)
        else:
            await message.reply("Lower ⬇️", mention_author=False)

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