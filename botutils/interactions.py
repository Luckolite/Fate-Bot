"""
botutils.interactions
~~~~~~~~~~~~~~~~~~~~~~

A module for template interaction classes

Classes:
    GetConfirmation : An object for confirming a question with a user
    ModView : A View that only moderators can interact with
    AuthorView : A view that only the author of the original message can interact with
    Menu : A ease of use embed paginator
    Configure : A menu for having a user modify a config

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

from contextlib import suppress
from typing import Any, Dict, Generator, List, Union

import discord
from discord import ui, Interaction, SelectOption, Embed, ButtonStyle
from discord.ext.commands import Context

from . import colors, emojis
from .cache_rewrite import DataContext
from .tools import Cooldown


class GetConfirmation(ui.View):
    value: bool = False

    def __init__(self, ctx: Context, question: str, user=None):
        self.user = user or ctx.author
        self.ctx = ctx
        self.question = question
        super().__init__(timeout=30)

    def __await__(self) -> Generator[None, None, bool]:
        return self._await().__await__()

    async def _await(self) -> bool:
        msg = await self.ctx.send(self.question, view=self)
        await self.wait()
        await msg.delete()
        if not self.value:
            await self.ctx.message.delete()
        return self.value

    @ui.button(label="Confirm", style=ButtonStyle.green)
    async def confirm(self, interaction, _button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(
                "This menu isn't for you", ephemeral=True
            )
        self.value = True
        await interaction.response.send_message(
            "Alright, confirmed", ephemeral=True
        )
        self.stop()

    @ui.button(label="Deny", style=ButtonStyle.red)
    async def deny(self, interaction, _button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(
                "This menu isn't for you", ephemeral=True
            )
        await interaction.response.send_message(
            "Alright", ephemeral=True
        )
        self.stop()


class ModView(ui.View):
    ctx: Context
    cd: Cooldown

    async def interaction_check(self, interaction: Interaction):
        """ Ensure the interaction is from the user who initiated the view """
        member = interaction.guild.get_member(interaction.user.id)
        if not self.ctx.bot.attrs.is_moderator(member):
            await interaction.response.send_message(
                "Only moderators can interact", ephemeral=True
            )
            return False
        if self.cd.check(member.id):
            await interaction.response.send_message("You're on cooldown. Try again in a few seconds")
            return False
        return True


class AuthorView(ui.View):
    ctx: Context
    cd: Cooldown = None
    def __init__(self, *args, **kwargs) -> None:
        if not self.cd:
            self.cd = Cooldown(2, 5)
        super().__init__(*args, **kwargs)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "Only the user who initiated this menu can interact", ephemeral=True
            )
            return False
        if self.cd.check(interaction.user.id):
            await interaction.response.send_message(
                "You're on cooldown; try again in a few seconds", ephemeral=True
            )
            return False
        return True


class Menu(AuthorView):
    items: Dict[str, Union[Embed, List[Embed]]] = {}
    message: discord.Message = None  # Filled when the class is awaited

    def __init__(self, ctx: Context, pages: Union[dict, list]):
        self.cd = Cooldown(3, 5)
        self.ctx = ctx
        self.page = 0
        super().__init__(timeout=120)

        if isinstance(pages, dict):
            self.items = pages
            self.pages = pages[list(pages.keys())[0]]
            if isinstance(self.pages, Embed):
                self.pages = [self.pages]
            self.add_item(_Dropdown(self))
            has_multiple_pages = any(
                not isinstance(item, Embed) and len(item) > 1
                for item in pages.values()
            )
        else:
            self.pages = pages
            has_multiple_pages = len(self.pages) > 1

        if not self.pages:
            raise ValueError("Menu requires at least one page")

        if has_multiple_pages:
            self.buttons = {
                "seek_left": ui.Button(emoji="◀", custom_id="seek_left"),
                "seek_right": ui.Button(emoji="▶", custom_id="seek_right")
            }
            for button in self.buttons.values():
                button.callback = self.seek
                self.add_item(button)
            self._sync_buttons()

    def _sync_buttons(self):
        if not hasattr(self, "buttons"):
            return
        self.buttons["seek_left"].disabled = self.page == 0
        self.buttons["seek_right"].disabled = self.page >= len(self.pages) - 1

    def __await__(self) -> Generator[None, None, None]:
        """ Fill in the message object """
        return self._init_message().__await__()

    async def _init_message(self) -> discord.Message:
        """ Send the View and fill in the message var """
        self.message = await self.ctx.send(
            embed=self.pages[self.page],
            view=self
        )
        return self.message

    async def on_timeout(self):
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=None)

    async def update_message(self, interaction: Interaction):
        try:
            await interaction.response.edit_message(
                embed=self.pages[self.page],
                view=self
            )
        except discord.NotFound:
            self.stop()
            return False
        return True

    async def seek(self, interaction: Interaction):
        """ Seek to the previous page """
        value: str = interaction.data["custom_id"]
        if value == "seek_left":
            self.page -= 1
            if self.page == 0:
                self.buttons[value].disabled = True
            self.buttons["seek_right"].disabled = False
        elif value == "seek_right":
            self.page += 1
        self._sync_buttons()
        await self.update_message(interaction)


class _Dropdown(ui.Select):
    """ The Dropdown button for the Menus class """
    def __init__(self, cls: Menu):
        self.cls = cls

        options = []
        for label, items in cls.items.items():
            options.append(SelectOption(label=label))

        super().__init__(
            placeholder="Change Category",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: Interaction):
        label: str = interaction.data["values"][0]
        selected = self.cls.items[label]
        self.cls.pages = [selected] if isinstance(selected, Embed) else selected
        self.cls.page = 0
        self.cls._sync_buttons()
        await self.cls.update_message(interaction)


class Configure(ui.View):
    message: discord.Message = None
    deleted: bool = False

    def __init__(self, ctx, options: dict) -> None:
        self.ctx = ctx
        self.options = options
        super().__init__(timeout=45)
        self.add_item(_ConfigureDropdown(self))

    def __await__(self) -> Generator[Any, Any, Union[dict, DataContext]]:
        return self._await().__await__()

    async def _await(self) -> Union[dict, DataContext]:
        self.message = await self.ctx.send(embed=self.embed, view=self)
        await self.wait()
        if not self.deleted:
            with suppress(Exception):
                await self.message.delete()
        return self.options

    @property
    def embed(self) -> discord.Embed:
        e = discord.Embed(color=colors.fate)
        e.set_author(name="Configure Options", icon_url=self.ctx.author.display_avatar.url)
        e.description = ""
        for option, toggle in self.options.items():
            emote = emojis.online if toggle else emojis.dnd
            e.description += f"\n{emote} **{option.title()}**"
        return e

    @ui.button(emoji=emojis.yes, row=2)
    async def done(self, interaction: Interaction, _button):
        self.deleted = True
        with suppress(Exception):
            await interaction.delete_original_response()
        self.stop()


class _ConfigureDropdown(ui.Select):
    def __init__(self, menu: Configure):
        self.menu = menu
        if not 1 <= len(menu.options) <= 25:
            raise ValueError("Configure requires between 1 and 25 options")
        super().__init__(
            placeholder="Toggle an Option",
            min_values=1,
            max_values=len(menu.options),
            options=self.get_options()
        )

    def get_options(self):
        return [
            SelectOption(
                emoji=emojis.online if toggle else emojis.dnd,
                label=option.title().replace("_", " "),
                value=option,
                description=f"click to {'disable' if toggle else 'enable'}"
            )
            for option, toggle in self.menu.options.items()
        ]

    async def callback(self, interaction: Interaction):
        for option in interaction.data["values"]:
            self.menu.options[option] = not self.menu.options[option]
        self.options = self.get_options()
        await interaction.response.edit_message(
            embed=self.menu.embed,
            view=self.menu
        )
