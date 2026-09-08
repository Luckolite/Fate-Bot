
import asyncio
from typing import *

from discord import (
    Color,
    Embed,
    Interaction,
    SelectOption as Option,
    TextStyle,
    ui
)
from discord.ext import commands
from discord.ext.commands import Context

from botutils.colors import red
from fate import Fate


class Ask(ui.Modal):
    # add a third option to set the limit on each field value
    def __init__(self, title: str, question: Union[str, list] = None, questions: list = None, limit: int = 4000):
        self.title = title
        super().__init__(timeout=60 * 15)

        self.interaction = None
        self.answer = None
        self.answers = {}

        if not questions:
            if not question:
                raise ValueError("Ask requires at least one question")
            questions = [question]

        for question in questions:
            placeholder = "Type here.."
            if isinstance(question, (list, tuple)):
                question, placeholder = question
            input = ui.TextInput(
                label=question,
                style=TextStyle.long,
                placeholder=placeholder,
                max_length=limit,
            )
            self.add_item(input)
            self.answer = input
            self.answers[question] = input

    async def on_submit(self, interaction: Interaction):
        self.interaction = interaction
        self.stop()
        await asyncio.sleep(1)


class EmbedCreatorView(ui.View):
    def __init__(self, ctx: Context):
        self.ctx = ctx

        self.color = ctx.bot.config["theme_color"]
        self.title = None
        self.author_name = None
        self.author_icon = None
        self.thumbnail = None
        self.description = None
        self.fields = []
        self.image = None
        self.footer_name = None
        self.footer_icon = None

        super().__init__(timeout=60 * 15)

        dropdown = ui.Select(
            min_values=1,
            max_values=1,
            options=[
                Option(label=label) for label in [
                    "Set Color",
                    "Set Title",
                    "Set Author Name",
                    "Set Author Icon",
                    "Set Thumbnail",
                    "Set Description",
                    "Add New Field",
                    "Set Image",
                    "Set Footer Text",
                    "Set Footer Icon",
                    "Done"
                ]
            ]
        )
        dropdown.callback = self.callback
        self.add_item(dropdown)

    @property
    def embed(self) -> Embed:
        try:
            e = Embed(
                color=self.color,
                title=self.title[:256] if self.title else None,
                description=self.description[:4096] if self.description else None
            )
            if self.author_name:
                e.set_author(
                    name=self.author_name[:256] if self.author_name else None,
                    icon_url=self.author_icon
                )
            if self.thumbnail:
                e.set_thumbnail(url=self.thumbnail)
            for name, value in self.fields:
                e.add_field(
                    name=name[:256] if name else None,
                    value=value[:1024] if value else None,
                    inline=False
                )
            if self.image:
                e.set_image(url=self.image)
            if self.footer_name:
                e.set_footer(
                    text=self.footer_name[:256] if self.footer_name else None,
                    icon_url=self.footer_icon
                )
        except Exception as error:
            e = Embed(color=red)
            e.add_field(
                name="Failed To Edit Embed",
                value=str(error)
            )
        return e

    async def callback(self, interaction: Interaction):
        choice = interaction.data["values"][0]
        modal = None
        questions = ["What should I set it as?"]
        match choice:
            case "Done":
                self.stop()
                return await interaction.response.send_message(
                    "Alright, removed the embed builder",
                    ephemeral=True
                )
            case "Set Color":
                questions = [("What should I set it as?", "Type in a #HEX")]
            case "Add New Field":
                questions = [
                    ("Field Name", "Short Text"),
                    ("Field Value", "Long Text")
                ]
            case _item if any(word in _item for word in ["Icon", "Thumbnail"]):
                questions = [
                    ("What should I set it as?", "Paste a URL here")
                ]
            # case "Convert To JSON":
            #     file = File(BytesIO(dumps(self.embed.to_dict(), indent=2).encode()), filename="embed.json")
            #     return await interaction.response.send_message(file=file, ephemeral=True)

        if not modal:
            title = "Embed " + " ".join(choice.split()[1:])
            if choice == "Add New Field":
                title = choice
            modal = Ask(
                title=title,
                questions=questions,
                limit=4000
            )
        await interaction.response.send_modal(modal)
        await modal.wait()
        if not (value := modal.answer.value):
            return

        if "Icon" in choice or "Image" in choice:
            if "https://" not in value:
                return await modal.interaction.response.send_message(
                    "That's not a valid link", ephemeral=True
                )

        match choice:
            case "Set Color":
                try:
                    color = Color(int("0x" + value.replace("#", ""), 0))
                except:
                    return await modal.interaction.response.send_message(
                    "That's not a valid HEX color", ephemeral=True
                )
                self.color = color
            case "Set Title":
                self.title = value
            case "Set Author Name":
                self.author_name = value
            case "Set Author Icon":
                self.author_icon = value
            case "Set Thumbnail":
                self.thumbnail = value
            case "Set Description":
                self.description = value
            case "Add New Field":
                self.fields.append([a.value for a in modal.answers.values()])
            case "Set Image":
                self.image = value
            case "Set Footer Text":
                self.footer_name = value
            case "Set Footer Icon":
                self.footer_icon = value

        await modal.interaction.response.edit_message(content=None, embed=self.embed)


class Embeds(commands.Cog):
    def __init__(self, bot: Fate):
        self.bot = bot

    @commands.hybrid_command(name="embed", description="Opens the interactive embed builder")
    async def embed(self, ctx):
        view = EmbedCreatorView(ctx)
        msg = await ctx.send("Choose a field to modify", view=view)
        await view.wait()
        await msg.edit(view=None)


async def setup(bot: Fate):
    await bot.add_cog(Embeds(bot))
