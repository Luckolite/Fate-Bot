"""
cogs.utility.SelfRoles
~~~~~~~~~~~~~~~~~~~~~~~~~

A selfroles module using buttons instead of reactions

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
from contextlib import suppress
from copy import deepcopy
from typing import *

import discord
from discord import ui, Interaction
from discord.ext import commands

from botutils import Cooldown, GetChoice, emojis, s
from fate import Fate

allowed_mentions = discord.AllowedMentions.none()

MEMBER_ABOVE_BOT_MESSAGE = (
    "I can't change your self-roles because your server position is at or above mine."
)


def record_selfrole_activity(bot, amount=1):
    telemetry = getattr(bot, "telemetry", None)
    if telemetry is not None and amount > 0:
        telemetry.increment("selfrole_activity", amount=amount)


def resolve_component_emoji(bot, value):
    """Return a usable component emoji, omitting stale custom emoji IDs."""
    if not value:
        return None
    parsed = (
        value
        if isinstance(value, discord.PartialEmoji)
        else discord.PartialEmoji.from_str(str(value))
    )
    if parsed.id is None:
        return value
    return bot.get_emoji(parsed.id)


def member_is_above_bot(guild, member) -> bool:
    """Whether Discord's hierarchy prevents the bot from editing this member."""
    member_id = getattr(member, "id", None)
    if member_id is not None and getattr(guild, "owner_id", None) == member_id:
        return True

    bot_member = getattr(guild, "me", None)
    member_top_role = getattr(member, "top_role", None)
    bot_top_role = getattr(bot_member, "top_role", None)
    if member_top_role is None or bot_top_role is None:
        return False
    return member_top_role.position >= bot_top_role.position


