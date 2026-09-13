"""Local, bounded reload jobs shared by Fate and authenticated FateControl."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from time import time
from uuid import uuid4


class ModuleReloadControl:
    def __init__(self, path: Path):
        self.path = path
        self.runtime_id = uuid4().hex

    def read(self, runtime_id=None):
        if not self.path.exists():
            return {"state": "idle"}
        if self.path.is_symlink() or self.path.stat().st_size > 65_536:
            raise ValueError("Invalid module reload status file")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("state") not in {
            "pending", "running", "completed", "failed",
        }:
            raise ValueError("Invalid module reload status")
        if not isinstance(data.get("runtime_id"), str) or not isinstance(data.get("requested_at"), (int, float)):
            raise ValueError("Invalid module reload request")
        if data["state"] in {"pending", "running"}:
            if runtime_id and data.get("runtime_id") != runtime_id:
                return {**data, "state": "failed", "message": "Fate restarted before the reload finished."}
            if data["state"] == "pending" and time() - data.get("requested_at", 0) > 60:
                return {**data, "state": "failed", "message": "Fate did not accept the reload request in time."}
        return data

    def write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".modules-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(data, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def queue(self, runtime_id):
        current = self.read(runtime_id)
        if current["state"] in {"pending", "running"}:
            return current
        job = {
            "id": uuid4().hex, "runtime_id": runtime_id, "state": "pending",
            "requested_at": time(), "message": "Waiting for Fate to reload modules…",
        }
        self.write(job)
        return job

    async def execute_pending(self, bot):
        job = await asyncio.to_thread(self.read, self.runtime_id)
        if job["state"] != "pending":
            return
        job.update(state="running", message="Reloading modules…")
        await asyncio.to_thread(self.write, job)
        try:
            loader = bot.get_cog("Reload")
            if loader is None:
                raise RuntimeError("The Reload module is not loaded.")
            result = await loader.reload_from_control()
        except asyncio.CancelledError:
            job.update(state="failed", message="Reload interrupted while Fate was stopping.")
            await asyncio.to_thread(self.write, job)
            raise
        except Exception as error:
            job.update(state="failed", message=f"Module reload failed: {str(error)[:500]}")
        else:
            job.update(result)
            job["state"] = "failed" if result.get("failed") else "completed"
        job["finished_at"] = time()
        await asyncio.to_thread(self.write, job)
