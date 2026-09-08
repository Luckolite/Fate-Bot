"""
botutils.views
~~~~~~~~~~~~~~~

Quick button menus for ease of use

Classes:
    ChoiceButtons
    CancelButton

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

from discord import ButtonStyle, Embed, Interaction, NotFound, ui

from . import colors


class ChoiceButtons(ui.View):
    def __init__(self):
        self.choice = None
        super().__init__(timeout=45)

    @ui.button(label="Yes", style=ButtonStyle.green)
    async def yes(self, interaction, _button):
        self.choice = True
        await interaction.message.edit(view=None)
        self.stop()

    @ui.button(label="No", style=ButtonStyle.red)
    async def no(self, interaction, _button):
        self.choice = False
        await interaction.message.edit(view=None)
        self.stop()


class CancelButton(ui.View):
    is_cancelled: bool = False
    def __init__(self, permission: str):
        self.permission = permission
        super().__init__(timeout=3600)

    async def on_error(self, interaction: Interaction, error: Exception, item: ui.Item) -> None:
        if not isinstance(error, NotFound):
            raise error

    @ui.button(label="Cancel", style=ButtonStyle.red)
    async def cancel_button(self, interaction: Interaction, _button: ui.Button):
        try:
            member = interaction.guild.get_member(interaction.user.id)
            if not member or not getattr(member.guild_permissions, self.permission):
                return await interaction.response.send_message(
                    "You need manage_message permissions to cancel this", ephemeral=True
                )
            self.is_cancelled = True
            await interaction.response.send_message("Cancelled the operation")
            await interaction.message.edit(view=None)
            self.stop()
        except NotFound:
            pass


class AddResponseButton(ui.View):
    def __init__(self, response, label=None, emoji=None, timeout=120):
        """
        :param Union[str, Embed] response:  what to respond with when tapped
        :param Optional[str] name:          what to name the button
        :param Optional[str] emoji:         sets the button as an emoji instead
        :param int timeout:                 how long to wait until removing the button
        """
        if isinstance(response, str):
            self.response = Embed(color=colors.fate)
            self.response.description = response
        elif isinstance(response, Embed):
            self.response: Embed = response
        else:
            raise ValueError(f"Type {repr(response.__class__.__name__)} isn't supported yet")
        super().__init__(timeout=timeout)

        button = ui.Button(label=label, emoji=emoji)
        button.callback = self.on_interaction
        self.add_item(button)

    async def on_interaction(self, interaction):
        await interaction.response.send_message(embed=self.response)
