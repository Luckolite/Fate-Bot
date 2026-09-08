"""Focused regressions for confirmed Sentry failures in the legacy Fate bot."""

import inspect
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord import app_commands
from discord.ext import commands

from botutils.interactions import Menu
from botutils.log_paths import (
    DISCORD_LOG_PATH,
    LAST_ERROR_LOG_PATH,
    LOGGING_DIRECTORY,
    WHAT_DIED_LOG_PATH,
    ensure_logging_directory,
)
from botutils.resources import _replace_file
from cogs.core.error_handler import safe_console_print, unwrap_original
from cogs.core.ranking import Ranking
from cogs.core.tasks import Tasks
from cogs.moderation.anti_spam import AntiSpam
from cogs.moderation.logger import (
    Logger,
    message_author_role_line,
    message_edit_time,
    message_type_label,
    poll_question_text,
)


def unknown_interaction() -> discord.NotFound:
    response = SimpleNamespace(status=404, reason="Not Found", headers={})
    return discord.NotFound(
        response,
        {"code": 10062, "message": "Unknown interaction"},
    )


class Cp1252Stream:
    encoding = "cp1252"

    def __init__(self):
        self.output = []

    def write(self, text):
        encoded = text.encode(self.encoding)
        self.output.append(encoded.decode(self.encoding))


class FakeCursor:
    def __init__(self, rows=((11,),)):
        self.rows = rows
        self.executions = []
        self.parameters = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, query, parameters=None):
        self.executions.append((query, parameters))
        self.parameters = parameters

    async def fetchall(self):
        return self.rows


class SentryRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_hybrid_command_error_unwraps_to_discord_forbidden(self):
        response = SimpleNamespace(status=403, reason="Forbidden", headers={})
        forbidden = discord.Forbidden(
            response,
            {"code": 50013, "message": "Missing Permissions"},
        )
        app_error = app_commands.CommandInvokeError(
            SimpleNamespace(name="role"), forbidden
        )
        hybrid_error = commands.CommandInvokeError(app_error)

        self.assertIs(unwrap_original(hybrid_error), forbidden)

    async def test_anti_spam_moderation_failure_disables_unusable_policy(self):
        stats = {"failures": 0}
        config = {
            "punishments": {
                "default": "adaptive",
                "rate_limit": "adaptive",
            }
        }
        cache = SimpleNamespace(flush=AsyncMock())
        logger = SimpleNamespace(warning=Mock(), debug=Mock())
        bot_member = object()
        cog = SimpleNamespace(
            bot=SimpleNamespace(log=logger),
            config=cache,
            get_config=lambda _guild_id: config,
            _stats=lambda _guild_id: stats,
            _announce_action=AsyncMock(),
        )
        channel = SimpleNamespace(
            permissions_for=lambda _member: SimpleNamespace(
                moderate_members=False
            )
        )
        message = SimpleNamespace(
            author=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=11, me=bot_member, channels=[channel]),
        )

        await AntiSpam._moderation_failure(
            cog,
            message,
            SimpleNamespace(module="rate_limit"),
            "adaptive",
            "Missing Moderate Members",
        )

        self.assertEqual(stats["failures"], 1)
        self.assertEqual(config["punishments"]["rate_limit"], "delete")
        cache.flush.assert_awaited_once()
        logger.warning.assert_not_called()
        logger.debug.assert_called_once_with(
            "AntiSpam could not apply adaptive to 7 in 11: Missing Moderate "
            "Members; disabled timeout enforcement for the rate_limit module "
            "because Fate lacks Moderate Members in every message channel"
        )

    async def test_anti_spam_channel_permission_failure_is_debug_only(self):
        stats = {"failures": 0}
        logger = SimpleNamespace(warning=Mock(), debug=Mock())
        bot_member = object()
        cog = SimpleNamespace(
            bot=SimpleNamespace(log=logger),
            _stats=lambda _guild_id: stats,
            _announce_action=AsyncMock(),
        )
        blocked = SimpleNamespace(
            permissions_for=lambda _member: SimpleNamespace(
                moderate_members=False
            )
        )
        allowed = SimpleNamespace(
            permissions_for=lambda _member: SimpleNamespace(
                moderate_members=True
            )
        )
        message = SimpleNamespace(
            author=SimpleNamespace(id=7),
            guild=SimpleNamespace(
                id=11, me=bot_member, channels=[blocked, allowed]
            ),
        )

        await AntiSpam._moderation_failure(
            cog,
            message,
            SimpleNamespace(module="rate_limit"),
            "adaptive",
            "Missing Moderate Members",
        )

        logger.warning.assert_not_called()
        logger.debug.assert_called_once_with(
            "AntiSpam could not apply adaptive to 7 in 11: "
            "Missing Moderate Members"
        )

    def test_message_edit_role_line_allows_uncached_user(self):
        author = SimpleNamespace(id=7, name="uncached-user")

        self.assertEqual(message_author_role_line(author), "")

    def test_message_edit_role_line_keeps_cached_member_details(self):
        author = SimpleNamespace(
            top_role=SimpleNamespace(mention="<@&12>", members=[object(), object()])
        )

        role_line = message_author_role_line(author)

        self.assertIn("Role: <@&12>", role_line)
        self.assertIn("2", role_line)

    def test_message_edit_time_falls_back_when_discord_omits_timestamp(self):
        fallback = discord.utils.utcnow()

        self.assertIs(
            message_edit_time(SimpleNamespace(edited_at=None), fallback), fallback
        )

    def test_poll_question_allows_string_representation(self):
        self.assertEqual(
            poll_question_text(SimpleNamespace(question="Which one?")),
            "Which one?",
        )

    def test_message_log_details_allows_string_poll_question(self):
        poll = SimpleNamespace(
            question="Which one?",
            answers=[],
            total_votes=0,
            multiple=False,
            expires_at=None,
            is_finalized=lambda: False,
        )
        message = SimpleNamespace(
            id=1,
            type=SimpleNamespace(name="default"),
            created_at=discord.utils.utcnow(),
            edited_at=None,
            attachments=[],
            embeds=[],
            stickers=[],
            components=[],
            message_snapshots=[],
            flags=SimpleNamespace(value=0),
            reference=None,
            interaction_metadata=None,
            poll=poll,
        )

        details = Logger.message_log_details(message)

        self.assertEqual(details["📊 Poll Question"], "Which one?")

    def test_message_interaction_is_compact_and_readable(self):
        interaction = SimpleNamespace(
            id=1540793800140660846,
            type=SimpleNamespace(name="application_command"),
            name="opal.dev",
            user=SimpleNamespace(mention="<@243233669148442624>"),
            target_user=None,
            target_message_id=None,
            interacted_message_id=None,
        )
        message = SimpleNamespace(
            type=SimpleNamespace(name="chat_input_command"),
            interaction_metadata=interaction,
            _interaction=None,
        )

        rendered = Logger.format_message_interaction(message)

        self.assertEqual(
            rendered,
            "**Slash command:** `/opal.dev`\n"
            "Invoked by <@243233669148442624> • ID: `1540793800140660846`",
        )
        self.assertNotIn("MessageInteraction", rendered)

    def test_message_type_uses_friendly_command_label(self):
        self.assertEqual(
            message_type_label(SimpleNamespace(name="chat_input_command")),
            "Slash Command",
        )

    async def test_windows_open_log_falls_back_to_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "discord.log"
            source = Path(directory) / "discord.log.tmp"
            handler = logging.FileHandler(destination, mode="w", encoding="utf-8")
            try:
                source.write_text("replacement", encoding="utf-8")
                await _replace_file(str(source), str(destination))
            finally:
                handler.close()

            self.assertEqual(destination.read_text(encoding="utf-8"), "replacement")
            self.assertFalse(source.exists())

    async def test_blacklist_handles_uncached_guild_owner(self):
        cursor = FakeCursor()
        channel = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(
            id=22,
            name="Owner not cached",
            owner=None,
            owner_id=11,
            members=[],
            leave=AsyncMock(),
        )
        bot = SimpleNamespace(
            guilds=[guild],
            utils=SimpleNamespace(cursor=lambda: cursor),
            get_channel=lambda _channel_id: channel,
            log=Mock(),
        )

        await Tasks.blacklist.coro(SimpleNamespace(bot=bot))

        self.assertEqual(cursor.executions, [("select user_id from blocked;", None)])
        channel.send.assert_awaited_once()
        guild.leave.assert_awaited_once()

    async def test_blacklist_uses_one_query_for_all_guilds(self):
        cursor = FakeCursor(rows=())
        guilds = [
            SimpleNamespace(id=index, owner=None, owner_id=index + 20_000)
            for index in range(14_000)
        ]
        bot = SimpleNamespace(
            guilds=guilds,
            utils=SimpleNamespace(cursor=lambda: cursor),
        )

        await Tasks.blacklist.coro(SimpleNamespace(bot=bot))

        self.assertEqual(cursor.executions, [("select user_id from blocked;", None)])

    async def test_menu_stops_after_unknown_interaction(self):
        menu = SimpleNamespace(pages=[object()], page=0, stop=Mock())
        interaction = SimpleNamespace(
            response=SimpleNamespace(
                edit_message=AsyncMock(side_effect=unknown_interaction())
            )
        )

        result = await Menu.update_message(menu, interaction)

        self.assertFalse(result)
        menu.stop.assert_called_once_with()


    def test_console_diagnostics_escape_unencodable_unicode(self):
        stream = Cp1252Stream()

        safe_console_print("diagnostic ✨", file=stream)

        self.assertEqual(stream.output, ["diagnostic \\u2728\n"])

    def test_monthly_cleanup_uses_current_monthly_message_column(self):
        source = inspect.getsource(Ranking.monthly_cleanup_task.coro)
        self.assertIn('(\"monthly_msg\", \"msg_time\")', source)
        self.assertNotIn('(\"monthly_msg\", \"timeframe\")', source)

    def test_runtime_logs_share_canonical_directory(self):
        self.assertEqual(ensure_logging_directory(), LOGGING_DIRECTORY)
        self.assertTrue(LOGGING_DIRECTORY.is_dir())
        self.assertEqual(DISCORD_LOG_PATH.parent, LOGGING_DIRECTORY)
        self.assertEqual(WHAT_DIED_LOG_PATH.parent, LOGGING_DIRECTORY)
        self.assertEqual(LAST_ERROR_LOG_PATH.parent, LOGGING_DIRECTORY)


if __name__ == "__main__":
    unittest.main()