class SelfRoles(commands.Cog):
    def __init__(self, bot: Fate):
        self.bot = bot
        self.config = bot.utils.cache("selfroles")
        fixed = 0
        for guild_id, config in self.config.items():
            for message_id, menu in config.items():
                if "show_roles" not in menu:
                    if "!roles" in menu["text"]:
                        print(f"Didn't update {guild_id}")
                    if "show_roles" not in menu:
                        self.config[guild_id][message_id]["show_roles"] = False
                        fixed += 1
        self._migration_pending = fixed > 0
        self._menus_loaded_once = False
        self._menu_load_lock = asyncio.Lock()
        print(f"Checked {len(self.config.items())} fixed {fixed}")
                    # self.config[guild_id][message_id]["show_roles"] = False

        self.global_cooldown = Cooldown(1, 5)

    async def cog_load(self) -> None:
        if self._migration_pending:
            await self.config.flush()
            self._migration_pending = False
        if self.bot.is_ready():
            await self.load_menus_on_start()

    @commands.Cog.listener("on_ready")
    async def load_menus_on_start(self):
        async with self._menu_load_lock:
            if self._menus_loaded_once:
                return
            changed = False
            for guild_id, menus in self.config.items():
                if not self.bot.get_guild(guild_id):
                    continue
                for msg_id, data in list(menus.items()):
                    if not data.get("roles", None):
                        if channel := self.bot.get_channel(data["channel_id"]):
                            with suppress(Exception):
                                await channel.get_partial_message(int(msg_id)).delete()
                        del self.config[guild_id][msg_id]
                        changed = True
                        continue
                    if data["style"] == "category":
                        self.bot.add_view(CategoryView(self, guild_id, msg_id))
                    else:
                        self.bot.add_view(RoleView(self, guild_id, msg_id))
            if changed:
                await self.config.flush()
            self.bot.menus_loaded = True
            self._menus_loaded_once = True

    async def cog_command_error(self, ctx, error) -> None:
        """ Handle KeyError's from modifying menus """
        if cog := self.bot.get_cog("ErrorHandler"):
            await cog.suppress_key_error(ctx, error)  # type: ignore

    async def refresh_menu(self, guild_id: int, message_id: str) -> discord.Message:
        """ Re-initiates the View and updates the message content """
        meta = self.config[guild_id][message_id]

        # Sort the roles by role position
        guild = self.bot.get_guild(guild_id)
        ordered = {}
        for role_id, data in meta["roles"].items():
            if role := guild.get_role(int(role_id)):
                ordered[role.position] = [role_id, data]
        ordered = {
            role_id: data for role_id, data in [
                v for k, v in sorted(ordered.items(), key=lambda kv: kv[0], reverse=True)
            ]
        }
        del self.config[guild_id][message_id]["roles"]
        await self.config.flush()
        self.config[guild_id][message_id]["roles"] = ordered

        channel = self.bot.get_channel(meta["channel_id"])
        if not channel:
            channel = await self.bot.fetch_channel(meta["channel_id"])
        message = await channel.fetch_message(int(message_id))  # type: ignore
        new_view = RoleView(self, guild_id, int(message_id))
        content = self.format_text(guild_id, message_id)
        message = await message.edit(
            content=content,
            view=new_view,
            embed=None,
            allowed_mentions=allowed_mentions,
        )
        if message.reactions:
            await message.clear_reactions()
        return message

    def format_text(self, guild_id: int, message_id: Union[int, str]) -> str:
        conf: dict = self.config[guild_id][message_id]
        text: str = conf["text"]
        if conf["show_roles"]:
            show_stats = False
            if "!stats" in text:
                show_stats = True
                text = text.replace("!stats", "").strip()

            guild = self.bot.get_guild(guild_id)
            roles = sorted(
                (
                    role
                    for role_id in self.config[guild_id][str(message_id)]["roles"]
                    if (role := guild.get_role(int(role_id)))
                ),
                key=lambda role: role.position,
                reverse=True,
            )

            formatted_roles = []
            for i, role in enumerate(roles):
                e = emojis.creply if i != len(roles) - 1 else emojis.reply
                line = f"{e}{role.mention}"
                if show_stats:
                    line += f" `{len(role.members)}{emojis.members}`"
                formatted_roles.append(line)

            text += "\n" + "\n".join(formatted_roles)
        return text

    @commands.group(name="selfroles", aliases=["selfrole", "self-roles"], description="Shows how to use the module")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.guild_only()
    @commands.bot_has_permissions(embed_links=True)
    async def self_roles(self, ctx: commands.Context):
        if not ctx.invoked_subcommand:
            e = discord.Embed(color=self.bot.config["theme_color"])
            e.set_author(name="Selfroles", icon_url=self.bot.user.display_avatar.url)
            if ctx.guild.icon:
                e.set_thumbnail(url=ctx.guild.icon.url)
            e.description = "Create menus for users to self assign roles via buttons or " \
                            "dropdown menus\n**NOTE:** The bot will have you choose which menu to apply a " \
                            "setting to **after** running a command that edits an existing menu"
            p: str = ctx.prefix
            e.add_field(
                name="◈ Usage",
                value=f"**{p}create-menu**\n"
                      f"{p}copy-menu `message_id`\n"
                      f"{p}add-role `@role`\n"
                      f"{p}remove-role `@role`\n"
                      f"{p}set-limit [limit]\n"
                      f"{p}edit-message `new message`\n"
                      f"{p}set-emoji `new emoji`\n"
                      f"{p}set-label `new label`\n"
                      f"{p}set-description `new description`\n"
                      f"{p}toggle-percentage\n"
                      f"{p}toggle-roles `shows roles in menus`"
            )
            e.add_field(
                name="◈ Formatting",
                value=f"- To change the 'Choose your role' message after "
                      f"creating a menu use the `edit-message` command"
            )
            count = 0
            if ctx.guild.id in self.config:
                count = len(self.config[ctx.guild.id])
            e.set_footer(text=f"You Currently Have {count} Menu{'s' if count == 0 or count > 1 else ''}")
            await ctx.send(embed=e)

    @commands.command(name="refresh", description="Regenerates a menu to update changes")
    @commands.cooldown(1, 25, commands.BucketType.guild)
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def refresh(self, ctx):
        if ctx.guild.id not in self.config:
            return await ctx.send("This server has no role-menu's to refresh")
        await ctx.send("Refreshing all role menus")
        for message_id in list(self.config[ctx.guild.id].keys())[:15]:
            try:
                await self.refresh_menu(ctx.guild.id, message_id)
            except discord.errors.NotFound:
                await self.config.remove_sub(ctx.guild.id, message_id)
                await ctx.send(f"Removed no longer existing menu '{message_id}'")
            except discord.errors.HTTPException:
                await ctx.send(f"Removing {self.config[ctx.guild.id][message_id]['text']}")
                await self.config.remove_sub(ctx.guild.id, message_id)
        await ctx.send("Success 👍")

    @commands.command(name="create-menu", description="Starts the menu setup process")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True, manage_guild=True)
    @commands.bot_has_permissions(add_reactions=True)
    async def create_menu(self, ctx):
        """ The command for interactively setting up a new menu """
        e = discord.Embed(color=self.bot.config["theme_color"])
        e.set_author(name="Instructions", icon_url=ctx.author.display_avatar.url)
        e.description = "> **Send the name of the role you want me to add**\n" \
                        "For example, you only have to send the name of each role " \
                        "you want in individual messages, and when they're " \
                        "all in the menu just say 'done'\n\n" \
                        "Or here's an example message with advanced formatting:\n" \
                        "```💚 | SomeDisplayLabel | [role_id, role name, or ping]\n" \
                        "some description on what it does```"
        e.set_footer(text="Reply with 'done' when complete")

        # Get the roles
        msg = await ctx.send(embed=e)
        selected_roles = {}
        while True:
            reply = await self.bot.utils.get_message(ctx, timeout=300)
            if "cancel" in reply.content.lower():
                return await msg.delete()
            if reply.content.lower() == "done":
                await msg.delete()
                await reply.delete()
                if not selected_roles:
                    return await ctx.send("It seems you didn't add any roles. Rerun the command and try again")
                break

            name: Optional[str] = reply.content
            emoji: Optional[str] = None
            label: Optional[str] = None
            description: Optional[str] = None

            args = list(reply.content.split("\n")[0].split(" | "))
            if len(args) > 1:
                # Set the emoji
                if all(c.lower() == c.upper() for c in args[0]) or "<" in args[0]:
                    emoji = args[0]
                    try:
                        await msg.add_reaction(emoji)
                        await msg.clear_reactions()
                    except:
                        emoji = None
                    else:
                        args.pop(0)

                # Set the label
                if len(args) > 1:
                    label = args[0][:100]
                    args.pop(0)

                # Get the role with the remaining args instead of msg content
                if emoji or label:
                    name = " ".join(args)

            # Set the description
            if "\n" in reply.content:
                description = reply.content.split("\n")[1][:100]

            role = await self.bot.utils.get_role(ctx, name or reply.content)
            if not role:
                await ctx.send("Role not found", delete_after=5)
                continue

            selected_roles[role] = {
                "emoji": emoji,
                "label": label,
                "description": description
            }

            if e.fields:
                e.remove_field(0)
            e.add_field(
                name="◈ Selected Roles",
                value="\n".join([f"• {role.mention}" for role in selected_roles.keys()])
            )
            await msg.edit(embed=e)
            await reply.delete()

            if len(selected_roles) == 24:
                break

        # Set the style of the menu
        m = await ctx.send(
            "Should I use a dropdown menu or buttons, (dropdowns look significantly cleaner). "
            "Reply with `dropdown` or `buttons`"
        )
        reply = await self.bot.utils.get_message(ctx)
        if "button" in reply.content.lower():
            style = "buttons"
        else:
            style = "dropdown"
        await ctx.send(f"Alright, I'll use a {style} menu", delete_after=5)
        await m.delete()
        await reply.delete()

        # Set the channel
        for _attempt in range(2):
            m = await ctx.send("#Mention the channel you want me to use")
            reply = await self.bot.utils.get_message(ctx, timeout=300)
            if not reply.channel_mentions:
                await ctx.send("You didn't #mention a channel, retry", delete_after=5)
                await m.delete()
                await reply.delete()
                continue
            channel = reply.channel_mentions[0]
            await m.delete()
            await reply.delete()
            break
        else:
            return await ctx.send("You didn't #mention a channel, rerun the command and try again")

        msg = await channel.send("Choose your role")
        if ctx.guild.id not in self.config:
            self.config[ctx.guild.id] = {}
        self.config[ctx.guild.id][str(msg.id)] = {
            "channel_id": channel.id,
            "label": "Select your role",
            "roles": {
                str(role.id): metadata for role, metadata in selected_roles.items()
            },
            "text": "Choose your role",
            "style": style,
            "limit": 1,
            "show_percentage": True,
            "show_roles": True
        }
        view = RoleView(cls=self, guild_id=ctx.guild.id, message_id=msg.id)
        await msg.edit(view=view)
        await self.config.flush()
        await self.bot.create_log(
            message=f"!pin **Self-Role Created** - `{ctx.guild}`",
            channel="module_log",
            embedded=True,
            color="green"
        )

    @commands.command(name="copy-menu", description="Copies a self-role menu to this server")
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def copy_menu(self, ctx, message_id):
        if not message_id.isdigit():
            return await ctx.send("The message_id must be a number")
        for guild_id, menus in self.config.items():
            await asyncio.sleep(0)
            if message_id in menus:
                guild_id, config = guild_id, deepcopy(menus[message_id])
                config["channel_id"] = ctx.channel.id
                break
        else:
            return await ctx.send("I couldn't find a menu with that message id")

        # Copy the non existent roles over to the current server
        guild = self.bot.get_guild(guild_id)
        for role_id in list(config["roles"].keys()):
            if role := guild.get_role(int(role_id)):
                if local_role := discord.utils.get(ctx.guild.roles, name=role.name):
                    if local_role.position > ctx.author.top_role.position:
                        return await ctx.send(f"The role {role.name} is too high for you to manage")
                else:
                    local_role = await ctx.guild.create_role(name=role.name, color=role.color)
                config["roles"][str(local_role.id)] = config["roles"].pop(role_id)
            else:
                del config["roles"][role_id]

        msg = await ctx.send("Choose your role")
        if ctx.guild.id not in self.config:
            self.config[ctx.guild.id] = {}
        self.config[ctx.guild.id][str(msg.id)] = config
        await self.refresh_menu(ctx.guild.id, str(msg.id))
        await self.config.flush()

    @commands.command(name="combine", description="Combines self-role menus into categories")
    @commands.is_owner()
    async def combine(self, ctx, *message_ids):
        new = {
            "label": "Select a category",
            "categories": {},
            "text": "Choose a role",
            "style": "category"
        }
        for message_id in message_ids:
            conf = self.config[ctx.guild.id][message_id]
            print(conf)
            new["channel_id"] = conf["channel_id"]
            new["categories"][conf["text"]] = conf["roles"]
            del self.config[ctx.guild.id][message_id]
            with suppress(Exception):
                self.bot.views[ctx.guild.id][message_id].stop()
                del self.bot.views[ctx.guild.id][message_id]
        self.config[ctx.guild.id][message_ids[0]] = new
        view = CategoryView(self, ctx.guild.id, message_ids[0])
        msg = await self.bot.get_channel(new["channel_id"]).fetch_message(message_ids[0])
        await msg.edit(content="Categories", view=view)
        await self.config.flush()

    async def get_menu_id(self, ctx) -> str:
        """ Gets the message_id of the wanted menu """
        menus = {
            meta["text"].split("\n")[0] + f" (Menu #{i + 1})": message_id
            for i, (message_id, meta) in enumerate(self.config[ctx.guild.id].items())
        }
        if len(menus) == 1:
            choice: str = list(menus.keys())[0]
        else:
            choice: str = await GetChoice(ctx, list(menus.keys()))
        key = [k for k in menus.keys() if choice in k][0]
        return menus[key]

    @commands.command(name="add-role", description="Adds a role to an existing menu")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def add_role(self, ctx, *, role):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")
        message_id = await self.get_menu_id(ctx)
        if len(self.config[guild_id][message_id]["roles"]) == 25:
            return await ctx.send("You can't have more than 25 roles in a menu")

        name: Optional[str] = role
        emoji: Optional[str] = None
        label: Optional[str] = None
        description: Optional[str] = None

        args = list(role.split("\n")[0].split(" | "))
        if len(args) > 1:
            # Set the emoji
            if all(c.lower() == c.upper() for c in args[0]) or "<" in args[0]:
                emoji = args[0]
                args.pop(0)

            # Set the label
            if len(args) > 1:
                label = args[0][:100]
                args.pop(0)

            # Get the role with the remaining args instead of msg content
            if emoji or label:
                name = " ".join(args)

        # Set the description
        if "\n" in role:
            description = role.split("\n")[1][:100]

        try:
            await ctx.message.add_reaction(emoji)
        except (TypeError, discord.errors.HTTPException):
            name = role
        else:
            await ctx.message.clear_reactions()

        role = await self.bot.utils.get_role(ctx, name)
        if not role:
            return await ctx.send("Role not found")
        if str(role.id) in self.config[guild_id][message_id]["roles"]:
            return await ctx.send("That role's already added")

        self.config[guild_id][message_id]["roles"][str(role.id)] = {
            "emoji": emoji,
            "label": label,
            "description": description
        }

        await self.refresh_menu(guild_id, message_id)
        await ctx.send(f"Added {role.mention}", allowed_mentions=discord.AllowedMentions.none())
        await self.config.flush()

    @commands.command(name="remove-role", description="Removes a role from an existing menu")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def remove_role(self, ctx, *, role):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")
        message_id = await self.get_menu_id(ctx)
        if len(self.config[guild_id][message_id]["roles"]) == 1:
            return await ctx.send("A selfrole menu can't have zero roles. Delete the menu to remove it")

        role = await self.bot.utils.get_role(ctx, role)
        if not role:
            return await ctx.send("Role not found")
        if str(role.id) not in self.config[guild_id][message_id]["roles"]:
            return await ctx.send(
                f"{role.mention} role isn't in that menu",
                allowed_mentions=discord.AllowedMentions.none()
            )

        del self.config[guild_id][message_id]["roles"][str(role.id)]
        await self.refresh_menu(guild_id, message_id)

        await ctx.send(f"Removed {role.mention}", allowed_mentions=discord.AllowedMentions.none())
        await self.config.flush()

    @commands.command(name="toggle-percentage", description="Toggles showing the % of how many have each role")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def toggle_percentage(self, ctx):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")
        message_id = await self.get_menu_id(ctx)
        old_setting = self.config[guild_id][message_id]["show_percentage"]
        self.config[guild_id][message_id]["show_percentage"] = not old_setting
        await self.refresh_menu(guild_id, message_id)
        toggle = "Enabled" if not old_setting else "Disabled"
        await ctx.send(f"{toggle} showing the percentage")
        await self.config.flush()

    @commands.command(name="toggle-roles", description="Toggles showing the role mentions in menus")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def toggle_roles(self, ctx):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")
        message_id = await self.get_menu_id(ctx)
        old_setting = self.config[guild_id][message_id]["show_roles"]
        self.config[guild_id][message_id]["show_roles"] = not old_setting
        await self.refresh_menu(guild_id, message_id)
        toggle = "Enabled" if not old_setting else "Disabled"
        await ctx.send(f"{toggle} showing role mentions")
        await self.config.flush()

    @commands.command(name="set-limit", description="Sets the max number of roles a user can choose")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def set_limit(self, ctx, new_limit: int):
        if new_limit > 25 or new_limit < 0:
            return await ctx.send("That's not a valid number")
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")
        message_id = await self.get_menu_id(ctx)
        if new_limit == 0:
            new_limit = None
        self.config[guild_id][message_id]["limit"] = new_limit
        await self.refresh_menu(guild_id, message_id)
        await ctx.send("Set the new limit 👍")
        await self.config.flush()

    @commands.command(name="set-label", description="Sets the display label of a role")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def set_label(self, ctx, *, new_label):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")
        message_id = await self.get_menu_id(ctx)

        roles = {}
        for role_id, meta in self.config[guild_id][message_id]["roles"].items():
            role = ctx.guild.get_role(int(role_id))
            if not role:
                continue
            roles[meta["label"] or role.name] = role_id

        if len(roles) == 1:
            choice: str = list(roles.values())[0]
        else:
            choice: str = await GetChoice(ctx, list(roles.keys()))
        if roles[choice] not in self.config[guild_id][message_id]["roles"]:
            return await ctx.send(f"{choice} doesn't seem to be in the config anymore")
        self.config[guild_id][message_id]["roles"][roles[choice]]["label"] = new_label

        await self.refresh_menu(guild_id, message_id)
        await ctx.send(f"Set its label 👍")
        await self.config.flush()

    @commands.command(name="edit-message", description="Sets the msg content of a menu")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def edit_message(self, ctx, *, new_message):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")

        message_id = await self.get_menu_id(ctx)
        self.config[guild_id][message_id]["text"] = new_message

        await self.refresh_menu(guild_id, message_id)
        await ctx.send(f"Edited the content 👍")
        await self.config.flush()

    @commands.command(name="set-emoji", description="Sets a roles emoji in an existing menu")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(add_reactions=True)
    async def set_emoji(self, ctx, *, new_emoji):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")
        try:
            await ctx.message.add_reaction(new_emoji)
        except discord.errors.HTTPException:
            return await ctx.send("Invalid emoji")
        await ctx.message.clear_reactions()
        message_id = await self.get_menu_id(ctx)

        roles = {}
        for role_id, meta in self.config[guild_id][message_id]["roles"].items():
            role = ctx.guild.get_role(int(role_id))
            if not role:
                continue
            roles[meta["label"] or role.name] = role_id

        if not roles:
            return await ctx.send(
                "There doesn't seem to be any available roles in that menu anymore. "
                "I likely don't have the role cached yet; try again in a short bit"
            )

        if len(roles) == 1:
            choice: str = list(roles.keys())[0]
        else:
            choice: str = await GetChoice(ctx, list(roles.keys()))
        if roles[choice] not in self.config[guild_id][message_id]["roles"]:
            return await ctx.send(f"{choice} doesn't seem to be in the config anymore")
        self.config[guild_id][message_id]["roles"][roles[choice]]["emoji"] = new_emoji

        await self.refresh_menu(guild_id, message_id)
        await ctx.send(f"Set the emoji 👍")
        await self.config.flush()

    @commands.command(name="set-description", description="Sets a roles description in an existing menu")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def set_description(self, ctx, *, new_description):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")
        message_id = await self.get_menu_id(ctx)

        roles = {}
        for role_id, meta in self.config[guild_id][message_id]["roles"].items():
            role = ctx.guild.get_role(int(role_id))
            if not role:
                continue
            roles[meta["label"] or role.name] = role_id

        if not roles:
            return await ctx.send(
                "There doesn't seem to be any available roles in that menu anymore. "
                "I likely don't have the role cached yet; try again in a short bit"
            )

        if len(roles) == 1:
            choice: str = list(roles.values())[0]
        else:
            choice: str = await GetChoice(ctx, list(roles.keys()))
        self.config[guild_id][message_id]["roles"][roles[choice]]["description"] = new_description[:100]

        await self.refresh_menu(guild_id, message_id)
        await ctx.send(f"Set the description 👍")
        await self.config.flush()

    @commands.command(name="swap-style", description="Swaps using buttons or dropdowns")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def swap_style(self, ctx):
        guild_id = ctx.guild.id
        if guild_id not in self.config:
            return await ctx.send("There arent any active role menus in this server")
        message_id = await self.get_menu_id(ctx)
        if self.config[guild_id][message_id]["style"] == "buttons":
            new_style = "dropdown"
        else:
            new_style = "buttons"
        self.config[guild_id][message_id]["style"] = new_style
        await self.refresh_menu(guild_id, message_id)
        await ctx.send(f"Switched to {new_style}")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id in self.config:
            if str(payload.message_id) in self.config[payload.guild_id]:
                await self.config.remove_sub(payload.guild_id, str(payload.message_id))
                if not self.config[payload.guild_id]:
                    await self.config.remove(payload.guild_id)


