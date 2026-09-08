"""Start Fate's project-local MongoDB and MySQL processes when required."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import pymysql

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DISABLED_VALUES = {"0", "false", "no", "off"}
MYSQL_CONFIG_KEYS = frozenset(
    {
        "host",
        "port",
        "user",
        "db",
        "autocommit",
        "min_pool_size",
        "max_pool_size",
    }
)


def mysql_settings(auth: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge public MySQL settings over encrypted authentication secrets."""
    auth_settings = auth.get("MySQL", {})
    config_settings = config.get("mysql", {})
    if not isinstance(auth_settings, dict):
        raise TypeError("auth.json MySQL must be an object")
    if not isinstance(config_settings, dict):
        raise TypeError("config.json mysql must be an object")
    merged = dict(auth_settings)
    for key in MYSQL_CONFIG_KEYS:
        if key in config_settings:
            merged[key] = config_settings[key]
    return merged


def _endpoint(value: str, default_port: int) -> Tuple[str, int, bool]:
    raw = str(value or "").strip()
    if "://" not in raw:
        raw = f"mongodb://{raw}"
    parsed = urlparse(raw)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or default_port
    return host, port, bool(parsed.username or parsed.password)


def _is_listening(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, process, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_listening(host, port):
            return
        if process.poll() is not None:
            raise RuntimeError(
                f"Local database process exited with code {process.returncode} "
                f"before {host}:{port} became ready"
            )
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for local database at {host}:{port}")


def _find_binary(root: Path, filename: str) -> Optional[Path]:
    matches = sorted((root / ".fate-test-services").glob(f"**/bin/{filename}"))
    return matches[0] if matches else None


def _ensure_binaries(root: Path) -> Tuple[Path, Path]:
    mysql = _find_binary(root, "mysqld.exe")
    mongo = _find_binary(root, "mongod.exe")
    if mysql and mongo:
        return mysql, mongo

    installer = root / "apps" / "DevServices" / "install-databases.ps1"
    if os.name != "nt" or not installer.is_file():
        raise RuntimeError(
            "Local database binaries are missing and no supported installer is available"
        )
    subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(installer),
        ],
        cwd=root,
        check=True,
        creationflags=_process_flags(),
        startupinfo=_hidden_startup_info(),
    )
    mysql = _find_binary(root, "mysqld.exe")
    mongo = _find_binary(root, "mongod.exe")
    if not mysql or not mongo:
        raise RuntimeError("The local database installer did not provide both binaries")
    return mysql, mongo


def _process_flags() -> int:
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


def _start_process(arguments, *, cwd: Path):
    return subprocess.Popen(
        [str(argument) for argument in arguments],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_process_flags(),
        startupinfo=_hidden_startup_info(),
    )


def _mysql_paths(
    root: Path,
    mysql_exe: Path,
    port: int,
    storage_root: Path | None = None,
):
    test_root = root / ".fate-test-services"
    test_data = test_root / "data" / "mysql"
    if port == 3307 and (storage_root is not None or (test_data / "mysql").is_dir()):
        data = storage_root / "mysql" if storage_root else test_data
        return data, test_root / "mysql.ini", test_root / "run" / "mysql.pid"

    service_root = root / ".fate-local-services"
    return (
        storage_root / "mysql" if storage_root else service_root / "data" / f"mysql-{port}",
        service_root / f"mysql-{port}.ini",
        service_root / "run" / f"mysql-{port}.pid",
    )


def _start_mysql(
    root: Path,
    executable: Path,
    sql: Dict[str, Any],
    storage_root: Path | None = None,
) -> None:
    host = str(sql.get("host", "127.0.0.1"))
    port = int(sql.get("port", 3306))
    data, config, pid_file = _mysql_paths(root, executable, port, storage_root)
    data.mkdir(parents=True, exist_ok=True)
    config.parent.mkdir(parents=True, exist_ok=True)
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    base = executable.parents[1]
    log = config.parent / f"mysql-{port}.log"
    config.write_text(
        "[mysqld]\n"
        f"basedir={base.as_posix()}\n"
        f"datadir={data.as_posix()}\n"
        f"port={port}\n"
        f"bind-address={host}\n"
        "mysqlx=0\n"
        "character-set-server=utf8mb4\n"
        "collation-server=utf8mb4_unicode_ci\n"
        f"log-error={log.as_posix()}\n\n"
        "[client]\n"
        f"host={host}\nport={port}\nprotocol=tcp\n",
        encoding="ascii",
    )
    defaults = f"--defaults-file={config}"
    if not (data / "mysql").is_dir():
        subprocess.run(
            [str(executable), defaults, "--initialize-insecure"],
            cwd=root,
            check=True,
            creationflags=_process_flags(),
            startupinfo=_hidden_startup_info(),
        )
    process = _start_process([executable, defaults], cwd=root)
    pid_file.write_text(str(process.pid), encoding="ascii")
    _wait_for_port(host, port, process)
    _bootstrap_mysql(sql)


