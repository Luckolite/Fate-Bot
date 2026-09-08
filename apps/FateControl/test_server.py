import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

from apps.FateControl.server import (
    CONTROL_PANEL_ROOT,
    CONTROL_SESSION_COOKIE,
    ConfigConflictError,
    ConfigError,
    FateController,
    backup_settings_payload,
    control_panel_config,
    read_config_snapshot,
    restore_control_panel_integers,
    update_backup_settings,
    update_config,
    validate_backup_settings,
    validate_config,
    write_status_server_restart_request,
)
from botutils.backups import BackupManager, BackupSettings, prune_local_backups
from botutils.google_drive import (
    DRIVE_SCOPES,
    GoogleDriveClient,
    GoogleDriveCredentials,
    GoogleDriveTokenStore,
)
from botutils.local_databases import (
    _mongo_paths,
    _mysql_paths,
    ensure_local_databases,
    mysql_settings,
)
from botutils.telemetry import (
    WINDOWS,
    MongoTelemetryListener,
    TelemetryCollector,
    TelemetryStore,
    TelemetryUnavailable,
    build_metric_payload,
    instrument_mysql_pool,
    telemetry_path_for_config,
)


class FateControlHostTests(unittest.TestCase):
    @staticmethod
    def valid_config() -> dict:
        return {
            "activity_status": "Online",
            "max_cached_messages": 1_000,
            "datastore_location": "./data",
            "extensions": {"core": ["core"]},
        }

    def test_configured_database_storage_keeps_test_runtime_files_local(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "external-databases"
            mysql_data, mysql_config, mysql_pid = _mysql_paths(
                root, root / "mysql" / "bin" / "mysqld.exe", 3307, storage
            )
            mongo_data, mongo_log, mongo_pid = _mongo_paths(root, 27018, storage)

        self.assertEqual(mysql_data, storage / "mysql")
        self.assertEqual(mongo_data, storage / "mongo")
        self.assertEqual(mysql_config, root / ".fate-test-services" / "mysql.ini")
        self.assertEqual(mysql_pid, root / ".fate-test-services" / "run" / "mysql.pid")
        self.assertEqual(mongo_log, root / ".fate-test-services" / "logs" / "mongo.log")
        self.assertEqual(mongo_pid, root / ".fate-test-services" / "run" / "mongo.pid")

    def test_browser_config_round_trips_large_discord_ids_without_rounding(self):
        original = {
            "bot_user_id": 506_735_111_543_193_600,
            "bot_owner_ids": [457_210_410_819_649_540],
            "max_cached_messages": 1_000,
        }
        encoded, paths = control_panel_config(original)
        self.assertEqual(encoded["bot_user_id"], "506735111543193600")
        self.assertEqual(encoded["bot_owner_ids"][0], "457210410819649540")
        self.assertEqual(restore_control_panel_integers(encoded, paths), original)

    def test_status_server_restart_request_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fate-status-server-16420.restart"
            write_status_server_restart_request(path)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertIn("requested_at", payload)

    def controller(self, root: Path) -> FateController:
        return FateController(
            config_path=root / "config.json",
            token_path=root / "control.token",
            bot_log_path=root / "bot.log",
            autostart=False,
        )

    def test_system_details_include_live_host_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            details = controller.system_details()

        self.assertTrue(details["hostname"])
        self.assertIn("used_percent", details["cpu"])
        self.assertIn("received_bytes", details["network"])
        self.assertIn("read_bytes", details["disk_io"])
        self.assertGreaterEqual(details["disk_io"]["read_bytes_per_second"], 0)
        self.assertGreaterEqual(details["disk_io"]["write_bytes_per_second"], 0)
        self.assertEqual(details["capabilities"]["reboot"], controller.allow_host_reboot)

    def test_health_check_reads_only_the_bounded_console_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(
                json.dumps(self.valid_config()), encoding="utf-8"
            )
            (root / "bot.log").write_text("old output\n", encoding="utf-8")
            controller = self.controller(root)
            with patch.object(
                controller,
                "console_tail",
                return_value={"text": "INFO ready\nERROR failed"},
            ) as console_tail:
                details = controller.health_details()

        console_tail.assert_called_once_with(500)
        self.assertEqual(details["recent_log_errors"], 1)

    def test_system_details_calculate_disk_rates_between_samples(self):
        samples = [
            SimpleNamespace(read_bytes=1_000, write_bytes=2_000),
            SimpleNamespace(read_bytes=5_000, write_bytes=8_000),
        ]
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            with (
                patch("apps.FateControl.server.monotonic", side_effect=[100.0, 102.0]),
                patch(
                    "apps.FateControl.server.psutil.disk_io_counters",
                    side_effect=samples,
                ),
            ):
                first = controller.system_details()
                second = controller.system_details()

        self.assertEqual(first["disk_io"]["read_bytes_per_second"], 0)
        self.assertEqual(first["disk_io"]["write_bytes_per_second"], 0)
        self.assertEqual(second["disk_io"]["read_bytes_per_second"], 2_000)
        self.assertEqual(second["disk_io"]["write_bytes_per_second"], 3_000)

    def test_reboot_is_disabled_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"FATE_ALLOW_HOST_REBOOT": ""}):
                controller = self.controller(Path(directory))
            with self.assertRaises(web.HTTPForbidden):
                controller.reboot_host()

    def test_controller_uses_config_settings_without_environment_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.valid_config()
            config["control_panel"] = {
                "instance_name": "Bedroom Fate",
                "bot_status_host": "192.0.2.10",
                "bot_status_port": 17420,
                "autostart": False,
                "allow_host_reboot": True,
            }
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "FATE_INSTANCE_NAME": "",
                    "FATE_BOT_STATUS_HOST": "",
                    "DASHBOARD_PORT": "",
                    "FATE_ALLOW_HOST_REBOOT": "",
                },
                clear=False,
            ):
                for key in (
                    "FATE_INSTANCE_NAME",
                    "FATE_BOT_STATUS_HOST",
                    "DASHBOARD_PORT",
                    "FATE_ALLOW_HOST_REBOOT",
                ):
                    os.environ.pop(key, None)
                controller = FateController(
                    config_path=root / "config.json",
                    token_path=root / "control.token",
                    bot_log_path=root / "bot.log",
                )

        self.assertEqual(controller.instance_name, "Bedroom Fate")
        self.assertEqual(controller.bot_host, "192.0.2.10")
        self.assertEqual(controller.bot_port, 17420)
        self.assertFalse(controller.autostart)
        self.assertTrue(controller.allow_host_reboot)

    def test_config_additions_are_validated(self):
        config = self.valid_config()
        config.update(
            {
                "local_databases": {"auto_start": True},
                "mysql": {
                    "host": "127.0.0.1",
                    "port": 3306,
                    "user": "fate",
                    "db": "fate",
                    "autocommit": True,
                    "min_pool_size": 1,
                    "max_pool_size": 16,
                },
                "translation": {
                    "endpoint": "https://translate.example/v1",
                    "timeout_seconds": 2.5,
                },
                "website": {
                    "host": "127.0.0.1",
                    "port": 16420,
                    "secure_cookies": False,
                    "dev_mode": False,
                    "trusted_proxies": ["127.0.0.1/32", "::1/128"],
                },
                "control_panel": {
                    "host": "0.0.0.0",
                    "port": 16421,
                    "allowed_ips": ["50.110.237.118/32", "127.0.0.1/32"],
                    "instance_name": "Fate",
                    "bot_status_host": "127.0.0.1",
                    "bot_status_port": 16420,
                    "autostart": True,
                    "allow_host_reboot": False,
                },
            }
        )
        self.assertIs(validate_config(config), config)
        config["website"]["trusted_proxies"] = ["not-a-network"]
        with self.assertRaises(ConfigError):
            validate_config(config)
        config["website"]["trusted_proxies"] = []
        config["control_panel"]["allowed_ips"] = ["not-a-network"]
        with self.assertRaises(ConfigError):
            validate_config(config)

    def test_control_panel_ip_allowlist_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.valid_config()
            config["control_panel"] = {
                "allowed_ips": ["50.110.237.118/32", "127.0.0.1/32"]
            }
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            controller = FateController(
                config_path=root / "config.json",
                token_path=root / "control.token",
                bot_log_path=root / "bot.log",
                autostart=False,
            )

        self.assertTrue(controller.client_ip_allowed("50.110.237.118"))
        self.assertTrue(controller.client_ip_allowed("127.0.0.1"))
        self.assertFalse(controller.client_ip_allowed("50.110.237.119"))
        self.assertFalse(controller.client_ip_allowed(None))

    def test_backup_limits_are_optional_and_strictly_validated(self):
        settings = {
            "enabled": True,
            "location": "./data/backups",
            "frequency_hours": 0.25,
            "retention_days": None,
            "max_storage_gb": None,
            "max_backups": None,
            "include_local_files": True,
            "google_drive": {"enabled": False},
        }
        self.assertIs(validate_backup_settings(settings), settings)
        settings["max_backups"] = True
        with self.assertRaises(ConfigError):
            validate_backup_settings(settings)
        settings["max_backups"] = 2
        settings["max_storage_gb"] = 0
        with self.assertRaises(ConfigError):
            validate_backup_settings(settings)

    def test_backup_settings_update_preserves_server_owned_drive_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = self.valid_config()
            config["backups"] = {
                "enabled": True,
                "location": "./old",
                "frequency_hours": 12,
                "retention_days": 7,
                "max_storage_gb": None,
                "max_backups": None,
                "include_local_files": True,
                "mysqldump_path": "custom-mysqldump",
                "google_drive": {
                    "enabled": False,
                    "client_credentials_path": "./private/client.json",
                    "token_path": "./private/token.json",
                },
            }
            path.write_text(json.dumps(config), encoding="utf-8")
            saved, _revision = update_backup_settings(
                path,
                {
                    "frequency_hours": 6,
                    "max_backups": 5,
                    "google_drive": {"enabled": False},
                },
            )
            full = json.loads(path.read_text(encoding="utf-8"))["backups"]

        self.assertEqual(saved["frequency_hours"], 6)
        self.assertEqual(saved["max_backups"], 5)
        self.assertEqual(full["mysqldump_path"], "custom-mysqldump")
        self.assertEqual(
            full["google_drive"]["client_credentials_path"],
            "./private/client.json",
        )

    def test_selecting_drive_folder_enables_remote_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = self.valid_config()
            path.write_text(json.dumps(config), encoding="utf-8")
            saved, _revision = update_backup_settings(
                path,
                {},
                folder_id="folder_123",
                folder_path="My Drive / Fate",
            )

        self.assertTrue(saved["google_drive"]["enabled"])
        self.assertEqual(saved["google_drive"]["folder_id"], "folder_123")
        self.assertEqual(saved["google_drive"]["folder_path"], "My Drive / Fate")

    def test_backup_settings_reject_stale_editor_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = self.valid_config()
            config["backups"] = {
                "enabled": True,
                "location": "./data/backups",
                "frequency_hours": 12,
            }
            path.write_text(json.dumps(config), encoding="utf-8")
            stale_revision = read_config_snapshot(path).fingerprint
            update_backup_settings(path, {"frequency_hours": 6})

            with self.assertRaises(ConfigConflictError):
                update_backup_settings(
                    path,
                    {"frequency_hours": 24},
                    expected_revision=stale_revision,
                )

    def test_local_backup_retention_removes_oldest_complete_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = []
            for index, size in enumerate((4, 5, 6)):
                path = root / f"fate-backup-2026010{index + 1}T000000Z.zip"
                path.write_bytes(b"x" * size)
                modified = 1_800_000_000 + index
                os.utime(path, (modified, modified))
                files.append(path)
            removed = prune_local_backups(
                root,
                retention_days=None,
                max_backups=2,
                max_storage_bytes=10,
                now=1_900_000_000,
            )

        self.assertEqual(
            removed,
            [files[0].name, files[1].name],
        )

    def test_backup_settings_convert_gib_cap_to_bytes(self):
        parsed = BackupSettings.from_config(
            {
                "backups": {
                    "max_storage_gb": 1.5,
                    "google_drive": {"enabled": False},
                }
            }
        )
        self.assertEqual(parsed.max_storage_bytes, round(1.5 * 1024 ** 3))
        self.assertEqual(
            backup_settings_payload({"backups": {"frequency_hours": 3}})[
                "frequency_hours"
            ],
            3,
        )

    def test_google_authorization_url_uses_state_and_configured_callback(self):
        credentials = GoogleDriveCredentials(
            "client-id",
            "client-secret",
            "https://fate.example/api/v1/backups/google/callback",
        )
        client = GoogleDriveClient(SimpleNamespace(), credentials, SimpleNamespace())
        url, state = client.authorization_url("known-state")

        self.assertEqual(state, "known-state")
        self.assertIn("state=known-state", url)
        self.assertIn("access_type=offline", url)
        self.assertIn("scope=", url)
        self.assertTrue(DRIVE_SCOPES)

    def test_google_token_store_round_trips_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token.json"
            store = GoogleDriveTokenStore(path)
            store.save({"refresh_token": "secret", "expires_at": 123})
            loaded = store.load()
            leftovers = list(path.parent.glob("*.tmp"))
            store.clear()

        self.assertEqual(loaded["refresh_token"], "secret")
        self.assertEqual(leftovers, [])

    def test_mongo_secret_is_written_to_private_config_not_process_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mongo.archive.gz"
            bot = SimpleNamespace(
                auth={"MongoDB": {"url": "mongodb://user:secret@localhost:27017"}},
                config={"mongodb": {"db": "fate"}, "backups": {}},
            )
            manager = BackupManager(bot)
            commands = []

            async def fake_process(command, **_kwargs):
                commands.append(command)
                output.write_bytes(b"archive")

            manager._run_process = fake_process
            asyncio.run(
                manager._dump_mongo(BackupSettings.from_config(bot.config), output)
            )
            private_config = (output.parent / "mongodump-config.yml").read_text(
                encoding="utf-8"
            )

        self.assertNotIn("secret", " ".join(commands[0]))
        self.assertIn("secret", private_config)
        self.assertTrue(any(argument.startswith("--config=") for argument in commands[0]))

    def test_mysql_config_overrides_public_fields_but_preserves_password(self):
        settings = mysql_settings(
            {
                "MySQL": {
                    "host": "old-host",
                    "port": 3306,
                    "user": "old-user",
                    "password": "encrypted-auth-secret",
                    "db": "old-db",
                }
            },
            {
                "mysql": {
                    "host": "new-host",
                    "port": 3307,
                    "user": "new-user",
                    "db": "new-db",
                    "min_pool_size": 2,
                    "max_pool_size": 12,
                }
            },
        )

        self.assertEqual(settings["host"], "new-host")
        self.assertEqual(settings["port"], 3307)
        self.assertEqual(settings["password"], "encrypted-auth-secret")
        self.assertEqual(settings["min_pool_size"], 2)

    def test_control_panel_change_reports_service_restart_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = self.valid_config()
            config["control_panel"] = {"autostart": True}
            path.write_text(json.dumps(config), encoding="utf-8")
            snapshot = read_config_snapshot(path)
            proposed = json.loads(json.dumps(config))
            proposed["control_panel"]["autostart"] = False
            _saved, _revision, restart_required = update_config(
                path,
                proposed,
                expected_revision=snapshot.fingerprint,
            )

        self.assertTrue(restart_required)

    def test_config_can_disable_local_database_autostart(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "botutils.local_databases._is_listening"
        ) as listening:
            started = ensure_local_databases(
                {},
                {"local_databases": {"auto_start": False}},
                root=Path.cwd(),
            )

        self.assertEqual(started, (False, False))
        listening.assert_not_called()

    def test_enabled_reboot_uses_a_fixed_os_command(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"FATE_ALLOW_HOST_REBOOT": "1"}):
                controller = self.controller(Path(directory))
            with patch("apps.FateControl.server.subprocess.run", return_value=completed) as run:
                result = controller.reboot_host()

        command = run.call_args.args[0]
        self.assertIn(command[0], {"shutdown", "shutdown.exe"})
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertTrue(result["accepted"])

    def test_stopped_process_has_stable_runtime_shape(self):
        details = FateController._process_details(None)
        self.assertEqual(details["state"], "stopped")
        self.assertIsNone(details["pid"])

    def test_metrics_follow_the_bot_profile_reported_by_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(
                json.dumps({"datastore_location": "./main-data"}), encoding="utf-8"
            )
            (root / "config.test.json").write_text(
                json.dumps({"datastore_location": "./test-data"}), encoding="utf-8"
            )
            controller = self.controller(root)
            store = controller.metrics_store_for_status(
                {"instance": {"config": "config.test.json"}}
            )

        self.assertEqual(store.path.name, "telemetry-config.test.sqlite3")
        self.assertEqual(store.path.parent.name, "test-data")