class Categories(ui.Select):
    def __init__(self, cls, message_id: int, categories: list):
        self.cls = cls
        self.bot = cls.bot
        self.config = cls.config
        self.guild_id = cls.guild_id
        self.limit = 1
        self.message_id = message_id

        # Prepare the components for the dropdown menu
        options = []
        for category in categories:
            option = discord.SelectOption(
                label=category[:100],
                value=category
            )
            options.append(option)

        super().__init__(
            custom_id=f"select_{message_id}",
            placeholder="Select a Role",
            min_values=1,
            max_values=1,
            options=options
        )

    async def sub_menu_callback(self, interaction):
        async def remove_category_role(role_id, reason):
            categories = self.config[self.guild_id][str(self.message_id)]["categories"]
            for category_roles in categories.values():
                category_roles.pop(str(role_id), None)
            await self.config.flush()
            return await interaction.response.send_message(reason, ephemeral=True)

        # Fetch required variables
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return await interaction.response.send_message(
                "This self-role menu is no longer available.", ephemeral=True
            )
        member = guild.get_member(interaction.user.id)
        if member is None:
            return await interaction.response.send_message(
                "I couldn't resolve your server membership. Try again in a moment.",
                ephemeral=True,
            )

        # Give selected roles
        selected_values = set(interaction.data["values"])
        roles_to_add = []
        for role_id in interaction.data["values"]:
            role = guild.get_role(int(role_id))
            if not role:
                return await remove_category_role(
                    role_id,
                    f"{role_id} doesn't seem to exist anymore",
                )

            if role.position >= guild.me.top_role.position:
                return await remove_category_role(
                    role_id,
                    f"{role.name} is too high for me to manage",
                )

            if role not in member.roles:
                roles_to_add.append(role)

        if member_is_above_bot(guild, member):
            return await interaction.response.send_message(
                MEMBER_ABOVE_BOT_MESSAGE,
                ephemeral=True,
            )

        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, atomic=False)
            except discord.Forbidden:
                if member_is_above_bot(guild, member):
                    return await interaction.response.send_message(
                        MEMBER_ABOVE_BOT_MESSAGE,
                        ephemeral=True,
                    )
                role = roles_to_add[0]
                return await remove_category_role(
                    role.id,
                    f"I couldn't access {role.name}, so I removed it from this "
                    "self-role menu.",
                )
            record_selfrole_activity(self.bot, len(roles_to_add))

        # Take away unselected roles
        data = self.config[self.guild_id][str(self.message_id)]["categories"]
        roles_to_remove = []
        for roles in data.values():
            if selected_values.intersection(roles):
                roles_to_remove.extend(
                    role
                    for role_id in roles
                    if role_id not in selected_values
                    and (role := guild.get_role(int(role_id)))
                    and role in member.roles
                )
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, atomic=False)
            record_selfrole_activity(self.bot, len(roles_to_remove))

        await interaction.response.edit_message(
            content="Successfully set your roles",
            view=None
        )

    async def callback(self, interaction: discord.Interaction):
        """ Let the main View class handle the interaction """
        category = interaction.data["values"][0]
        view = ui.View(timeout=45)

        index = {}
        guild = self.bot.get_guild(self.guild_id)
        data = self.config[self.guild_id][str(self.message_id)]["categories"][category]
        for role_id, meta in data.items():
            role = guild.get_role(int(role_id))
            if role:
                index[role] = meta

        select = Select(
            self,
            self.guild_id,
            self.message_id,
            index,
            self.sub_menu_callback
        )
        view.add_item(select)
        await interaction.response.send_message("Choose which role", view=view, ephemeral=True)


