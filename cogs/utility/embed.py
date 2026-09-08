import asyncio
from io import BytesIO
from json import dumps

from discord import (
    Embed,
    File,
    Interaction,
    SelectOption as Option,
    TextStyle,
    ui
)
from discord.ext import commands
from discord.ext.commands import Context

from fate import Fate


class Ask(ui.Modal):
    def __init__(self, title: str, question: str = None, questions: list = None, limit: int = 4000):
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
            input = ui.TextInput(
                label="Shortly describe the option",
                style=TextStyle.long,
                placeholder="Type here..",
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
                    "Set Title",
                    "Set Author Name",
                    "Set Author Icon",
                    "Set Thumbnail",
                    "Set Description",
                    "Add New Field",
                    "Set Image",
                    "Set Footer Text",
                    "Set Footer Icon",
                    "Convert To JSON",
                    "Convert From JSON"
                ]
            ]
        )
        dropdown.callback = self.callback
        self.add_item(dropdown)

    @property
    def embed(self) -> Embed:
        e = Embed(color=self.color)
        if self.author_name:
            e.set_author(name=self.author_name, icon_url=self.author_icon)
        if self.thumbnail:
            e.set_thumbnail(url=self.thumbnail)
        e.description = self.description
        for name, value in self.fields:
            e.add_field(name=name, value=value, inline=False)
        if self.image:
            e.set_image(url=self.image)
        if self.footer_name:
            e.set_footer(text=self.footer_name, icon_url=self.footer_icon)
        return e

    async def callback(self, interaction: Interaction):
        choice = interaction.data["values"][0]
        modal = None
        match choice:
            case "Add New Field":
                modal = Ask(
                    title="Placeholder",
                    questions=[
                        "What should the field name be",
                        "What should the field value be"
                    ],
                    limit=1024
                )
            case "Convert To JSON":
                file = File(BytesIO(dumps(self.embed.to_dict(), indent=2).encode()), filename="embed.json")
                return await interaction.response.send_message(file=file, ephemeral=True)
            case "Convert From JSON":
                return await interaction.response.send_message(
                    "This isn't supported yet", ephemeral=True
                )

        if not modal:
            modal = Ask(
                title="Placeholder",
                question="What should I set it as?",
                limit=4000
            )
        await interaction.response.send_modal(modal)
        await modal.wait()
        if not (value := modal.answer.value):
            return

        match choice:
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

    @commands.command(name="xembed", description="Opens the experimental embed builder")
    async def xembed(self, ctx):
        view = EmbedCreatorView(ctx)
        await ctx.send("Placeholder", view=view)


async def setup(bot: Fate):
    await bot.add_cog(Embeds(bot))
