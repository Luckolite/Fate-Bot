"""
String-Formatting Tools
~~~~~~~~~~~~~~~~~~~~~~~~

A collection of helper functions

:copyright: (C) 2022-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

from typing import Union

from . import emojis


def chain(obj: Union[list, str] = None, skip_first=False) -> str:
    """ Chains multiple lines of information """
    if obj is None:
        return ""
    if isinstance(obj, str):
        obj = [obj]
    rows = [line for row in obj for line in str(row).split("\n")]
    result = []
    for i, row in enumerate(rows):
        if skip_first and i == 0:
            result.append(row)
        elif i+1 == len(rows):
            result.append(f"\n{emojis.reply} {row}")
        else:
            result.append(f"\n{emojis.creply} {row}")
    return "".join(result)
