"""Read downloaded resources without buffering past their byte limit."""

from io import BytesIO

from discord.ext import commands

_READ_CHUNK_BYTES = 65_536


async def read_resource(response, max_size: int | None, label: str) -> bytes:
    """Read response bytes, stopping as soon as the configured limit is exceeded."""
    if max_size is None:
        return await response.read()
    with BytesIO() as output:
        while True:
            # Reading one byte beyond the limit detects oversized bodies without
            # first allocating the entire response (including decompressed data).
            remaining = max_size - output.tell()
            chunk = await response.content.read(min(_READ_CHUNK_BYTES, remaining + 1))
            if not chunk:
                return output.getvalue()
            if output.tell() + len(chunk) > max_size:
                raise commands.BadArgument(f"{label} is too large")
            output.write(chunk)
