"""A SQL-authoritative rewrite of Fate's factions game."""

import asyncio
import hashlib
import json
import random
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Mapping, Optional

import discord
from discord import Interaction, SelectOption, ui
from discord.ext import commands
from pymysql.err import IntegrityError, OperationalError

from botutils.colors import pink, purple

FORAGE_ENERGY_REGEN_SECONDS = 10 * 60
FORAGE_LEVEL_XP = (
    0, 60, 180, 400, 750, 1_250, 1_900, 2_800, 4_000, 5_500,
    7_500, 9_800, 12_500, 15_750, 19_500,
)
FORAGE_ITEMS = {
    "plant_fiber": {"name": "Plant fiber", "emoji": "🌾", "value": 2},
    "wild_herbs": {"name": "Wild herbs", "emoji": "🌿", "value": 3},
    "berries": {"name": "Berries", "emoji": "🫐", "value": 4},
    "wood": {"name": "Timber", "emoji": "🪵", "value": 5},
    "stone": {"name": "River stone", "emoji": "🪨", "value": 5},
    "mushrooms": {"name": "Mooncap", "emoji": "🍄", "value": 7},
    "clay": {"name": "Red clay", "emoji": "🧱", "value": 7},
    "iron_ore": {"name": "Iron ore", "emoji": "⛏️", "value": 10},
    "amber": {"name": "Amber", "emoji": "🔶", "value": 14},
    "river_pearl": {"name": "River pearl", "emoji": "🦪", "value": 18},
    "ancient_coin": {"name": "Ancient coin", "emoji": "🪙", "value": 30},
    "lost_relic": {"name": "Lost relic", "emoji": "🏺", "value": 45},
    "starmetal": {"name": "Starmetal", "emoji": "☄️", "value": 75},
}
FORAGE_LOCATIONS = {
    "meadow": {
        "name": "Sunmeadow", "emoji": "🌾", "level": 1, "energy": 1,
        "rolls": 2,
        "description": "Reliable fibers, herbs, berries, and loose stone.",
        "loot": (
            ("plant_fiber", 32), ("wild_herbs", 28), ("berries", 24),
            ("stone", 11), ("mushrooms", 4), ("amber", 1),
        ),
    },
    "woodland": {
        "name": "Whisperwood", "emoji": "🌲", "level": 2, "energy": 1,
        "rolls": 2,
        "description": "Timber and mooncaps with amber beneath old roots.",
        "loot": (
            ("wood", 35), ("mushrooms", 25), ("plant_fiber", 18),
            ("wild_herbs", 12), ("amber", 8), ("ancient_coin", 2),
        ),
    },
    "riverbank": {
        "name": "Silverrun Bank", "emoji": "🌊", "level": 5, "energy": 2,
        "rolls": 4,
        "description": "Clay, ore, and pearls washed down from the highlands.",
        "loot": (
            ("stone", 27), ("clay", 25), ("iron_ore", 22),
            ("plant_fiber", 12), ("river_pearl", 10), ("ancient_coin", 4),
        ),
    },
    "ruins": {
        "name": "Old Kingdom Ruins", "emoji": "🏛️", "level": 9,
        "energy": 3, "rolls": 5,
        "description": "Dangerous ground holding coins, amber, and forgotten relics.",
        "loot": (
            ("stone", 23), ("iron_ore", 25), ("clay", 15),
            ("amber", 16), ("ancient_coin", 13), ("lost_relic", 7),
            ("starmetal", 1),
        ),
    },
    "hollow": {
        "name": "Starfall Hollow", "emoji": "🌌", "level": 13,
        "energy": 3, "rolls": 6,
        "description": "An endgame trail where relics and starmetal still surface.",
        "loot": (
            ("mushrooms", 20), ("iron_ore", 22), ("amber", 20),
            ("river_pearl", 14), ("ancient_coin", 13), ("lost_relic", 9),
            ("starmetal", 2),
        ),
    },
}
FORAGE_TOOLS = (
    {
        "name": "Old pouch", "emoji": "👜", "capacity": 24, "energy": 10,
        "bonus": 0.00, "level": 1,
    },
    {
        "name": "Woven satchel", "emoji": "🎒", "capacity": 40, "energy": 12,
        "bonus": 0.10, "level": 3,
        "recipe": {"plant_fiber": 12, "wood": 6},
    },
    {
        "name": "Field kit", "emoji": "🧰", "capacity": 60, "energy": 14,
        "bonus": 0.20, "level": 7,
        "recipe": {"wood": 16, "iron_ore": 8, "wild_herbs": 8},
    },
    {
        "name": "Survey pack", "emoji": "🗺️", "capacity": 90, "energy": 16,
        "bonus": 0.30, "level": 12,
        "recipe": {"iron_ore": 18, "amber": 6, "ancient_coin": 2},
    },
)
FORAGE_MARKET_ITEMS = (
    "plant_fiber", "wild_herbs", "berries", "wood", "stone", "mushrooms", "amber",
)
FORAGE_ITEM_CLUES = {
    "plant_fiber": "Long pale strands peel apart cleanly and are strong enough to weave.",
    "wild_herbs": "The crushed leaves smell sharp and medicinal rather than sweet.",
    "berries": "Small blue fruit grows in tight clusters beneath low leaves.",
    "wood": "A dry fallen limb is straight, solid, and free of rot.",
    "stone": "A smooth dense pebble has been rounded by years of running water.",
    "mushrooms": "A pale cap glows faintly beneath the shade of an old log.",
    "clay": "Heavy red earth holds its shape when pressed between your fingers.",
    "iron_ore": "A dark, unusually heavy rock leaves a rusty streak when scratched.",
    "amber": "A warm golden fragment catches light with something trapped inside.",
    "river_pearl": "A small iridescent sphere is hidden inside a freshwater shell.",
    "ancient_coin": "A round metal disk carries a worn crest and an unreadable date.",
    "lost_relic": "A decorated ceramic fragment fits no ordinary modern container.",
    "starmetal": "A black metallic shard is cold, glassy, and faintly magnetic.",
}
FORAGE_TRAIL_QUESTIONS = (
    {
        "title": "Read the weather",
        "prompt": "Rain is about to break and you need dry tinder. Where do you search?",
        "choices": (
            ("shelter", "Beneath a rock overhang", "🪨"),
            ("river", "Along the waterline", "🌊"),
            ("field", "In exposed grass", "🌾"),
        ),
        "correct": "shelter",
    },
    {
        "title": "Follow the signs",
        "prompt": "A trail splits. One branch has freshly bent grass and wet soil. Which do you follow?",
        "choices": (
            ("fresh", "Freshly bent grass", "🍃"),
            ("dust", "Undisturbed dust", "💨"),
            ("old", "Faded old prints", "🐾"),
        ),
        "correct": "fresh",
    },
    {
        "title": "Choose your ground",
        "prompt": "You are looking for clay after a storm. Which ground is most promising?",
        "choices": (
            ("bank", "A damp cut riverbank", "🧱"),
            ("ridge", "A dry rocky ridge", "⛰️"),
            ("roots", "Loose soil under roots", "🌲"),
        ),
        "correct": "bank",
    },
    {
        "title": "Protect the find",
        "prompt": "You uncover a fragile old ceramic piece. What is the safest way to pack it?",
        "choices": (
            ("wrap", "Wrap it in soft fiber", "🌾"),
            ("pocket", "Drop it into a full pocket", "👜"),
            ("wash", "Scrub it clean immediately", "🫧"),
        ),
        "correct": "wrap",
    },
)


def forage_level(xp: int) -> int:
    """Return the bounded foraging level for a persisted XP total."""
    xp = max(0, int(xp))
    level = 1
    for candidate, required in enumerate(FORAGE_LEVEL_XP[1:], start=2):
        if xp < required:
            break
        level = candidate
    return min(level, len(FORAGE_LEVEL_XP))


def forage_inventory_size(inventory: Mapping[str, int]) -> int:
    return sum(max(0, int(amount)) for amount in inventory.values())


def refresh_forage_energy(player: dict, now: Optional[int] = None) -> int:
    """Regenerate energy without allowing offline time to exceed the tool cap."""
    now = int(time() if now is None else now)
    tool_level = min(max(0, int(player.get("tool", 0))), len(FORAGE_TOOLS) - 1)
    cap = FORAGE_TOOLS[tool_level]["energy"]
    energy = min(cap, max(0, int(player.get("energy", cap))))
    updated_at = min(now, max(0, int(player.get("energy_updated_at", now))))
    if energy >= cap:
        player["energy"] = cap
        player["energy_updated_at"] = now
        return cap
    gained = max(0, now - updated_at) // FORAGE_ENERGY_REGEN_SECONDS
    if gained:
        energy = min(cap, energy + gained)
        updated_at += gained * FORAGE_ENERGY_REGEN_SECONDS
    if energy >= cap:
        updated_at = now
    player["energy"] = energy
    player["energy_updated_at"] = updated_at
    return energy


def forage_market_item(now: Optional[int] = None) -> str:
    """Select the same deterministic daily request in every server."""
    day = int(time() if now is None else now) // 86_400
    digest = hashlib.sha256(f"fate:global-forage:{day}".encode()).digest()
    return FORAGE_MARKET_ITEMS[int.from_bytes(digest[:4], "big") % len(FORAGE_MARKET_ITEMS)]


