"""
cogs.fun.responses
~~~~~~~~~~~~~~~~~~~

A cog for adding random bot responses to messages in a server

:copyright: (C) 2019-present Luckolite, All Rights Reserved
:license: Proprietary, see LICENSE for details
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from os import path

import discord
from chatterbot import ChatBot
from chatterbot.trainers import ListTrainer
from discord.ext import commands

from botutils import abcs


class Responses(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cb = None
        self.trainer = None
        self.conversations = []
        self.listening = False
        self._worker = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="fate-responses",
        )

        self.responses = bot.utils.cache("responses")

    @staticmethod
    def _initialize_chatterbot():
        chatbot = ChatBot(
            name="Fate",
            logic_adapters=[
                "chatterbot.logic.BestMatch",
                "chatterbot.logic.TimeLogicAdapter"
            ]
        )
        trainer = ListTrainer(chatbot)
        conversations = []
        if path.isfile("conversations.json"):
            with open("conversations.json", "r") as f:
                conversations = json.load(f)
            for convo in conversations:
                trainer.train(convo)
        return chatbot, trainer, conversations

    async def cog_load(self):
        loop = asyncio.get_running_loop()
        self.cb, self.trainer, self.conversations = await loop.run_in_executor(
            self._worker,
            self._initialize_chatterbot,
        )

    def cog_unload(self):
        self._worker.shutdown(wait=False, cancel_futures=True)

    @commands.command(description="Disables automatic responses in this server")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def disableresponses(self, ctx):
        self.responses[ctx.guild.id] = {}
        await ctx.send("Disabled responses")
        await self.responses.flush()

    @commands.command(description="Enables automatic responses in this server")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def enableresponses(self, ctx):
        self.responses[ctx.guild.id] = {}
        await ctx.send("Enabled responses")
        await self.responses.flush()

    @commands.command(name="dump-convos", description="Exports learned response conversations")
    @commands.is_owner()
    async def dump_convos(self, ctx):
        conversations = list(self.conversations)

        def dump():
            with open("conversations.json", "w") as f:
                json.dump(conversations, f)

        await asyncio.get_running_loop().run_in_executor(self._worker, dump)
        await ctx.send("Done")

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message):
        if self.listening or msg.guild.id != 1234 or msg.author.bot:
            return
        self.listening = True

        def check(m):
            return (
                m.channel.id == msg.channel.id
                and m.content
                and any(m.content.startswith(c) for c in abcs)
                and not m.author.bot
            )

        convo = [msg.content]
        while True:
            try:
                reply = await self.bot.wait_for(
                    "message", check=check, timeout=60
                )
            except asyncio.TimeoutError:
                self.listening = False
                break
            else:
                if reply.content:
                    convo.append(reply.content)
                else:
                    # End the convo at the attachment
                    self.listening = False
                    break

        if len(convo) > 1:
            print(f"Learned convo: {convo}")
            await asyncio.get_running_loop().run_in_executor(
                self._worker,
                self.trainer.train,
                convo,
            )
            self.conversations.append(convo)


async def setup(bot):
    await bot.add_cog(Responses(bot), override=True)
