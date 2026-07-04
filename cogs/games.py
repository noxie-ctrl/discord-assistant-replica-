"""
cogs/games.py

v3: added two more engaging games on top of the existing set — one built for
playing together, one that works solo or as a group free-for-all:

  - /tictactoe @opponent          — button-grid, two human players, winner earns coins
  - /connect4 @opponent            — column-drop buttons + rendered board, two players
  - /guessnumber [opponent] [vs_ai] [max]  — solo, vs a member, or vs Lucy; first to
    guess right wins the pot
  - /rps                           — rock/paper/scissors vs Lucy, buttons
  - /trivia [rounds] [category]    — multi-round multiple choice, open to the whole
    channel or fine playing it alone; categories include gaming/anime/sports/internet
    culture as well as general knowledge
  - /balance [user]                 — check coin balance
  - /leaderboard                    — top balances in the server
  - /gamestats [user]               — win/loss/draw record across all games

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
    "connect4_win": 20,
    "connect4_draw": 8,
    "trivia_correct": 8,
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
# Connect Four — two human players, column-drop buttons + rendered board
# ---------------------------------------------------------------------------

C4_EMPTY, C4_RED, C4_YELLOW = "⚫", "🔴", "🟡"
C4_ROWS, C4_COLS = 6, 7


class Connect4ColumnButton(discord.ui.Button):
    def __init__(self, column: int):
        # Discord caps each button row at 5 — 7 columns need to split across
        # two rows (5 + 2) rather than all sitting in row 0.
        row = 0 if column < 5 else 1
        super().__init__(style=discord.ButtonStyle.secondary, label=str(column + 1), row=row)
        self.column = column

    async def callback(self, interaction: discord.Interaction):
        view: "Connect4View" = self.view
        await view.handle_drop(interaction, self.column)


class Connect4View(discord.ui.View):
    def __init__(self, player_red: discord.Member, player_yellow: discord.Member, guild_id: int):
        super().__init__(timeout=300)
        self.player_red = player_red
        self.player_yellow = player_yellow
        self.guild_id = guild_id
        self.current = player_red
        # columns[c] is a bottom-to-top stack of "R"/"Y"
        self.columns: list[list[str]] = [[] for _ in range(C4_COLS)]
        for c in range(C4_COLS):
            self.add_item(Connect4ColumnButton(c))

    def _symbol_for(self, player: discord.Member) -> str:
        return "R" if player.id == self.player_red.id else "Y"

    def _render(self) -> str:
        emoji = {"R": C4_RED, "Y": C4_YELLOW}
        lines = []
        for row_from_top in range(C4_ROWS):
            idx_from_bottom = C4_ROWS - 1 - row_from_top
            cells = []
            for col in self.columns:
                if idx_from_bottom < len(col):
                    cells.append(emoji[col[idx_from_bottom]])
                else:
                    cells.append(C4_EMPTY)
            lines.append("".join(cells))
        lines.append("".join(f"{i+1}\u20e3" if i < 9 else "🔟" for i in range(C4_COLS)))
        return "\n".join(lines)

    def _grid(self) -> list[list[str | None]]:
        """Full row-major grid (row 0 = bottom), for win checking."""
        grid = [[None] * C4_COLS for _ in range(C4_ROWS)]
        for c, col in enumerate(self.columns):
            for r, piece in enumerate(col):
                grid[r][c] = piece
        return grid

    def _check_winner(self) -> str | None:
        grid = self._grid()
        directions = [(0, 1), (1, 0), (1, 1), (1, -1)]
        for r in range(C4_ROWS):
            for c in range(C4_COLS):
                piece = grid[r][c]
                if piece is None:
                    continue
                for dr, dc in directions:
                    cells = [
                        grid[r + dr * i][c + dc * i]
                        for i in range(4)
                        if 0 <= r + dr * i < C4_ROWS and 0 <= c + dc * i < C4_COLS
                    ]
                    if len(cells) == 4 and all(cell == piece for cell in cells):
                        return piece
        if all(len(col) == C4_ROWS for col in self.columns):
            return "draw"
        return None

    async def handle_drop(self, interaction: discord.Interaction, column: int):
        if interaction.user.id != self.current.id:
            await interaction.response.send_message("It's not your turn.", ephemeral=True)
            return
        if len(self.columns[column]) >= C4_ROWS:
            await interaction.response.send_message("That column's full.", ephemeral=True)
            return

        symbol = self._symbol_for(self.current)
        self.columns[column].append(symbol)

        if len(self.columns[column]) >= C4_ROWS:
            for child in self.children:
                if isinstance(child, Connect4ColumnButton) and child.column == column:
                    child.disabled = True

        result = self._check_winner()
        board = self._render()

        if result is None:
            self.current = self.player_yellow if self.current.id == self.player_red.id else self.player_red
            next_symbol = C4_RED if self._symbol_for(self.current) == "R" else C4_YELLOW
            await interaction.response.edit_message(
                content=f"{board}\n\n{self.current.mention}'s turn ({next_symbol})", view=self,
            )
            return

        for child in self.children:
            child.disabled = True

        if result == "draw":
            await db.record_game_result(self.guild_id, self.player_red.id, "connect4", "draw")
            await db.record_game_result(self.guild_id, self.player_yellow.id, "connect4", "draw")
            await db.adjust_balance(self.guild_id, self.player_red.id, PAYOUT["connect4_draw"])
            await db.adjust_balance(self.guild_id, self.player_yellow.id, PAYOUT["connect4_draw"])
            content = f"{board}\n\nIt's a draw! Both earn {PAYOUT['connect4_draw']} coins."
        else:
            winner = self.player_red if result == "R" else self.player_yellow
            loser = self.player_yellow if result == "R" else self.player_red
            await db.record_game_result(self.guild_id, winner.id, "connect4", "win")
            await db.record_game_result(self.guild_id, loser.id, "connect4", "loss")
            new_balance = await db.adjust_balance(self.guild_id, winner.id, PAYOUT["connect4_win"])
            content = f"{board}\n\n🎉 {winner.mention} connects four! +{PAYOUT['connect4_win']} coins (balance: {new_balance})."

        await interaction.response.edit_message(content=content, view=self)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


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


# ---------------------------------------------------------------------------
# Trivia — solo or open free-for-all, multiple choice, multi-round
# ---------------------------------------------------------------------------

# Mixed categories on purpose — general knowledge alongside gaming/anime/
# internet-culture questions, so it works whether someone wants a quick
# solo round or a competitive group session that rewards "knowing ball."
TRIVIA_BANK = [
    {"q": "Which company developed the game Elden Ring?", "choices": ["FromSoftware", "CD Projekt Red", "Naughty Dog", "Bungie"], "answer": 0, "cat": "gaming"},
    {"q": "In Minecraft, what do you need to craft a Nether Portal?", "choices": ["Iron blocks", "Obsidian", "Netherite", "Cobblestone"], "answer": 1, "cat": "gaming"},
    {"q": "What was the first battle royale game to hit mainstream popularity?", "choices": ["Apex Legends", "Fortnite", "PUBG", "Warzone"], "answer": 2, "cat": "gaming"},
    {"q": "In One Piece, what is the name of Luffy's signature attack style?", "choices": ["Gum-Gum", "Ice-Ice", "Soul King", "Water Seven"], "answer": 0, "cat": "anime"},
    {"q": "Which anime features the Survey Corps fighting Titans?", "choices": ["Demon Slayer", "Attack on Titan", "My Hero Academia", "Naruto"], "answer": 1, "cat": "anime"},
    {"q": "What does 'GOAT' mean in internet slang?", "choices": ["Great Or Awful Time", "Greatest Of All Time", "Game Over And Terminated", "Getting Older And Tired"], "answer": 1, "cat": "internet"},
    {"q": "What's the internet slang term for a message someone regrets sending?", "choices": ["L take", "Ratio", "Delete this", "Cringe"], "answer": 3, "cat": "internet"},
    {"q": "In football (soccer), how many players per team are on the pitch at once?", "choices": ["9", "10", "11", "12"], "answer": 2, "cat": "sports"},
    {"q": "How many points is a touchdown worth in American football (before extra point/conversion)?", "choices": ["5", "6", "7", "3"], "answer": 1, "cat": "sports"},
    {"q": "Which country has won the most FIFA World Cups?", "choices": ["Germany", "Argentina", "Brazil", "Italy"], "answer": 2, "cat": "sports"},
    {"q": "What is the capital of Japan?", "choices": ["Osaka", "Kyoto", "Tokyo", "Nagoya"], "answer": 2, "cat": "general"},
    {"q": "What is the largest planet in our solar system?", "choices": ["Saturn", "Jupiter", "Neptune", "Uranus"], "answer": 1, "cat": "general"},
    {"q": "Which element has the chemical symbol 'Fe'?", "choices": ["Fluorine", "Iron", "Francium", "Ferrite"], "answer": 1, "cat": "general"},
    {"q": "Who directed the movie 'Inception'?", "choices": ["Steven Spielberg", "James Cameron", "Christopher Nolan", "Denis Villeneuve"], "answer": 2, "cat": "movies"},
    {"q": "Which movie won the Academy Award for Best Picture in 2020 (for 2019 films)?", "choices": ["1917", "Joker", "Parasite", "Once Upon a Time in Hollywood"], "answer": 2, "cat": "movies"},
    {"q": "In gaming slang, what does 'GG' stand for?", "choices": ["Great Game", "Go Get", "Good Game", "Game Grid"], "answer": 2, "cat": "gaming"},
    {"q": "What does 'nerfed' mean when talking about a game update?", "choices": ["Made stronger", "Made weaker", "Removed entirely", "Made faster"], "answer": 1, "cat": "gaming"},
    {"q": "Which streaming platform is Discord most commonly used alongside for gaming communities?", "choices": ["Netflix", "Twitch", "Hulu", "Disney+"], "answer": 1, "cat": "internet"},
    {"q": "In Naruto, what village is Naruto from?", "choices": ["Sand Village", "Leaf Village", "Mist Village", "Cloud Village"], "answer": 1, "cat": "anime"},
    {"q": "How many Grand Slam tournaments are there in tennis each year?", "choices": ["2", "3", "4", "5"], "answer": 2, "cat": "sports"},
]


class TriviaAnswerButton(discord.ui.Button):
    def __init__(self, label: str, index: int):
        super().__init__(style=discord.ButtonStyle.secondary, label=label[:80])
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view: "TriviaRoundView" = self.view
        await view.handle_answer(interaction, self.index)


class TriviaRoundView(discord.ui.View):
    """One question. Open to anyone in the channel (solo or free-for-all) —
    first correct click wins the round, wrong clicks just get a quiet
    ephemeral nudge so the round stays live for everyone else."""

    def __init__(self, question: dict, guild_id: int, scoreboard: dict[int, int]):
        super().__init__(timeout=20)
        self.question = question
        self.guild_id = guild_id
        self.scoreboard = scoreboard
        self.winner: discord.Member | None = None
        self.resolved = asyncio.Event()
        labels = ["🇦", "🇧", "🇨", "🇩"]
        for i, choice in enumerate(question["choices"]):
            self.add_item(TriviaAnswerButton(f"{labels[i]} {choice}", i))

    async def handle_answer(self, interaction: discord.Interaction, index: int):
        if self.resolved.is_set():
            await interaction.response.send_message("This question's already been answered.", ephemeral=True)
            return

        if index != self.question["answer"]:
            await interaction.response.send_message("Nope — try another option.", ephemeral=True)
            return

        self.winner = interaction.user
        self.resolved.set()
        for child in self.children:
            child.disabled = True

        new_balance = await db.adjust_balance(self.guild_id, interaction.user.id, PAYOUT["trivia_correct"])
        await db.record_game_result(self.guild_id, interaction.user.id, "trivia", "win")
        self.scoreboard[interaction.user.id] = self.scoreboard.get(interaction.user.id, 0) + 1

        correct_text = self.question["choices"][self.question["answer"]]
        await interaction.response.edit_message(
            content=(
                f"**{self.question['q']}**\n\n✅ {interaction.user.mention} got it — "
                f"**{correct_text}**! +{PAYOUT['trivia_correct']} coins (balance: {new_balance})."
            ),
            view=self,
        )
        self.stop()

    async def on_timeout(self):
        if self.resolved.is_set():
            return
        self.resolved.set()
        for child in self.children:
            child.disabled = True


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_guess_games: dict[int, GuessNumberGame] = {}
        self.active_trivia_channels: set[int] = set()

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

    # -- Connect Four ---------------------------------------------------------

    @app_commands.command(name="connect4", description="Challenge someone to Connect Four")
    @app_commands.describe(opponent="Who do you want to play against?")
    async def connect4(self, interaction: discord.Interaction, opponent: discord.Member):
        if opponent.bot:
            await interaction.response.send_message("Pick a human opponent.", ephemeral=True)
            return
        if opponent.id == interaction.user.id:
            await interaction.response.send_message("You can't play yourself.", ephemeral=True)
            return

        view = Connect4View(interaction.user, opponent, interaction.guild_id)
        board = view._render()
        await interaction.response.send_message(
            f"{board}\n\n{interaction.user.mention} (🔴) vs {opponent.mention} (🟡) — "
            f"{interaction.user.mention}'s turn",
            view=view,
        )

    # -- Trivia ---------------------------------------------------------------

    @app_commands.command(name="trivia", description="Trivia round(s) — solo or open free-for-all")
    @app_commands.describe(
        rounds="How many questions (default 5, max 10)",
        category="Optional: limit to one category",
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="General knowledge", value="general"),
        app_commands.Choice(name="Gaming", value="gaming"),
        app_commands.Choice(name="Anime", value="anime"),
        app_commands.Choice(name="Sports", value="sports"),
        app_commands.Choice(name="Movies", value="movies"),
        app_commands.Choice(name="Internet culture", value="internet"),
    ])
    async def trivia(
        self,
        interaction: discord.Interaction,
        rounds: app_commands.Range[int, 1, 10] = 5,
        category: app_commands.Choice[str] | None = None,
    ):
        if interaction.channel_id in self.active_trivia_channels:
            await interaction.response.send_message(
                "There's already a trivia round running in this channel.", ephemeral=True
            )
            return

        pool = TRIVIA_BANK if category is None else [q for q in TRIVIA_BANK if q["cat"] == category.value]
        if not pool:
            await interaction.response.send_message("No questions in that category yet.", ephemeral=True)
            return

        questions = random.sample(pool, k=min(rounds, len(pool)))
        self.active_trivia_channels.add(interaction.channel_id)
        scoreboard: dict[int, int] = {}

        await interaction.response.send_message(
            f"🧠 Trivia time — {len(questions)} question(s), first correct answer wins each round. Go!"
        )

        try:
            for i, question in enumerate(questions, start=1):
                view = TriviaRoundView(question, interaction.guild_id, scoreboard)
                msg = await interaction.channel.send(
                    f"**Q{i}/{len(questions)} ({question['cat']}):** {question['q']}", view=view
                )
                await view.wait()
                if not view.winner:
                    correct_text = question["choices"][question["answer"]]
                    for child in view.children:
                        child.disabled = True
                    try:
                        await msg.edit(
                            content=f"**{question['q']}**\n\n⏰ Time's up! The answer was **{correct_text}**.",
                            view=view,
                        )
                    except discord.HTTPException:
                        pass
                await asyncio.sleep(2)

            if scoreboard:
                lines = []
                for uid, score in sorted(scoreboard.items(), key=lambda kv: kv[1], reverse=True):
                    member = interaction.guild.get_member(uid)
                    name = member.display_name if member else f"User {uid}"
                    lines.append(f"**{name}** — {score} correct")
                embed = discord.Embed(
                    title="🏆 Trivia results", description="\n".join(lines), color=discord.Color.gold()
                )
                await interaction.channel.send(embed=embed)
            else:
                await interaction.channel.send("Nobody got one right that round — tough crowd. 😏")
        finally:
            self.active_trivia_channels.discard(interaction.channel_id)

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
        games = ["tictactoe", "rps", "guessnumber", "connect4", "trivia"]
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