def _mysql_connect(sql, *, root=False):
    return pymysql.connect(
        host=str(sql.get("host", "127.0.0.1")),
        port=int(sql.get("port", 3306)),
        user="root" if root else str(sql["user"]),
        password="" if root else str(sql.get("password", "")),
        database=None if root else str(sql["db"]),
        autocommit=True,
        connect_timeout=3,
    )


def _bootstrap_mysql(sql: Dict[str, Any]) -> None:
    try:
        connection = _mysql_connect(sql)
    except pymysql.MySQLError:
        connection = _mysql_connect(sql, root=True)
    else:
        connection.close()
        return

    database = str(sql["db"])
    if not re.fullmatch(r"[A-Za-z0-9_$-]+", database):
        connection.close()
        raise ValueError(f"Unsafe MySQL database name: {database!r}")
    user = str(sql["user"])
    password = str(sql.get("password", ""))
    quoted_database = "`" + database.replace("`", "``") + "`"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {quoted_database} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            if user == "root":
                if password:
                    cursor.execute(
                        "ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (password,)
                    )
            else:
                for allowed_host in ("localhost", "127.0.0.1"):
                    cursor.execute(
                        "CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                        (user, allowed_host, password),
                    )
                    cursor.execute(
                        f"GRANT ALL PRIVILEGES ON {quoted_database}.* TO %s@%s",
                        (user, allowed_host),
                    )
            cursor.execute("FLUSH PRIVILEGES")
    finally:
        connection.close()


def _mongo_paths(root: Path, port: int, storage_root: Path | None = None):
    test_data = root / ".fate-test-services" / "data" / "mongo"
    if port == 27018 and (storage_root is not None or test_data.is_dir()):
        base = root / ".fate-test-services"
        data = storage_root / "mongo" if storage_root else test_data
        return data, base / "logs" / "mongo.log", base / "run" / "mongo.pid"
    base = root / ".fate-local-services"
    return (
        storage_root / "mongo" if storage_root else base / "data" / f"mongo-{port}",
        base / f"mongo-{port}.log",
        base / "run" / f"mongo-{port}.pid",
    )


def _start_mongo(
    root: Path,
    executable: Path,
    host: str,
    port: int,
    storage_root: Path | None = None,
) -> None:
    data, log, pid_file = _mongo_paths(root, port, storage_root)
    data.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    process = _start_process(
        [
            executable, "--dbpath", data, "--port", port, "--bind_ip", host,
            "--logpath", log, "--logappend",
        ],
        cwd=root,
    )
    pid_file.write_text(str(process.pid), encoding="ascii")
    _wait_for_port(host, port, process)


def ensure_local_databases(
    auth: Dict[str, Any], config: Dict[str, Any], *, root: Path
) -> Tuple[bool, bool]:
    """Start missing loopback databases and return ``(mysql_started, mongo_started)``."""
    environment_setting = os.environ.get("FATE_AUTO_START_DATABASES")
    if environment_setting is not None:
        auto_start = environment_setting.strip().lower() not in DISABLED_VALUES
    else:
        database_settings = config.get("local_databases", {})
        if not isinstance(database_settings, dict):
            raise TypeError("config.json local_databases must be an object")
        auto_start = database_settings.get("auto_start", True)
        if type(auto_start) is not bool:
            raise TypeError("config.json local_databases.auto_start must be true or false")
    if not auto_start:
        return False, False

    configured_storage = config.get("database_storage_location")
    storage_root = None
    if isinstance(configured_storage, str) and configured_storage.strip():
        storage_root = Path(os.path.expandvars(os.path.expanduser(configured_storage.strip())))
        if not storage_root.is_absolute():
            storage_root = root / storage_root
        storage_root = storage_root.resolve()

    sql = mysql_settings(auth, config)
    mysql_host = str(sql.get("host", "127.0.0.1"))
    mysql_port = int(sql.get("port", 3306))
    mongo = auth.get("MongoDB") or config.get("mongodb", {})
    mongo_host, mongo_port, mongo_has_credentials = _endpoint(
        mongo.get("url", "127.0.0.1:27017"), 27017
    )
    missing_mysql = mysql_host in LOOPBACK_HOSTS and not _is_listening(
        mysql_host, mysql_port
    )
    missing_mongo = mongo_host in LOOPBACK_HOSTS and not _is_listening(
        mongo_host, mongo_port
    )
    if not missing_mysql and not missing_mongo:
        return False, False
    if os.name != "nt":
        raise RuntimeError(
            "Automatic local database startup currently requires Windows"
        )
    if missing_mongo and mongo_has_credentials:
        raise RuntimeError(
            "Cannot safely bootstrap a new authenticated MongoDB automatically"
        )

    mysql_exe, mongo_exe = _ensure_binaries(root)
    if missing_mysql:
        _start_mysql(root, mysql_exe, sql, storage_root)
    if missing_mongo:
        _start_mongo(root, mongo_exe, mongo_host, mongo_port, storage_root)
    return missing_mysql, missing_mongo