class FateControlApiTests(unittest.IsolatedAsyncioTestCase):
    class Request:
        def __init__(
            self,
            token,
            payload=None,
            *,
            query=None,
            match_info=None,
            cookies=None,
            method="GET",
        ):
            self.headers = {"Authorization": f"Bearer {token}"}
            self.payload = payload
            self.query = query or {}
            self.match_info = match_info or {}
            self.cookies = cookies or {}
            self.method = method
            self.secure = False

        async def json(self):
            return self.payload

    def test_shared_control_panel_assets_are_bundled(self):
        self.assertTrue((CONTROL_PANEL_ROOT / "index.html").is_file())
        self.assertTrue((CONTROL_PANEL_ROOT / "styles.css").is_file())
        self.assertTrue((CONTROL_PANEL_ROOT / "app.js").is_file())
        self.assertTrue((CONTROL_PANEL_ROOT / "favicon.svg").is_file())
        theme_root = CONTROL_PANEL_ROOT / "assets" / "themes"
        self.assertEqual(
            {path.stem for path in theme_root.glob("*.webp")},
            {
                "amoled_neon", "classic", "clockwork", "galaxy",
                "music", "nature", "underwater", "waterfall",
            },
        )
        menu_root = theme_root / "menus"
        expected_menu_art = {
            f"{theme}_{section}"
            for theme in (
                "amoled_neon", "classic", "clockwork", "galaxy",
                "music", "nature", "underwater", "waterfall",
            )
            for section in ("overview", "metrics", "config", "backups", "console")
        }
        self.assertEqual(
            {path.stem for path in menu_root.glob("*.webp")},
            expected_menu_art,
        )

    def test_shared_control_panel_exposes_matching_theme_capabilities(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")
        page = (CONTROL_PANEL_ROOT / "index.html").read_text(encoding="utf-8")

        for theme in (
            "amoled_neon", "classic", "clockwork", "galaxy",
            "music", "nature", "underwater", "waterfall",
        ):
            self.assertIn(f'key: "{theme}"', script)
            self.assertIn(f'data-theme="{theme}"', styles)
        self.assertIn('id="theme-effects"', page)
        self.assertIn('id="settings-nav"', page)
        self.assertIn('id="section-settings"', page)
        self.assertIn('id="settings-theme-options"', page)
        self.assertIn('id="settings-theme-effects"', page)
        self.assertIn("function applyVisualTheme", script)
        self.assertIn("z-index: 0; background: linear-gradient", styles)
        self.assertIn(".app-shell { position: relative; z-index: 1;", styles)

    def test_shared_metric_charts_keep_drag_scrubbing(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")

        self.assertIn("function enableMetricScrubbing", script)
        self.assertIn('svg.addEventListener("pointermove"', script)
        self.assertIn('svg.addEventListener("touchmove"', script)
        self.assertIn('{ passive: false }', script)
        self.assertIn('if (event.pointerType === "touch") return;', script)
        self.assertIn('svg.dataset.suppressClick = "true"', script)
        self.assertIn("metric-scrub-bubble", script)
        self.assertIn('scrub.style.display = "none"', script)
        self.assertIn('scrub.style.display = "inline"', script)
        self.assertIn("touch-action: pan-y", styles)

    def test_shared_metric_cards_and_customizer_stay_compact_and_editable(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")
        page = (CONTROL_PANEL_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("align-items: start", styles)
        self.assertIn("height: 112px", styles)
        self.assertIn('edit.textContent = "Edit"', script)
        self.assertIn("openMetricSettings(metric, true)", script)
        self.assertIn("renderMetricLayoutDraft();", script)
        self.assertIn("function saveDialogOnBackdrop", script)
        self.assertIn('event.target === dialog', script)
        self.assertIn("Done or tapping outside saves", page)

    def test_metric_grid_uses_measured_masonry_without_changing_mobile_order(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")
        page = (CONTROL_PANEL_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("grid-auto-rows: 1px", styles)
        self.assertIn("function sizeMetricCard", script)
        self.assertIn("new ResizeObserver", script)
        self.assertIn("Math.ceil((height + rowGap) / (rowHeight + rowGap))", script)
        self.assertIn("grid-row-end: auto !important", styles)
        self.assertIn("grid-template-columns: repeat(var(--metric-column-count, 3), minmax(0, 1fr))", styles)
        medium = styles.split("@media (max-width: 1100px)", 1)[1].split("@media (max-width: 760px)", 1)[0]
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", medium)
        self.assertIn("styles.css?v=20260902-17", page)
        self.assertIn("app.js?v=20260902-21", page)

    def test_shared_metrics_include_requested_activity_counters(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        page = (CONTROL_PANEL_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn('"dashboard_signins"', script)
        self.assertIn('return "Dashboard sign-ins"', script)
        self.assertIn('"active_servers"', script)
        self.assertIn('return "Active servers"', script)
        self.assertIn('"logger_events"', script)
        self.assertIn('return "Logger activity"', script)
        self.assertIn('"selfrole_activity"', script)
        self.assertIn('return "Self-role activity"', script)
        self.assertIn('"welcome_leave_messages"', script)
        self.assertIn('return "Welcome / leave messages"', script)
        self.assertIn('"autorole_activity"', script)
        self.assertIn('return "Auto-role activity"', script)
        self.assertIn('"discord_rate_limits"', script)
        self.assertIn('return "Discord rate limits"', script)
        self.assertIn("top_discord_routes", script)
        self.assertIn("Affected Discord routes", script)
        self.assertIn("app.js?v=20260902-21", page)

    def test_control_clients_coalesce_refreshes_and_accept_copied_panel_urls(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")
        desktop = (CONTROL_PANEL_ROOT.parent / "Windows" / "Program.cs").read_text(
            encoding="utf-8"
        )
        android_root = (
            CONTROL_PANEL_ROOT.parent.parent
            / "FateStatus"
            / "app"
            / "src"
            / "main"
            / "java"
            / "xyz"
            / "fatebot"
            / "status"
        )
        android_api = (android_root / "FateApi.java").read_text(encoding="utf-8")
        scheduler = (android_root / "SyncScheduler.java").read_text(encoding="utf-8")
        android_activity = (android_root / "MainActivity.java").read_text(
            encoding="utf-8"
        )
        shared_panel = (android_root / "SharedControlPanel.java").read_text(
            encoding="utf-8"
        )

        self.assertIn("statusPromise: null", script)
        self.assertIn("consolePromise: null", script)
        self.assertIn("metricsPromise: null", script)
        self.assertIn("if (state.statusPromise) return state.statusPromise", script)
        self.assertIn("if (state.consolePromise) return state.consolePromise", script)
        self.assertIn("if (state.metricsPromise)", script)
        self.assertNotIn("preloadPanelData", script)
        self.assertIn("env(safe-area-inset-bottom) + 68px", styles)
        self.assertIn('route.Equals("/control"', desktop)
        self.assertIn('route.equalsIgnoreCase("/control")', android_api)
        self.assertIn(
            "try (OutputStream output = connection.getOutputStream())", android_api
        )
        self.assertIn("FateConsoleWidgetProvider.class", scheduler)
        self.assertIn("FateControlWidgetProvider.class", scheduler)
        self.assertIn('.put("revision", form.revision)', android_activity)
        self.assertIn("window.FateAndroid.sectionChanged(section)", script)
        self.assertIn("public void sectionChanged(String section)", shared_panel)
        self.assertIn("if (!requestedSection.equals(loadedSection))", shared_panel)
        self.assertIn("min-height: 48px", styles)

    def test_metric_cards_drag_reorder_and_keep_scroll_after_edit(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")
        launcher = (CONTROL_PANEL_ROOT.parent / "Windows" / "Program.cs").read_text(
            encoding="utf-8"
        )

        self.assertIn("function enableMetricCardReordering(card)", script)
        self.assertIn("persistVisibleMetricOrder(grid)", script)
        self.assertIn('dragHandle.className = "metric-drag-handle"', script)
        self.assertIn("metric-card-dragging", styles)
        self.assertIn("metric-card-placeholder", styles)
        self.assertIn('placeholder = document.createElement("div")', script)
        self.assertIn('grid.insertBefore(card, placeholder)', script)
        self.assertIn("window.requestAnimationFrame(updatePlaceholder)", script)
        self.assertIn('candidate.getBoundingClientRect()', script)
        self.assertNotIn('document.elementFromPoint(event.clientX, event.clientY)', script)
        self.assertIn("touch-action: none", styles)
        self.assertIn("event.preventDefault();\n    pointerId = event.pointerId;", script)
        self.assertIn("-webkit-user-select: none", styles)
        self.assertIn("function restoreMetricScroll(position)", script)
        self.assertIn("if (cardAtIndex !== card) grid.insertBefore", script)
        self.assertIn("await loadAllMetrics(true);", script)
        self.assertIn("restoreMetricScroll(scrollPosition);", script)
        self.assertIn(
            ".metric-grid{grid-template-columns:repeat(var(--metric-column-count,3),minmax(0,1fr))!important;}",
            launcher,
        )

    def test_metric_column_density_is_saved_and_desktop_hosted(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")
        page = (CONTROL_PANEL_ROOT / "index.html").read_text(encoding="utf-8")
        launcher = (CONTROL_PANEL_ROOT.parent / "Windows" / "Program.cs").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="metrics-columns"', page)
        for value in ("auto", "2", "3", "4", "5"):
            self.assertIn(f'<option value="{value}">', page)
        self.assertIn('metricColumnStorageKey = "fate-control.metric-columns.v1"', script)
        self.assertIn("function applyMetricColumns", script)
        self.assertIn("--metric-column-count", script)
        self.assertIn("fate-control.metric-columns.v1", launcher)
        self.assertIn("columnOptions=['auto','2','3','4','5']", launcher)
        self.assertIn('[data-active-section="metrics"][data-metric-columns="4"] .app-shell', styles)
        self.assertIn("width: min(1580px, 100%)", styles)
        self.assertIn("width: min(1880px, 100%)", styles)
        self.assertIn("data-active-section='metrics'", launcher)

    def test_metrics_can_be_hidden_and_revealed_in_place(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")
        page = (CONTROL_PANEL_ROOT / "index.html").read_text(encoding="utf-8")
        launcher = (CONTROL_PANEL_ROOT.parent / "Windows" / "Program.cs").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="metrics-show-hidden"', page)
        self.assertIn('id="toggle-metric-hidden"', page)
        self.assertIn("showHiddenMetrics: false", script)
        self.assertIn("state.showHiddenMetrics || !state.metricHidden.has(metric)", script)
        self.assertIn("updateMetricEditorVisibilityControl", script)
        self.assertIn("commitMetricSettings(true)", script)
        self.assertIn("Keep at least one graph visible.", script)
        self.assertIn("metric-card-hidden-preview", styles)
        self.assertIn("metric-hidden-badge", styles)
        self.assertIn("metrics-show-hidden", launcher)
        self.assertIn("metric-editor-visibility", launcher)

    def test_system_metric_combines_ram_network_and_disk_graphs(self):
        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")

        self.assertIn('systemMetricHistoryStorageKey = "fate-control.system-metric-history.v2"', script)
        self.assertIn("function recordHostLoadSample(status)", script)
        self.assertIn("function hostLoadChartPoints(setting)", script)
        self.assertIn("function renderSystemMetricChart(card, points, setting)", script)
        self.assertIn("memory.used_percent", script)
        self.assertIn("network.receive_bytes_per_second", script)
        self.assertIn("network.send_bytes_per_second", script)
        self.assertIn("disk.read_bytes_per_second", script)
        self.assertIn("disk.write_bytes_per_second", script)
        self.assertIn("system-metric-legend", script)
        self.assertIn("system-ram-line", styles)
        self.assertIn("system-network-line", styles)
        self.assertIn("system-disk-line", styles)

    def test_mobile_process_card_does_not_keep_desktop_flex_height(self):
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")
        mobile = styles.split("@media (max-width: 760px)", 1)[1]
        self.assertIn(
            ".control-card > div:first-child { flex: 0 1 auto; width: 100%; }",
            mobile,
        )

    def test_desktop_title_uses_separate_identity_and_action_capsules(self):
        styles = (CONTROL_PANEL_ROOT / "styles.css").read_text(encoding="utf-8")
        launcher = (CONTROL_PANEL_ROOT.parent / "Windows" / "Program.cs").read_text(
            encoding="utf-8"
        )

        self.assertIn(".topbar { justify-content: space-between", styles)
        self.assertIn("border: 0; border-radius: 0; background: transparent", styles)
        self.assertIn(".topbar > div:first-child", styles)
        self.assertIn(".topbar-actions", styles)
        self.assertIn('".topbar{border:0!important;background:transparent!important;', launcher)
        self.assertIn('".topbar>div:first-child{display:block!important;', launcher)
        self.assertIn("border-radius:24px!important", launcher)
        self.assertIn("clip-path:inset(0 round 24px)!important", launcher)
        self.assertIn("max-width:calc(100% - 250px)!important", launcher)

    def test_desktop_launcher_keeps_appearance_query_before_ticket_fragment(self):
        source = (CONTROL_PANEL_ROOT.parent / "Windows" / "Program.cs").read_text(
            encoding="utf-8"
        )

        self.assertIn("int fragmentIndex = launchUrl.IndexOf('#');", source)
        self.assertIn("string baseUrl = fragmentIndex >= 0", source)
        self.assertIn("return baseUrl + separator + appearance + fragment;", source)

    def test_desktop_loopback_launch_uses_local_key_without_overwriting_profile(self):
        source = (CONTROL_PANEL_ROOT.parent / "Windows" / "Program.cs").read_text(
            encoding="utf-8"
        )

        self.assertIn("public string LocalToken() => LegacyToken();", source)
        self.assertIn("ticketPath is null && IsLoopback(profile.Endpoint)", source)
        self.assertIn("store.LocalToken()", source)
        self.assertIn("CreateLaunchTicket(client, profile.Endpoint, localToken)", source)
        self.assertIn("The saved control key was rejected", source)
        self.assertNotIn("store.UpdateActive(profile.Name", source)

    def test_desktop_hides_profile_chooser_while_panel_is_open(self):
        source = (CONTROL_PANEL_ROOT.parent / "Windows" / "Program.cs").read_text(
            encoding="utf-8"
        )

        self.assertIn("if (reachable && panel is not null)", source)
        self.assertIn("panel.FormClosed += (_, _) => Close();", source)
        self.assertIn("panel.Show();", source)
        self.assertIn("Hide();", source)
        self.assertNotIn("profile window remains available", source)

    async def test_native_launch_ticket_creates_one_time_browser_session(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            launch = await controller.create_control_session(self.Request(controller.token))
            launch_payload = json.loads(launch.text)
            ticket = launch_payload["launch_url"].split("ticket=", 1)[1]
            exchange = await controller.exchange_control_session(
                self.Request("", {"ticket": ticket}, method="POST")
            )
            session = exchange.cookies[CONTROL_SESSION_COOKIE].value
            controller.require_auth(
                self.Request("", cookies={CONTROL_SESSION_COOKIE: session})
            )
            with self.assertRaises(web.HTTPUnauthorized):
                await controller.exchange_control_session(
                    self.Request("", {"ticket": ticket}, method="POST")
                )

    async def test_native_launch_ticket_points_to_control_route(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            launch = await controller.create_control_session(self.Request(controller.token))
            launch_payload = json.loads(launch.text)

        self.assertTrue(launch_payload["launch_url"].startswith("/control#ticket="))

    async def test_control_key_login_sets_http_only_session_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            response = await controller.login_control_session(
                self.Request(
                    "",
                    {"control_key": controller.token},
                    method="POST",
                )
            )
        cookie = response.cookies[CONTROL_SESSION_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")

    async def test_control_session_survives_controller_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.controller(root)
            response = await first.login_control_session(
                self.Request(
                    "",
                    {"control_key": first.token},
                    method="POST",
                )
            )
            session = response.cookies[CONTROL_SESSION_COOKIE].value
            restarted = self.controller(root)
            restarted.require_auth(
                self.Request("", cookies={CONTROL_SESSION_COOKIE: session})
            )

    async def test_control_session_rejects_tampered_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            response = await controller.login_control_session(
                self.Request(
                    "",
                    {"control_key": controller.token},
                    method="POST",
                )
            )
            session = response.cookies[CONTROL_SESSION_COOKIE].value
            tampered = session[:-1] + ("0" if session[-1] != "0" else "1")
            with self.assertRaises(web.HTTPUnauthorized):
                controller.require_auth(
                    self.Request("", cookies={CONTROL_SESSION_COOKIE: tampered})
                )

    async def test_control_key_login_rejects_wrong_key(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            with self.assertRaises(web.HTTPUnauthorized):
                await controller.login_control_session(
                    self.Request("", {"control_key": "wrong"}, method="POST")
                )

    async def test_json_endpoints_reject_valid_non_object_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            with self.assertRaises(web.HTTPBadRequest) as raised:
                await controller.bot_action(
                    self.Request(controller.token, ["restart"], method="POST")
                )

        self.assertIn("Expected a JSON object", raised.exception.text)

    async def test_snapshot_scans_for_fate_process_once(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            controller.bot_status = AsyncMock(return_value=None)
            with (
                patch.object(controller, "_bot_process", return_value=None) as finder,
                patch.object(controller, "storage_details", return_value={}),
                patch.object(controller, "system_details", return_value={}),
                patch.object(
                    controller,
                    "health_details",
                    return_value={"summary": "Healthy"},
                ),
            ):
                await controller.snapshot(include_private=True)

        finder.assert_called_once_with()

    async def test_monitor_recovers_after_one_failed_iteration(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            controller._monitor_once = AsyncMock(
                side_effect=[RuntimeError("temporary failure"), None, asyncio.CancelledError()]
            )
            with (
                patch("apps.FateControl.server.asyncio.sleep", AsyncMock()),
                patch("apps.FateControl.server.LOGGER.exception") as logged,
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await controller.monitor()

        self.assertEqual(controller._monitor_once.await_count, 3)
        logged.assert_called_once()
        self.assertIsNone(controller.last_error)

    def controller(self, root: Path) -> FateController:
        return FateController(
            config_path=root / "config.json",
            token_path=root / "control.token",
            bot_log_path=root / "bot.log",
            autostart=False,
        )

    async def test_host_action_requires_exact_instance_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            request = self.Request(
                controller.token,
                {"action": "reboot", "confirm": "REBOOT somebody-else"},
            )
            with self.assertRaises(web.HTTPBadRequest):
                await controller.host_action(request)

    async def test_host_action_rejects_non_string_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            request = self.Request(
                controller.token,
                {"action": "reboot", "confirm": {"host": controller.instance_name}},
            )
            with self.assertRaises(web.HTTPBadRequest):
                await controller.host_action(request)

    async def test_host_action_returns_accepted_result(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            request = self.Request(
                controller.token,
                {"action": "reboot", "confirm": f"REBOOT {controller.instance_name}"},
            )
            with patch.object(
                controller,
                "reboot_host",
                return_value={"accepted": True, "action": "reboot"},
            ):
                response = await controller.host_action(request)
        self.assertEqual(response.status, 202)

    async def test_bot_restart_action_returns_private_status(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            controller.restart_bot = AsyncMock(return_value={"changed": True})
            controller.snapshot = AsyncMock(return_value={"online": False})
            request = self.Request(controller.token, {"action": "restart"})
            response = await controller.bot_action(request)
        self.assertEqual(response.status, 200)
        controller.snapshot.assert_awaited_once_with(include_private=True)

    async def test_status_server_action_requests_listener_only_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            controller.restart_status_server = AsyncMock(
                return_value={"accepted": True, "message": "Fate Status server restarted."}
            )
            request = self.Request(controller.token, {"action": "restart"})
            response = await controller.status_server_action(request)

        self.assertEqual(response.status, 200)
        controller.restart_status_server.assert_awaited_once_with()

    async def test_status_server_restart_waits_for_fate_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = self.controller(root)
            request_path = root / "fate-status-server-16420.restart"
            controller.bot_status = AsyncMock(
                return_value={"capabilities": {"status_server_restart": True}}
            )

            async def acknowledge(_delay):
                request_path.unlink(missing_ok=True)

            with (
                patch.object(controller, "_bot_process", return_value=SimpleNamespace(pid=1234)),
                patch.object(controller, "status_server_restart_path", return_value=request_path),
                patch("apps.FateControl.server.asyncio.sleep", side_effect=acknowledge),
            ):
                result = await controller.restart_status_server()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["action"], "restart_status_server")

    async def test_restart_transfers_verified_external_process_to_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            external = SimpleNamespace(pid=1234)
            with (
                patch.object(controller, "owns_running_process", return_value=False),
                patch.object(controller, "_bot_process", return_value=external),
                patch.object(
                    controller, "_terminate_external_fate", return_value=0
                ) as terminate,
                patch.object(
                    controller,
                    "start_bot",
                    AsyncMock(return_value={"changed": True}),
                ) as start,
            ):
                result = await controller.restart_bot()

        terminate.assert_called_once_with(external)
        start.assert_awaited_once()
        self.assertTrue(result["adopted_external"])
        self.assertTrue(controller.desired_running)

    def test_external_restart_revalidates_the_exact_fate_process(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            process = Mock(pid=1234)
            with patch.object(controller, "_is_fate_process", return_value=False):
                with self.assertRaises(RuntimeError):
                    controller._terminate_external_fate(process)

        process.terminate.assert_not_called()

    async def test_metrics_api_returns_bounded_compatible_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metrics_path = root / "metrics.sqlite3"
            controller = FateController(
                config_path=root / "config.json",
                token_path=root / "control.token",
                bot_log_path=root / "bot.log",
                metrics_path=metrics_path,
                autostart=False,
            )
            clock = [1_800_000_000]
            collector = TelemetryCollector(
                controller.metrics_store, clock=lambda: clock[0]
            )
            collector.increment("commands", 3, dimension="ping")
            collector.increment("commands", 1, dimension="help")
            self.assertTrue(collector.flush())
            request = self.Request(
                controller.token,
                query={"metric": "commands", "window": "7d"},
            )
            with patch("botutils.telemetry.time", return_value=clock[0]):
                response = await controller.get_metrics(request)

        payload = json.loads(response.text)
        self.assertEqual(payload["metric"], "commands")
        self.assertEqual(payload["window"], "7d")
        self.assertLessEqual(len(payload["points"]), 720)
        self.assertEqual(payload["summary"]["total"], 4)
        self.assertEqual(payload["top_commands"][0]["name"], "ping")
        self.assertEqual(payload["top_commands"][0]["count"], 3)

    async def test_command_metrics_route_selects_individual_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = FateController(
                config_path=root / "config.json",
                token_path=root / "control.token",
                bot_log_path=root / "bot.log",
                metrics_path=root / "metrics.sqlite3",
                autostart=False,
            )
            collector = TelemetryCollector(
                controller.metrics_store, clock=lambda: 1_800_000_000
            )
            collector.increment("commands", 2, dimension="ping")
            collector.increment("commands", 7, dimension="help")
            self.assertTrue(collector.flush())
            request = self.Request(
                controller.token,
                query={"window": "1h"},
                match_info={"command": "ping"},
            )
            with patch("botutils.telemetry.time", return_value=1_800_000_000):
                response = await controller.get_command_metrics(request)

        payload = json.loads(response.text)
        self.assertEqual(payload["command"], "ping")
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual(payload["top_commands"][0]["name"], "help")

    async def test_metrics_api_rejects_unknown_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = FateController(
                config_path=root / "config.json",
                token_path=root / "control.token",
                bot_log_path=root / "bot.log",
                metrics_path=root / "metrics.sqlite3",
                autostart=False,
            )
            request = self.Request(
                controller.token,
                query={"metric": "servers", "window": "forever"},
            )
            with self.assertRaises(web.HTTPBadRequest):
                await controller.get_metrics(request)


class FateTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def test_configured_telemetry_path_overrides_datastore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "databases" / "metrics.sqlite3"
            actual = telemetry_path_for_config(
                root / "config.json",
                {
                    "datastore_location": "data",
                    "telemetry_path": str(expected),
                },
                repository_root=root,
            )
        self.assertEqual(actual, expected.resolve())

    def test_every_supported_window_has_at_most_720_points(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory) / "metrics.sqlite3")
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            collector.increment("messages_sent")
            self.assertTrue(collector.flush())
            for window in ("1m", "5m", "15m", "1h", "6h", "12h", "24h", "7d", "2w", "30d"):
                payload = build_metric_payload(
                    store, "messages_sent", window, now=1_800_000_000
                )
                self.assertLessEqual(len(payload["points"]), 720, window)

    def test_two_week_window_uses_two_hour_default_buckets(self):
        self.assertEqual(WINDOWS["2w"], (14 * 24 * 60 * 60, 2 * 60 * 60))

        script = (CONTROL_PANEL_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('"2w": [1209600, 7200]', script)

    def test_custom_bucket_size_is_bounded_and_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory) / "metrics.sqlite3")
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            collector.increment("antispam_triggers")
            self.assertTrue(collector.flush())
            payload = build_metric_payload(
                store,
                "antispam_triggers",
                "1h",
                bucket_seconds=20,
                now=1_800_000_000,
            )

        self.assertEqual(payload["bucket_seconds"], 20)
        self.assertEqual(len(payload["points"]), 180)
        self.assertEqual(payload["summary"]["total"], 1)
        unrestricted = build_metric_payload(
            store,
            "antispam_triggers",
            "1h",
            bucket_seconds=31,
            now=1_800_000_000,
        )
        self.assertEqual(unrestricted["bucket_seconds"], 31)
        self.assertLessEqual(len(unrestricted["points"]), 117)
        ten_second = build_metric_payload(
            store,
            "antispam_triggers",
            "1h",
            bucket_seconds=10,
            now=1_800_000_000,
        )
        self.assertEqual(ten_second["bucket_seconds"], 10)
        self.assertLessEqual(len(ten_second["points"]), 360)

    def test_twelve_hour_window_accepts_sixty_second_buckets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory) / "metrics.sqlite3")
            payload = build_metric_payload(
                store, "mysql_calls", "12h", bucket_seconds=60, now=1_800_000_000
            )
        self.assertEqual(payload["bucket_seconds"], 60)
        self.assertEqual(len(payload["points"]), 720)

    def test_uno_module_metrics_keep_games_and_player_totals_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory) / "metrics.sqlite3")
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            collector.increment("uno_games")
            collector.increment("uno_players", amount=4)
            self.assertTrue(collector.flush())
            games = build_metric_payload(store, "uno_games", "1m", now=1_800_000_000)
            players = build_metric_payload(
                store, "uno_players", "1m", now=1_800_000_000
            )

        self.assertEqual(games["summary"]["total"], 1)
        self.assertEqual(players["summary"]["total"], 4)

    def test_dashboard_signins_are_stored_as_content_free_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            store = TelemetryStore(path)
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            collector.increment("dashboard_signins")
            collector.increment("dashboard_signins")
            self.assertTrue(collector.flush())
            payload = build_metric_payload(
                store, "dashboard_signins", "1m", now=1_800_000_000
            )
            raw = path.read_bytes().lower()

        self.assertEqual(payload["summary"]["total"], 2)
        self.assertNotIn(b"discord", raw)

    def test_logger_events_are_stored_as_content_free_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            store = TelemetryStore(path)
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            collector.increment("logger_events")
            collector.increment("logger_events")
            self.assertTrue(collector.flush())
            payload = build_metric_payload(
                store, "logger_events", "1m", now=1_800_000_000
            )

        self.assertEqual(payload["summary"]["total"], 2)

    def test_module_activity_metrics_are_independent_content_free_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory) / "metrics.sqlite3")
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            expected = {
                "selfrole_activity": 2,
                "welcome_leave_messages": 3,
                "autorole_activity": 4,
            }
            for metric, amount in expected.items():
                collector.increment(metric, amount=amount)
            self.assertTrue(collector.flush())
            payloads = {
                metric: build_metric_payload(store, metric, "1m", now=1_800_000_000)
                for metric in expected
            }

        self.assertEqual(
            {metric: payload["summary"]["total"] for metric, payload in payloads.items()},
            expected,
        )

    def test_history_pruning_keeps_one_year(self):
        now = 1_800_000_000
        clock = [now - (364 * 24 * 60 * 60)]
        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory) / "metrics.sqlite3")
            collector = TelemetryCollector(store, clock=lambda: clock[0])
            collector.gauge("servers", 364)
            self.assertTrue(collector.flush())
            clock[0] = now - (366 * 24 * 60 * 60)
            collector.gauge("servers", 366)
            self.assertTrue(collector.flush())
            store.prune(now=now)
            rows = store.read_rows(
                "servers", start=0, end=now, history=True
            )

        self.assertEqual([row[3] for row in rows], [364])

    def test_unavailable_store_returns_stable_empty_schema(self):
        class UnavailableStore:
            def read_rows(self, *_args, **_kwargs):
                raise TelemetryUnavailable("offline")

        payload = build_metric_payload(
            UnavailableStore(), "servers", "1h", now=1_800_000_000
        )
        self.assertFalse(payload["available"])
        self.assertEqual(payload["points"], [])
        self.assertEqual(payload["summary"]["current"], 0)

    def test_gauges_show_per_bucket_differences_not_population_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1_800_000_000]
            store = TelemetryStore(Path(directory) / "metrics.sqlite3")
            collector = TelemetryCollector(store, clock=lambda: clock[0])
            collector.gauge("servers", 5)
            self.assertTrue(collector.flush())
            clock[0] += 10
            collector.gauge("servers", 7)
            self.assertTrue(collector.flush())
            payload = build_metric_payload(
                store, "servers", "1m", now=clock[0] + 4
            )

        self.assertEqual(payload["summary"]["current"], 2)
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual([point["value"] for point in payload["points"]], [0, 0, 2])
        self.assertTrue(payload["available"])

    def test_active_server_gauge_shows_the_latest_rolling_total(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1_800_000_000]
            store = TelemetryStore(Path(directory) / "metrics.sqlite3")
            collector = TelemetryCollector(store, clock=lambda: clock[0])
            collector.gauge("active_servers", 18)
            self.assertTrue(collector.flush())
            clock[0] += 10
            collector.gauge("active_servers", 21)
            self.assertTrue(collector.flush())
            payload = build_metric_payload(
                store, "active_servers", "1m", now=clock[0] + 4
            )

        self.assertEqual(payload["summary"]["current"], 21)
        self.assertEqual(
            [point["value"] for point in payload["points"]],
            [18, 18, 21],
        )
        self.assertTrue(payload["available"])

    def test_store_persists_only_registered_command_labels_and_numeric_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            store = TelemetryStore(path)
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            collector.increment("commands", dimension="  BAN  ")
            collector.increment("mysql_calls")
            self.assertTrue(collector.flush())
            payload = build_metric_payload(
                store, "commands", "1m", now=1_800_000_000
            )

        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["top_commands"][0]["name"], "ban")

    def test_rate_limit_metrics_preserve_channel_and_message_route_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            store = TelemetryStore(path)
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            route = "DELETE /channels/123456/messages/987654"
            collector.increment("discord_rate_limits", 2, dimension=route)
            collector.increment(
                "discord_rate_limits",
                dimension="POST /channels/123456/messages",
            )
            collector.increment(
                "discord_rate_limits",
                dimension="POST /webhooks/:id/secret-token",
            )
            self.assertTrue(collector.flush())
            payload = build_metric_payload(
                store, "discord_rate_limits", "1m", now=1_800_000_000
            )

        self.assertEqual(payload["summary"]["total"], 3)
        self.assertEqual(payload["top_discord_routes"][0]["route"], route)
        self.assertEqual(payload["top_discord_routes"][0]["count"], 2)
        self.assertEqual(payload["top_discord_routes"][0]["share"], 66.67)
        self.assertNotIn(
            "secret-token",
            [item["route"] for item in payload["top_discord_routes"]],
        )

    def test_mongo_listener_ignores_driver_housekeeping(self):
        class Event:
            def __init__(self, name):
                self.command_name = name

        with tempfile.TemporaryDirectory() as directory:
            store = TelemetryStore(Path(directory) / "metrics.sqlite3")
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            listener = MongoTelemetryListener(collector)
            listener.started(Event("hello"))
            listener.started(Event("find"))
            self.assertTrue(collector.flush())
            payload = build_metric_payload(
                store, "mongo_calls", "1m", now=1_800_000_000
            )

        self.assertEqual(payload["summary"]["total"], 1)

    async def test_mysql_pool_proxy_counts_execute_without_retaining_sql(self):
        class Cursor:
            async def execute(self, *_args, **_kwargs):
                return 1

        class CursorContext:
            def __init__(self):
                self.cursor = Cursor()

            async def get(self):
                return self.cursor

            def __await__(self):
                return self.get().__await__()

            async def __aenter__(self):
                return self.cursor

            async def __aexit__(self, *_args):
                return None

        class Connection:
            def cursor(self):
                return CursorContext()

        class AcquireContext:
            def __init__(self, connection):
                self.connection = connection

            async def get(self):
                return self.connection

            def __await__(self):
                return self.get().__await__()

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return None

        class Pool:
            def __init__(self):
                self.connection = Connection()
                self.released = None

            def acquire(self):
                return AcquireContext(self.connection)

            def release(self, connection):
                self.released = connection

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            store = TelemetryStore(path)
            collector = TelemetryCollector(store, clock=lambda: 1_800_000_000)
            raw_pool = Pool()
            pool = instrument_mysql_pool(raw_pool, collector)
            connection = await pool.acquire()
            cursor = await connection.cursor()
            await cursor.execute("SELECT top_secret FROM private_table")
            pool.release(connection)
            self.assertTrue(collector.flush())
            payload = build_metric_payload(
                store, "mysql_calls", "1m", now=1_800_000_000
            )
            raw = path.read_bytes().lower()

        self.assertIs(raw_pool.released, raw_pool.connection)
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertNotIn(b"top_secret", raw)


if __name__ == "__main__":
    unittest.main()
