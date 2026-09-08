import asyncio
import unittest
from types import SimpleNamespace

from cogs.utility.vc_log import VcLog


class VcLogCooldownTests(unittest.TestCase):
    def make_cog(self, channel):
        cog = VcLog.__new__(VcLog)
        cog.config = {1: {"channel": 10}}
        cog.join_cd = {}
        cog.leave_cd = {}
        cog.move_cd = {}
        cog.bot = SimpleNamespace(get_channel=lambda channel_id: channel)
        return cog

    def test_cleanup_during_send_does_not_break_join_or_leave_events(self):
        async def exercise(event):
            channel = SimpleNamespace(
                name="General",
                permissions_for=lambda member: SimpleNamespace(send_messages=True),
            )
            cog = self.make_cog(channel)
            guild = SimpleNamespace(id=1, me=object())
            member = SimpleNamespace(id=2, guild=guild, display_name="Member")
            voice_channel = SimpleNamespace(id=20, name="General")

            if event == "join":
                cache = cog.join_cd
                before = SimpleNamespace(channel=None)
                after = SimpleNamespace(channel=voice_channel)
            else:
                cache = cog.leave_cd
                before = SimpleNamespace(channel=voice_channel)
                after = SimpleNamespace(channel=None)

            async def send(message):
                self.assertIn(member.id, cache[guild.id])
                cache.pop(guild.id)

            channel.send = send
            await cog.on_voice_state_update(member, before, after)

        for event in ("join", "leave"):
            with self.subTest(event=event):
                asyncio.run(exercise(event))

    def test_repeated_event_is_suppressed_during_cooldown(self):
        cache = {}

        self.assertTrue(VcLog.claim_cooldown(cache, 1, 2))
        self.assertFalse(VcLog.claim_cooldown(cache, 1, 2))


if __name__ == "__main__":
    unittest.main()
