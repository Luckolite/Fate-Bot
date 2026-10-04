"""
cogs.utility.audit
~~~~~~~~~~~~~~~~~~~

A cog for searching the audit log for information

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio

import discord
from discord.ext import commands

from botutils import colors, get_prefix


class Audit(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.perms = [p for p in dir(discord.AuditLogAction) if not p.startswith("_") and p != "try_value"]

    @commands.group(name="audit", description="Helps search through the audit")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.has_permissions(view_audit_log=True)
    @commands.bot_has_permissions(
        view_audit_log=True, add_reactions=True, manage_messages=True
    )
    async def _audit(self, ctx, *args):
        prefix = get_prefix(ctx)
        if not args or len(args) > 2:
            embed = discord.Embed(color=colors.cyan)
            embed.set_author(name="Audit Log Data", icon_url=ctx.author.display_avatar.url)
            if ctx.guild.icon:
                embed.set_thumbnail(url=ctx.guild.icon.url)
            embed.add_field(
                name="◈ Commands ◈",
                value=".audit [action]\n"
                      ".audit [amount]\n"
                      ".audit [action] [amount]\n"
                      ".audit @user [amount]\n",
                inline=False,
            )
            embed.add_field(
                name="◈ Actions ◈",
                value="Examples: kick, ban, message_delete\nFor a full list "
                     f"run `{prefix}audit types`",
                inline=False,
            )
            return await ctx.send(embed=embed)

        if args[0] == "types":
            return await ctx.send(", ".join(self.perms))

        audit_actions = discord.AuditLogAction
        entries = []
        log_type = None
        user = None
        limit = 1

        if len(args) == 1:
            if not args[0].isdigit() and args[0] not in self.perms:
                return await ctx.send(f"That's not an event. Run `{prefix}audit types`")
            if args[0].isdigit():
                limit = int(args[0])
                if limit > 50:
                    limit = 50
            elif args[0] in self.perms:
                log_type = getattr(audit_actions, args[0])

        elif len(args) == 2:
            if "@" in args[0]:
                converter = commands.UserConverter()
                try:
                    user = await converter.convert(ctx, args[0])
                except discord.errors.NotFound:
                    return await ctx.send("User not found")
            elif args[0] in self.perms:
                log_type = getattr(audit_actions, args[0])
            else:
                return await ctx.send("Invalid usage")
            if not args[1].isdigit():
                return await ctx.send("Invalid usage")
            limit = int(args[1])

        if user:
            search = ctx.guild.audit_logs(limit=128, action=log_type)
        else:
            search = ctx.guild.audit_logs(limit=limit, action=log_type)
        async for entry in search:
            if user:
                if hasattr(entry, "user") and entry.user and entry.user.id == user.id:
                    entries.append(entry)
                elif hasattr(entry, "target") and entry.target and entry.target.id == user.id:
                    entries.append(entry)
            else:
                entries.append(entry)
            if len(entries) == limit:
                break
        if not entries:
            return await ctx.send("Nothing found")

        def create_embed():
            embed = discord.Embed(color=self.bot.config["theme_color"])
            embed.set_author(name="AuditLog Results", icon_url=ctx.author.display_avatar.url)
            if ctx.guild.icon:
                embed.set_thumbnail(url=ctx.guild.icon.url)
            embed.description = "\n".join(page_lines)
            embed.set_footer(text=f"Page 1/{len(pages)}")
            pages.append(embed)

        pages = []
        page_lines = []
        for entry in entries:
            target = entry.target
            if isinstance(target, discord.Object):
                target = target.id
            line = f"{entry.user} {entry.action.name} to {target}"
            page_lines.append(line)
            if len(page_lines) == 9:
                create_embed()
                page_lines = []
        if page_lines:
            create_embed()

        async def add_emojis_task():
            """ So the bot can read reactions before all are added """
            for emoji in emojis:
                await msg.add_reaction(emoji)
            return

        index = 0
        emojis = ["🏡", "⏪", "⏩"]
        pages[0].set_footer(
            text=f"Page {index + 1}/{len(pages)}"
        )
        msg = await ctx.send(embed=pages[0])
        if len(pages) == 1:
            return

        await add_emojis_task()
        while True:
            try:
                reaction, user = await self.bot.utils.get_reaction(
                    lambda reaction, user: (
                        user == ctx.author
                        and reaction.message.id == msg.id
                        and str(reaction.emoji) in emojis
                    ),
                    timeout=25,
                    ignore_timeout=False,
                )
            except asyncio.TimeoutError:
                return await msg.clear_reactions()
            emoji = reaction.emoji

            if emoji == emojis[0]:  # home
                index = 0

            if emoji == emojis[1]:
                index -= 1

            if emoji == emojis[2]:
                index += 1

            if index > len(pages) - 1:
                index = len(pages) - 1

            if index < 0:
                index = 0

            pages[index].set_footer(
                text=f"Page {index + 1}/{len(pages)}"
            )
            await msg.edit(embed=pages[index])
            await msg.remove_reaction(reaction, ctx.author)


async def setup(bot):
    await bot.add_cog(Audit(bot), override=True)