def forage_sale_value(inventory: Mapping[str, int], demand_item: Optional[str] = None) -> int:
    total = 0
    for item, raw_amount in inventory.items():
        if item not in FORAGE_ITEMS:
            continue
        amount = max(0, int(raw_amount))
        value = FORAGE_ITEMS[item]["value"] * amount
        total += value + (value // 2 if item == demand_item else 0)
    return total


def create_forage_encounter(
    location_key: str,
    *,
    now: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> dict:
    """Create a small JSON-safe game that can resume from any server."""
    if location_key not in FORAGE_LOCATIONS:
        raise ValueError("Unknown forage location")
    now = int(time() if now is None else now)
    rng = rng or random
    kind = rng.choice(("memory", "identify", "trail"))
    encounter = {
        "id": secrets.token_hex(8),
        "kind": kind,
        "location": location_key,
        "started_at": now,
    }
    if kind == "memory":
        available = [item for item, _weight in FORAGE_LOCATIONS[location_key]["loot"]]
        sequence = rng.sample(available, 4)
        sequences = [
            sequence, list(reversed(sequence)),
            [sequence[1], sequence[0], *sequence[2:]],
        ]
        rng.shuffle(sequences)
        choices = []
        correct = None
        for index, option in enumerate(sequences):
            value = f"memory_{index}"
            choices.append({
                "value": value,
                "label": "  ".join(FORAGE_ITEMS[item]["emoji"] for item in option),
            })
            if option == sequence:
                correct = value
        encounter.update({
            "title": "Remember the trail",
            "prompt": "Memorize this order. It disappears when you select **I'm ready**.",
            "preview": "  ".join(FORAGE_ITEMS[item]["emoji"] for item in sequence),
            "phase": "preview",
            "choices": choices,
            "correct": correct,
        })
    elif kind == "identify":
        available = [
            item for item, _weight in FORAGE_LOCATIONS[location_key]["loot"]
            if item in FORAGE_ITEM_CLUES
        ]
        correct_item = rng.choice(available)
        options = [
            correct_item,
            *rng.sample([item for item in FORAGE_ITEM_CLUES if item != correct_item], 2),
        ]
        rng.shuffle(options)
        encounter.update({
            "title": "Identify the find",
            "prompt": FORAGE_ITEM_CLUES[correct_item],
            "phase": "answer",
            "choices": [
                {
                    "value": item,
                    "label": FORAGE_ITEMS[item]["name"],
                    "emoji": FORAGE_ITEMS[item]["emoji"],
                }
                for item in options
            ],
            "correct": correct_item,
        })
    else:
        question = rng.choice(FORAGE_TRAIL_QUESTIONS)
        options = list(question["choices"])
        rng.shuffle(options)
        encounter.update({
            "title": question["title"],
            "prompt": question["prompt"],
            "phase": "answer",
            "choices": [
                {"value": value, "label": label, "emoji": emoji}
                for value, label, emoji in options
            ],
            "correct": question["correct"],
        })
    return encounter


def roll_forage_loot(
    location_key: str,
    tool_level: int,
    available_space: int,
    rng: Optional[random.Random] = None,
    *,
    bonus_rolls: int = 0,
    rolls_override: Optional[int] = None,
) -> dict[str, int]:
    """Roll a bounded expedition haul that can never overflow the pack."""
    if location_key not in FORAGE_LOCATIONS or available_space <= 0:
        return {}
    rng = rng or random
    location = FORAGE_LOCATIONS[location_key]
    tool_level = min(max(0, int(tool_level)), len(FORAGE_TOOLS) - 1)
    rolls = (
        int(location["rolls"])
        if rolls_override is None else max(1, int(rolls_override))
    )
    if rolls_override is None and rng.random() < FORAGE_TOOLS[tool_level]["bonus"]:
        rolls += 1
    rolls += max(0, int(bonus_rolls))
    rolls = min(rolls, int(available_space))
    population = [item for item, _weight in location["loot"]]
    weights = [weight for _item, weight in location["loot"]]
    loot: dict[str, int] = {}
    for item in rng.choices(population, weights=weights, k=rolls):
        loot[item] = loot.get(item, 0) + 1
    return loot

SCHEMA = {
    "factions": """
    CREATE TABLE IF NOT EXISTS factions (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        guild_id BIGINT UNSIGNED NOT NULL,
        name VARCHAR(25) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
        owner_id BIGINT UNSIGNED NOT NULL,
        balance BIGINT NOT NULL DEFAULT 0,
        slots SMALLINT UNSIGNED NOT NULL DEFAULT 15,
        is_public TINYINT UNSIGNED NOT NULL DEFAULT 1,
        bio VARCHAR(1024) NOT NULL DEFAULT '',
        icon TEXT NULL,
        banner TEXT NULL,
        income_multiplier DECIMAL(4,2) NOT NULL DEFAULT 1.00,
        compounding TINYINT UNSIGNED NOT NULL DEFAULT 0,
        work_income DECIMAL(30,0) NOT NULL DEFAULT 0,
        land_income DECIMAL(30,0) NOT NULL DEFAULT 0,
        alliance_income DECIMAL(30,0) NOT NULL DEFAULT 0,
        game_income DECIMAL(30,0) NOT NULL DEFAULT 0,
        transfer_income DECIMAL(30,0) NOT NULL DEFAULT 0,
        industry_income DECIMAL(30,0) NOT NULL DEFAULT 0,
        daily_income DECIMAL(30,0) NOT NULL DEFAULT 0,
        trade_income DECIMAL(30,0) NOT NULL DEFAULT 0,
        forage_income DECIMAL(30,0) NOT NULL DEFAULT 0,
        industry_level TINYINT UNSIGNED NOT NULL DEFAULT 0,
        industry_updated_at BIGINT UNSIGNED NOT NULL DEFAULT 0,
        trade_available_at BIGINT UNSIGNED NOT NULL DEFAULT 0,
        extra_income_until BIGINT UNSIGNED NOT NULL DEFAULT 0,
        land_guard_until BIGINT UNSIGNED NOT NULL DEFAULT 0,
        anti_raid_until BIGINT UNSIGNED NOT NULL DEFAULT 0,
        time_chamber_until BIGINT UNSIGNED NOT NULL DEFAULT 0,
        created_at BIGINT UNSIGNED NOT NULL,
        updated_at BIGINT UNSIGNED NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY faction_name_per_guild (guild_id, name),
        KEY faction_owner_lookup (guild_id, owner_id),
        KEY faction_balance_board (guild_id, balance)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    "faction_members": """
    CREATE TABLE IF NOT EXISTS faction_members (
        guild_id BIGINT UNSIGNED NOT NULL,
        user_id BIGINT UNSIGNED NOT NULL,
        faction_id BIGINT UNSIGNED NOT NULL,
        role_name VARCHAR(12) NOT NULL DEFAULT 'member',
        contribution DECIMAL(30,0) NOT NULL DEFAULT 0,
        work_notifications TINYINT UNSIGNED NOT NULL DEFAULT 0,
        daily_claimed_at BIGINT UNSIGNED NOT NULL DEFAULT 0,
        joined_at BIGINT UNSIGNED NOT NULL,
        PRIMARY KEY (guild_id, user_id),
        KEY faction_roster (faction_id, role_name, contribution),
        CONSTRAINT faction_members_parent FOREIGN KEY (faction_id)
            REFERENCES factions (id) ON DELETE CASCADE
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    "faction_claims": """
    CREATE TABLE IF NOT EXISTS faction_claims (
        guild_id BIGINT UNSIGNED NOT NULL,
        channel_id BIGINT UNSIGNED NOT NULL,
        faction_id BIGINT UNSIGNED NOT NULL,
        claimed_at BIGINT UNSIGNED NOT NULL,
        PRIMARY KEY (guild_id, channel_id),
        KEY faction_claim_owner (faction_id),
        CONSTRAINT faction_claims_parent FOREIGN KEY (faction_id)
            REFERENCES factions (id) ON DELETE CASCADE
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    "faction_alliances": """
    CREATE TABLE IF NOT EXISTS faction_alliances (
        faction_low_id BIGINT UNSIGNED NOT NULL,
        faction_high_id BIGINT UNSIGNED NOT NULL,
        created_at BIGINT UNSIGNED NOT NULL,
        PRIMARY KEY (faction_low_id, faction_high_id),
        KEY faction_alliance_reverse (faction_high_id, faction_low_id),
        CONSTRAINT faction_alliance_low FOREIGN KEY (faction_low_id)
            REFERENCES factions (id) ON DELETE CASCADE,
        CONSTRAINT faction_alliance_high FOREIGN KEY (faction_high_id)
            REFERENCES factions (id) ON DELETE CASCADE
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    "faction_blackjack_games": """
    CREATE TABLE IF NOT EXISTS faction_blackjack_games (
        faction_id BIGINT UNSIGNED NOT NULL,
        guild_id BIGINT UNSIGNED NOT NULL,
        user_id BIGINT UNSIGNED NOT NULL,
        wager BIGINT UNSIGNED NOT NULL,
        started_at BIGINT UNSIGNED NOT NULL,
        PRIMARY KEY (guild_id, user_id),
        KEY faction_blackjack_parent (faction_id),
        CONSTRAINT faction_blackjack_games_parent FOREIGN KEY (faction_id)
            REFERENCES factions (id) ON DELETE CASCADE
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    "faction_foragers": """
    CREATE TABLE IF NOT EXISTS faction_foragers (
        user_id BIGINT UNSIGNED NOT NULL,
        xp BIGINT UNSIGNED NOT NULL DEFAULT 0,
        tool_level TINYINT UNSIGNED NOT NULL DEFAULT 0,
        energy SMALLINT UNSIGNED NOT NULL DEFAULT 10,
        energy_updated_at BIGINT UNSIGNED NOT NULL,
        selected_location VARCHAR(32) NOT NULL DEFAULT 'meadow',
        searches BIGINT UNSIGNED NOT NULL DEFAULT 0,
        lifetime_earned BIGINT UNSIGNED NOT NULL DEFAULT 0,
        games BIGINT UNSIGNED NOT NULL DEFAULT 0,
        correct_answers BIGINT UNSIGNED NOT NULL DEFAULT 0,
        best_find VARCHAR(32) NULL,
        active_encounter LONGTEXT NULL,
        created_at BIGINT UNSIGNED NOT NULL,
        updated_at BIGINT UNSIGNED NOT NULL,
        PRIMARY KEY (user_id)
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    "faction_forager_inventory": """
    CREATE TABLE IF NOT EXISTS faction_forager_inventory (
        user_id BIGINT UNSIGNED NOT NULL,
        item_key VARCHAR(32) NOT NULL,
        quantity BIGINT UNSIGNED NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, item_key),
        CONSTRAINT faction_forager_inventory_parent FOREIGN KEY (user_id)
            REFERENCES faction_foragers (user_id) ON DELETE CASCADE
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
    "faction_forager_discoveries": """
    CREATE TABLE IF NOT EXISTS faction_forager_discoveries (
        user_id BIGINT UNSIGNED NOT NULL,
        item_key VARCHAR(32) NOT NULL,
        discovered_at BIGINT UNSIGNED NOT NULL,
        PRIMARY KEY (user_id, item_key),
        CONSTRAINT faction_forager_discoveries_parent FOREIGN KEY (user_id)
            REFERENCES faction_foragers (user_id) ON DELETE CASCADE
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    """,
}

SCHEMA_UPGRADES = {
    "factions": {
        "industry_income": "industry_income DECIMAL(30,0) NOT NULL DEFAULT 0",
        "daily_income": "daily_income DECIMAL(30,0) NOT NULL DEFAULT 0",
        "trade_income": "trade_income DECIMAL(30,0) NOT NULL DEFAULT 0",
        "forage_income": "forage_income DECIMAL(30,0) NOT NULL DEFAULT 0",
        "industry_level": "industry_level TINYINT UNSIGNED NOT NULL DEFAULT 0",
        "industry_updated_at": "industry_updated_at BIGINT UNSIGNED NOT NULL DEFAULT 0",
        "trade_available_at": "trade_available_at BIGINT UNSIGNED NOT NULL DEFAULT 0",
    },
    "faction_members": {
        "daily_claimed_at": "daily_claimed_at BIGINT UNSIGNED NOT NULL DEFAULT 0",
    },
}

DAILY_COOLDOWN_SECONDS = 20 * 60 * 60
TRADE_COOLDOWN_SECONDS = 6 * 60 * 60
INDUSTRY_OFFLINE_CAP_SECONDS = 7 * 24 * 60 * 60
BLACKJACK_STALE_SECONDS = 3 * 60
INDUSTRY_TIERS = (
    ("No industry", 0, 0),
    ("Farmstead", 500, 20),
    ("Workshop", 2_500, 60),
    ("Marketplace", 10_000, 180),
    ("Factory", 40_000, 500),
    ("Trade hub", 150_000, 1_500),
)
BLACKJACK_SUITS = ("♠", "♥", "♦", "♣")
BLACKJACK_RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")


def calculate_industry_accrual(level, updated_at, now, multiplier=1.0):
    """Return (payout, new timestamp, whole hours) for capped offline production."""
    if level <= 0 or level >= len(INDUSTRY_TIERS) or updated_at <= 0:
        return 0, updated_at, 0
    elapsed = max(0, int(now) - int(updated_at))
    capped_elapsed = min(elapsed, INDUSTRY_OFFLINE_CAP_SECONDS)
    hours = capped_elapsed // 3600
    if not hours:
        return 0, updated_at, 0
    hourly_rate = INDUSTRY_TIERS[level][2]
    payout = int(hourly_rate * hours * max(0.0, float(multiplier)))
    new_updated_at = int(now) if elapsed >= INDUSTRY_OFFLINE_CAP_SECONDS else int(updated_at) + hours * 3600
    return payout, new_updated_at, hours


def blackjack_deck():
    deck = [(rank, suit) for suit in BLACKJACK_SUITS for rank in BLACKJACK_RANKS]
    random.shuffle(deck)
    return deck


def blackjack_value(hand):
    value = 0
    aces = 0
    for rank, _suit in hand:
        if rank == "A":
            value += 11
            aces += 1
        elif rank in {"J", "Q", "K"}:
            value += 10
        else:
            value += int(rank)
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value


def format_blackjack_hand(hand):
    return " ".join(f"`{rank}{suit}`" for rank, suit in hand)


FACTION_COLUMNS = (
    "id, guild_id, name, owner_id, balance, slots, is_public, bio, icon, "
    "banner, income_multiplier, compounding, work_income, land_income, "
    "alliance_income, game_income, transfer_income, industry_income, "
    "daily_income, trade_income, forage_income, industry_level, industry_updated_at, "
    "trade_available_at, extra_income_until, land_guard_until, "
    "anti_raid_until, time_chamber_until"
)
FACTION_COLUMN_COUNT = len(FACTION_COLUMNS.split(","))
QUALIFIED_FACTION_COLUMNS = ", ".join(
    "f." + column.strip() for column in FACTION_COLUMNS.split(",")
)


@dataclass(slots=True)
class Faction:
    id: int
    guild_id: int
    name: str
    owner_id: int
    balance: int
    slots: int
    is_public: bool
    bio: str
    icon: Optional[str]
    banner: Optional[str]
    income_multiplier: float
    compounding: bool
    work_income: int
    land_income: int
    alliance_income: int
    game_income: int
    transfer_income: int
    industry_income: int
    daily_income: int
    trade_income: int
    forage_income: int
    industry_level: int
    industry_updated_at: int
    trade_available_at: int
    extra_income_until: int
    land_guard_until: int
    anti_raid_until: int
    time_chamber_until: int

    @classmethod
    def from_row(cls, row):
        values = list(row)
        for index in (
            0, 1, 3, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22,
            23, 24, 25, 26, 27,
        ):
            values[index] = int(values[index])
        values[6] = bool(values[6])
        values[10] = float(values[10])
        values[11] = bool(values[11])
        return cls(*values)


class FactionLookupError(commands.CheckFailure):
    pass


class FactionRepository:
    """Small query layer; MySQL remains the only source of faction state."""

    def __init__(self, bot):
        self.bot = bot

    async def initialize(self):
        await self.bot.wait_for_pool()
        async with self.bot.utils.cursor() as cursor:
            await cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name IN "
                "('factions', 'faction_members', 'faction_claims', "
                "'faction_alliances', 'faction_blackjack_games', "
                "'faction_foragers', 'faction_forager_inventory', "
                "'faction_forager_discoveries')"
            )
            existing = {str(row[0]) for row in await cursor.fetchall()}
            for table, statement in SCHEMA.items():
                if table not in existing:
                    await cursor.execute(statement)
            await cursor.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name IN "
                "('factions', 'faction_members')"
            )
            columns = {(str(table), str(column)) for table, column in await cursor.fetchall()}
            for table, upgrades in SCHEMA_UPGRADES.items():
                for column, definition in upgrades.items():
                    if (table, column) not in columns:
                        try:
                            await cursor.execute(
                                f"ALTER TABLE `{table}` ADD COLUMN {definition}"
                            )
                        except OperationalError as error:
                            if not error.args or error.args[0] != 1060:
                                raise

    @asynccontextmanager
    async def transaction(self):
        pool = await self.bot.wait_for_pool()
        async with pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor() as cursor:
                    yield cursor
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def one(self, sql, args=()):
        async with self.bot.utils.cursor() as cursor:
            await cursor.execute(sql, args)
            return await cursor.fetchone()

    async def all(self, sql, args=()):
        async with self.bot.utils.cursor() as cursor:
            await cursor.execute(sql, args)
            return list(await cursor.fetchall())

    async def execute(self, sql, args=()):
        async with self.bot.utils.cursor() as cursor:
            await cursor.execute(sql, args)
            return cursor.rowcount

    async def by_id(self, faction_id: int) -> Optional[Faction]:
        row = await self.one(
            f"SELECT {FACTION_COLUMNS} FROM factions WHERE id = %s", (faction_id,)
        )
        return Faction.from_row(row) if row else None

    async def for_user(self, guild_id: int, user_id: int):
        row = await self.one(
            f"SELECT {QUALIFIED_FACTION_COLUMNS}, m.role_name, m.contribution, "
            "m.work_notifications FROM factions f JOIN faction_members m "
            "ON m.faction_id = f.id WHERE m.guild_id = %s AND m.user_id = %s",
            (guild_id, user_id),
        )
        if not row:
            return None
        offset = FACTION_COLUMN_COUNT
        return (
            Faction.from_row(row[:offset]), str(row[offset]),
            int(row[offset + 1]), bool(row[offset + 2]),
        )

    async def find(self, guild_id: int, name: str) -> Optional[Faction]:
        exact = await self.all(
            f"SELECT {FACTION_COLUMNS} FROM factions "
            "WHERE guild_id = %s AND LOWER(name) = LOWER(%s) LIMIT 3",
            (guild_id, name),
        )
        if len(exact) > 1:
            matches = ", ".join(row[2] for row in exact)
            raise FactionLookupError(f"That matches multiple factions: {matches}")
        if exact:
            return Faction.from_row(exact[0])
        rows = await self.all(
            f"SELECT {FACTION_COLUMNS} FROM factions "
            "WHERE guild_id = %s AND LOWER(name) LIKE CONCAT('%%', LOWER(%s), '%%') "
            "ORDER BY name LIMIT 3",
            (guild_id, name),
        )
        if len(rows) > 1:
            matches = ", ".join(row[2] for row in rows)
            raise FactionLookupError(f"That matches multiple factions: {matches}")
        return Faction.from_row(rows[0]) if rows else None

    async def members(self, faction_id: int):
        return await self.all(
            "SELECT user_id, role_name, contribution, work_notifications "
            "FROM faction_members WHERE faction_id = %s "
            "ORDER BY FIELD(role_name, 'owner', 'co_owner', 'member'), "
            "contribution DESC",
            (faction_id,),
        )

    async def claims(self, faction_id: int):
        return await self.all(
            "SELECT channel_id, claimed_at FROM faction_claims "
            "WHERE faction_id = %s ORDER BY claimed_at", (faction_id,)
        )

    async def allies(self, faction_id: int):
        return [
            Faction.from_row(row)
            for row in await self.all(
                f"SELECT {QUALIFIED_FACTION_COLUMNS} "
                "FROM faction_alliances a JOIN factions f ON f.id = "
                "IF(a.faction_low_id = %s, a.faction_high_id, a.faction_low_id) "
                "WHERE a.faction_low_id = %s OR a.faction_high_id = %s "
                "ORDER BY f.name",
                (faction_id, faction_id, faction_id),
            )
        ]

    async def rankings(self, guild_id=None, limit=15):
        where = "WHERE f.guild_id = %s" if guild_id is not None else ""
        args = (guild_id, limit) if guild_id is not None else (limit,)
        return await self.all(
            "SELECT f.id, f.guild_id, f.name, f.balance, COUNT(c.channel_id), "
            "f.balance + COUNT(c.channel_id) * 500 AS net_worth "
            "FROM factions f LEFT JOIN faction_claims c ON c.faction_id = f.id "
            f"{where} GROUP BY f.id ORDER BY net_worth DESC LIMIT %s",
            args,
        )

    async def credit(self, faction_id: int, amount: int, source: str, user_id=None):
        columns = {
            "work": "work_income",
            "land": "land_income",
            "alliance": "alliance_income",
            "game": "game_income",
            "transfer": "transfer_income",
            "industry": "industry_income",
            "daily": "daily_income",
            "trade": "trade_income",
            "forage": "forage_income",
        }
        column = columns[source]
        async with self.transaction() as cursor:
            await cursor.execute(
                f"UPDATE factions SET balance = balance + %s, {column} = {column} + %s, "
                "updated_at = %s WHERE id = %s AND balance + %s >= 0",
                (amount, amount, int(time()), faction_id, amount),
            )
            if cursor.rowcount != 1:
                return False
            if user_id is not None and amount > 0:
                await cursor.execute(
                    "UPDATE faction_members SET contribution = contribution + %s "
                    "WHERE faction_id = %s AND user_id = %s",
                    (amount, faction_id, user_id),
                )
        return True

    @staticmethod
    def _forage_profile_from_row(row) -> dict:
        encounter = None
        if row[10]:
            with suppress(TypeError, ValueError, json.JSONDecodeError):
                raw_encounter = row[10]
                if isinstance(raw_encounter, bytes):
                    raw_encounter = raw_encounter.decode("utf-8")
                candidate = json.loads(raw_encounter)
                if (
                    isinstance(candidate, dict)
                    and candidate.get("kind") in {"memory", "identify", "trail"}
                    and candidate.get("location") in FORAGE_LOCATIONS
                    and candidate.get("phase") in {"preview", "answer"}
                    and isinstance(candidate.get("choices"), list)
                    and candidate.get("correct") is not None
                ):
                    encounter = candidate
        tool = min(max(0, int(row[1])), len(FORAGE_TOOLS) - 1)
        xp = max(0, int(row[0]))
        location = str(row[4])
        if (
            location not in FORAGE_LOCATIONS
            or FORAGE_LOCATIONS[location]["level"] > forage_level(xp)
        ):
            location = next(
                key for key, data in reversed(tuple(FORAGE_LOCATIONS.items()))
                if data["level"] <= forage_level(xp)
            )
        best_find = str(row[9]) if row[9] is not None else None
        return {
            "xp": xp,
            "tool": tool,
            "energy": max(0, int(row[2])),
            "energy_updated_at": max(0, int(row[3])),
            "location": location,
            "searches": max(0, int(row[5])),
            "earned": max(0, int(row[6])),
            "games": max(0, int(row[7])),
            "correct_answers": max(0, min(int(row[7]), int(row[8]))),
            "best_find": best_find if best_find in FORAGE_ITEMS else None,
            "encounter": encounter,
        }

    async def _forage_locked(self, cursor, user_id: int, now: int) -> dict:
        """Return one globally keyed profile while its rows are transaction-locked."""
        await cursor.execute(
            "INSERT IGNORE INTO faction_foragers "
            "(user_id, energy, energy_updated_at, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, FORAGE_TOOLS[0]["energy"], now, now, now),
        )
        await cursor.execute(
            "SELECT xp, tool_level, energy, energy_updated_at, selected_location, "
            "searches, lifetime_earned, games, correct_answers, best_find, "
            "active_encounter FROM faction_foragers WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        profile = self._forage_profile_from_row(await cursor.fetchone())
        old_energy = profile["energy"]
        old_energy_at = profile["energy_updated_at"]
        refresh_forage_energy(profile, now)
        await cursor.execute(
            "SELECT item_key, quantity FROM faction_forager_inventory "
            "WHERE user_id = %s FOR UPDATE",
            (user_id,),
        )
        profile["inventory"] = {
            str(item): max(0, int(quantity))
            for item, quantity in await cursor.fetchall()
            if str(item) in FORAGE_ITEMS and int(quantity) > 0
        }
        await cursor.execute(
            "SELECT item_key FROM faction_forager_discoveries WHERE user_id = %s",
            (user_id,),
        )
        profile["discoveries"] = [
            str(row[0]) for row in await cursor.fetchall()
            if str(row[0]) in FORAGE_ITEMS
        ]
        if (
            old_energy != profile["energy"]
            or old_energy_at != profile["energy_updated_at"]
        ):
            await cursor.execute(
                "UPDATE faction_foragers SET energy = %s, energy_updated_at = %s, "
                "updated_at = %s WHERE user_id = %s",
                (profile["energy"], profile["energy_updated_at"], now, user_id),
            )
        return profile

    async def forage_profile(self, user_id: int, now=None) -> dict:
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            return await self._forage_locked(cursor, user_id, now)

    async def set_forage_location(self, user_id: int, location: str, now=None) -> dict:
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            profile = await self._forage_locked(cursor, user_id, now)
            if location not in FORAGE_LOCATIONS:
                return {"status": "missing", "profile": profile}
            if FORAGE_LOCATIONS[location]["level"] > forage_level(profile["xp"]):
                return {"status": "locked", "profile": profile}
            await cursor.execute(
                "UPDATE faction_foragers SET selected_location = %s, updated_at = %s "
                "WHERE user_id = %s",
                (location, now, user_id),
            )
            profile["location"] = location
        return {"status": "selected", "profile": profile}

    async def start_forage(self, user_id: int, now=None, rng=None) -> dict:
        """Spend global energy and persist a mini-game before granting rewards."""
        now = int(time() if now is None else now)
        rng = rng or random
        async with self.transaction() as cursor:
            profile = await self._forage_locked(cursor, user_id, now)
            if profile["encounter"]:
                return {"status": "pending", "encounter": profile["encounter"]}
            location_key = profile["location"]
            location = FORAGE_LOCATIONS[location_key]
            if forage_level(profile["xp"]) < location["level"]:
                return {"status": "locked"}
            if profile["energy"] < location["energy"]:
                return {"status": "energy", "energy": profile["energy"]}
            tool = FORAGE_TOOLS[profile["tool"]]
            if forage_inventory_size(profile["inventory"]) >= tool["capacity"]:
                return {"status": "full"}
            if profile["energy"] >= tool["energy"]:
                profile["energy_updated_at"] = now
            profile["energy"] -= location["energy"]
            encounter = create_forage_encounter(location_key, now=now, rng=rng)
            await cursor.execute(
                "UPDATE faction_foragers SET energy = %s, energy_updated_at = %s, "
                "active_encounter = %s, updated_at = %s WHERE user_id = %s",
                (
                    profile["energy"], profile["energy_updated_at"],
                    json.dumps(encounter, separators=(",", ":")), now, user_id,
                ),
            )
        return {"status": "started", "encounter": encounter, "energy": profile["energy"]}

    async def advance_forage(self, user_id: int, now=None) -> dict:
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            profile = await self._forage_locked(cursor, user_id, now)
            encounter = profile["encounter"]
            if not encounter:
                return {"status": "missing"}
            if encounter["kind"] != "memory" or encounter["phase"] != "preview":
                return {"status": "answer", "encounter": encounter}
            encounter["phase"] = "answer"
            await cursor.execute(
                "UPDATE faction_foragers SET active_encounter = %s, updated_at = %s "
                "WHERE user_id = %s",
                (json.dumps(encounter, separators=(",", ":")), now, user_id),
            )
        return {"status": "answer", "encounter": encounter}

    async def abandon_forage(self, user_id: int, now=None) -> bool:
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            profile = await self._forage_locked(cursor, user_id, now)
            if not profile["encounter"]:
                return False
            await cursor.execute(
                "UPDATE faction_foragers SET active_encounter = NULL, updated_at = %s "
                "WHERE user_id = %s",
                (now, user_id),
            )
        return True

    async def resolve_forage(self, user_id: int, choice: str, now=None, rng=None) -> dict:
        """Resolve one mini-game and persist its loot, XP, discoveries, and stats."""
        now = int(time() if now is None else now)
        rng = rng or random
        async with self.transaction() as cursor:
            profile = await self._forage_locked(cursor, user_id, now)
            encounter = profile["encounter"]
            if not encounter:
                return {"status": "missing"}
            if encounter["phase"] != "answer":
                return {"status": "preview"}
            if choice not in {option.get("value") for option in encounter["choices"]}:
                return {"status": "invalid"}
            tool = FORAGE_TOOLS[profile["tool"]]
            available_space = tool["capacity"] - forage_inventory_size(profile["inventory"])
            if available_space <= 0:
                return {"status": "full"}
            location_key = encounter["location"]
            location = FORAGE_LOCATIONS[location_key]
            correct = choice == encounter["correct"]
            loot = roll_forage_loot(
                location_key,
                profile["tool"],
                available_space,
                rng=rng,
                bonus_rolls=1 if correct else 0,
                rolls_override=None if correct else max(1, location["rolls"] // 2),
            )
            xp_gain = (
                rng.randint(8, 12) if correct else rng.randint(4, 6)
            ) * location["energy"]
            level_before = forage_level(profile["xp"])
            new_xp = profile["xp"] + xp_gain
            best_find = profile["best_find"]
            if loot:
                best = max(loot, key=lambda item: FORAGE_ITEMS[item]["value"])
                if (
                    not best_find
                    or FORAGE_ITEMS[best]["value"] > FORAGE_ITEMS[best_find]["value"]
                ):
                    best_find = best
            for item, amount in loot.items():
                await cursor.execute(
                    "INSERT INTO faction_forager_inventory (user_id, item_key, quantity) "
                    "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE "
                    "quantity = quantity + VALUES(quantity)",
                    (user_id, item, amount),
                )
                await cursor.execute(
                    "INSERT IGNORE INTO faction_forager_discoveries "
                    "(user_id, item_key, discovered_at) VALUES (%s, %s, %s)",
                    (user_id, item, now),
                )
            await cursor.execute(
                "UPDATE faction_foragers SET xp = %s, searches = searches + 1, "
                "games = games + 1, correct_answers = correct_answers + %s, "
                "best_find = %s, active_encounter = NULL, updated_at = %s "
                "WHERE user_id = %s",
                (new_xp, int(correct), best_find, now, user_id),
            )
        level_after = forage_level(new_xp)
        return {
            "status": "resolved",
            "correct": correct,
            "title": encounter["title"],
            "loot": loot,
            "xp": xp_gain,
            "level": level_after,
            "leveled_up": level_after > level_before,
        }

    async def craft_forage_tool(self, user_id: int, now=None) -> dict:
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            profile = await self._forage_locked(cursor, user_id, now)
            current = profile["tool"]
            if current >= len(FORAGE_TOOLS) - 1:
                return {"status": "max", "tool": current}
            next_tool = FORAGE_TOOLS[current + 1]
            if forage_level(profile["xp"]) < next_tool["level"]:
                return {"status": "locked", "level": next_tool["level"]}
            missing = {
                item: amount - profile["inventory"].get(item, 0)
                for item, amount in next_tool["recipe"].items()
                if profile["inventory"].get(item, 0) < amount
            }
            if missing:
                return {"status": "missing", "missing": missing}
            for item, amount in next_tool["recipe"].items():
                await cursor.execute(
                    "UPDATE faction_forager_inventory SET quantity = quantity - %s "
                    "WHERE user_id = %s AND item_key = %s AND quantity >= %s",
                    (amount, user_id, item, amount),
                )
                await cursor.execute(
                    "DELETE FROM faction_forager_inventory "
                    "WHERE user_id = %s AND item_key = %s AND quantity = 0",
                    (user_id, item),
                )
            await cursor.execute(
                "UPDATE faction_foragers SET tool_level = %s, updated_at = %s "
                "WHERE user_id = %s",
                (current + 1, now, user_id),
            )
        return {"status": "crafted", "tool": current + 1}

    async def sell_forage_inventory(
        self, user_id: int, guild_id: int, faction_id: int, now=None,
    ) -> dict:
        """Atomically empty the global pack into the user's current local faction."""
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            profile = await self._forage_locked(cursor, user_id, now)
            if not profile["inventory"]:
                return {"status": "empty", "payout": 0}
            await cursor.execute(
                "SELECT f.name FROM factions f JOIN faction_members m "
                "ON m.faction_id = f.id WHERE f.id = %s AND f.guild_id = %s "
                "AND m.user_id = %s AND m.guild_id = %s FOR UPDATE",
                (faction_id, guild_id, user_id, guild_id),
            )
            faction_row = await cursor.fetchone()
            if not faction_row:
                return {"status": "membership"}
            demand = forage_market_item(now)
            base = forage_sale_value(profile["inventory"])
            payout = forage_sale_value(profile["inventory"], demand)
            await cursor.execute(
                "UPDATE factions SET balance = balance + %s, "
                "forage_income = forage_income + %s, updated_at = %s WHERE id = %s",
                (payout, payout, now, faction_id),
            )
            await cursor.execute(
                "UPDATE faction_members SET contribution = contribution + %s "
                "WHERE faction_id = %s AND user_id = %s",
                (payout, faction_id, user_id),
            )
            await cursor.execute(
                "DELETE FROM faction_forager_inventory WHERE user_id = %s",
                (user_id,),
            )
            await cursor.execute(
                "UPDATE faction_foragers SET lifetime_earned = lifetime_earned + %s, "
                "updated_at = %s WHERE user_id = %s",
                (payout, now, user_id),
            )
        return {
            "status": "sold",
            "faction": str(faction_row[0]),
            "payout": payout,
            "premium": payout - base,
            "demand": demand,
        }

    async def settle_industry(self, faction_id: int, now=None):
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            await cursor.execute(
                "SELECT industry_level, industry_updated_at, income_multiplier "
                "FROM factions WHERE id = %s FOR UPDATE", (faction_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return 0, 0
            payout, updated_at, hours = calculate_industry_accrual(
                int(row[0]), int(row[1]), now, float(row[2]),
            )
            if updated_at != int(row[1]):
                await cursor.execute(
                    "UPDATE factions SET balance = balance + %s, "
                    "industry_income = industry_income + %s, industry_updated_at = %s, "
                    "updated_at = %s WHERE id = %s",
                    (payout, payout, updated_at, now, faction_id),
                )
        return payout, hours

    async def claim_daily(self, faction_id: int, user_id: int, amount: int, now=None):
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            await cursor.execute(
                "SELECT 1 FROM factions WHERE id = %s FOR UPDATE", (faction_id,),
            )
            if not await cursor.fetchone():
                return False, 0
            await cursor.execute(
                "SELECT daily_claimed_at FROM faction_members "
                "WHERE faction_id = %s AND user_id = %s FOR UPDATE",
                (faction_id, user_id),
            )
            row = await cursor.fetchone()
            if not row:
                return False, 0
            available_at = int(row[0]) + DAILY_COOLDOWN_SECONDS
            if available_at > now:
                return False, available_at
            await cursor.execute(
                "UPDATE faction_members SET daily_claimed_at = %s, "
                "contribution = contribution + %s "
                "WHERE faction_id = %s AND user_id = %s",
                (now, amount, faction_id, user_id),
            )
            await cursor.execute(
                "UPDATE factions SET balance = balance + %s, "
                "daily_income = daily_income + %s, updated_at = %s WHERE id = %s",
                (amount, amount, now, faction_id),
            )
        return True, now + DAILY_COOLDOWN_SECONDS

    async def dispatch_trade(self, faction_id: int, user_id: int, now=None):
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            await cursor.execute(
                "SELECT trade_available_at, income_multiplier FROM factions "
                "WHERE id = %s FOR UPDATE", (faction_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return False, 0, 0, 0, 0
            available_at = int(row[0])
            if available_at > now:
                return False, available_at, 0, 0, 0
            await cursor.execute(
                "SELECT 1 FROM faction_members WHERE faction_id = %s AND user_id = %s "
                "FOR UPDATE", (faction_id, user_id),
            )
            if not await cursor.fetchone():
                return False, 0, 0, 0, 0
            await cursor.execute(
                "SELECT COUNT(*) FROM faction_claims WHERE faction_id = %s",
                (faction_id,),
            )
            claims = int((await cursor.fetchone())[0])
            await cursor.execute(
                "SELECT COUNT(*) FROM faction_alliances "
                "WHERE faction_low_id = %s OR faction_high_id = %s",
                (faction_id, faction_id),
            )
            allies = int((await cursor.fetchone())[0])
            payout = round(
                (random.randint(150, 260) + claims * 35 + allies * 60) * float(row[1])
            )
            available_at = now + TRADE_COOLDOWN_SECONDS
            await cursor.execute(
                "UPDATE factions SET balance = balance + %s, "
                "trade_income = trade_income + %s, trade_available_at = %s, "
                "updated_at = %s WHERE id = %s",
                (payout, payout, available_at, now, faction_id),
            )
            await cursor.execute(
                "UPDATE faction_members SET contribution = contribution + %s "
                "WHERE faction_id = %s AND user_id = %s",
                (payout, faction_id, user_id),
            )
        return True, available_at, payout, claims, allies

    async def upgrade_industry(self, faction_id: int, now=None):
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            await cursor.execute(
                "SELECT balance, industry_level FROM factions WHERE id = %s FOR UPDATE",
                (faction_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return "missing", 0, 0
            current_level = min(max(0, int(row[1])), len(INDUSTRY_TIERS) - 1)
            if current_level == len(INDUSTRY_TIERS) - 1:
                return "max", current_level, 0
            next_level = current_level + 1
            cost = INDUSTRY_TIERS[next_level][1]
            if int(row[0]) < cost:
                return "funds", next_level, cost
            await cursor.execute(
                "UPDATE factions SET balance = balance - %s, industry_level = %s, "
                "industry_updated_at = %s, updated_at = %s WHERE id = %s",
                (cost, next_level, now, now, faction_id),
            )
        return "upgraded", next_level, cost

    async def refund_stale_blackjack(self, guild_id: int, user_id: int, now=None):
        now = int(time() if now is None else now)
        row = await self.one(
            "SELECT faction_id, started_at FROM faction_blackjack_games "
            "WHERE guild_id = %s AND user_id = %s",
            (guild_id, user_id),
        )
        if not row or int(row[1]) + BLACKJACK_STALE_SECONDS > now:
            return False
        settled, _balance, _profit = await self.settle_blackjack(
            int(row[0]), guild_id, user_id, "push", now=now,
        )
        return settled

    async def start_blackjack(
        self, faction_id: int, guild_id: int, user_id: int, wager: int, now=None,
    ):
        now = int(time() if now is None else now)
        await self.refund_stale_blackjack(guild_id, user_id, now=now)
        async with self.transaction() as cursor:
            await cursor.execute(
                "SELECT balance FROM factions WHERE id = %s FOR UPDATE", (faction_id,),
            )
            faction_row = await cursor.fetchone()
            if not faction_row:
                return "missing", 0
            await cursor.execute(
                "SELECT 1 FROM faction_blackjack_games "
                "WHERE guild_id = %s AND user_id = %s FOR UPDATE",
                (guild_id, user_id),
            )
            if await cursor.fetchone():
                return "active", int(faction_row[0])
            await cursor.execute(
                "SELECT 1 FROM faction_members WHERE faction_id = %s AND user_id = %s "
                "FOR UPDATE", (faction_id, user_id),
            )
            if not await cursor.fetchone():
                return "missing", int(faction_row[0])
            balance = int(faction_row[0])
            if wager > balance:
                return "funds", balance
            await cursor.execute(
                "UPDATE factions SET balance = balance - %s, updated_at = %s "
                "WHERE id = %s", (wager, now, faction_id),
            )
            await cursor.execute(
                "INSERT INTO faction_blackjack_games "
                "(faction_id, guild_id, user_id, wager, started_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (faction_id, guild_id, user_id, wager, now),
            )
        return "started", balance - wager

    async def settle_blackjack(
        self, faction_id: int, guild_id: int, user_id: int, outcome: str, now=None,
    ):
        if outcome not in {"loss", "push", "win", "blackjack"}:
            raise ValueError("invalid blackjack outcome")
        now = int(time() if now is None else now)
        async with self.transaction() as cursor:
            await cursor.execute(
                "SELECT balance FROM factions WHERE id = %s FOR UPDATE", (faction_id,),
            )
            faction_row = await cursor.fetchone()
            if not faction_row:
                return False, 0, 0
            await cursor.execute(
                "SELECT wager FROM faction_blackjack_games "
                "WHERE faction_id = %s AND guild_id = %s AND user_id = %s FOR UPDATE",
                (faction_id, guild_id, user_id),
            )
            game_row = await cursor.fetchone()
            if not game_row:
                return False, int(faction_row[0]), 0
            wager = int(game_row[0])
            profits = {
                "loss": -wager,
                "push": 0,
                "win": wager,
                "blackjack": max(1, wager * 3 // 2),
            }
            profit = profits[outcome]
            returned = wager + profit
            await cursor.execute(
                "UPDATE factions SET balance = balance + %s, "
                "game_income = game_income + %s, updated_at = %s WHERE id = %s",
                (returned, profit, now, faction_id),
            )
            if profit > 0:
                await cursor.execute(
                    "UPDATE faction_members SET contribution = contribution + %s "
                    "WHERE faction_id = %s AND user_id = %s",
                    (profit, faction_id, user_id),
                )
            await cursor.execute(
                "DELETE FROM faction_blackjack_games "
                "WHERE faction_id = %s AND guild_id = %s AND user_id = %s",
                (faction_id, guild_id, user_id),
            )
        return True, int(faction_row[0]) + returned, profit


class _StartupMigrationContext:
    """Minimal context that routes the existing owner migration to the log."""

    prefix = "."

    def __init__(self, bot):
        self.bot = bot

    async def send(self, message):
        self.bot.log.info(str(message))


class FactionsRewrite(commands.Cog, name="Factions"):
    def __init__(self, bot):
        self.bot = bot
        self.repo = FactionRepository(bot)
        self.legacy_path = Path(bot.get_fp_for("userdata/test_factions.json"))
        self.icon = (
            "https://cdn.discordapp.com/attachments/641032731962114096/"
            "641742675808223242/13_Swords-512.png"
        )
        self.ready = asyncio.Event()
        self.initialization_error = None
        self.claim_counter = {}
        self.work_cooldowns = {}
        self.work_counter = {}
        self.blackjack_views = set()
        self.blocked = set()
        self.database_task = asyncio.create_task(self._initialize())
        self.factions_usage = self._help

    async def _initialize(self):
        try:
            await self.repo.initialize()
            migration = self.bot.config.get("factions", {})
            if (
                isinstance(migration, dict)
                and migration.get("migrate_legacy_on_start") is True
                and self.legacy_path.is_file()
            ):
                existing = await self.repo.one("SELECT COUNT(*) FROM factions")
                if not int(existing[0]):
                    await self.migrate.callback(
                        self, _StartupMigrationContext(self.bot), "confirm"
                    )
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self.initialization_error = error
            self.bot.log.critical(f"Couldn't initialize factions MySQL schema\n{error}")
        finally:
            self.ready.set()

    async def cog_unload(self):
        if not self.database_task.done():
            self.database_task.cancel()
        for view in tuple(self.blackjack_views):
            with suppress(Exception):
                await view.refund()

    async def cog_check(self, _ctx):
        await self.ready.wait()
        if self.initialization_error:
            raise commands.CheckFailure("The factions database is unavailable.")
        return True

    async def cog_command_error(self, ctx, error):
        error = getattr(error, "original", error)
        if isinstance(error, FactionLookupError):
            await ctx.send(str(error))
            return
        if cog := self.bot.get_cog("ErrorHandler"):
            await cog.suppress_key_error(ctx, error)
        else:
            raise error

    async def require_member(self, ctx):
        result = await self.repo.for_user(ctx.guild.id, ctx.author.id)
        if not result:
            raise commands.CheckFailure("You're not currently in a faction.")
        if (
            result[0].industry_level
            and result[0].industry_updated_at + 3600 <= int(time())
        ):
            payout, _hours = await self.repo.settle_industry(result[0].id)
            if payout:
                result = await self.repo.for_user(ctx.guild.id, ctx.author.id)
        return result

    async def require_manager(self, ctx, *, owner=False):
        faction, role, contribution, notifications = await self.require_member(ctx)
        allowed = {"owner"} if owner else {"owner", "co_owner"}
        if role not in allowed:
            raise commands.CheckFailure("You don't have permission to manage this faction.")
        return faction, role, contribution, notifications

    async def find_faction(self, guild_id, name):
        faction = await self.repo.find(guild_id, name)
        if not faction:
            raise FactionLookupError("I couldn't find that faction.")
        return faction

    def faction_icon(self, ctx, faction):
        if faction.icon:
            return faction.icon
        owner = ctx.guild.get_member(faction.owner_id) or self.bot.get_user(faction.owner_id)
        return str(owner.display_avatar) if owner else self.bot.user.display_avatar.url

    async def confirm(self, ctx, prompt):
        await ctx.send(prompt + " Reply with `yes` or `no`.")
        message = await self.bot.utils.get_message(ctx)
        return bool(message and "yes" in message.content.casefold())

    @commands.group(
        name="factions", aliases=["f"], invoke_without_command=True,
        description="Open the factions dashboard",
    )
    @commands.guild_only()
    @commands.bot_has_permissions(send_messages=True, embed_links=True)
    async def factions(self, ctx):
        result = await self.repo.for_user(ctx.guild.id, ctx.author.id)
        if result:
            view = FactionDashboard(self, ctx, result[0].id)
            return await ctx.send(embed=await view.embed("overview"), view=view)
        view = HelpView(ctx)
        await ctx.send(embed=view.embed("start"), view=view)

    @factions.command(name="help", aliases=["commands"])
    async def _help(self, ctx):
        view = HelpView(ctx)
        await ctx.send(embed=view.embed("start"), view=view)

    @commands.command(name="gib", hidden=True)
    @commands.is_owner()
    async def gib(self, ctx, guild_id: int, faction: str, amount: int):
        target = await self.find_faction(guild_id, faction)
        if not await self.repo.credit(target.id, amount, "transfer"):
            return await ctx.send("That would make the faction balance negative.")
        await ctx.send(f"Adjusted **{target.name}** by ${amount:,}.")

    @commands.command(name="taek", hidden=True)
    @commands.is_owner()
    async def taek(self, ctx, guild_id: int, faction: str, amount: int):
        await self.gib.callback(self, ctx, guild_id, faction, -abs(amount))

    @factions.command(name="create")
    async def create(self, ctx, *, name: str):
        name = name.strip()
        if not 3 <= len(name) <= 25:
            return await ctx.send("Faction names must be between 3 and 25 characters.")
        if any(char in name for char in "@#") or getattr(ctx.message, "raw_mentions", []):
            return await ctx.send("Faction names cannot contain mentions.")
        now = int(time())
        async with self.repo.transaction() as cursor:
            await cursor.execute(
                "SELECT 1 FROM faction_members WHERE guild_id = %s AND user_id = %s",
                (ctx.guild.id, ctx.author.id),
            )
            if await cursor.fetchone():
                return await ctx.send("Leave your current faction first.")
            await cursor.execute(
                "SELECT 1 FROM factions WHERE guild_id = %s AND LOWER(name) = LOWER(%s)",
                (ctx.guild.id, name),
            )
            if await cursor.fetchone():
                return await ctx.send("That name is already taken.")
            await cursor.execute(
                "INSERT INTO factions (guild_id, name, owner_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (ctx.guild.id, name, ctx.author.id, now, now),
            )
            faction_id = cursor.lastrowid
            await cursor.execute(
                "INSERT INTO faction_members "
                "(guild_id, user_id, faction_id, role_name, joined_at) "
                "VALUES (%s, %s, %s, 'owner', %s)",
                (ctx.guild.id, ctx.author.id, faction_id, now),
            )
        await ctx.send(f"Created **{name}**.")

    @factions.command(name="disband")
    async def disband(self, ctx):
        faction, *_ = await self.require_manager(ctx, owner=True)
        if not await self.confirm(ctx, f"Disband **{faction.name}** permanently?"):
            return await ctx.send("Disband cancelled.")
        await self.repo.execute("DELETE FROM factions WHERE id = %s", (faction.id,))
        await ctx.send(f"Disbanded **{faction.name}**.")

    @factions.command(name="transfer")
    async def transfer(self, ctx, user: discord.Member):
        faction, *_ = await self.require_manager(ctx, owner=True)
        target = await self.repo.for_user(ctx.guild.id, user.id)
        if target and target[0].id != faction.id:
            return await ctx.send("That user belongs to another faction.")
        if not await self.confirm(ctx, f"Transfer **{faction.name}** to {user.mention}?"):
            return await ctx.send("Transfer cancelled.")
        now = int(time())
        async with self.repo.transaction() as cursor:
            if not target:
                await cursor.execute(
                    "INSERT INTO faction_members "
                    "(guild_id, user_id, faction_id, role_name, joined_at) "
                    "VALUES (%s, %s, %s, 'owner', %s)",
                    (ctx.guild.id, user.id, faction.id, now),
                )
            else:
                await cursor.execute(
                    "UPDATE faction_members SET role_name = 'owner' "
                    "WHERE guild_id = %s AND user_id = %s",
                    (ctx.guild.id, user.id),
                )
            await cursor.execute(
                "UPDATE faction_members SET role_name = 'co_owner' "
                "WHERE guild_id = %s AND user_id = %s",
                (ctx.guild.id, ctx.author.id),
            )
            await cursor.execute(
                "UPDATE factions SET owner_id = %s, updated_at = %s WHERE id = %s",
                (user.id, now, faction.id),
            )
        await ctx.send(f"Transferred **{faction.name}** to {user.mention}.")

    @factions.command(name="join")
    async def join(self, ctx, *, faction: str):
        if await self.repo.for_user(ctx.guild.id, ctx.author.id):
            return await ctx.send("You're already in a faction.")
        target = await self.find_faction(ctx.guild.id, faction)
        if not target.is_public:
            return await ctx.send("That faction is invite-only.")
        if not await self._add_member(target, ctx.author.id):
            return await ctx.send("That faction is full or you joined another faction.")
        await ctx.send(f"Joined **{target.name}**.")

    async def _add_member(self, faction, user_id):
        async with self.repo.transaction() as cursor:
            await cursor.execute("SELECT slots FROM factions WHERE id = %s FOR UPDATE", (faction.id,))
            slots = int((await cursor.fetchone())[0])
            await cursor.execute("SELECT COUNT(*) FROM faction_members WHERE faction_id = %s", (faction.id,))
            if int((await cursor.fetchone())[0]) >= slots:
                return False
            await cursor.execute(
                "INSERT IGNORE INTO faction_members "
                "(guild_id, user_id, faction_id, joined_at) VALUES (%s, %s, %s, %s)",
                (faction.guild_id, user_id, faction.id, int(time())),
            )
            return cursor.rowcount == 1

    @factions.command(name="invite")
    async def invite(self, ctx, user: discord.Member):
        faction, *_ = await self.require_manager(ctx)
        if await self.repo.for_user(ctx.guild.id, user.id):
            return await ctx.send("That user is already in a faction.")
        await ctx.send(f"{user.mention}, reply with `.accept` to join **{faction.name}**.")

        def check(message):
            return message.channel == ctx.channel and message.author == user and ".accept" in message.content.casefold()

        try:
            await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            return await ctx.send("The invitation expired.")
        if not await self._add_member(faction, user.id):
            return await ctx.send("The faction filled up or the user joined another faction.")
        await ctx.send(f"{user.mention} joined **{faction.name}**.")

    @factions.command(name="leave")
    async def leave(self, ctx):
        faction, role, *_ = await self.require_member(ctx)
        if role == "owner":
            return await ctx.send("Transfer or disband the faction before leaving.")
        await self.repo.execute(
            "DELETE FROM faction_members WHERE guild_id = %s AND user_id = %s",
            (ctx.guild.id, ctx.author.id),
        )
        await ctx.send(f"Left **{faction.name}**.")

    @factions.command(name="kick")
    async def kick(self, ctx, user: discord.Member):
        faction, actor_role, *_ = await self.require_manager(ctx)
        target = await self.repo.for_user(ctx.guild.id, user.id)
        if not target or target[0].id != faction.id:
            return await ctx.send("That user is not in your faction.")
        if target[1] == "owner" or (target[1] == "co_owner" and actor_role != "owner"):
            return await ctx.send("You cannot kick that member.")
        await self.repo.execute(
            "DELETE FROM faction_members WHERE guild_id = %s AND user_id = %s",
            (ctx.guild.id, user.id),
        )
        await ctx.send(f"Removed {user.mention} from **{faction.name}**.")

    @factions.command(name="promote")
    async def promote(self, ctx, user: discord.Member):
        faction, *_ = await self.require_manager(ctx, owner=True)
        changed = await self.repo.execute(
            "UPDATE faction_members SET role_name = 'co_owner' "
            "WHERE faction_id = %s AND user_id = %s AND role_name = 'member'",
            (faction.id, user.id),
        )
        await ctx.send("Promoted that member." if changed else "That member cannot be promoted.")

    @factions.command(name="demote")
    async def demote(self, ctx, user: discord.Member):
        faction, *_ = await self.require_manager(ctx, owner=True)
        changed = await self.repo.execute(
            "UPDATE faction_members SET role_name = 'member' "
            "WHERE faction_id = %s AND user_id = %s AND role_name = 'co_owner'",
            (faction.id, user.id),
        )
        await ctx.send("Demoted that co-owner." if changed else "That user is not a co-owner.")

    @factions.command(name="privacy")
    async def privacy(self, ctx):
        faction, *_ = await self.require_manager(ctx)
        await self.repo.execute(
            "UPDATE factions SET is_public = NOT is_public, updated_at = %s WHERE id = %s",
            (int(time()), faction.id),
        )
        await ctx.send(f"**{faction.name}** is now {'invite-only' if faction.is_public else 'public'}.")

    @factions.command(name="set-bio", aliases=["setbio", "set_bio"])
    async def set_bio(self, ctx, *, bio: str):
        faction, *_ = await self.require_manager(ctx)
        if len(bio) > 1024:
            return await ctx.send("The biography cannot exceed 1,024 characters.")
        await self.repo.execute("UPDATE factions SET bio = %s WHERE id = %s", (bio, faction.id))
        await ctx.send("Updated the faction biography.")

    async def _set_image(self, ctx, column, url):
        faction, *_ = await self.require_manager(ctx)
        attachments = getattr(ctx.message, "attachments", [])
        if attachments:
            url = attachments[0].url
        if url and not url.startswith(("http://", "https://")):
            return await ctx.send("Use an HTTP(S) image URL or attachment.")
        await self.repo.execute(f"UPDATE factions SET {column} = %s WHERE id = %s", (url, faction.id))
        await ctx.send(f"{'Reset' if not url else 'Updated'} the faction {column}.")

    @factions.command(name="set-icon", aliases=["seticon", "set_icon"])
    async def set_icon(self, ctx, url: str = None):
        await self._set_image(ctx, "icon", url)

    @factions.command(name="set-banner", aliases=["setbanner", "set_banner"])
    async def set_banner(self, ctx, url: str = None):
        await self._set_image(ctx, "banner", url)

    @factions.command(name="rename")
    async def rename(self, ctx, *, name: str):
        faction, *_ = await self.require_manager(ctx, owner=True)
        name = name.strip()
        if not 3 <= len(name) <= 25:
            return await ctx.send("Faction names must be between 3 and 25 characters.")
        existing = await self.repo.find(ctx.guild.id, name)
        if existing and existing.id != faction.id:
            return await ctx.send("That name is already taken.")
        try:
            await self.repo.execute("UPDATE factions SET name = %s WHERE id = %s", (name, faction.id))
        except IntegrityError:
            return await ctx.send("That name is already taken.")
        await ctx.send(f"Renamed **{faction.name}** to **{name}**.")

    @factions.command(name="info")
    async def info(self, ctx, *, faction: str = None):
        target = await self.find_faction(ctx.guild.id, faction) if faction else (await self.require_member(ctx))[0]
        view = FactionDashboard(self, ctx, target.id)
        await ctx.send(embed=await view.embed("overview"), view=view)

    @factions.command(name="members")
    async def members(self, ctx, *, faction: str = None):
        target = await self.find_faction(ctx.guild.id, faction) if faction else (await self.require_member(ctx))[0]
        view = FactionDashboard(self, ctx, target.id)
        await ctx.send(embed=await view.embed("roster"), view=view)

    @factions.command(name="income")
    async def income(self, ctx):
        target = (await self.require_member(ctx))[0]
        view = FactionDashboard(self, ctx, target.id)
        await ctx.send(embed=await view.embed("economy"), view=view)

    @factions.command(name="daily")
    async def daily(self, ctx):
        faction, *_ = await self.require_member(ctx)
        payout = round(random.randint(100, 175) * faction.income_multiplier)
        claimed, available_at = await self.repo.claim_daily(
            faction.id, ctx.author.id, payout,
        )
        if not claimed:
            if not available_at:
                raise commands.CheckFailure("You're not currently in that faction.")
            return await ctx.send(f"Your treasury grant is available <t:{available_at}:R>.")
        await ctx.send(
            f"Daily treasury grant: **{faction.name}** received ${payout:,}. "
            f"Your next grant is available <t:{available_at}:R>."
        )

    @factions.command(name="trade")
    async def trade(self, ctx):
        faction, *_ = await self.require_member(ctx)
        dispatched, available_at, payout, claims, allies = await self.repo.dispatch_trade(
            faction.id, ctx.author.id,
        )
        if not dispatched:
            if not available_at:
                raise commands.CheckFailure("You're not currently in that faction.")
            return await ctx.send(f"Your faction's trade route returns <t:{available_at}:R>.")
        await ctx.send(
            f"Trade route returned **${payout:,}** for **{faction.name}** "
            f"({claims} claim{'s' if claims != 1 else ''}, "
            f"{allies} alliance{'s' if allies != 1 else ''}, "
            f"{faction.income_multiplier:.2f}× multiplier). "
            f"It can depart again <t:{available_at}:R>."
        )

    @factions.command(name="industry")
    async def industry(self, ctx):
        faction, *_ = await self.require_member(ctx)
        level = min(max(0, faction.industry_level), len(INDUSTRY_TIERS) - 1)
        name, _cost, hourly_rate = INDUSTRY_TIERS[level]
        effective_rate = round(hourly_rate * faction.income_multiplier)
        embed = discord.Embed(
            title=f"{faction.name} Industry",
            description=(
                f"**{name}** (tier {level}/{len(INDUSTRY_TIERS) - 1})\n"
                f"Produces **${effective_rate:,}/hour** automatically.\n"
                "Production is deposited whenever the faction is accessed and stores up to 7 days."
            ),
            color=purple,
        )
        if level < len(INDUSTRY_TIERS) - 1:
            next_name, cost, next_rate = INDUSTRY_TIERS[level + 1]
            embed.add_field(
                name="Next investment",
                value=(
                    f"**{next_name}** - ${cost:,}\n"
                    f"${round(next_rate * faction.income_multiplier):,}/hour with your multiplier\n"
                    f"Use `{ctx.prefix}f invest` to build it."
                ),
                inline=False,
            )
        else:
            embed.add_field(name="Next investment", value="Maximum industry tier reached.")
        await ctx.send(embed=embed)

    @factions.command(name="invest")
    async def invest(self, ctx):
        faction, *_ = await self.require_manager(ctx)
        result, level, cost = await self.repo.upgrade_industry(faction.id)
        if result == "max":
            return await ctx.send("Your faction already has the maximum industry tier.")
        if result == "funds":
            return await ctx.send(
                f"Your faction needs ${cost:,} to build **{INDUSTRY_TIERS[level][0]}**."
            )
        if result != "upgraded":
            return await ctx.send("That faction no longer exists.")
        name, _cost, hourly_rate = INDUSTRY_TIERS[level]
        await ctx.send(
            f"Built **{name}** for ${cost:,}. It now produces "
            f"${round(hourly_rate * faction.income_multiplier):,} per hour automatically."
        )

    @factions.command(name="collect", aliases=["harvest"])
    async def collect(self, ctx):
        result = await self.repo.for_user(ctx.guild.id, ctx.author.id)
        if not result:
            raise commands.CheckFailure("You're not currently in a faction.")
        faction = result[0]
        if faction.industry_level <= 0:
            return await ctx.send(
                f"Your faction has no industry yet. A manager can use `{ctx.prefix}f invest`."
            )
        payout, hours = await self.repo.settle_industry(faction.id)
        if not payout:
            return await ctx.send("Industry has not completed its next hourly production cycle yet.")
        level = min(max(0, faction.industry_level), len(INDUSTRY_TIERS) - 1)
        await ctx.send(
            f"Collected **${payout:,}** from {hours:,} hour{'s' if hours != 1 else ''} "
            f"of **{INDUSTRY_TIERS[level][0]}** production."
        )

    @factions.command(name="boosts")
    async def boosts(self, ctx, *, faction: str = None):
        target = await self.find_faction(ctx.guild.id, faction) if faction else (await self.require_member(ctx))[0]
        view = FactionDashboard(self, ctx, target.id)
        await ctx.send(embed=await view.embed("boosts"), view=view)

    @factions.command(name="claim")
    async def claim(self, ctx, channel: discord.TextChannel = None):
        faction, *_ = await self.require_manager(ctx)
        channel = channel or ctx.channel
        now = int(time())
        async with self.repo.transaction() as cursor:
            await cursor.execute(
                "SELECT faction_id FROM faction_claims WHERE guild_id = %s AND channel_id = %s FOR UPDATE",
                (ctx.guild.id, channel.id),
            )
            row = await cursor.fetchone()
            cost = 750 if row and int(row[0]) != faction.id else 500
            if row and int(row[0]) == faction.id:
                return await ctx.send("Your faction already owns that claim.")
            if row:
                await cursor.execute("SELECT land_guard_until FROM factions WHERE id = %s", (row[0],))
                if int((await cursor.fetchone())[0]) > now:
                    return await ctx.send("That claim is currently guarded.")
            await cursor.execute(
                "UPDATE factions SET balance = balance - %s WHERE id = %s AND balance >= %s",
                (cost, faction.id, cost),
            )
            if cursor.rowcount != 1:
                return await ctx.send(f"Your faction needs ${cost:,} to claim that channel.")
            await cursor.execute(
                "INSERT INTO faction_claims (guild_id, channel_id, faction_id, claimed_at) "
                "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
                "faction_id = VALUES(faction_id), claimed_at = VALUES(claimed_at)",
                (ctx.guild.id, channel.id, faction.id, now),
            )
        await ctx.send(f"Claimed {channel.mention} for **{faction.name}** (${cost:,}).")

    @factions.command(name="unclaim")
    async def unclaim(self, ctx, channel: discord.TextChannel = None):
        faction, *_ = await self.require_manager(ctx)
        channel = channel or ctx.channel
        async with self.repo.transaction() as cursor:
            await cursor.execute(
                "DELETE FROM faction_claims WHERE guild_id = %s AND channel_id = %s AND faction_id = %s",
                (ctx.guild.id, channel.id, faction.id),
            )
            if cursor.rowcount != 1:
                return await ctx.send("Your faction does not own that claim.")
            await cursor.execute("UPDATE factions SET balance = balance + 250 WHERE id = %s", (faction.id,))
        await ctx.send(f"Unclaimed {channel.mention} and returned $250.")

    @factions.command(name="claims")
    async def claims(self, ctx, *, faction: str = None):
        target = await self.find_faction(ctx.guild.id, faction) if faction else (await self.require_member(ctx))[0]
        view = FactionDashboard(self, ctx, target.id)
        await ctx.send(embed=await view.embed("territory"), view=view)

    @factions.command(name="pay")
    async def pay(self, ctx, faction: str, amount: int):
        source, *_ = await self.require_manager(ctx, owner=True)
        target = await self.find_faction(ctx.guild.id, faction)
        if target.id == source.id or amount <= 0 or amount > source.balance // 5:
            return await ctx.send("Payments must be positive and no more than one fifth of your balance.")
        async with self.repo.transaction() as cursor:
            await cursor.execute(
                "UPDATE factions SET balance = balance - %s, transfer_income = transfer_income - %s "
                "WHERE id = %s AND balance >= %s", (amount, amount, source.id, amount),
            )
            if cursor.rowcount != 1:
                return await ctx.send("Your faction cannot afford that payment.")
            await cursor.execute(
                "UPDATE factions SET balance = balance + %s, transfer_income = transfer_income + %s "
                "WHERE id = %s", (amount, amount, target.id),
            )
        await ctx.send(f"Paid **{target.name}** ${amount:,}.")

    @factions.command(name="balance", aliases=["bal"])
    async def balance(self, ctx, *, faction: str = None):
        target = await self.find_faction(ctx.guild.id, faction) if faction else (await self.require_member(ctx))[0]
        await ctx.send(embed=discord.Embed(title=target.name, description=f"${target.balance:,}", color=purple))

    @factions.command(name="work")
    async def work(self, ctx):
        faction, _role, _contribution, notifications = await self.require_member(ctx)
        key = (ctx.guild.id, ctx.author.id)
        remaining = round(self.work_cooldowns.get(key, 0) - time())
        if remaining > 0:
            return await ctx.send(f"You're on cooldown for another {remaining}s.")
        self.work_cooldowns[key] = time() + 60
        now = time()
        stacks = 1
        last_work = self.work_counter.get(key)
        if last_work and faction.time_chamber_until > now:
            stacks = min(3, max(1, int((now - last_work) // 60)))
        self.work_counter[key] = now
        base = sum(random.randint(15, 25) for _ in range(stacks))
        bonus = round(faction.balance * 0.00025) if faction.compounding else 0
        timed = 5 if faction.extra_income_until > time() else 0
        pay = round((base + bonus + timed) * faction.income_multiplier)
        await self.repo.credit(faction.id, pay, "work", ctx.author.id)
        embed = discord.Embed(
            description=f"You earned **{faction.name}** ${pay:,}.", color=purple
        )
        if bonus or timed or faction.income_multiplier > 1:
            embed.add_field(
                name="Bonuses",
                value=f"Compounding: ${bonus:,}\nTimed: ${timed:,}\n"
                f"Time chamber uses: {stacks}\nMultiplier: {faction.income_multiplier:.2f}×",
            )
        await ctx.send(embed=embed)
        await asyncio.sleep(max(0, self.work_cooldowns[key] - time()))
        self.work_cooldowns.pop(key, None)
        if notifications:
            message = await ctx.send(
                f"{ctx.author.mention}, your work cooldown is ready.", delete_after=5,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            self.bot.suppressed.append(message.id)

    @factions.command(name="toggle-notifs")
    async def toggle_notifs(self, ctx):
        faction, _role, _contribution, enabled = await self.require_member(ctx)
        await self.repo.execute(
            "UPDATE faction_members SET work_notifications = %s "
            "WHERE faction_id = %s AND user_id = %s",
            (int(not enabled), faction.id, ctx.author.id),
        )
        await ctx.send(f"{'Enabled' if not enabled else 'Disabled'} work notifications.")

    async def _small_earning(self, ctx, minimum, maximum, text):
        faction, *_ = await self.require_member(ctx)
        pay = random.randint(minimum, maximum)
        if faction.extra_income_until > time():
            pay += 2
        pay = round(pay * faction.income_multiplier)
        await self.repo.credit(faction.id, pay, "game", ctx.author.id)
        await ctx.send(text.format(faction=faction.name, amount=pay))

    @factions.command(name="forage", enabled=False)
    async def forage(self, ctx):
        faction, *_ = await self.require_member(ctx)
        menu = ForageMenu(self, ctx, faction)
        await menu.start()

    @factions.command(name="scrabble")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def scrabble(self, ctx):
        await self.require_member(ctx)
        word = random.choice(("fate", "water", "server", "toast", "scrabble", "saturn", "pizza", "empire"))
        middle = list(word[1:-1])
        random.shuffle(middle)
        scrambled = word[0] + "".join(middle) + word[-1]
        await ctx.send(f"Unscramble `{scrambled}` within 25 seconds.")

        def check(message):
            return message.channel == ctx.channel and message.author == ctx.author and message.content.casefold() == word

        try:
            await self.bot.wait_for("message", check=check, timeout=25)
        except asyncio.TimeoutError:
            return await ctx.send(f"Time ran out. The word was `{word}`.")
        await self._small_earning(ctx, 3, 7, "Correct — **{faction}** earned ${amount:,}.")

    @factions.command(name="coinflip", aliases=["flip"])
    @commands.cooldown(1, 150, commands.BucketType.user)
    async def coinflip(self, ctx, amount: int):
        faction, role, *_ = await self.require_member(ctx)
        if amount <= 0 or amount > faction.balance or (amount > 1000 and role not in {"owner", "co_owner"}):
            return await ctx.send("That wager is not allowed or the faction cannot afford it.")
        won = random.choice((True, False))
        change = amount if won else -amount
        await self.repo.credit(faction.id, change, "game", ctx.author.id)
        await ctx.send(f"🪙 **{ctx.author.display_name}** {'won' if won else 'lost'} ${amount:,}.")

    @factions.command(name="blackjack", aliases=["bj", "21"])
    async def blackjack(self, ctx, amount: int):
        faction, role, *_ = await self.require_member(ctx)
        if amount <= 0 or (amount > 1000 and role not in {"owner", "co_owner"}):
            return await ctx.send(
                "Use a positive wager; wagers above $1,000 require a faction manager."
            )
        status, balance = await self.repo.start_blackjack(
            faction.id, ctx.guild.id, ctx.author.id, amount,
        )
        if status == "active":
            return await ctx.send("Finish your current blackjack hand first.")
        if status == "funds":
            return await ctx.send(f"Your faction only has ${balance:,} available.")
        if status != "started":
            raise commands.CheckFailure("You're not currently in that faction.")

        deck = blackjack_deck()
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]
        view = BlackjackView(
            self, ctx, faction.id, amount, deck, player, dealer,
        )
        try:
            player_blackjack = blackjack_value(player) == 21
            dealer_blackjack = blackjack_value(dealer) == 21
            if player_blackjack or dealer_blackjack:
                outcome = (
                    "push" if player_blackjack and dealer_blackjack
                    else "blackjack" if player_blackjack else "loss"
                )
                embed = await view.finish(outcome)
                return await ctx.send(embed=embed)
            view.message = await ctx.send(embed=view.embed(), view=view)
            self.blackjack_views.add(view)
        except BaseException:
            with suppress(BaseException):
                await asyncio.shield(view.refund())
            raise

    @factions.command(name="vote")
    async def vote(self, ctx):
        if not self.bot.vote_url:
            return await ctx.send(
                "Vote rewards are only available on the primary Fate bot."
            )
        faction, *_ = await self.require_member(ctx)
        async with self.repo.transaction() as cursor:
            await cursor.execute(
                "SELECT vote_time FROM votes WHERE user_id = %s ORDER BY vote_time LIMIT 1 FOR UPDATE",
                (ctx.author.id,),
            )
            if not await cursor.fetchone():
                return await ctx.send(
                    f"Vote at {self.bot.vote_url}, then rerun this command."
                )
            await cursor.execute(
                "DELETE FROM votes WHERE user_id = %s ORDER BY vote_time LIMIT 1",
                (ctx.author.id,),
            )
            await cursor.execute(
                "UPDATE factions SET balance = balance + 250, game_income = game_income + 250 WHERE id = %s",
                (faction.id,),
            )
            await cursor.execute(
                "UPDATE faction_members SET contribution = contribution + 250 "
                "WHERE faction_id = %s AND user_id = %s", (faction.id, ctx.author.id),
            )
        await ctx.send("Redeemed $250 for your faction.")

    @factions.command(name="battle")
    async def battle(self, ctx, user: discord.Member, amount: int = 50):
        source, *_ = await self.require_member(ctx)
        target_result = await self.repo.for_user(ctx.guild.id, user.id)
        if not target_result or target_result[0].id == source.id or not 0 < amount <= 1000:
            return await ctx.send("Choose a member of another faction and wager $1–$1,000.")
        target = target_result[0]
        if source.balance < amount or target.balance < amount:
            return await ctx.send("Both factions must be able to cover the wager.")
        await ctx.send(f"{user.mention}, reply with `.confirm {amount}` to accept.")

        def check(message):
            return message.channel == ctx.channel and message.author == user and message.content.casefold() == f".confirm {amount}"

        try:
            await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            return await ctx.send("The battle offer expired.")
        winner, loser = random.choice(((source, target), (target, source)))
        async with self.repo.transaction() as cursor:
            await cursor.execute("UPDATE factions SET balance = balance - %s WHERE id = %s AND balance >= %s", (amount, loser.id, amount))
            if cursor.rowcount != 1:
                return await ctx.send("A faction could no longer cover the wager.")
            await cursor.execute("UPDATE factions SET balance = balance + %s, game_income = game_income + %s WHERE id = %s", (amount, amount, winner.id))
        await ctx.send(f"⚔️ **{winner.name}** defeated **{loser.name}** and won ${amount:,}.")

    @factions.command(name="raid")
    @commands.cooldown(1, 120, commands.BucketType.user)
    async def raid(self, ctx, *, faction: str):
        attacker, *_ = await self.require_member(ctx)
        defender = await self.find_faction(ctx.guild.id, faction)
        if attacker.id == defender.id:
            return await ctx.send("You cannot raid your own faction.")
        if defender.anti_raid_until > time():
            return await ctx.send(f"That faction is raid-protected <t:{defender.anti_raid_until}:R>.")
        if min(attacker.balance, defender.balance) < 25:
            return await ctx.send("Both factions need at least $25 to raid.")
        loot = max(25, min(1000, round(min(attacker.balance, defender.balance) * 0.08)))
        winner, loser = random.choice(((attacker, defender), (defender, attacker)))
        async with self.repo.transaction() as cursor:
            await cursor.execute("UPDATE factions SET balance = balance - %s WHERE id = %s AND balance >= %s", (loot, loser.id, loot))
            if cursor.rowcount != 1:
                return await ctx.send("The losing faction could no longer cover the raid.")
            await cursor.execute("UPDATE factions SET balance = balance + %s, game_income = game_income + %s WHERE id = %s", (loot, loot, winner.id))
        await ctx.send(f"⚔️ **{winner.name}** raided **{loser.name}** for ${loot:,}.")

    @factions.command(name="ally")
    async def ally(self, ctx, *, faction: str):
        source, *_ = await self.require_manager(ctx, owner=True)
        target = await self.find_faction(ctx.guild.id, faction)
        if source.id == target.id:
            return await ctx.send("A faction cannot ally with itself.")
        if len(await self.repo.allies(source.id)) >= 2 or len(await self.repo.allies(target.id)) >= 2:
            return await ctx.send("One of those factions already has two allies.")
        await ctx.send(f"A manager of **{target.name}** must reply with `.accept`.")

        def check(message):
            return message.channel == ctx.channel and ".accept" in message.content.casefold()

        while True:
            try:
                message = await self.bot.wait_for("message", check=check, timeout=60)
            except asyncio.TimeoutError:
                return await ctx.send("The alliance offer expired.")
            result = await self.repo.for_user(ctx.guild.id, message.author.id)
            if result and result[0].id == target.id and result[1] in {"owner", "co_owner"}:
                break
        low, high = sorted((source.id, target.id))
        await self.repo.execute(
            "INSERT IGNORE INTO faction_alliances (faction_low_id, faction_high_id, created_at) VALUES (%s, %s, %s)",
            (low, high, int(time())),
        )
        await ctx.send(f"**{source.name}** and **{target.name}** are now allied.")

    @factions.command(name="annex")
    async def annex(self, ctx, *, faction: str):
        source, *_ = await self.require_manager(ctx, owner=True)
        target = await self.find_faction(ctx.guild.id, faction)
        if source.id == target.id:
            return await ctx.send("A faction cannot annex itself.")
        await ctx.send(f"The owner of **{target.name}** must reply with `.accept annex`.")

        def check(message):
            return message.channel == ctx.channel and message.content.casefold() == ".accept annex"

        try:
            message = await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            return await ctx.send("The annex offer expired.")
        result = await self.repo.for_user(ctx.guild.id, message.author.id)
        if not result or result[0].id != target.id or result[1] != "owner":
            return await ctx.send("Only the target faction owner can accept.")
        async with self.repo.transaction() as cursor:
            await cursor.execute("UPDATE factions SET balance = balance + %s WHERE id = %s", (target.balance, source.id))
            await cursor.execute("UPDATE faction_members SET faction_id = %s, role_name = 'member' WHERE faction_id = %s", (source.id, target.id))
            await cursor.execute("UPDATE faction_claims SET faction_id = %s WHERE faction_id = %s", (source.id, target.id))
            await cursor.execute("DELETE FROM factions WHERE id = %s", (target.id,))
        await ctx.send(f"**{source.name}** annexed **{target.name}**.")

    @factions.command(name="shop")
    async def shop(self, ctx):
        faction, *_ = await self.require_manager(ctx)
        view = ShopView(self, ctx, faction.id)
        await ctx.send(embed=await view.embed(), view=view)

    @factions.command(name="top", aliases=["leaderboard", "lb"])
    async def top(self, ctx):
        view = LeaderboardView(self, ctx, global_board=False)
        await ctx.send(embed=await view.embed("net"), view=view)

    @factions.command(name="glb")
    async def glb(self, ctx):
        view = LeaderboardView(self, ctx, global_board=True)
        await ctx.send(embed=await view.embed("net"), view=view)

    @commands.command(name="factions-migrate", aliases=["factions-import"], hidden=True)
    @commands.is_owner()
    async def migrate(self, ctx, mode: str = "preview"):
        if not self.legacy_path.is_file():
            return await ctx.send(f"No legacy file exists at `{self.legacy_path}`.")
        try:
            source = await asyncio.to_thread(self.legacy_path.read_text, encoding="utf-8")
            payload = await asyncio.to_thread(json.loads, source)
            plan = self._migration_plan(payload)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            return await ctx.send(f"Legacy data is invalid: `{error}`")
        summary = (
            f"`{len(plan['factions']):,}` factions • `{len(plan['members']):,}` members • "
            f"`{len(plan['claims']):,}` claims • `{len(plan['alliances']):,}` alliances • "
            f"`{len(plan['foragers']):,}` global foragers"
        )
        mode = mode.casefold()
        if mode == "preview":
            return await ctx.send(
                f"Migration preview: {summary}\nRun `{ctx.prefix}factions-migrate confirm` "
                "for an empty database or use `replace` to overwrite rewrite data."
            )
        existing = await self.repo.one("SELECT COUNT(*) FROM factions")
        if mode == "confirm" and int(existing[0]):
            return await ctx.send("The rewrite database is not empty; use `replace` deliberately.")
        if mode not in {"confirm", "replace"}:
            return await ctx.send("Mode must be `preview`, `confirm`, or `replace`.")
        now = int(time())
        async with self.repo.transaction() as cursor:
            if mode == "replace":
                await cursor.execute("DELETE FROM factions")
            id_map = {}
            for row in plan["factions"]:
                await cursor.execute(
                    "INSERT INTO factions (guild_id, name, owner_id, balance, slots, is_public, bio, icon, banner, "
                    "income_multiplier, compounding, work_income, land_income, alliance_income, game_income, "
                    "extra_income_until, land_guard_until, anti_raid_until, time_chamber_until, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    row[2],
                )
                id_map[(row[0], row[1])] = cursor.lastrowid
            for guild_id, name, user_id, role, contribution, notifications, joined_at in plan["members"]:
                await cursor.execute(
                    "INSERT INTO faction_members (guild_id, user_id, faction_id, role_name, contribution, work_notifications, joined_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (guild_id, user_id, id_map[(guild_id, name)], role, contribution, notifications, joined_at),
                )
            for guild_id, name, channel_id, claimed_at in plan["claims"]:
                await cursor.execute(
                    "INSERT INTO faction_claims (guild_id, channel_id, faction_id, claimed_at) VALUES (%s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE faction_id = VALUES(faction_id), "
                    "claimed_at = VALUES(claimed_at)",
                    (guild_id, channel_id, id_map[(guild_id, name)], claimed_at),
                )
            for guild_id, left, right, created_at in plan["alliances"]:
                low, high = sorted((id_map[(guild_id, left)], id_map[(guild_id, right)]))
                await cursor.execute(
                    "INSERT IGNORE INTO faction_alliances (faction_low_id, faction_high_id, created_at) VALUES (%s, %s, %s)",
                    (low, high, created_at),
                )
            for user_id, profile in plan["foragers"]:
                await cursor.execute(
                    "INSERT INTO faction_foragers "
                    "(user_id, xp, tool_level, energy, energy_updated_at, "
                    "selected_location, searches, lifetime_earned, games, "
                    "correct_answers, best_find, active_encounter, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE xp = GREATEST(xp, VALUES(xp)), "
                    "tool_level = GREATEST(tool_level, VALUES(tool_level)), "
                    "energy = GREATEST(energy, VALUES(energy)), "
                    "searches = GREATEST(searches, VALUES(searches)), "
                    "lifetime_earned = GREATEST(lifetime_earned, VALUES(lifetime_earned)), "
                    "games = GREATEST(games, VALUES(games)), "
                    "correct_answers = GREATEST(correct_answers, VALUES(correct_answers)), "
                    "best_find = COALESCE(best_find, VALUES(best_find)), "
                    "active_encounter = COALESCE(active_encounter, VALUES(active_encounter)), "
                    "updated_at = GREATEST(updated_at, VALUES(updated_at))",
                    (
                        user_id, profile["xp"], profile["tool"], profile["energy"],
                        profile["energy_updated_at"], profile["location"],
                        profile["searches"], profile["earned"], profile["games"],
                        profile["correct_answers"], profile["best_find"],
                        json.dumps(profile["encounter"], separators=(",", ":"))
                        if profile["encounter"] else None,
                        now, now,
                    ),
                )
                for item, amount in profile["inventory"].items():
                    await cursor.execute(
                        "INSERT INTO faction_forager_inventory "
                        "(user_id, item_key, quantity) VALUES (%s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE quantity = GREATEST(quantity, VALUES(quantity))",
                        (user_id, item, amount),
                    )
                for item in profile["discoveries"]:
                    await cursor.execute(
                        "INSERT IGNORE INTO faction_forager_discoveries "
                        "(user_id, item_key, discovered_at) VALUES (%s, %s, %s)",
                        (user_id, item, now),
                    )
        await ctx.send(f"Migrated legacy factions into MySQL: {summary}")

    @staticmethod
    def _migration_plan(payload):
        main = payload.get("main", {})
        if not isinstance(main, dict):
            raise ValueError("'main' must be a mapping")
        notifications = {int(value) for value in payload.get("notifs", [])}
        boosts = payload.get("boosts", {})
        now = int(time())
        plan = {
            "factions": [], "members": [], "claims": [], "alliances": [],
            "foragers": [],
        }
        for raw_guild_id, guild in main.items():
            guild_id = int(raw_guild_id)
            names = set(map(str, guild))
            seen_members = set()
            seen_claims = set()
            seen_alliances = set()
            for raw_name, data in guild.items():
                name = str(raw_name).strip()
                owner = int(data["owner"])
                if not 1 <= len(name) <= 25:
                    raise ValueError(f"invalid faction name in guild {guild_id}")
                features = data.get("features", [])
                if isinstance(features, dict):
                    features = [key for key, enabled in features.items() if enabled]
                multiplier = 1.0
                for feature in features:
                    if "%" in str(feature):
                        with suppress(ValueError):
                            multiplier = max(multiplier, 1 + int(str(feature).split("%", 1)[0].lstrip("+")) / 100)
                income = data.get("income", {})
                user_income = sum(int(value) for key, value in income.items() if str(key).isdigit())
                land_income = int(income.get("land_claims", 0))
                alliance_income = int(income.get("alliances", 0))
                game_income = sum(int(value) for key, value in income.items() if not str(key).isdigit() and key not in {"land_claims", "alliances"})
                def expiry(boost, *, guild_id=guild_id, name=name):
                    return int(boosts.get(boost, {}).get(str(guild_id), {}).get(name, 0))
                faction_values = (
                    guild_id, name, owner, int(data.get("balance", 0)), max(1, int(data.get("slots", 15))),
                    int(bool(data.get("public", True))), str(data.get("bio", ""))[:1024], data.get("icon"),
                    data.get("banner"), multiplier, int("compounding" in features), user_income, land_income,
                    alliance_income, game_income, expiry("extra-income"), expiry("land-guard"),
                    expiry("anti-raid"), expiry("time-chamber"), now, now,
                )
                plan["factions"].append((guild_id, name, faction_values))
                members = list(dict.fromkeys([owner, *map(int, data.get("members", []))]))
                co_owners = {int(value) for value in data.get("co-owners", [])}
                for user_id in members:
                    if user_id in seen_members:
                        continue
                    seen_members.add(user_id)
                    role = "owner" if user_id == owner else "co_owner" if user_id in co_owners else "member"
                    plan["members"].append((
                        guild_id, name, user_id, role, int(income.get(str(user_id), 0)),
                        int(user_id in notifications), now,
                    ))
                for channel_id in map(int, data.get("claims", [])):
                    if channel_id not in seen_claims:
                        seen_claims.add(channel_id)
                        plan["claims"].append((guild_id, name, channel_id, now))
                for ally in map(str, data.get("allies", [])):
                    pair = tuple(sorted((name, ally)))
                    if ally in names and name != ally and pair not in seen_alliances:
                        seen_alliances.add(pair)
                        plan["alliances"].append((guild_id, pair[0], pair[1], now))
        legacy_foragers = payload.get("foragers", {})
        if legacy_foragers is not None and not isinstance(legacy_foragers, dict):
            raise ValueError("'foragers' must be a mapping")
        for raw_user_id, source in (legacy_foragers or {}).items():
            if not isinstance(source, dict):
                continue
            user_id = int(raw_user_id)
            xp = max(0, int(source.get("xp", 0)))
            tool = min(
                max(0, int(source.get("tool", 0))), len(FORAGE_TOOLS) - 1,
            )
            location = str(source.get("location", "meadow"))
            if (
                location not in FORAGE_LOCATIONS
                or FORAGE_LOCATIONS[location]["level"] > forage_level(xp)
            ):
                location = "meadow"
            inventory = source.get("inventory", {})
            if not isinstance(inventory, dict):
                inventory = {}
            discoveries = source.get("discoveries", [])
            if not isinstance(discoveries, list):
                discoveries = []
            encounter = source.get("encounter")
            if not (
                isinstance(encounter, dict)
                and encounter.get("kind") in {"memory", "identify", "trail"}
                and encounter.get("location") in FORAGE_LOCATIONS
                and encounter.get("phase") in {"preview", "answer"}
                and isinstance(encounter.get("choices"), list)
                and encounter.get("correct") is not None
            ):
                encounter = None
            games = max(0, int(source.get("games", 0)))
            profile = {
                "xp": xp,
                "tool": tool,
                "energy": min(
                    FORAGE_TOOLS[tool]["energy"],
                    max(0, int(source.get("energy", FORAGE_TOOLS[tool]["energy"]))),
                ),
                "energy_updated_at": max(0, int(source.get("energy_updated_at", now))) or now,
                "location": location,
                "searches": max(0, int(source.get("searches", 0))),
                "earned": max(0, int(source.get("earned", 0))),
                "games": games,
                "correct_answers": min(
                    games, max(0, int(source.get("correct_answers", 0))),
                ),
                "best_find": (
                    source.get("best_find")
                    if source.get("best_find") in FORAGE_ITEMS else None
                ),
                "encounter": encounter,
                "inventory": {
                    str(item): max(0, int(amount))
                    for item, amount in inventory.items()
                    if str(item) in FORAGE_ITEMS and int(amount) > 0
                },
                "discoveries": list(dict.fromkeys(
                    str(item) for item in discoveries if str(item) in FORAGE_ITEMS
                )),
            }
            plan["foragers"].append((user_id, profile))
        return plan

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.ready.is_set() or self.initialization_error or not message.guild or message.author.bot:
            return
        key = (message.guild.id, message.channel.id)
        self.claim_counter[key] = self.claim_counter.get(key, 0) + 1
        if self.claim_counter[key] < 5:
            return
        self.claim_counter[key] = 0
        row = await self.repo.one(
            "SELECT f.id FROM faction_claims c JOIN factions f ON f.id = c.faction_id "
            "WHERE c.guild_id = %s AND c.channel_id = %s", key,
        )
        if not row:
            return
        faction_id = int(row[0])
        pay = random.randint(1, 5)
        async with self.repo.transaction() as cursor:
            await cursor.execute(
                "UPDATE factions SET balance = balance + %s, land_income = land_income + %s WHERE id = %s",
                (pay, pay, faction_id),
            )
            await cursor.execute(
                "UPDATE factions f JOIN faction_alliances a ON f.id = "
                "IF(a.faction_low_id = %s, a.faction_high_id, a.faction_low_id) "
                "SET f.balance = f.balance + 1, f.alliance_income = f.alliance_income + 1 "
                "WHERE a.faction_low_id = %s OR a.faction_high_id = %s",
                (faction_id, faction_id, faction_id),
            )


FORAGE_PAGES = {
    "camp": ("Trail", "🍃", "Search with energy and collect materials"),
    "pack": ("Pack", "🎒", "Sell materials or save them for better gear"),
    "guide": ("How to play", "📖", "See progression and your global record"),
}


def forage_item_text(item: str, amount: int = 1) -> str:
    data = FORAGE_ITEMS[item]
    quantity = f" ×{amount:,}" if amount != 1 else ""
    return f"{data['emoji']} **{data['name']}**{quantity}"


class ForageMenu(ui.LayoutView):
    """Components V2 field journal backed by the global SQL forage profile."""

    def __init__(self, cog: FactionsRewrite, ctx: commands.Context, faction: Faction):
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.guild_id = ctx.guild.id
        self.faction_id = faction.id
        self.faction = faction
        self.user_id = ctx.author.id
        self.player: dict = {}
        self.page = "camp"
        self.notice: Optional[str] = None
        self.message: Optional[discord.Message] = None

    async def start(self) -> None:
        await self.reload()
        self.rebuild()
        self.message = await self.ctx.send(
            view=self, ephemeral=bool(getattr(self.ctx, "interaction", None)),
        )

    async def reload(self) -> None:
        self.player = await self.cog.repo.forage_profile(self.user_id)
        membership = await self.cog.repo.for_user(self.guild_id, self.user_id)
        if membership and membership[0].id == self.faction_id:
            self.faction = membership[0]

    def rebuild(self) -> None:
        self.clear_items()
        player = self.player
        tool = FORAGE_TOOLS[player["tool"]]
        level = forage_level(player["xp"])
        location = FORAGE_LOCATIONS[player["location"]]
        pack_size = forage_inventory_size(player["inventory"])
        demand = forage_market_item()
        faction_name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(self.faction.name)
        )
        header = (
            f"## 🍃 {faction_name} · Foraging\n"
            f"{self.ctx.author.mention}'s **global** field journal\n"
            f"-# Level {level}/{len(FORAGE_LEVEL_XP)} · {tool['emoji']} {tool['name']} · "
            f"{player['energy']}/{tool['energy']} energy · "
            f"{pack_size}/{tool['capacity']} packed · Same profile in every server"
        )
        icon = self.cog.faction_icon(self.ctx, self.faction)
        if icon:
            header_item = ui.Section(
                ui.TextDisplay(header),
                accessory=ui.Thumbnail(str(icon), description=f"{self.faction.name} icon"),
            )
        else:
            header_item = ui.TextDisplay(header)
        children: list[ui.Item[Any]] = [header_item]
        if self.notice:
            children.append(ui.TextDisplay(f"> ✨ **Trail update** · {self.notice}"))
        children.extend((
            ui.ActionRow(ForagePageSelect(self)),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
        ))
        if self.page == "camp":
            if player["encounter"]:
                children.extend(self._encounter_items(player["encounter"], tool, pack_size))
            else:
                children.extend(
                    self._camp_items(player, level, location, tool, pack_size, demand)
                )
        elif self.page == "pack":
            children.extend(self._pack_items(player, tool, pack_size, demand))
        else:
            children.extend(self._guide_items(player, level))
        self.add_item(ui.Container(*children, accent_colour=purple))

    def _encounter_items(
        self, encounter: dict, tool: dict, pack_size: int,
    ) -> list[ui.Item[Any]]:
        location = FORAGE_LOCATIONS[encounter["location"]]
        if encounter["phase"] == "preview":
            game = [
                ui.TextDisplay(
                    f"### 🧠 {encounter['title']}\n{encounter['prompt']}\n\n"
                    f"# {encounter['preview']}"
                ),
                ui.ActionRow(
                    ForageEncounterButton(
                        self, "ready", "I'm ready", "🧠", discord.ButtonStyle.primary,
                    ),
                    ForageEncounterButton(
                        self, "abandon", "Abandon", "🚪", discord.ButtonStyle.danger,
                    ),
                ),
            ]
        else:
            buttons = [
                ForageEncounterButton(
                    self,
                    "answer",
                    option["label"],
                    option.get("emoji"),
                    discord.ButtonStyle.secondary,
                    choice=option["value"],
                )
                for option in encounter["choices"]
            ]
            buttons.append(
                ForageEncounterButton(
                    self, "abandon", "Abandon", "🚪", discord.ButtonStyle.danger,
                )
            )
            game = [
                ui.TextDisplay(
                    f"### 🎯 {encounter['title']}\n{encounter['prompt']}\n"
                    "Choose one answer below."
                ),
                ui.ActionRow(*buttons),
            ]
        return [
            ui.TextDisplay(
                f"### {location['emoji']} Active search · {location['name']}\n"
                "Your energy is already spent. Finish the mini-game to collect the haul.\n"
                f"-# Pack {pack_size}/{tool['capacity']} · This search follows you across servers."
            ),
            *game,
            ui.TextDisplay(
                "✅ **Correct:** full haul, one bonus find, and full XP\n"
                "↪️ **Incorrect:** smaller haul and half XP — you still bring something home"
            ),
        ]

    def _camp_items(
        self,
        player: dict,
        level: int,
        location: dict,
        tool: dict,
        pack_size: int,
        demand: str,
    ) -> list[ui.Item[Any]]:
        now = int(time())
        if player["energy"] < tool["energy"]:
            next_energy = player["energy_updated_at"] + FORAGE_ENERGY_REGEN_SECONDS
            energy_detail = f"next charge <t:{next_energy}:R>"
        else:
            energy_detail = "fully rested"
        if level < len(FORAGE_LEVEL_XP):
            progress = f"{FORAGE_LEVEL_XP[level] - player['xp']:,} XP to level {level + 1}"
        else:
            progress = "maximum field level"
        base_value = forage_sale_value(player["inventory"])
        market_value = forage_sale_value(player["inventory"], demand)
        market = FORAGE_ITEMS[demand]
        can_search = (
            player["energy"] >= location["energy"]
            and pack_size < tool["capacity"]
        )
        return [
            ui.TextDisplay(
                "**Search** → finish a quick mini-game → collect materials → "
                "**sell** for faction money or **save** them to craft better gear."
            ),
            ui.TextDisplay(
                f"### {location['emoji']} {location['name']}\n"
                f"{location['description']}\n"
                f"**Cost** {location['energy']} energy · **Yield** {location['rolls']} finds "
                f"· **Gear bonus** {round(tool['bonus'] * 100)}%"
            ),
            ui.ActionRow(ForageLocationSelect(self, level)),
            ui.TextDisplay(
                f"**Energy** `{player['energy']}/{tool['energy']}` · {energy_detail}\n"
                f"**Field XP** `{player['xp']:,}` · {progress}\n"
                f"**Pack value** `${market_value:,}`"
                + (
                    f" · includes `${market_value - base_value:,}` market premium"
                    if market_value > base_value else ""
                )
            ),
            ui.TextDisplay(
                "### 📈 Daily market request\n"
                f"{market['emoji']} **{market['name']}** sells for **50% more** until "
                f"<t:{((now // 86_400) + 1) * 86_400}:R>."
            ),
            ui.ActionRow(
                ForageButton(
                    self, "search", f"Search {location['name']}", location["emoji"],
                    discord.ButtonStyle.primary, disabled=not can_search,
                ),
                ForageButton(
                    self, "sell", "Sell pack", "💰", discord.ButtonStyle.success,
                    disabled=not bool(player["inventory"]),
                ),
                ForageButton(self, "refresh", "Refresh", "🔄"),
                ForageButton(self, "close", "Close", "✖️"),
            ),
            ui.TextDisplay(
                "-# XP, energy, materials, gear, encounters, and stats are global. "
                "Only the faction receiving a sale depends on this server."
            ),
        ]

    def _pack_items(
        self, player: dict, tool: dict, pack_size: int, demand: str,
    ) -> list[ui.Item[Any]]:
        inventory = player["inventory"]
        if inventory:
            lines = []
            for item, amount in sorted(
                inventory.items(),
                key=lambda pair: FORAGE_ITEMS[pair[0]]["value"] * pair[1],
                reverse=True,
            ):
                value = FORAGE_ITEMS[item]["value"] * amount
                if item == demand:
                    value += value // 2
                marker = " · **market +50%**" if item == demand else ""
                lines.append(f"{forage_item_text(item, amount)} · `${value:,}`{marker}")
            inventory_text = "\n".join(lines)
        else:
            inventory_text = "Your pack is empty. Return to **Trail** and search an area."
        total = forage_sale_value(inventory, demand)
        if player["tool"] < len(FORAGE_TOOLS) - 1:
            next_tool = FORAGE_TOOLS[player["tool"] + 1]
            level = forage_level(player["xp"])
            level_ready = level >= next_tool["level"]
            has_materials = all(
                inventory.get(item, 0) >= needed
                for item, needed in next_tool["recipe"].items()
            )
            recipe = " · ".join(
                f"{FORAGE_ITEMS[item]['emoji']} {FORAGE_ITEMS[item]['name']} "
                f"`{inventory.get(item, 0)}/{needed}`"
                for item, needed in next_tool["recipe"].items()
            )
            recipe_text = (
                f"### 🛠️ Next craft · {next_tool['name']}\n"
                f"**Level {next_tool['level']} required** · "
                f"{'ready' if level_ready else f'you are level {level}'}\n{recipe}\n"
                f"`{next_tool['capacity']}` pack · `{next_tool['energy']}` energy · "
                f"`{round(next_tool['bonus'] * 100)}%` bonus-find chance"
            )
            can_craft = level_ready and has_materials
            craft_label = f"Craft {next_tool['name']}"
        else:
            recipe_text = (
                f"### 🛠️ Field gear complete\nYour **{tool['name']}** is the strongest kit."
            )
            can_craft = False
            craft_label = "Maximum gear"
        return [
            ui.TextDisplay(
                f"### 🎒 Pack · `{pack_size}/{tool['capacity']}`\n{inventory_text}"
            ),
            ui.TextDisplay(
                f"**Sale total** `${total:,}` · **Lifetime faction forage income** "
                f"`${self.faction.forage_income:,}`"
            ),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(recipe_text),
            ui.ActionRow(
                ForageButton(
                    self, "sell", f"Sell all for ${total:,}", "💰",
                    discord.ButtonStyle.success, disabled=not bool(inventory),
                ),
                ForageButton(
                    self, "craft", craft_label, "🛠️", discord.ButtonStyle.primary,
                    disabled=not can_craft,
                ),
                ForageButton(self, "close", "Close", "✖️"),
            ),
            ui.TextDisplay(
                "-# Selling empties your global pack into the faction you belong to here."
            ),
        ]

    def _guide_items(self, player: dict, level: int) -> list[ui.Item[Any]]:
        trail_lines = []
        for location in FORAGE_LOCATIONS.values():
            unlocked = level >= location["level"]
            status = "available" if unlocked else f"level {location['level']}"
            trail_lines.append(
                f"{'✅' if unlocked else '🔒'} {location['emoji']} **{location['name']}** · "
                f"{status} · {location['energy']} energy"
            )
        discoveries = set(player["discoveries"])
        resources = " · ".join(
            f"{data['emoji']} {data['name']} `${data['value']}`"
            if item in discoveries else "❔ Undiscovered"
            for item, data in FORAGE_ITEMS.items()
        )
        accuracy = (
            round(player["correct_answers"] / player["games"] * 100)
            if player["games"] else 0
        )
        best = player["best_find"]
        trail_text = "\n".join(trail_lines)
        return [
            ui.TextDisplay(
                "### 🍃 The whole loop\n"
                "**1. Search** a trail and finish one short mini-game.\n"
                "**2. Choose** between selling the pack or saving recipe materials.\n"
                "**3. Progress** with XP for trails and XP plus materials for gear.\n"
                "-# Games rotate between memory, field identification, and trail reading."
            ),
            ui.TextDisplay(f"### 🗺️ Trails\n{trail_text}"),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(
                f"### 📚 Discoveries · `{len(discoveries)}/{len(FORAGE_ITEMS)}`\n{resources}"
            ),
            ui.TextDisplay(
                "### 🏅 Global record\n"
                f"**Searches** `{player['searches']:,}` · **Faction earnings** `${player['earned']:,}`\n"
                f"**Mini-games** `{player['correct_answers']:,}/{player['games']:,}` correct · "
                f"`{accuracy}%` accuracy\n"
                f"**Best discovery** {forage_item_text(best) if best else 'Nothing recorded yet'}"
            ),
            ui.ActionRow(
                ForageButton(self, "refresh", "Refresh", "🔄"),
                ForageButton(self, "close", "Close", "✖️"),
            ),
        ]

    async def refresh_interaction(self, interaction: Interaction) -> None:
        await self.reload()
        self.rebuild()
        self.message = await interaction.edit_original_response(view=self)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This field journal belongs to another forager.", ephemeral=True,
            )
            return False
        membership = await self.cog.repo.for_user(self.guild_id, interaction.user.id)
        if not membership or membership[0].id != self.faction_id:
            await interaction.response.send_message(
                "You are no longer in the faction that opened this journal.", ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)

    async def on_error(
        self, interaction: Interaction, error: Exception, _item: ui.Item,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "I couldn't update the field journal. Try that action once more.",
                ephemeral=True,
            )
        raise error


class ForagePageSelect(ui.Select):
    def __init__(self, menu: ForageMenu):
        self.menu = menu
        super().__init__(
            placeholder="Navigate the foraging field journal",
            options=[
                SelectOption(
                    label=label,
                    value=value,
                    emoji=emoji,
                    description=description,
                    default=menu.page == value,
                )
                for value, (label, emoji, description) in FORAGE_PAGES.items()
            ],
        )

    async def callback(self, interaction: Interaction) -> None:
        self.menu.page = self.values[0]
        self.menu.notice = None
        await interaction.response.defer()
        await self.menu.refresh_interaction(interaction)


class ForageLocationSelect(ui.Select):
    def __init__(self, menu: ForageMenu, level: int):
        self.menu = menu
        options = []
        for key, location in FORAGE_LOCATIONS.items():
            if location["level"] > level:
                continue
            options.append(SelectOption(
                label=location["name"],
                value=key,
                emoji=location["emoji"],
                description=(
                    f"{location['energy']} energy · {location['rolls']} finds · "
                    f"{location['description']}"
                )[:100],
                default=menu.player["location"] == key,
            ))
        super().__init__(placeholder="Choose a trail", options=options)

    async def callback(self, interaction: Interaction) -> None:
        await interaction.response.defer()
        selected = self.values[0]
        result = await self.menu.cog.repo.set_forage_location(
            self.menu.user_id, selected,
        )
        if result["status"] == "selected":
            self.menu.notice = f"Trail set to {FORAGE_LOCATIONS[selected]['name']}."
        else:
            self.menu.notice = "That trail has not been unlocked yet."
        await self.menu.refresh_interaction(interaction)


class ForageEncounterButton(ui.Button):
    def __init__(
        self,
        menu: ForageMenu,
        action: str,
        label: str,
        emoji: Optional[str],
        style: discord.ButtonStyle,
        *,
        choice: Optional[str] = None,
    ):
        super().__init__(label=label[:80], emoji=emoji, style=style)
        self.menu = menu
        self.action = action
        self.choice = choice

    async def callback(self, interaction: Interaction) -> None:
        menu = self.menu
        await interaction.response.defer()
        if self.action == "ready":
            result = await menu.cog.repo.advance_forage(menu.user_id)
            menu.notice = (
                "Sequence hidden — choose the order you just saw."
                if result["status"] == "answer"
                else "That search is no longer waiting."
            )
        elif self.action == "abandon":
            abandoned = await menu.cog.repo.abandon_forage(menu.user_id)
            menu.notice = (
                "Search abandoned. Its spent energy was not refunded."
                if abandoned else "There is no active search to abandon."
            )
        else:
            result = await menu.cog.repo.resolve_forage(
                menu.user_id, self.choice or "",
            )
            if result["status"] == "full":
                menu.page = "pack"
                menu.notice = "Your pack filled elsewhere. Make space, then finish this search."
            elif result["status"] != "resolved":
                menu.notice = "That search is no longer ready for this answer."
            else:
                haul = ", ".join(
                    forage_item_text(item, amount)
                    for item, amount in result["loot"].items()
                )
                menu.notice = (
                    f"{'Correct!' if result['correct'] else 'Not quite.'} "
                    f"Found {haul} and gained {result['xp']} forage XP."
                )
                if result["leveled_up"]:
                    menu.notice += f" Level {result['level']} reached!"
        await menu.refresh_interaction(interaction)


class ForageButton(ui.Button):
    def __init__(
        self,
        menu: ForageMenu,
        action: str,
        label: str,
        emoji: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        *,
        disabled: bool = False,
    ):
        super().__init__(label=label[:80], emoji=emoji, style=style, disabled=disabled)
        self.menu = menu
        self.action = action

    async def callback(self, interaction: Interaction) -> None:
        menu = self.menu
        if self.action == "close":
            await interaction.response.defer()
            await interaction.delete_original_response()
            menu.stop()
            return
        await interaction.response.defer()
        if self.action == "search":
            result = await menu.cog.repo.start_forage(menu.user_id)
            if result["status"] == "energy":
                menu.notice = "You need more energy before searching this trail."
            elif result["status"] == "full":
                menu.page = "pack"
                menu.notice = "Your pack is full. Sell materials or craft an upgrade first."
            elif result["status"] == "locked":
                menu.notice = "That trail has not been unlocked yet."
            elif result["status"] == "pending":
                menu.notice = "Resumed your unfinished global search."
            else:
                menu.notice = (
                    f"Search started: {result['encounter']['title']}. "
                    "Finish the mini-game for your haul."
                )
        elif self.action == "sell":
            result = await menu.cog.repo.sell_forage_inventory(
                menu.user_id, menu.guild_id, menu.faction_id,
            )
            if result["status"] == "empty":
                menu.notice = "Your pack is already empty."
            elif result["status"] == "membership":
                menu.notice = "You are no longer in this faction."
            else:
                menu.notice = (
                    f"Deposited ${result['payout']:,} into {result['faction']}'s treasury."
                )
                if result["premium"]:
                    menu.notice += f" The market request added ${result['premium']:,}."
        elif self.action == "craft":
            result = await menu.cog.repo.craft_forage_tool(menu.user_id)
            if result["status"] == "crafted":
                tool = FORAGE_TOOLS[result["tool"]]
                menu.notice = f"Crafted {tool['emoji']} {tool['name']}. Your limits increased."
            elif result["status"] == "missing":
                menu.notice = "That recipe still needs more gathered materials."
            elif result["status"] == "locked":
                menu.notice = f"That tool unlocks at forage level {result['level']}."
            else:
                menu.notice = "Your field gear is already fully upgraded."
        else:
            menu.notice = "Energy and market information refreshed."
        await menu.refresh_interaction(interaction)


class OwnedView(ui.View):
    def __init__(self, ctx, timeout=90):
        super().__init__(timeout=timeout)
        self.user_id = ctx.author.id

    async def interaction_check(self, interaction: Interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
        return False


class BlackjackView(OwnedView):
    def __init__(self, cog, ctx, faction_id, wager, deck, player, dealer):
        super().__init__(ctx, timeout=90)
        self.cog = cog
        self.ctx = ctx
        self.faction_id = faction_id
        self.wager = wager
        self.deck = deck
        self.player = player
        self.dealer = dealer
        self.message = None
        self.finished = False
        self.final_embed = None
        self.finish_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()

    def embed(self, *, reveal=False, outcome=None, balance=None, profit=0):
        dealer_cards = format_blackjack_hand(self.dealer)
        dealer_value = blackjack_value(self.dealer)
        if not reveal:
            dealer_cards = f"`??` {format_blackjack_hand(self.dealer[1:])}"
            dealer_value = blackjack_value(self.dealer[1:])
        embed = discord.Embed(
            title="Faction Blackjack",
            description=f"Treasury wager: **${self.wager:,}**",
            color=purple,
        )
        embed.add_field(
            name=f"Dealer - {dealer_value if reveal else f'{dealer_value} showing'}",
            value=dealer_cards,
            inline=False,
        )
        embed.add_field(
            name=f"{self.ctx.author.display_name} - {blackjack_value(self.player)}",
            value=format_blackjack_hand(self.player),
            inline=False,
        )
        if outcome:
            labels = {
                "loss": f"Dealer wins. **${self.wager:,} lost.**",
                "push": "Push. **The wager was returned.**",
                "win": f"You win. **${profit:,} profit.**",
                "blackjack": f"Blackjack! **${profit:,} profit** at 3:2.",
                "timeout": "Hand expired. **The wager was returned.**",
                "cancelled": "Hand cancelled. **The wager was returned.**",
            }
            result = labels[outcome]
            if balance is not None:
                result += f"\nTreasury balance: **${balance:,}**"
            embed.add_field(name="Result", value=result, inline=False)
        embed.set_footer(
            text=(
                "Blackjack pays 3:2 • Dealer stands on 17 • "
                "Hit or stand within 90 seconds"
            )
        )
        return embed

    async def finish(self, outcome):
        async with self.finish_lock:
            if self.finished:
                return self.final_embed
            settled, balance, profit = await self.cog.repo.settle_blackjack(
                self.faction_id, self.ctx.guild.id, self.ctx.author.id, outcome,
            )
            if not settled:
                outcome = "cancelled"
            self.finished = True
            for child in self.children:
                child.disabled = True
            self.stop()
            self.cog.blackjack_views.discard(self)
            self.final_embed = self.embed(
                reveal=True, outcome=outcome, balance=balance, profit=profit,
            )
            return self.final_embed

    async def refund(self, *, timed_out=False):
        async with self.action_lock:
            async with self.finish_lock:
                if self.finished:
                    return self.final_embed
                settled, balance, _profit = await self.cog.repo.settle_blackjack(
                    self.faction_id, self.ctx.guild.id, self.ctx.author.id, "push",
                )
                self.finished = True
                for child in self.children:
                    child.disabled = True
                self.stop()
                self.cog.blackjack_views.discard(self)
                self.final_embed = self.embed(
                    reveal=True,
                    outcome="timeout" if timed_out and settled else "cancelled",
                    balance=balance,
                )
                return self.final_embed

    async def on_timeout(self):
        embed = await self.refund(timed_out=True)
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(embed=embed, view=self)

    async def _stand(self):
        while blackjack_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
        player_value = blackjack_value(self.player)
        dealer_value = blackjack_value(self.dealer)
        if dealer_value > 21 or player_value > dealer_value:
            outcome = "win"
        elif player_value == dealer_value:
            outcome = "push"
        else:
            outcome = "loss"
        return await self.finish(outcome)

    @ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit(self, interaction, _button):
        async with self.action_lock:
            if self.finished:
                return await interaction.response.send_message(
                    "That hand is already finished.", ephemeral=True,
                )
            self.player.append(self.deck.pop())
            value = blackjack_value(self.player)
            if value > 21:
                embed = await self.finish("loss")
            elif value == 21:
                embed = await self._stand()
            else:
                embed = self.embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Stand", style=discord.ButtonStyle.success, emoji="✋")
    async def stand(self, interaction, _button):
        async with self.action_lock:
            if self.finished:
                return await interaction.response.send_message(
                    "That hand is already finished.", ephemeral=True,
                )
            embed = await self._stand()
        await interaction.response.edit_message(embed=embed, view=self)


HELP_SECTIONS = {
    "start": ("Getting started", "`{p}f create <name>` • `{p}f join <faction>`\n`{p}f info [faction]` • `{p}f top` • `{p}f glb`"),
    "members": ("Members", "`{p}f invite @user` • `{p}f leave` • `{p}f kick @user`\n`{p}f promote @user` • `{p}f demote @user` • `{p}f members`"),
    "manage": ("Management", "`{p}f rename <name>` • `{p}f privacy` • `{p}f transfer @user`\n`{p}f set-bio <bio>` • `{p}f set-icon` • `{p}f set-banner` • `{p}f disband`"),
    "economy": ("Economy", "`{p}f daily` • `{p}f trade` • `{p}f industry` • `{p}f invest`\n`{p}f work` • `{p}f forage` • `{p}f scrabble` • `{p}f vote`\n`{p}f blackjack <amount>` • `{p}f coinflip <amount>` • `{p}f balance`\n`{p}f pay` • `{p}f shop`"),
    "combat": ("Territory & combat", "`{p}f claim` • `{p}f unclaim` • `{p}f claims`\n`{p}f battle @user [amount]` • `{p}f raid <faction>` • `{p}f ally <faction>` • `{p}f annex <faction>`"),
}


class HelpSelect(ui.Select):
    def __init__(self):
        super().__init__(placeholder="Browse command categories", options=[
            SelectOption(label=title, value=key) for key, (title, _text) in HELP_SECTIONS.items()
        ])

    async def callback(self, interaction):
        await interaction.response.edit_message(embed=self.view.embed(self.values[0]), view=self.view)


class HelpView(OwnedView):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.prefix = ctx.prefix
        self.icon = ctx.author.display_avatar.url
        self.add_item(HelpSelect())

    def embed(self, section):
        title, body = HELP_SECTIONS[section]
        embed = discord.Embed(
            title=f"Factions • {title}",
            description=body.format(p=self.prefix),
            color=purple,
        )
        position = list(HELP_SECTIONS).index(section) + 1
        embed.set_footer(
            text=f"Command guide {position}/{len(HELP_SECTIONS)} • Select a category • Prefix: {self.prefix}",
            icon_url=self.icon,
        )
        return embed


class DashboardSelect(ui.Select):
    def __init__(self):
        super().__init__(placeholder="Explore this faction", options=[
            SelectOption(label="Overview", value="overview", emoji="🏰"),
            SelectOption(label="Roster", value="roster", emoji="👥"),
            SelectOption(label="Economy", value="economy", emoji="💰"),
            SelectOption(label="Territory", value="territory", emoji="🚩"),
            SelectOption(label="Diplomacy", value="diplomacy", emoji="🤝"),
            SelectOption(label="Boosts", value="boosts", emoji="✨"),
        ])

    async def callback(self, interaction):
        await interaction.response.edit_message(embed=await self.view.embed(self.values[0]), view=self.view)


class FactionDashboard(OwnedView):
    def __init__(self, cog, ctx, faction_id):
        super().__init__(ctx)
        self.cog = cog
        self.ctx = ctx
        self.faction_id = faction_id
        self.add_item(DashboardSelect())

    async def embed(self, section):
        faction = await self.cog.repo.by_id(self.faction_id)
        if not faction:
            return discord.Embed(description="That faction no longer exists.", color=purple)
        if (
            faction.industry_level
            and faction.industry_updated_at + 3600 <= int(time())
        ):
            payout, _hours = await self.cog.repo.settle_industry(self.faction_id)
            if payout:
                faction = await self.cog.repo.by_id(self.faction_id)
        icon = self.cog.faction_icon(self.ctx, faction)
        embed = discord.Embed(title=faction.name, color=purple)
        embed.set_thumbnail(url=icon)
        members = await self.cog.repo.members(faction.id)
        claims = await self.cog.repo.claims(faction.id)
        allies = await self.cog.repo.allies(faction.id)
        if section == "overview":
            owner = self.ctx.guild.get_member(faction.owner_id)
            embed.description = faction.bio or "No biography has been set."
            embed.add_field(name="Owner", value=owner.mention if owner else str(faction.owner_id))
            embed.add_field(name="Members", value=f"{len(members)}/{faction.slots}")
            embed.add_field(name="Balance", value=f"${faction.balance:,}")
            embed.add_field(name="Joining", value="Public" if faction.is_public else "Invite only")
            embed.add_field(name="Claims", value=str(len(claims)))
            embed.add_field(name="Allies", value=str(len(allies)))
            if faction.banner:
                embed.set_image(url=faction.banner)
        elif section == "roster":
            lines = []
            for user_id, role, contribution, _notifications in members[:25]:
                member = self.ctx.guild.get_member(int(user_id))
                lines.append(f"**{str(role).replace('_', ' ').title()}:** {member.mention if member else user_id} — ${int(contribution):,}")
            embed.description = "\n".join(lines) or "No members."
        elif section == "economy":
            sources = (
                ("Work", faction.work_income), ("Land", faction.land_income),
                ("Alliances", faction.alliance_income), ("Games", faction.game_income),
                ("Daily grants", faction.daily_income), ("Trade", faction.trade_income),
                ("Foraging", faction.forage_income),
                ("Industry", faction.industry_income), ("Transfers", faction.transfer_income),
            )
            embed.description = f"**Balance:** ${faction.balance:,}\n**Multiplier:** {faction.income_multiplier:.2f}×"
            embed.add_field(name="Lifetime income", value="\n".join(f"{name}: ${amount:,}" for name, amount in sources), inline=False)
            level = min(max(0, faction.industry_level), len(INDUSTRY_TIERS) - 1)
            industry_name, _cost, hourly_rate = INDUSTRY_TIERS[level]
            embed.add_field(
                name="Industry",
                value=(
                    f"{industry_name} (tier {level}) - "
                    f"${round(hourly_rate * faction.income_multiplier):,}/hour\n"
                    "Automatic production stores up to 7 days."
                ),
                inline=False,
            )
        elif section == "territory":
            embed.description = "\n".join(
                (self.ctx.guild.get_channel(int(channel_id)).mention if self.ctx.guild.get_channel(int(channel_id)) else f"Deleted channel `{channel_id}`")
                for channel_id, _claimed_at in claims
            ) or "No claimed channels."
        elif section == "diplomacy":
            embed.description = "\n".join(f"• {ally.name}" for ally in allies) or "No alliances."
        else:
            now = int(time())
            boosts = (
                ("Extra income", faction.extra_income_until),
                ("Land guard", faction.land_guard_until),
                ("Anti raid", faction.anti_raid_until),
                ("Time chamber", faction.time_chamber_until),
            )
            active = [f"**{name}:** <t:{expires}:R>" for name, expires in boosts if expires > now]
            embed.description = "\n".join(active) or "No active boosts."
            embed.add_field(name="Permanent upgrades", value=(f"{faction.income_multiplier:.2f}× income\n" if faction.income_multiplier > 1 else "") + ("Compounding" if faction.compounding else "None"))
        section_names = {
            "overview": "Command Center",
            "roster": "Roster Hall",
            "economy": "Treasury Ledger",
            "territory": "Territory Map",
            "diplomacy": "Alliance Council",
            "boosts": "Upgrade Vault",
        }
        section_position = list(section_names).index(section) + 1
        embed.set_footer(
            text=(
                f"{section_names[section]} • Panel {section_position}/{len(section_names)} "
                f"• {len(members)}/{faction.slots} members • ${faction.balance:,} treasury"
            ),
            icon_url=icon,
        )
        return embed


SHOP = {
    "slots": (250, "Extra Slots", "slots = LEAST(25, slots + 5)"),
    "income": (50, "Extra Income (2h)", "extra_income_until = %s"),
    "raid": (75, "Anti Raid (12h)", "anti_raid_until = %s"),
    "guard": (100, "Land Guard (2h)", "land_guard_until = %s"),
    "chamber": (500, "Time Chamber (4h)", "time_chamber_until = %s"),
    "multiplier_125": (10000, "1.25× Income", "income_multiplier = GREATEST(income_multiplier, 1.25)"),
    "multiplier_150": (25000, "1.50× Income", "income_multiplier = GREATEST(income_multiplier, 1.50)"),
    "multiplier_175": (75000, "1.75× Income", "income_multiplier = GREATEST(income_multiplier, 1.75)"),
    "compound": (125000, "Compounding", "compounding = 1"),
}


class ShopSelect(ui.Select):
    def __init__(self):
        super().__init__(placeholder="Select an item", options=[
            SelectOption(label=label, value=key, description=f"${cost:,}")
            for key, (cost, label, _update) in SHOP.items()
        ])

    async def callback(self, interaction):
        self.view.selected = self.values[0]
        await interaction.response.edit_message(embed=await self.view.embed(), view=self.view)


class ShopView(OwnedView):
    def __init__(self, cog, ctx, faction_id):
        super().__init__(ctx)
        self.cog = cog
        self.ctx = ctx
        self.faction_id = faction_id
        self.selected = None
        self.add_item(ShopSelect())

    async def embed(self):
        faction = await self.cog.repo.by_id(self.faction_id)
        icon = self.cog.faction_icon(self.ctx, faction)
        embed = discord.Embed(title="Faction Shop", description=f"**{faction.name}** • ${faction.balance:,}", color=pink)
        if self.selected:
            cost, label, _update = SHOP[self.selected]
            embed.add_field(name=label, value=f"Price: **${cost:,}**")
            remaining = faction.balance - cost
            footer = (
                f"Previewing {label} • Balance after purchase: ${remaining:,} "
                "• Confirm below"
            )
        else:
            embed.add_field(name="Inventory", value="\n".join(f"**{label}** — ${cost:,}" for cost, label, _update in SHOP.values()))
            footer = (
                f"{len(SHOP)} upgrades available • Select an item to inspect "
                "• Purchases apply immediately"
            )
        embed.set_footer(text=footer, icon_url=icon)
        return embed

    @ui.button(label="Confirm purchase", style=discord.ButtonStyle.success, emoji="🛒", row=1)
    async def confirm_purchase(self, interaction, _button):
        if not self.selected:
            return await interaction.response.send_message("Select an item first.", ephemeral=True)
        faction = await self.cog.repo.by_id(self.faction_id)
        cost, label, update = SHOP[self.selected]
        if faction.balance < cost:
            return await interaction.response.send_message("Your faction cannot afford that.", ephemeral=True)
        if self.selected == "slots" and faction.slots >= 25:
            return await interaction.response.send_message("You already have 25 slots.", ephemeral=True)
        if self.selected == "compound" and faction.compounding:
            return await interaction.response.send_message("You already own Compounding.", ephemeral=True)
        multiplier = {
            "multiplier_125": 1.25,
            "multiplier_150": 1.50,
            "multiplier_175": 1.75,
        }.get(self.selected)
        if multiplier and faction.income_multiplier >= multiplier:
            return await interaction.response.send_message("You already own that income tier.", ephemeral=True)
        hours = {"income": 2, "raid": 12, "guard": 2, "chamber": 4}.get(self.selected)
        args = (int(time() + hours * 3600), cost, faction.id, cost) if hours else (cost, faction.id, cost)
        changed = await self.cog.repo.execute(
            f"UPDATE factions SET {update}, balance = balance - %s WHERE id = %s AND balance >= %s",
            args,
        )
        if not changed:
            return await interaction.response.send_message("The purchase could not be completed.", ephemeral=True)
        self.selected = None
        await interaction.response.edit_message(embed=await self.embed(), view=self)


class LeaderboardSelect(ui.Select):
    def __init__(self):
        super().__init__(placeholder="Leaderboard metric", options=[
            SelectOption(label="Net worth", value="net", emoji="🏰"),
            SelectOption(label="Cash balance", value="cash", emoji="💰"),
        ])

    async def callback(self, interaction):
        await interaction.response.edit_message(embed=await self.view.embed(self.values[0]), view=self.view)


class LeaderboardView(OwnedView):
    def __init__(self, cog, ctx, global_board):
        super().__init__(ctx)
        self.cog = cog
        self.ctx = ctx
        self.global_board = global_board
        self.add_item(LeaderboardSelect())

    async def embed(self, metric):
        rows = await self.cog.repo.rankings(None if self.global_board else self.ctx.guild.id, 15)
        index = 5 if metric == "net" else 3
        rows.sort(key=lambda row: int(row[index]), reverse=True)
        description = "\n".join(
            f"**#{place}.** {name} — ${int(row[index]):,}"
            for place, row in enumerate(rows, 1) for name in [row[2]]
        ) or "No factions yet."
        scope = "Global" if self.global_board else self.ctx.guild.name
        title = "Net Worth" if metric == "net" else "Cash Balance"
        embed = discord.Embed(
            title=f"{scope} • {title}", description=description, color=purple
        )
        explanation = (
            "Net worth includes $500 per claimed channel"
            if metric == "net"
            else "Cash currently held in each treasury"
        )
        embed.set_footer(
            text=f"Top {len(rows)} factions • {explanation} • Switch metrics above",
            icon_url=(
                self.cog.bot.user.display_avatar.url
                if self.global_board or not self.ctx.guild.icon
                else self.ctx.guild.icon.url
            ),
        )
        return embed


async def setup(bot):
    await bot.add_cog(FactionsRewrite(bot), override=True)
