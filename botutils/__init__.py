"""
Utility Functions Wrapper
~~~~~~~~~~~~~~~~~~~~~~~~~~

An ease of use wrapper for use with Fate(written in discord.py)

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

__title__ = "BotUtils"
__author__ = "Luckolite"
__license__ = "Proprietary, see LICENSE for details"
__copyright__ = "Copyright (C) 2021-present Luckolite, All Rights Reserved"
__version__ = "1.0.0"

from functools import partial
from typing import Callable

from discord.ext.commands import Cog

from . import colors as colors
from . import emojis as emojis
from . import pillow as pillow
from .attributes import Attributes
from .formatting import chain
from .get_user import GetUser
from .interactions import AuthorView, Configure, GetConfirmation, Menu, ModView
from .listeners import Conversation as Conversation
from .listeners import Listener
from .menus import GetChoice, Menus
from .prefixes import get_prefix, get_prefixes_async
from .regex import find_link, find_links, findall, sanitize, search
from .resources import (
    AsyncFileManager, Cache, Cursor, FileCache, TempDownload, download,
    save_json,
)
from .stack import Stack as Stack
from .tools import (
    Cooldown, Formatting, OperationLock, PersistentTasks, TempConvo,
    TemporaryList, bytes2human, cln, cooldowns, extract_time, format_date,
    formulas, get_images, get_role, get_seconds, get_time, get_user_rewrite,
    operators, s, split, total_seconds, update_msg, url_from,
)
from .vars import abcs, file_exts, notable_permissions
from .views import AddResponseButton, CancelButton, ChoiceButtons

__all__ = (
    "AddResponseButton", "AsyncFileManager", "Attributes", "AuthorView",
    "Cache", "CancelButton", "ChoiceButtons", "Configure", "Conversation",
    "Cooldown", "Cursor", "FileCache", "Formatting", "GetChoice",
    "GetConfirmation", "GetUser", "Listener", "Menu", "Menus", "ModView",
    "OperationLock", "PersistentTasks", "Stack", "TempConvo", "TempDownload",
    "TemporaryList", "Utils", "abcs", "bytes2human", "chain", "cln",
    "colors", "cooldowns", "download", "emojis", "extract_time", "file_exts",
    "find_link", "find_links", "findall", "format_date", "formulas",
    "get_images", "get_prefix", "get_prefixes_async", "get_role", "get_seconds",
    "get_time", "get_user_rewrite", "notable_permissions", "operators", "pillow",
    "s", "sanitize", "save_json", "search", "split", "total_seconds",
    "update_msg", "url_from",
)


class Utils(Cog):
    """Represents the bot.utils attribute for utils requiring access to the running instance"""
    def __init__(self, bot):
        self.bot = bot
        self.attrs = Attributes(bot)

        OperationLock.bot = bot
        self.operation_lock = OperationLock

        # Remove the bot arg
        self.get_user = partial(GetUser, bot)
        self.cache = partial(Cache, bot)
        self.open = partial(AsyncFileManager, bot)
        self.save_json = partial(save_json, bot)

        # Menus
        ui = Menus(bot)
        self.verify_user = ui.verify_user
        self.get_choice = ui.get_choice
        self.configure = ui.configure

        # Listeners
        listener = Listener(bot)
        self.get_message = listener.get_message
        self.get_reaction = listener.get_reaction
        self.get_role = get_role

        # Formatting
        formatting = Formatting(bot)
        self.format_dict = formatting.format_dict
        self.add_field = formatting.add_field
        self.dump_json = formatting.dump_json

    def cursor(self, *args, **kwargs) -> Cursor:
        return Cursor(self.bot, *args, **kwargs)

    def persistent_tasks(self, database: str, callback: Callable, identifier: str, debug: bool = False):
        return PersistentTasks(self.bot, database, callback, identifier, debug)