class CategoryView(ui.View):
    def __init__(self, cls: SelfRoles, guild_id: int, message_id: int):
        self.bot = cls.bot
        self.config = cls.config
        self.guild_id = guild_id
        self.message_id = message_id

        # Replacing existing instances to refresh information
        if guild_id not in self.bot.views:
            self.bot.views[guild_id] = {}
        if str(message_id) in self.bot.views[guild_id]:
            with suppress(Exception):
                self.bot.views[guild_id][str(message_id)].stop()
        self.bot.views[guild_id][str(message_id)] = self

        super().__init__(timeout=None)

        conf = self.config[guild_id][str(message_id)]
        self.add_item(Categories(self, message_id, conf["categories"].keys()))


class RoleView(ui.View):
    def __init__(self, cls: SelfRoles, guild_id: int, message_id: int):
        self.cls = cls
        self.bot = cls.bot
        self.config = cls.config
        self.guild_id = guild_id
        self.message_id = message_id

        # Replacing existing instances to refresh information
        if guild_id not in self.bot.views:
            self.bot.views[guild_id] = {}
        if str(message_id) in self.bot.views[guild_id]:
            with suppress(Exception):
                self.bot.views[guild_id][str(message_id)].stop()
        self.bot.views[guild_id][str(message_id)] = self

        conf: Dict[str, Optional[Any]] = cls.config[guild_id][str(message_id)]
        self.style: str = cls.config[guild_id][str(message_id)]["style"]
        self.limit: int = conf["limit"]
        if not self.limit or self.limit > len(conf["roles"]):
            self.limit = len(conf["roles"])

        self.buttons = {}

        # Setup cooldowns for interactions
        self.global_cooldown = cls.global_cooldown
        cd = [5, 25]
        if self.style == "buttons":
            cd = [1, 5]
        self.cooldown = Cooldown(*cd)

        super().__init__(timeout=None)

        if self.style == "buttons":
            self.add_buttons()
        else:
            self.menu = Select(self, guild_id, message_id, self.index())
            self.add_item(self.menu)

    def add_buttons(self):
        self.clear_items()
        conf = self.config[self.guild_id][str(self.message_id)]
        guild = self.bot.get_guild(self.guild_id)

        roles = []
        for role_id, data in conf["roles"].items():
            role = guild.get_role(int(role_id))
            if role:
                roles.append([(role, role_id, data), role.position])

        for (role, role_id, data), _position in sorted(roles, key=lambda x: x[1], reverse=True):
            configured_label = data.get("label")
            component_emoji = resolve_component_emoji(self.bot, data.get("emoji"))
            label = configured_label or role.name
            if conf["show_percentage"]:
                member_count = role.guild.member_count or len(role.guild.members)
                percentage = round(len(role.members) / member_count * 100)
                label = f"({percentage}%) {label[:90]}"

            # Add a new button to the class
            button = ui.Button(
                label=label,
                emoji=component_emoji,
                style=discord.ButtonStyle.blurple,
                custom_id=f"{role_id}@{self.message_id}"
            )
            if component_emoji is not None and configured_label == data.get("emoji"):
                button.label = None
            button.callback = self.surface_callback
            self.buttons[button.custom_id] = button
            self.add_item(button)

    def index(self) -> dict:
        index = {}
        guild = self.bot.get_guild(self.guild_id)
        roles = []
        for role_id, meta in list(self.config[self.guild_id][str(self.message_id)]["roles"].items()):
            role = guild.get_role(int(role_id))
            if role:
                roles.append([(role, meta), role.position])
            else:
                del self.config[self.guild_id][str(self.message_id)]["roles"][role_id]
        for (role, meta), _position in sorted(roles, key=lambda x: x[1], reverse=True):
            index[role] = meta
        return index

    async def on_error(self, interaction, error, item) -> None:
        self.bot.loop.call_exception_handler(
            {
                "message": "Self-role interaction failed",
                "exception": error,
                "interaction": interaction,
                "item": item,
            }
        )

    async def surface_callback(self, interaction) -> None:
        """ Handle cooldowns and suppress exceptions in the actual callback function """
        with suppress(discord.errors.NotFound):
            check1 = self.global_cooldown.check(interaction.user.id)
            check2 = self.cooldown.check(interaction.user.id)
            if check1 or check2:
                return await interaction.response.send_message(
                    "You're on cooldown, try again in a moment", ephemeral=True
                )
            if self.style == "buttons":
                await self.button_callback(interaction)
            else:
                await self.select_callback(interaction)

    async def button_callback(self, interaction: Interaction) -> Optional[discord.Message]:
        """ The callback function for when a buttons pressed """

        async def remove_button(reason) -> discord.Message:
            """ Remove a button that can no longer be used """
            button = self.buttons.pop(custom_id, None)
            if button is not None:
                self.remove_item(button)
            with suppress(KeyError):
                self.config[self.guild_id][key]["roles"].pop(str(role_id), None)
            await self.config.flush()
            conf = self.config[self.guild_id][key]
            if conf.get("show_roles", False):
                content = self.cls.format_text(self.guild_id, key)
                await interaction.message.edit(
                    content=content,
                    view=self,
                    allowed_mentions=allowed_mentions,
                )
            else:
                await interaction.message.edit(view=self)
            return await interaction.response.send_message(reason, ephemeral=True)

        # Parse the key and get its relative data
        custom_id = interaction.data["custom_id"]
        key = custom_id.split("@")[1]
        role_id = int(custom_id.split("@")[0])

        # Fetch required variables
        guild = interaction.guild or self.bot.get_guild(self.guild_id)
        if guild is None:
            return await interaction.response.send_message(
                "This self-role menu is no longer available.", ephemeral=True
            )
        member = guild.get_member(interaction.user.id)
        role = guild.get_role(role_id)
        name = self.buttons[custom_id].label

        if member is None:
            return await interaction.response.send_message(
                "I couldn't resolve your server membership. Try again in a moment.",
                ephemeral=True,
            )
        if not role:
            return await remove_button(f"{name} doesn't seem to exist anymore")
        if role.position >= guild.me.top_role.position:
            return await remove_button(f"{role.name} is too high for me to manage")
        if member_is_above_bot(guild, member):
            return await interaction.response.send_message(
                MEMBER_ABOVE_BOT_MESSAGE,
                ephemeral=True,
            )

        conf = self.config[guild.id][str(interaction.message.id)]
        if conf["limit"] == 1:
            to_remove = [
                menu_role
                for menu_role in self.index()
                if menu_role in member.roles and menu_role.id != role.id
            ]
            if to_remove:
                await member.remove_roles(*to_remove, atomic=False)
                record_selfrole_activity(self.bot, len(to_remove))

        if role in member.roles:
            await member.remove_roles(role)
            record_selfrole_activity(self.bot)
            action = "Removed"
        else:
            action = "Gave you"
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                # Re-check the member in case their hierarchy changed while the
                # interaction was in flight. Their position is not a menu defect.
                if member_is_above_bot(guild, member):
                    return await interaction.response.send_message(
                        MEMBER_ABOVE_BOT_MESSAGE,
                        ephemeral=True,
                    )
                return await remove_button(
                    f"I couldn't access {role.name}, so I removed it from this "
                    "self-role menu."
                )
            record_selfrole_activity(self.bot)
        await interaction.response.send_message(
            f"{action} {role.mention}",
            ephemeral=True
        )

        message_id = str(self.message_id)
        if self.config[self.guild_id][message_id]["show_percentage"]:
            try:
                self.add_buttons()
                conf = self.config[self.guild_id][message_id]
                if "show_roles" not in conf:
                    print(self.bot.get_guild(self.guild_id))
                if conf["show_roles"]:
                    content = self.cls.format_text(self.guild_id, message_id)
                    await interaction.message.edit(
                        content=content, view=self, allowed_mentions=allowed_mentions
                    )
                else:
                    await interaction.message.edit(view=self)
            except Exception as error:
                self.bot.loop.call_exception_handler(
                    {
                        "message": "Failed to refresh a self-role percentage menu",
                        "exception": error,
                    }
                )

    async def select_callback(self, interaction: Interaction) -> Optional[discord.Message]:
        """ The callback function for when a buttons pressed """

        async def adjust_options(reason=None, remove_role_ids=()) -> None:
            """ Remove a button that can no longer be used """
            if remove_role_ids:
                roles = self.config[self.guild_id][str(self.message_id)]["roles"]
                for remove_role_id in remove_role_ids:
                    roles.pop(str(remove_role_id), None)
                await self.config.flush()
            self.clear_items()
            self.menu = Select(self, self.guild_id, self.message_id, self.index())
            self.add_item(self.menu)
            msg_id = str(interaction.message.id)
            if self.config[guild.id][msg_id]["show_roles"]:
                content = self.cls.format_text(guild.id, msg_id)
                await interaction.message.edit(
                    content=content, view=self, allowed_mentions=allowed_mentions
                )
            else:
                await interaction.message.edit(view=self)
            if reason:
                await interaction.response.send_message(reason, ephemeral=True)
            return

        # Fetch required variables
        guild = interaction.guild or self.bot.get_guild(self.guild_id)
        if guild is None:
            return await interaction.response.send_message(
                "This self-role menu is no longer available.", ephemeral=True
            )
        member = guild.get_member(interaction.user.id)
        if member is None:
            return await interaction.response.send_message(
                "I couldn't resolve your server membership. Try again in a moment.",
                ephemeral=True,
            )
        # Give selected roles
        roles_to_add = []
        if "remove_all" not in interaction.data["values"]:
            for role_id in interaction.data["values"]:
                role = guild.get_role(int(role_id))
                if not role:
                    return await adjust_options(
                        f"{role_id} doesn't seem to exist anymore",
                        (role_id,),
                    )

                if role.position >= guild.me.top_role.position:
                    return await adjust_options(
                        f"{role.name} is too high for me to manage",
                        (role_id,),
                    )

                if role not in member.roles:
                    roles_to_add.append(role)

        if member_is_above_bot(guild, member):
            return await interaction.response.send_message(
                MEMBER_ABOVE_BOT_MESSAGE,
                ephemeral=True,
            )

        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, atomic=False)
            except discord.Forbidden:
                if member_is_above_bot(guild, member):
                    return await interaction.response.send_message(
                        MEMBER_ABOVE_BOT_MESSAGE,
                        ephemeral=True,
                    )
                return await adjust_options(
                    "I couldn't access one or more selected roles, so I removed "
                    "them from this self-role menu.",
                    (role.id for role in roles_to_add),
                )
            record_selfrole_activity(self.bot, len(roles_to_add))

        # Take away unselected roles
        selected_values = set(interaction.data["values"])
        roles_to_remove = [
            role
            for role in self.index()
            if str(role.id) not in selected_values and role in member.roles
        ]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, atomic=False)
            record_selfrole_activity(self.bot, len(roles_to_remove))

        await interaction.response.send_message(
            "Successfully set your roles",
            ephemeral=True
        )
        if self.config[guild.id][str(interaction.message.id)]["show_percentage"]:
            await adjust_options()


