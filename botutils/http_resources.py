"""Read downloaded resources without buffering past their byte limit."""

from io import BytesIO

from discord.ext import commands


async def read_resource(response, max_size, label):
    if max_size is None:
        return await response.read()
    with BytesIO() as output:
        while True:
            # Reading one byte beyond the limit detects oversized bodies without
            # first allocating the entire response (including decompressed data).
            chunk = await response.content.read(min(65_536, max_size - output.tell() + 1))
            if not chunk:
                return output.getvalue()
            if output.tell() + len(chunk) > max_size:
                raise commands.BadArgument(f"{label} is too large")
            output.write(chunk)
