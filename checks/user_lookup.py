"""User lookup preserves cache, prompt, and cancellation behavior."""

import asyncio
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

from botutils.get_user import GetUser
from checks.exceptions import IgnoredExit


@dataclass
class User:
    id: int
    name: str

    @property
    def mention(self):
        return f"<@{self.id}>"

    def __str__(self):
        return self.name


def guild_with(*members):
    return SimpleNamespace(
        members=list(members),
        get_member=lambda user_id: next(
            (member for member in members if member.id == user_id), None,
        ),
    )


class UserLookupChecks(unittest.IsolatedAsyncioTestCase):
    def make_bot(self):
        return SimpleNamespace(
            get_user=Mock(return_value=None),
            fetch_user=AsyncMock(),
            utils=SimpleNamespace(get_choice=AsyncMock()),
        )

    def make_context(self, guild=None):
        return SimpleNamespace(
            guild=guild, channel=SimpleNamespace(guild=guild), send=AsyncMock(),
        )

    async def test_duplicate_name_matches_keep_selected_user_aligned(self):
        alex, ben = User(1, "alex"), User(2, "ben")
        bot = self.make_bot()
        context = self.make_context(guild_with(alex, ben))
        lookup = GetUser(bot, ctx=context, names=["alex", "alex", "ben"])

        with patch("botutils.GetChoice", AsyncMock(return_value="ben")) as choose:
            self.assertIs(await lookup, ben)

        choose.assert_awaited_once_with(context, ["alex", "ben"])
        bot.fetch_user.assert_not_awaited()

    async def test_all_preserves_id_and_name_match_order(self):
        alex, ben = User(1, "alex"), User(2, "ben")
        bot = self.make_bot()
        lookup = GetUser(
            bot, guild=guild_with(alex, ben), user_ids=[2],
            names=["alex", "alex", "ben"],
        ).all()

        self.assertEqual(await lookup, [ben, alex, alex, ben])
        bot.fetch_user.assert_not_awaited()

    async def test_guild_name_search_without_channel_uses_first_match(self):
        members = [User(1, "alex-one"), User(2, "alex-two")]
        bot = self.make_bot()

        self.assertIs(await GetUser(bot, guild=guild_with(*members), name="alex"), members[0])

        bot.utils.get_choice.assert_not_awaited()
        bot.fetch_user.assert_not_awaited()

    async def test_global_lookup_prefers_cached_user_then_fetches_missing_id(self):
        cached, fetched = User(1, "cached"), User(2, "fetched")
        bot = self.make_bot()
        bot.get_user.side_effect = lambda user_id: cached if user_id == 1 else None
        bot.fetch_user.return_value = fetched

        self.assertEqual(await GetUser(bot, user_ids=[1, 2]).all(), [cached, fetched])

        bot.fetch_user.assert_awaited_once_with(2)

    async def test_unavailable_global_user_is_skipped(self):
        bot = self.make_bot()
        bot.fetch_user.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown user",
        )

        self.assertIsNone(await GetUser(bot, user_id=1))

    async def test_global_name_conversion_skips_unmatched_names(self):
        user = User(1, "alex")
        bot = self.make_bot()
        context = self.make_context()
        with patch(
            "discord.ext.commands.UserConverter.convert",
            AsyncMock(side_effect=[commands.BadArgument("No match"), user]),
        ) as convert:
            self.assertIs(await GetUser(bot, ctx=context, names=["missing", "alex"]), user)

        self.assertEqual(convert.await_count, 2)
        context.send.assert_not_awaited()

    async def test_name_lookup_without_context_requires_a_context(self):
        with self.assertRaisesRegex(TypeError, "'ctx' is required"):
            await GetUser(self.make_bot(), name="alex")

    async def test_missing_guild_member_reports_no_match(self):
        context = self.make_context(guild_with())

        with self.assertRaises(IgnoredExit):
            await GetUser(self.make_bot(), ctx=context, user_id=1)

        context.send.assert_awaited_once_with("Couldn't find any users going by that")

    async def test_cancelled_selection_exits_lookup(self):
        context = self.make_context(guild_with(User(1, "alex-one"), User(2, "alex-two")))

        with patch("botutils.GetChoice", AsyncMock(return_value=None)):
            with self.assertRaises(IgnoredExit):
                await GetUser(self.make_bot(), ctx=context, name="alex")

    async def test_channel_selection_without_context_uses_mentions(self):
        alex, ben = User(1, "alex"), User(2, "ben")
        guild = guild_with(alex, ben)
        bot = self.make_bot()
        bot.utils.get_choice.return_value = ben.mention

        selected = await GetUser(bot, channel=SimpleNamespace(guild=guild), names=["alex", "ben"])

        self.assertIs(selected, ben)
        bot.utils.get_choice.assert_awaited_once_with(
            None, [alex.mention, ben.mention], name="Which user",
        )

    async def test_invalid_id_is_rejected_before_discord_lookup(self):
        bot = self.make_bot()

        with self.assertRaises(commands.BadArgument):
            await GetUser(bot, ctx=self.make_context(), user_id=2**63)

        bot.get_user.assert_not_called()
        bot.fetch_user.assert_not_awaited()

    async def test_fetch_cancellation_propagates(self):
        bot = self.make_bot()
        bot.fetch_user.side_effect = asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await GetUser(bot, user_id=1)


if __name__ == "__main__":
    unittest.main()
