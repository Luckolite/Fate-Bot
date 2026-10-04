"""Slash adapters keep stable ordering, visibility, and grouping."""

import unittest
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

from botutils.slash_commands import (
    Namespace, _add_faction_commands, _namespace_commands, _subgroup,
    install_legacy_slash_commands,
)


def command(name, *, module="cogs.utility.notes", group=False, **options):
    async def callback(ctx):
        pass

    callback.__module__ = module
    command_type = commands.Group if group else commands.Command
    return command_type(callback, name=name, description=f"Run {name}", **options)


class SlashCommandOrderChecks(unittest.IsolatedAsyncioTestCase):
    def tree(self):
        client = discord.Client(intents=discord.Intents.none())
        self.addAsyncCleanup(client.close)
        return app_commands.CommandTree(client)

    async def test_namespace_schema_does_not_depend_on_source_iteration_order(self):
        source = [command("zulu"), command("alpha"), command("hidden", hidden=True)]
        namespace = Namespace("notes", "Notes")
        tree = self.tree()

        forward = _namespace_commands(namespace, iter(source)).to_dict(tree)
        reverse = _namespace_commands(namespace, reversed(source)).to_dict(tree)

        self.assertEqual(forward, reverse)
        self.assertEqual([option["name"] for option in forward["options"]], ["alpha", "zulu"])

    async def test_names_are_ordered_after_slash_renaming(self):
        group = _namespace_commands(
            Namespace("notes", "Notes"),
            [command("quicknote"), command("note"), command("notes")],
        )

        self.assertEqual([child.name for child in group.commands], ["add", "list", "quick"])

    async def test_nested_groups_keep_overview_first_and_public_children_sorted(self):
        source = command("history", group=True)
        nested = command("nested", group=True)
        nested.add_command(command("gamma"))
        nested.add_command(command("beta"))
        for child in (command("zulu"), nested, command("hidden", hidden=True), command("alpha")):
            source.add_command(child)

        group = _subgroup(source)

        self.assertEqual([child.name for child in group.commands], ["overview", "alpha", "nested", "zulu"])
        nested_group = group.get_command("nested")
        self.assertEqual([child.name for child in nested_group.commands], ["overview", "beta", "gamma"])

    async def test_faction_sections_keep_their_structure_and_sorted_members(self):
        source = command("factions", module="cogs.fun.factions_rewrite", group=True)
        for name in ("join", "create", "income", "balance", "help"):
            source.add_command(command(name, module="cogs.fun.factions_rewrite"))
        group = app_commands.Group(name="factions", description="Factions")

        _add_faction_commands(group, source)

        self.assertEqual([child.name for child in group.commands], ["overview", "help", "management", "economy"])
        self.assertEqual([child.name for child in group.get_command("management").commands], ["create", "join"])
        self.assertEqual([child.name for child in group.get_command("economy").commands], ["balance", "income"])

    async def test_complete_install_is_stable_and_rebuild_does_not_duplicate_groups(self):
        source = [
            command("zulu"), command("alpha"),
            command("gamma", module="cogs.fun.actions"),
            command("beta", module="cogs.fun.actions"),
        ]
        bot = SimpleNamespace(commands=source, tree=self.tree())

        self.assertEqual(install_legacy_slash_commands(bot), 2)
        first = [group.to_dict(bot.tree) for group in bot.tree.get_commands()]
        bot.commands = list(reversed(source))
        self.assertEqual(install_legacy_slash_commands(bot), 2)
        second = [group.to_dict(bot.tree) for group in bot.tree.get_commands()]

        self.assertEqual(first, second)
        self.assertEqual([group["name"] for group in second], ["actions", "notes"])

    async def test_group_capacity_guard_is_preserved(self):
        source = [command(f"item-{index:02}") for index in range(26)]

        with self.assertRaisesRegex(RuntimeError, "25-child limit"):
            _namespace_commands(Namespace("notes", "Notes"), source)


if __name__ == "__main__":
    unittest.main()
