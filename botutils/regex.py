"""
botutils.regex
~~~~~~~~~~~~~~~

Async friendly regular expression coroutine functions

Functions:
    search : Returns a single match for a pattern
    findall : Returns a iterable results object
    sanitize : Sanitizes a string of pings, and urls
    find_links : Finds all the urls in a string
    find_link : Finds the first url in a string

:copyright: (C) 2021-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import re
from functools import partial
from typing import Callable, List, Optional

from discord.ext.commands import Context

url_expression = r"[a-zA-Z0-9]+\.[a-zA-Z]{2,16}[a-zA-Z0-9./_?\-]*"
ping_expression = r"(@(?:everyone|here))|(<@!?&?[0-9]+>)"


async def _run_in_executor(func: Callable):
    """ Runs a function in the bots thread pool to avoid blocking the loop """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func)


async def search(pattern: str, string: str) -> Optional[str]:
    """ Returns a single match for a pattern """
    result = await _run_in_executor(partial(re.search, pattern, string))
    return result.group() if result else None


async def findall(pattern: str, string: str) -> List[str]:
    """ Returns a iterable results object """
    def collect_matches():
        return [match.group() for match in re.finditer(pattern, string, re.S)]

    return await _run_in_executor(collect_matches)


async def sanitize(string: str, ctx: Context = None) -> str:
    """ Sanitizes a string of pings, and urls """
    fs = map(findall, [url_expression, ping_expression], [string] * 2)
    for future in asyncio.as_completed(fs):
        results = await future
        for result in results:
            string = string.replace(result, "🚫")
    if cog := ctx.bot.get_cog("ChatFilter") if ctx else None:
        clean_content, _flags = await cog.run_default_filter(ctx.guild.id, string)
        if clean_content:
            string = clean_content
    return string


async def find_links(string: str) -> List[str]:
    """ Finds all the urls in a string """
    return await findall(url_expression, string)


async def find_link(string: str) -> Optional[str]:
    """ Finds the first url in a string """
    return await search(url_expression, string)
