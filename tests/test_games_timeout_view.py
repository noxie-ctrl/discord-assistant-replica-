import unittest
from unittest.mock import AsyncMock

import discord

from cogs.games import TimeoutAwareView


class TimeoutAwareViewTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the 'timed-out games still look clickable'
    fix. Plain discord.ui.View's default on_timeout only flips
    button.disabled on the in-memory objects — Discord is never told, so
    the message kept showing live-looking buttons forever after a game
    actually expired (TicTacToeView, RPSView, Connect4View all hit this;
    RPSView didn't even have an on_timeout override before this fix)."""

    def _view_with_a_button(self):
        view = TimeoutAwareView(timeout=30)
        view.add_item(discord.ui.Button(label="test"))
        return view

    async def test_disables_all_children(self):
        view = self._view_with_a_button()
        view.message = AsyncMock()
        await view._disable_and_finalize("timed out")
        self.assertTrue(all(child.disabled for child in view.children))

    async def test_edits_the_real_message_with_the_note(self):
        view = self._view_with_a_button()
        fake_message = AsyncMock()
        view.message = fake_message
        await view._disable_and_finalize("⏱️ timed out, nobody moved")
        fake_message.edit.assert_awaited_once_with(content="⏱️ timed out, nobody moved", view=view)

    async def test_noop_when_message_was_never_captured(self):
        # Shouldn't happen in practice (every call site sets view.message
        # right after sending), but must not crash if it's ever missed.
        view = self._view_with_a_button()
        view.message = None
        await view._disable_and_finalize("timed out")  # should not raise
        self.assertTrue(all(child.disabled for child in view.children))

    async def test_swallows_http_exception_if_message_already_deleted(self):
        view = self._view_with_a_button()
        fake_message = AsyncMock()
        fake_message.edit.side_effect = discord.HTTPException(AsyncMock(), "gone")
        fake_message.id = 12345
        view.message = fake_message
        await view._disable_and_finalize("timed out")  # should not raise


if __name__ == "__main__":
    unittest.main()