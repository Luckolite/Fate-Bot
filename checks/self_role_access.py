import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.utility.self_roles import (
    Categories,
    MEMBER_ABOVE_BOT_MESSAGE,
    RoleView,
    allowed_mentions,
)


class AsyncCache(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flush_count = 0

    async def flush(self):
        self.flush_count += 1


def make_button_case(*, member_position=20, owner_id=99, add_error=None):
    role = SimpleNamespace(id=5, name="Member", position=10)
    member = SimpleNamespace(
        id=42,
        roles=[],
        top_role=SimpleNamespace(position=member_position),
        add_roles=AsyncMock(side_effect=add_error),
        remove_roles=AsyncMock(),
    )
    guild = SimpleNamespace(
        id=1,
        owner_id=owner_id,
        me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
        get_member=lambda _member_id: member,
        get_role=lambda _role_id: role,
    )
    config = AsyncCache(
        {
            1: {
                "10": {
                    "roles": {"5": {}},
                    "limit": 2,
                    "show_roles": True,
                }
            }
        }
    )
    button = SimpleNamespace(label="Member")
    view = SimpleNamespace(
        bot=SimpleNamespace(get_guild=lambda _guild_id: guild),
        guild_id=1,
        message_id=10,
        config=config,
        cls=SimpleNamespace(format_text=Mock(return_value="Updated menu")),
        buttons={"5@10": button},
        remove_item=Mock(),
    )
    interaction = SimpleNamespace(
        guild=guild,
        data={"custom_id": "5@10"},
        user=SimpleNamespace(id=42),
        message=SimpleNamespace(id=10, edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    return role, member, config, button, view, interaction


class SelfRoleAccessChecks(unittest.IsolatedAsyncioTestCase):
    async def test_owner_and_members_above_bot_are_ignored(self):
        for owner_id, member_position in ((42, 1), (99, 101)):
            with self.subTest(owner_id=owner_id, member_position=member_position):
                _, member, config, _, view, interaction = make_button_case(
                    owner_id=owner_id,
                    member_position=member_position,
                )

                await RoleView.button_callback(view, interaction)

                member.add_roles.assert_not_awaited()
                member.remove_roles.assert_not_awaited()
                self.assertIn("5", config[1]["10"]["roles"])
                interaction.response.send_message.assert_awaited_once_with(
                    MEMBER_ABOVE_BOT_MESSAGE,
                    ephemeral=True,
                )

    async def test_missing_access_removes_role_button_and_persists(self):
        response = SimpleNamespace(status=403, reason="Forbidden", headers={})
        forbidden = discord.Forbidden(
            response,
            {"code": 50001, "message": "Missing Access"},
        )
        _, _, config, button, view, interaction = make_button_case(
            add_error=forbidden
        )

        await RoleView.button_callback(view, interaction)

        self.assertNotIn("5", config[1]["10"]["roles"])
        self.assertEqual(config.flush_count, 1)
        view.remove_item.assert_called_once_with(button)
        interaction.message.edit.assert_awaited_once_with(
            content="Updated menu",
            view=view,
            allowed_mentions=allowed_mentions,
        )
        interaction.response.send_message.assert_awaited_once_with(
            "I couldn't access Member, so I removed it from this self-role menu.",
            ephemeral=True,
        )

    async def test_dropdown_removes_role_above_bot_and_persists(self):
        role = SimpleNamespace(id=5, name="Too High", position=100)
        member = SimpleNamespace(
            id=42,
            roles=[],
            top_role=SimpleNamespace(position=20),
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=1,
            owner_id=99,
            me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
            get_member=lambda _member_id: member,
            get_role=lambda _role_id: role,
        )
        config = AsyncCache(
            {
                1: {
                    "10": {
                        "roles": {"5": {}},
                        "show_roles": False,
                        "show_percentage": False,
                    }
                }
            }
        )
        replacement_menu = object()
        view = SimpleNamespace(
            bot=SimpleNamespace(get_guild=lambda _guild_id: guild),
            guild_id=1,
            message_id=10,
            config=config,
            clear_items=Mock(),
            add_item=Mock(),
            index=Mock(return_value={}),
        )
        interaction = SimpleNamespace(
            guild=guild,
            data={"values": ["5"]},
            user=SimpleNamespace(id=42),
            message=SimpleNamespace(id=10, edit=AsyncMock()),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        with patch("cogs.utility.self_roles.Select", return_value=replacement_menu):
            await RoleView.select_callback(view, interaction)

        self.assertNotIn("5", config[1]["10"]["roles"])
        self.assertEqual(config.flush_count, 1)
        view.add_item.assert_called_once_with(replacement_menu)
        member.add_roles.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            "Too High is too high for me to manage",
            ephemeral=True,
        )

    async def test_category_menu_removes_role_above_bot_and_persists(self):
        role = SimpleNamespace(id=5, name="Too High", position=100)
        member = SimpleNamespace(
            id=42,
            roles=[],
            top_role=SimpleNamespace(position=20),
            add_roles=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=1,
            owner_id=99,
            me=SimpleNamespace(top_role=SimpleNamespace(position=100)),
            get_member=lambda _member_id: member,
            get_role=lambda _role_id: role,
        )
        config = AsyncCache(
            {1: {"10": {"categories": {"Colors": {"5": {}}}}}}
        )
        menu = SimpleNamespace(
            bot=SimpleNamespace(get_guild=lambda _guild_id: guild),
            guild_id=1,
            message_id=10,
            config=config,
        )
        interaction = SimpleNamespace(
            data={"values": ["5"]},
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await Categories.sub_menu_callback(menu, interaction)

        self.assertNotIn("5", config[1]["10"]["categories"]["Colors"])
        self.assertEqual(config.flush_count, 1)
        member.add_roles.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            "Too High is too high for me to manage",
            ephemeral=True,
        )


if __name__ == "__main__":
    unittest.main()
