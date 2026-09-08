"""Create Fate's isolated test database and encrypted test configuration."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from copy import deepcopy
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = ROOT / ".fate-test-services"
SECRET_PATH = SERVICE_ROOT / "test-secrets.json"
AUTH_PATH = ROOT / "data" / "auth.test.json"
CONFIG_PATH = ROOT / "data" / "config.test.json"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MYSQL_EXE = (
    SERVICE_ROOT
    / "mysql-8.4.9-winx64"
    / "mysql-8.4.9-winx64"
    / "bin"
    / "mysql.exe"
)
AUTH_KEY_PATH = Path(
    os.path.expandvars(
        os.path.expanduser(
            os.environ.get(
                "FATE_AUTH_KEY_PATH", str(Path.home() / "Desktop" / "fate_auth.key")
            )
        )
    )
)


def auth_key() -> bytes:
    key = os.environ.get("FATE_AUTH_KEY", "").strip()
    if not key:
        key = AUTH_KEY_PATH.read_text(encoding="utf-8-sig").strip()
    Fernet(key.encode())
    return key.encode()


def load_auth(path: Path) -> dict:
    raw = path.read_bytes().strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return json.loads(Fernet(auth_key()).decrypt(raw))


def save_encrypted(path: Path, value: dict) -> None:
    payload = json.dumps(value, indent=2).encode()
    path.write_bytes(Fernet(auth_key()).encrypt(payload))


def ensure_secrets() -> dict:
    if SECRET_PATH.exists():
        return json.loads(SECRET_PATH.read_text(encoding="utf-8"))
    value = {
        "mysql_user": "fate_test",
        "mysql_password": secrets.token_urlsafe(32),
        "mysql_database": "fate_test",
    }
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRET_PATH.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return value


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def bootstrap_mysql(local: dict) -> None:
    setup_sql = "\n".join(
        (
            "CREATE DATABASE IF NOT EXISTS fate_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
            "CREATE USER IF NOT EXISTS "
            f"{sql_literal(local['mysql_user'])}@'127.0.0.1' "
            f"IDENTIFIED BY {sql_literal(local['mysql_password'])};",
            "ALTER USER "
            f"{sql_literal(local['mysql_user'])}@'127.0.0.1' "
            f"IDENTIFIED BY {sql_literal(local['mysql_password'])};",
            "GRANT ALL PRIVILEGES ON fate_test.* TO "
            f"{sql_literal(local['mysql_user'])}@'127.0.0.1';",
            "FLUSH PRIVILEGES;",
        )
    )
    # MYSQL_EXE is a fixed executable inside the isolated local test bundle.
    subprocess.run(  # noqa: S603
        [str(MYSQL_EXE), "--protocol=tcp", "-h127.0.0.1", "-P3307", "-uroot"],
        input=setup_sql,
        text=True,
        check=True,
    )
    subprocess.run(  # noqa: S603 - fixed local test-service executable
        [
            str(MYSQL_EXE),
            "--protocol=tcp",
            "-h127.0.0.1",
            "-P3307",
            f"-u{local['mysql_user']}",
            local["mysql_database"],
        ],
        input=SCHEMA_PATH.read_text(encoding="utf-8"),
        text=True,
        env={**os.environ, "MYSQL_PWD": local["mysql_password"]},
        check=True,
    )


def write_test_configs(local: dict) -> None:
    auth = deepcopy(load_auth(ROOT / "data" / "auth.json"))
    auth["tokens"] = {"beta": ""}
    auth["MySQL"] = {
        "host": "127.0.0.1",
        "port": 3307,
        "user": local["mysql_user"],
        "password": local["mysql_password"],
        "db": local["mysql_database"],
    }
    auth["MongoDB"] = {
        "url": "mongodb://127.0.0.1:27018",
        "connection_args": {},
        "db": "fate_test",
    }
    save_encrypted(AUTH_PATH, auth)

    config = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
    config["token_id"] = "beta"  # noqa: S105 - token selector, not a secret
    config["bot_owner_id"] = 264838866480005122
    config["bot_owner_ids"] = []
    organism = config.get("organism")
    if not isinstance(organism, dict):
        organism = {}
        config["organism"] = organism
    organism["enabled"] = False
    discovery = organism.get("discovery")
    if not isinstance(discovery, dict):
        discovery = {}
        organism["discovery"] = discovery
    discovery["enabled"] = False
    discovery["advisor_ids"] = []
    config.setdefault("mysql", {}).update(
        {
            "host": "127.0.0.1",
            "port": 3307,
            "user": local["mysql_user"],
            "db": local["mysql_database"],
        }
    )
    config.pop("debug_mode", None)
    config.pop("debug_logging", None)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def main() -> None:
    local = ensure_secrets()
    bootstrap_mysql(local)
    write_test_configs(local)
    print("Created the isolated fate_test schema and encrypted test configuration.")


if __name__ == "__main__":
    main()
