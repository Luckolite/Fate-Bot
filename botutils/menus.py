"""
Menus Utils
~~~~~~~~~~~~

Contains classes for emulating menus

:copyright: (C) 2020-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import random
import secrets
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from time import time
from typing import Generator, List, Optional, Union

import discord
from PIL import Image, ImageFont, ImageDraw
from discord import Forbidden, HTTPException, NotFound

from checks.exceptions import IgnoredExit
from . import colors
from .tools import get_time, format_date

style = discord.ButtonStyle


CAPTCHA_FILENAME = "fate-captcha.png"


def _normalize_captcha(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _render_captcha(code: str) -> BytesIO:
    """Render a high-contrast captcha without touching the filesystem."""
    width, height = 720, 260
    image = Image.new("RGB", (width, height), (15, 23, 42))
    draw = ImageDraw.Draw(image)

    # A subtle indigo gradient keeps the card readable in either Discord theme.
    for y in range(height):
        blend = y / max(height - 1, 1)
        color = (
            int(15 + (30 - 15) * blend),
            int(23 + (27 - 23) * blend),
            int(42 + (75 - 42) * blend),
        )
        draw.line((0, y, width, y), fill=color)

    for _ in range(90):
        x = random.randrange(width)
        y = random.randrange(height)
        radius = random.choice((1, 1, 2, 3))
        shade = random.choice(((99, 102, 241), (56, 189, 248), (45, 212, 191)))
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=shade)

    font_path = Path(__file__).with_name("fonts") / "Roboto-Bold.ttf"
    font = ImageFont.truetype(str(font_path), 96)
    palette = ((248, 250, 252), (165, 180, 252), (125, 211, 252), (153, 246, 228))
    character_width = 96
    start_x = (width - (character_width * len(code))) // 2
    for index, character in enumerate(code):
        x = start_x + index * character_width
        y = 70 + random.randint(-10, 10)
        draw.text((x + 4, y + 5), character, font=font, fill=(2, 6, 23))
        draw.text((x, y), character, font=font, fill=random.choice(palette))

    for _ in range(5):
        points = [
            (0, random.randrange(35, height - 35)),
            (width // 3, random.randrange(35, height - 35)),
            ((width * 2) // 3, random.randrange(35, height - 35)),
            (width, random.randrange(35, height - 35)),
        ]
        draw.line(points, fill=random.choice(palette), width=random.choice((2, 3)))

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def _captcha_result_view(user: discord.abc.User, passed: bool, elapsed: int):
    color = colors.green if passed else colors.red
    title = "Human check complete" if passed else "Challenge expired"
    detail = (
        f"{user.mention}, your code was accepted in **{format_date(seconds=elapsed)}**."
        if passed
        else f"{user.mention}, start a new challenge when you're ready to try again."
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            discord.ui.TextDisplay(f"## {'✅' if passed else '⌛'} {title}"),
            discord.ui.TextDisplay(detail),
            accent_colour=color,
        )
    )
    return view


class CaptchaAnswerModal(discord.ui.Modal):
    def __init__(self, challenge: "CaptchaChallenge"):
        super().__init__(title="Complete the human check")
        self.challenge = challenge
        self.answer = discord.ui.TextInput(
            custom_id="fate:captcha:answer",
            placeholder="Enter the 6 characters",
            min_length=6,
            max_length=12,
        )
        self.add_item(
            discord.ui.Label(
                text="Captcha code",
                description="Capitalization and spaces do not matter.",
                component=self.answer,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        challenge = self.challenge
        if interaction.user.id != challenge.user.id:
            return await interaction.response.send_message(
                "This challenge belongs to someone else.", ephemeral=True
            )
        if challenge.is_finished():
            return await interaction.response.send_message(
                "That challenge is no longer active.", ephemeral=True
            )

        if _normalize_captcha(self.answer.value) == _normalize_captcha(challenge.code):
            challenge.passed = True
            challenge.stop()
            return await interaction.response.send_message(
                "Code accepted — finishing up now.", ephemeral=True
            )

        challenge.attempts += 1
        await interaction.response.send_message(
            "That code doesn't match. Check the image and try again — your timer is still running.",
            ephemeral=True,
        )


class CaptchaButton(discord.ui.Button):
    def __init__(self, challenge: "CaptchaChallenge"):
        super().__init__(
            label="Enter code",
            emoji="🔐",
            style=discord.ButtonStyle.primary,
            custom_id=f"fate:captcha:{secrets.token_hex(8)}",
        )
        self.challenge = challenge

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.challenge.user.id:
            return await interaction.response.send_message(
                "This challenge belongs to someone else.", ephemeral=True
            )
        await interaction.response.send_modal(CaptchaAnswerModal(self.challenge))


class CaptchaChallenge(discord.ui.LayoutView):
    def __init__(
        self,
        user: discord.abc.User,
        code: str,
        timeout: int,
        reason: Optional[str] = None,
    ):
        super().__init__(timeout=timeout)
        self.user = user
        self.code = code
        self.passed = False
        self.attempts = 0
        self.message: Optional[discord.Message] = None

        prompt = reason or f"{user.mention}, prove you're human to continue."
        expires = int(time() + timeout)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("## 🛡️ Quick human check"),
                discord.ui.TextDisplay(prompt),
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(
                        f"attachment://{CAPTCHA_FILENAME}",
                        description="Six-character Fate captcha",
                    )
                ),
                discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
                discord.ui.TextDisplay(
                    f"Select **Enter code** and copy the six characters shown above. "
                    f"This challenge expires <t:{expires}:R>."
                ),
                discord.ui.ActionRow(CaptchaButton(self)),
                accent_colour=colors.fate,
            )
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "I couldn't process that response. Please try once more.", ephemeral=True
            )
        raise error


class _Select(discord.ui.Select):
    """ The dropdown menu for picking a choice """
    def __init__(self, user_id, choices, limit=1, placeholder="Select your choice") -> None:
        """
        :param int user_id:
        :param Sequence or Dict[str, str] choices:
        :param int limit:
        :param str placeholder:
        """
        self.user_id = user_id
        self.limit = limit

        if not choices:
            raise TypeError("No options were provided")
        options = []
        if isinstance(choices, dict):
            choices = list(choices.items())
        for option in list(choices):
            if isinstance(option, tuple):
                label, description = option
                if any(str(label) == o.label for o in options):
                    continue
                description = str(description)
                if " " not in description:
                    options.append(discord.SelectOption(
                        label=str(label)[:100],
                        value=description[:100]
                    ))
                else:
                    options.append(discord.SelectOption(
                        label=str(label)[:100],
                        description=str(description)[:100]
                    ))
            else:
                if any(str(option) == o.label for o in options):
                    continue
                options.append(discord.SelectOption(label=str(option)[:100]))

        super().__init__(
            custom_id=f"select_choice_{time()}",
            placeholder=placeholder,
            min_values=1,
            max_values=limit,
            options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """ Handle interactions with the dropdown menu """
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the user who initiated this command can interact",
                ephemeral=True
            )
            return
        self.view.selected = interaction.data["values"]
        await interaction.response.defer()
        if not self.view.message:
            await interaction.message.delete()
        self.view.stop()


class GetChoice(discord.ui.View):
    """ Has the author choose between a collection of options """
    selected: List[str] = None
    message: Optional[discord.Message] = None
    message_passed = False

    def __init__(self, ctx, choices, limit=1, placeholder="Options", message=None, delete_after=True) -> None:
        """
        :param Context ctx:
        :param Sequence or KeysView or Dict[str, str] choices:
        :param int or None limit:
        :param str placeholder:
        :param discord.Message message:
        :param bool delete_after:
        """
        if len(choices) > 25:
            if isinstance(choices, dict):
                choices = dict(list(choices.items())[:25])
            else:
                choices = list(choices)[:25]

        self.ctx = ctx
        self.choices = choices
        if limit is None:
            limit = len(choices)
        self.limit = limit
        self.message = message
        self.delete_after = delete_after

        self.original_choices = list(choices)
        if limit > len(choices):
            self.limit = len(choices)
        if message:
            self.message_passed = True

        super().__init__(timeout=45)
        self.add_item(_Select(ctx.author.id, choices, self.limit, placeholder))

    def __await__(self) -> Generator[None, None, Union[str, List[str]]]:
        """ Makes the class an awaitable """
        return self._await_callback().__await__()

    async def _await_callback(self) -> Union[str, List[str]]:
        """ Callback for when the class is awaited """
        if self.message_passed:
            await self.message.edit(view=self)
            message = self.message
        else:
            message = await self.ctx.send("Select your choice", view=self)
            self.message = message

        await self.wait()

        if not self.selected and self.delete_after:
            with suppress(Exception):
                self.ctx.bot.suppressed.append(message.id)
                await message.delete()
            raise IgnoredExit

        elif not self.selected:
            e = discord.Embed(color=colors.red)
            e.set_author(name="Expired Menu", icon_url=self.ctx.author.display_avatar.url)
            await message.edit(content=None, view=None, embed=e)
            raise IgnoredExit

        if self.delete_after:
            with suppress(Exception):
                self.ctx.bot.suppressed.append(message.id)
                await message.delete()

        if self.limit == 1:
            choices = self.choices
            if isinstance(choices, dict):
                choices = choices.keys()
            for choice in choices:
                await asyncio.sleep(0)
                if self.selected[0] == choice:
                    return choice
            for choice in choices:
                await asyncio.sleep(0)
                if self.selected[0] in choice:
                    return choice
            return self.selected[0]
        return self.selected

    async def on_error(self, _interaction, error, _item) -> None:
        """ Ignore NotFound and IgnoredExit errors """
        if not isinstance(error, (NotFound, IgnoredExit)):
            raise error


class Menus:
    def __init__(self, bot):
        self.bot = bot
        self._tasks = set()

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        """Keep short-lived menu helpers alive and observe their failures."""
        self._tasks.add(task)

        def discard(completed: asyncio.Task) -> None:
            self._tasks.discard(completed)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    self.bot.loop.call_exception_handler({
                        "message": "Menu reaction setup failed",
                        "exception": error,
                        "task": completed,
                    })

        task.add_done_callback(discard)
        return task

    async def verify_user(
        self,
        context=None,
        channel=None,
        user=None,
        timeout=45,
        delete_after=False,
        reason=None,
        interaction: Optional[discord.Interaction] = None,
    ):
        if interaction is None and context is not None:
            interaction = getattr(context, "interaction", None)
        if not user and not context:
            raise TypeError(
                "verify_user() requires either 'context' or 'user', and neither was given"
            )
        if not channel and not context and not interaction:
            raise TypeError(
                "verify_user() requires 'context', 'channel', or 'interaction'"
            )
        if not user:
            user = context.author
        if not channel and context:
            channel = context.channel

        timeout = max(15, int(timeout))
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        image = await self.bot.loop.run_in_executor(None, _render_captcha, code)
        challenge = CaptchaChallenge(user, code, timeout, reason)
        captcha_file = discord.File(
            image,
            filename=CAPTCHA_FILENAME,
            description="Six-character human verification captcha",
        )
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=True)

        if interaction:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            message = await interaction.followup.send(
                file=captcha_file,
                view=challenge,
                allowed_mentions=mentions,
                ephemeral=True,
                wait=True,
            )
        else:
            message = await channel.send(
                file=captcha_file,
                view=challenge,
                allowed_mentions=mentions,
            )

        challenge.message = message
        started_at = time()
        try:
            await challenge.wait()
        except asyncio.CancelledError:
            challenge.stop()
            elapsed = max(1, int(time() - started_at))
            ephemeral = bool(
                getattr(getattr(message, "flags", None), "ephemeral", False)
            )
            if delete_after and not ephemeral:
                with suppress(NotFound, Forbidden, HTTPException):
                    await message.delete()
            else:
                with suppress(NotFound, Forbidden, HTTPException):
                    await message.edit(
                        view=_captcha_result_view(user, False, elapsed), attachments=[]
                    )
            raise
        elapsed = max(1, int(time() - started_at))
        result = _captcha_result_view(user, challenge.passed, elapsed)
        ephemeral = bool(getattr(getattr(message, "flags", None), "ephemeral", False))

        if delete_after and not ephemeral:
            with suppress(NotFound, Forbidden, HTTPException):
                await message.delete()
        else:
            with suppress(NotFound, Forbidden, HTTPException):
                await message.edit(view=result, attachments=[])
        return challenge.passed

    async def get_choice(self, ctx, *options, user=None, name="Select which option", timeout=30):
        """ Reaction based menu for users to choose between things """

        if not options:
            raise ValueError("get_choice() requires at least one option")

        async def add_reactions(message) -> None:
            for emoji in emojis:
                if not message:
                    return
                try:
                    await message.add_reaction(emoji)
                except (discord.NotFound, discord.Forbidden):
                    return
                if len(options) > 5:
                    await asyncio.sleep(1)
                elif len(options) > 2:
                    await asyncio.sleep(0.5)

        def predicate(r, u) -> bool:
            return u.id == user.id and str(r.emoji) in emojis

        options = options[:8] if not isinstance(options[0], list) else options[0][:8]
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️"][
            : len(options)
        ]
        if not user:
            user = ctx.author

        e = discord.Embed(color=colors.fate)
        e.set_author(name=name, icon_url=ctx.author.display_avatar.url)
        e.description = "\n".join(
            f"{emojis[i]} {option}" for i, option in enumerate(options)
        )
        e.set_footer(text=f"You have {get_time(timeout)}")
        message = await ctx.send(embed=e)
        reaction_task = asyncio.create_task(add_reactions(message))

        try:
            reaction, _user = await self.bot.wait_for(
                "reaction_add", check=predicate, timeout=timeout
            )
        except asyncio.TimeoutError:
            await message.delete()
            return None
        else:
            await message.delete()
            return options[emojis.index(str(reaction.emoji))]
        finally:
            if not reaction_task.done():
                reaction_task.cancel()
            with suppress(asyncio.CancelledError, HTTPException):
                await reaction_task

    async def configure(self, ctx, options: dict) -> Union[dict, None]:
        """ Reaction based configuration """
        if not options:
            return {}

        def r_check(reaction, user):
            return user.id == ctx.author.id and reaction.message.id == message.id

        async def clear_user_reactions(message) -> None:
            with suppress(NotFound, Forbidden, NameError):
                await message.remove_reaction(reaction.emoji, user)

        async def init_reactions_task() -> None:
            if len(options) > 9:
                other = ["🏡", "◀", "▶"]
                for i, emoji in enumerate(other):
                    if i > 0:
                        await asyncio.sleep(1)
                    await message.add_reaction(emoji)
            for emoji in emojis[:len(options)]:
                await message.add_reaction(emoji)
            await message.add_reaction("✅")

        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️"]
        option_items = list(options.items())
        pages = [
            dict(option_items[index:index + len(emojis)])
            for index in range(0, len(option_items), len(emojis))
        ]
        page = 0

        def overview():
            e = discord.Embed(color=0x992D22)
            e.description = ""
            for i, (key, value) in enumerate(pages[page].items()):
                if isinstance(value, list):
                    value = " ".join([str(v) for v in value])
                e.description += f"\n{emojis[i]} | {key} - {'enabled' if value else 'disabled'}"
            return e

        message = await ctx.send(embed=overview())
        self._track_task(asyncio.create_task(init_reactions_task()))
        while True:
            reaction, user = await self.bot.utils.get_reaction(r_check)
            await clear_user_reactions(message)
            emoji = str(reaction.emoji)

            # Changing pages
            if emoji == "🏡":
                await message.edit(embed=overview())
                continue
            elif emoji == "▶":
                page = min(page + 1, len(pages) - 1)
                await message.edit(embed=overview())
                continue
            elif emoji == "◀":
                page = max(page - 1, 0)
                await message.edit(embed=overview())
                continue
            elif emoji == "✅":
                full = {}
                for page in pages:
                    for key, value in page.items():
                        full[key] = value
                await message.edit(content="Menu Inactive")
                await message.clear_reactions()
                return full

            # Altering values
            index = emojis.index(str(reaction.emoji))
            value = pages[page][list(pages[page].keys())[index]]

            # Toggling between enabled and disabled
            if isinstance(value, bool):
                pages[page][list(pages[page].keys())[index]] = not value
                await message.edit(embed=overview())
                continue

            # Setting a string
            await ctx.send(
                f"Send the new text for {list(pages[page].keys())[index]}",
                delete_after=30,
            )
            msg = await self.bot.utils.get_message(ctx)
            if not msg.content:
                continue
            pages[page][list(pages[page].keys())[index]] = msg.content

            await message.edit(embed=overview())
            await msg.delete()
