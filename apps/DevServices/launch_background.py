"""Launch a long-running development service without a console window."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Sequence


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return subprocess.CREATE_NO_WINDOW


def _hidden_startup_info():
    if os.name != "nt":
        return None
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    return startup_info


def launch(command: Sequence[str], *, cwd: Path, pid_file: Path) -> int:
    if not command:
        raise ValueError("A service command is required")

    process = subprocess.Popen(  # noqa: S603 - caller supplies a fixed local binary
        list(command),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_creation_flags(),
        startupinfo=_hidden_startup_info(),
        start_new_session=os.name != "nt",
    )
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(process.pid), encoding="ascii")
    return process.pid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    launch(command, cwd=arguments.cwd, pid_file=arguments.pid_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