class Select(discord.ui.Select):
    def __init__(self, cls: Union[RoleView, Any], guild_id: int, message_id: int, roles: dict, callback=None):
        self.cls = cls
        self.custom_callback = callback

        # Prepare the components for the dropdown menu
        options = [discord.SelectOption(
            label="Remove All",
            value="remove_all",
            emoji="🚫",
            description="Removes all your roles from this menu"
        )]
        has_descriptions = any(meta["description"] for meta in roles.values())
        for role, meta in roles.items():
            meta = dict(meta)
            label = meta.pop("label") or role.name[:100]
            meta["emoji"] = resolve_component_emoji(cls.bot, meta.get("emoji"))
            if cls.config[guild_id][str(message_id)]["show_percentage"]:
                total = len(role.members)
                member_count = role.guild.member_count or len(role.guild.members)
                percentage = round(total/member_count * 100)
                if has_descriptions:
                    label = f"({percentage}%) {label[:90]}"
                else:
                    meta["description"] = f"{total} Member{s(total)} ({percentage}%)"
            option = discord.SelectOption(
                label=label,
                value=str(role.id),
                **meta
            )
            options.append(option)
        if self.cls.limit > len(options):
            self.cls.limit = len(options)
        super().__init__(
            custom_id=f"select_{message_id}",
            placeholder="Select a Role",
            min_values=1,
            max_values=self.cls.limit,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        """ Let the main View class handle the interaction """
        if self.custom_callback:
            await self.custom_callback(interaction)
        else:
            await self.cls.surface_callback(interaction)


async def setup(bot: Fate):
    await bot.add_cog(SelfRoles(bot))
