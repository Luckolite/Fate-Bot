"""
Emojis Shortcut
~~~~~~~~~~~~~~~~

A module for quick access to (discord) emojis

Functions:
    arrow : an emoji that changes depending on the holiday

:copyright: (C) 2020-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

from datetime import datetime
from random import choice


# Presences
online = "<:status_online:659976003334045727>"
idle = "<:status_idle:659976006030983206>"
dnd = do_not_disturb = "<:status_dnd:659976008627388438>"
offline = invisible = "<:status_offline:659976011651219462>"

# Server Indicators
discord = "<:discordlogo:673955433131671552>"
members = "👥"
text_channel = "<:textchannel:679179620867899412>"
voice_channel = "<:voicechannel:679179727994617881>"
boost = "<:boost15:673955479675994122>"
booster = "<:boost12:673955482578190356>"
verified = "<:verified:673955386839269396>"
partner = "<:partner:673955399694548994>"
pin = "<:pin:926750162787917844>"

# Indicators
on = "<:toggle_on:673955968857407541>"
off = "<:toggle_off:673955971726311424>"
typing = "<a:typing:673955389431349249>"
loading = "<a:loading:673956001174781983>"
yes = "<a:yes:789735035813101638>"
soon = "<:soontm:739960152140152963>"
never = "<:never:739960284965502987>"
plus = "<:plus:548465119462424595>"
edited = "<:edited:550291696861315093>"
home = "🏡"
up = "⬆️"
down = "⬇️"
double_down = "⏬"
approve = "<:approve:673956036612194325>"
disapprove = "<:disapprove:673956034108194870>"
reply = "<:reply:884827060214841374>"
creply = "<:chain_reply:1017885121971499148>"

# Misc
youtube = "<:YouTube:498050040384978945>"
nice = "<a:nice:770080922380271657>"
empty = "<:blank:931435521035624448>"


def arrow():
    date = datetime.utcnow()
    if date.month == 1 and date.day == 26:  # Chinese New Year
        return "🐉"
    if date.month == 2 and date.day == 14:  # Valentines Day
        return "❤"
    if date.month == 6:  # Pride Month
        return "<a:arrow:679213991721173012>"
    if date.month == 7 and date.day == 4:  # July 4th
        return "🎆"
    if date.month == 10 and date.day == 31:  # Halloween
        return "🎃"
    if date.month == 11 and date.day == 26:  # Thanksgiving
        return "🦃"
    if date.month == 12 and date.day == 25:  # Christmas
        return "🎄"
    return "<:enter:673955417994559539>"

def random():
    options = "<a:vibe:817484146145493032> <:uwu:871124826209787994> <a:washchannel:919331864550985748> <:shod:834271806579671081> <:Sasuke_Hmmm:842916924723953694> <a:think_about_it:807490330710376509> <a:sansdance:871124822661402705> <a:pufferfish:871125794578112542> <:notlikethis:886789483469619220> <:juicesip:800826413883588678> <:jeff:808083271875035187> <a:jerryfeasting:871125816568864858> <a:gimmefood:871125822776422460> <a:cutedance:543470067929579530> <a:cowdance:807490351946268693> <a:chips:862236661965389844> <a:catSwell:871125827809595464> <a:catwiggle:871125825091686441> <:bingus:898713198465785887> <:catagree:921908542364086322> <a:catgetreal:921908639298621501> <a:adhd_girl:578096764951986178> <a:5926_Amongus_shy:818208561795563530>"
    return choice(options.split())
