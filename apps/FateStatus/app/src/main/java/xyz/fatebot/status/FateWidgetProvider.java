package xyz.fatebot.status;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.View;
import android.widget.RemoteViews;

import java.text.DecimalFormat;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class FateWidgetProvider extends AppWidgetProvider {
    static final String ACTION_REFRESH = "xyz.fatebot.status.REFRESH";
    static final String ACTION_SCAN = "xyz.fatebot.status.SCAN";
    static final String ACTION_SCHEDULED = "xyz.fatebot.status.SCHEDULED";
    static final String PREFS = "fate_status";
    static final String PREF_ENDPOINT = "endpoint";
    private static final String PREF_CACHE_PROFILE = "cache_profile";
    private static final String PREF_CACHE_ENDPOINT = "cache_endpoint";
    static final String PREF_CONTROL_KEY = "control_key";
    static final String PREF_METRIC_SERVERS = "widget_metric_servers";
    static final String PREF_METRIC_USERS = "widget_metric_users";
    static final String PREF_METRIC_PING = "widget_metric_ping";
    static final String PREF_METRIC_SHARDS = "widget_metric_shards";
    static final String PREF_METRIC_COMMANDS = "widget_metric_commands";
    static final String PREF_METRIC_CPU = "widget_metric_cpu";
    static final String PREF_METRIC_MEMORY = "widget_metric_memory";
    static final String PREF_METRIC_STORAGE = "widget_metric_storage";
    static final String PREF_METRIC_NETWORK = "widget_metric_network";
    static final String PREF_METRIC_RESTARTS = "widget_metric_restarts";
    static final String DEFAULT_ENDPOINT = "http://144.76.105.17:16420";

    private static final String[] METRICS = {
        "servers", "users", "ping", "shards", "commands",
        "cpu", "memory", "storage", "network", "restarts"
    };
    private static final int[] METRIC_PANELS = {
        R.id.metric_1_panel, R.id.metric_2_panel, R.id.metric_3_panel,
        R.id.metric_4_panel, R.id.metric_5_panel
    };
    private static final int[] METRIC_VALUES = {
        R.id.metric_1_value, R.id.metric_2_value, R.id.metric_3_value,
        R.id.metric_4_value, R.id.metric_5_value
    };
    private static final int[] METRIC_LABELS = {
        R.id.metric_1_label, R.id.metric_2_label, R.id.metric_3_label,
        R.id.metric_4_label, R.id.metric_5_label
    };

    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] appWidgetIds) {
        refresh(context, manager, appWidgetIds, false);
        SyncScheduler.schedule(context);
    }

    @Override
    public void onAppWidgetOptionsChanged(
        Context context,
        AppWidgetManager manager,
        int appWidgetId,
        Bundle newOptions
    ) {
        render(context, manager, appWidgetId, cachedStatus(context), false);
        refresh(context, manager, new int[]{appWidgetId}, false);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        String action = intent.getAction();
        if (ACTION_REFRESH.equals(action)) {
            refreshAll(context);
        } else if (ACTION_SCAN.equals(action)) {
            scanAll(context);
        } else if (ACTION_SCHEDULED.equals(action) || Intent.ACTION_BOOT_COMPLETED.equals(action)) {
            // The graph widget owns its own alarm and boot receiver, so avoid
            // issuing the same authenticated metrics request twice here.
            refreshAll(context, false);
            SyncScheduler.schedule(context);
        }
    }

    static void refreshAll(Context context) {
        refreshAll(context, true);
    }

    private static void refreshAll(Context context, boolean includeMetricsGraph) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        ComponentName provider = new ComponentName(context, FateWidgetProvider.class);
        refresh(context, manager, manager.getAppWidgetIds(provider), false);
        FateMemoryWidgetProvider.refreshAll(context);
        FateConsoleWidgetProvider.refreshAll(context);
        FateControlWidgetProvider.refreshAll(context);
        if (includeMetricsGraph) MetricsGraphWidgetProvider.refreshAll(context);
    }

    static void scanAll(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        ComponentName provider = new ComponentName(context, FateWidgetProvider.class);
        refresh(context, manager, manager.getAppWidgetIds(provider), true);
    }

    private static void refresh(
        Context context,
        AppWidgetManager manager,
        int[] ids,
        boolean forceScan
    ) {
        if (ids == null || ids.length == 0) {
            return;
        }
        Context appContext = context.getApplicationContext();
        BotProfileStore.Profile profile = BotProfileStore.active(appContext);
        for (int id : ids) {
            renderLoading(appContext, manager, id, forceScan);
        }
        EXECUTOR.execute(() -> {
            BotProfileStore.Profile requestProfile = profile;
            StatusData status = null;
            boolean failed = false;
            String endpoint = profile.endpoint;
            try {
                if (forceScan) {
                    endpoint = discoverEndpoint();
                    if (!BotProfileStore.isActive(appContext, requestProfile)) return;
                    BotProfileStore.updateActiveEndpoint(appContext, endpoint);
                    requestProfile = BotProfileStore.active(appContext);
                }
                status = fetchStatus(appContext, endpoint, requestProfile.id);
                if (!BotProfileStore.isActive(appContext, requestProfile)) return;
                saveStatus(appContext, status, requestProfile);
            } catch (Exception firstError) {
                if (!BotProfileStore.isActive(appContext, requestProfile)) return;
                // Named profiles are authoritative. A background LAN scan must never replace
                // their saved address or silently show data from a different local bot.
                failed = true;
                status = cachedStatus(appContext);
            }
            if (!BotProfileStore.isActive(appContext, requestProfile)) return;
            for (int id : ids) {
                render(appContext, manager, id, status, failed);
            }
            SyncScheduler.schedule(appContext);
        });
    }

    private static String discoverEndpoint() throws Exception {
        List<NetworkScanner.Device> devices = NetworkScanner.scan(null);
        if (devices.isEmpty()) {
            throw new IllegalStateException("No Fate devices found");
        }
        return devices.get(0).endpoint;
    }

    static StatusData fetchStatus(String endpoint) throws Exception {
        return FateApi.publicStatus(endpoint);
    }

    static StatusData fetchStatus(Context context, String endpoint) throws Exception {
        return FateApi.status(endpoint, ControlKeyStore.read(context));
    }

    static StatusData fetchStatus(Context context, String endpoint, String profileId) throws Exception {
        return FateApi.status(endpoint, ControlKeyStore.read(context, profileId));
    }

    static void saveStatus(Context context, StatusData status) {
        saveStatus(context, status, BotProfileStore.active(context));
    }

    static void saveStatus(Context context, StatusData status, BotProfileStore.Profile profile) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(PREF_CACHE_PROFILE, profile.id)
            .putString(PREF_CACHE_ENDPOINT, profile.endpoint)
            .putBoolean("cache_online", status.online)
            .putLong("cache_servers", status.servers)
            .putLong("cache_users", status.users)
            .putInt("cache_shards", status.shards)
            .putInt("cache_latency", status.latencyMs == null ? -1 : status.latencyMs)
            .putLong("cache_commands_month", status.commandsThisMonth)
            .putLong("cache_uptime", status.uptimeSeconds)
            .putLong("cache_fetched", status.fetchedAt)
            .putString("cache_service", status.service)
            .putString("cache_instance", status.instanceName)
            .putBoolean("cache_controller", status.controllerOnline)
            .putBoolean("cache_process", status.processRunning)
            .putBoolean("cache_desired", status.desiredRunning)
            .putBoolean("cache_owned", status.processOwned)
            .putString("cache_health", status.healthSummary)
            .putString("cache_config_health", status.configHealth)
            .putLong("cache_disk_bits", Double.doubleToRawLongBits(status.diskFreeGb))
            .putInt("cache_errors", status.recentErrors)
            .putLong("cache_memory_total", status.memoryTotalBytes)
            .putLong("cache_memory_used", status.memoryUsedBytes)
            .putLong("cache_memory_available", status.memoryAvailableBytes)
            .putLong("cache_memory_percent_bits", Double.doubleToRawLongBits(status.memoryUsedPercent))
            .putString("cache_memory_breakdown", StatusData.encodeBreakdown(status.memoryBreakdown))
            .putLong("cache_bot_memory", status.botMemoryBytes)
            .putString("cache_bot_memory_breakdown", StatusData.encodeBreakdown(status.botMemoryBreakdown))
            .putLong("cache_storage_total", status.storageTotalBytes)
            .putLong("cache_storage_used", status.storageUsedBytes)
            .putLong("cache_storage_free", status.storageFreeBytes)
            .putLong("cache_fate_storage", status.fateStorageBytes)
            .putLong("cache_storage_percent_bits", Double.doubleToRawLongBits(status.storageUsedPercent))
            .putString("cache_storage_breakdown", StatusData.encodeBreakdown(status.storageBreakdown))
            .putLong("cache_controller_uptime", status.controllerUptimeSeconds)
            .putBoolean("cache_system_available", status.system.available)
            .putString("cache_hostname", status.system.hostname)
            .putString("cache_os_name", status.system.osName)
            .putString("cache_os_version", status.system.osVersion)
            .putString("cache_architecture", status.system.architecture)
            .putString("cache_python", status.system.pythonVersion)
            .putString("cache_addresses", status.system.addresses)
            .putLong("cache_host_uptime", status.system.uptimeSeconds)
            .putInt("cache_process_count", status.system.processCount)
            .putLong("cache_cpu_bits", Double.doubleToRawLongBits(status.system.cpuPercent))
            .putInt("cache_logical_cores", status.system.logicalCores)
            .putInt("cache_physical_cores", status.system.physicalCores)
            .putLong("cache_frequency_bits", Double.doubleToRawLongBits(
                status.system.frequencyMhz == null ? -1 : status.system.frequencyMhz
            ))
            .putString("cache_load_average", status.system.loadAverage)
            .putLong("cache_net_received", status.system.networkReceivedBytes)
            .putLong("cache_net_sent", status.system.networkSentBytes)
            .putLong("cache_net_receive_rate", status.system.networkReceiveRate)
            .putLong("cache_net_send_rate", status.system.networkSendRate)
            .putLong("cache_disk_read", status.system.diskReadBytes)
            .putLong("cache_disk_written", status.system.diskWrittenBytes)
            .putLong("cache_disk_read_rate", status.system.diskReadRate)
            .putLong("cache_disk_write_rate", status.system.diskWriteRate)
            .putLong("cache_temperature_bits", Double.doubleToRawLongBits(
                status.system.temperatureCelsius == null ? -999 : status.system.temperatureCelsius
            ))
            .putBoolean("cache_reboot_allowed", status.system.rebootAllowed)
            .putInt("cache_runtime_pid", status.runtime.pid == null ? -1 : status.runtime.pid)
            .putLong("cache_runtime_cpu_bits", Double.doubleToRawLongBits(status.runtime.cpuPercent))
            .putInt("cache_runtime_threads", status.runtime.threadCount)
            .putString("cache_runtime_started", status.runtime.startedAt)
            .putString("cache_runtime_state", status.runtime.state)
            .putInt("cache_restart_count", status.runtime.restartCount)
            .putInt("cache_last_exit", status.runtime.lastExitCode == null ? Integer.MIN_VALUE : status.runtime.lastExitCode)
            .putString("cache_last_error", status.runtime.lastError)
            .putString("cache_discord_health", status.components.discord)
            .putString("cache_database_health", status.components.database)
            .putString("cache_disk_health", status.components.disk)
            .putInt("cache_extensions_loaded", status.components.extensionsLoaded)
            .putInt("cache_extensions_configured", status.components.extensionsConfigured)
            .putInt("cache_command_count", status.components.commands)
            .apply();
    }

    static StatusData cachedStatus(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        BotProfileStore.Profile profile = BotProfileStore.active(context);
        if (!profile.id.equals(prefs.getString(PREF_CACHE_PROFILE, ""))
            || !profile.endpoint.equals(prefs.getString(PREF_CACHE_ENDPOINT, ""))) {
            return null;
        }
        int latency = prefs.getInt("cache_latency", -1);
        double frequency = Double.longBitsToDouble(prefs.getLong(
            "cache_frequency_bits", Double.doubleToRawLongBits(-1)
        ));
        double temperature = Double.longBitsToDouble(prefs.getLong(
            "cache_temperature_bits", Double.doubleToRawLongBits(-999)
        ));
        int runtimePid = prefs.getInt("cache_runtime_pid", -1);
        int lastExit = prefs.getInt("cache_last_exit", Integer.MIN_VALUE);
        return new StatusData(
            prefs.getBoolean("cache_online", false),
            prefs.getLong("cache_servers", 0),
            prefs.getLong("cache_users", 0),
            Math.max(1, prefs.getInt("cache_shards", 1)),
            latency < 0 ? null : latency,
            prefs.getLong("cache_commands_month", 0),
            prefs.getLong("cache_uptime", 0),
            prefs.getLong("cache_fetched", 0),
            prefs.getString("cache_service", "fate-bot"),
            prefs.getString("cache_instance", "Fate"),
            prefs.getBoolean("cache_controller", false),
            prefs.getBoolean("cache_process", false),
            prefs.getBoolean("cache_desired", false),
            prefs.getBoolean("cache_owned", false),
            prefs.getString("cache_health", "No health data yet"),
            prefs.getString("cache_config_health", "unknown"),
            Double.longBitsToDouble(prefs.getLong("cache_disk_bits", Double.doubleToRawLongBits(-1))),
            prefs.getInt("cache_errors", 0),
            prefs.getLong("cache_memory_total", 0),
            prefs.getLong("cache_memory_used", 0),
            prefs.getLong("cache_memory_available", 0),
            Double.longBitsToDouble(prefs.getLong("cache_memory_percent_bits", 0)),
            StatusData.decodeBreakdown(prefs.getString("cache_memory_breakdown", "")),
            prefs.getLong("cache_bot_memory", 0),
            StatusData.decodeBreakdown(prefs.getString("cache_bot_memory_breakdown", "")),
            prefs.getLong("cache_storage_total", 0),
            prefs.getLong("cache_storage_used", 0),
            prefs.getLong("cache_storage_free", 0),
            prefs.getLong("cache_fate_storage", 0),
            Double.longBitsToDouble(prefs.getLong("cache_storage_percent_bits", 0)),
            StatusData.decodeBreakdown(prefs.getString("cache_storage_breakdown", "")),
            prefs.getLong("cache_controller_uptime", 0),
            new StatusData.SystemInfo(
                prefs.getBoolean("cache_system_available", false),
                prefs.getString("cache_hostname", ""),
                prefs.getString("cache_os_name", ""),
                prefs.getString("cache_os_version", ""),
                prefs.getString("cache_architecture", ""),
                prefs.getString("cache_python", ""),
                prefs.getString("cache_addresses", ""),
                prefs.getLong("cache_host_uptime", 0),
                prefs.getInt("cache_process_count", 0),
                Double.longBitsToDouble(prefs.getLong("cache_cpu_bits", 0)),
                prefs.getInt("cache_logical_cores", 0),
                prefs.getInt("cache_physical_cores", 0),
                frequency < 0 ? null : frequency,
                prefs.getString("cache_load_average", ""),
                prefs.getLong("cache_net_received", 0),
                prefs.getLong("cache_net_sent", 0),
                prefs.getLong("cache_net_receive_rate", 0),
                prefs.getLong("cache_net_send_rate", 0),
                prefs.getLong("cache_disk_read", 0),
                prefs.getLong("cache_disk_written", 0),
                prefs.getLong("cache_disk_read_rate", 0),
                prefs.getLong("cache_disk_write_rate", 0),
                temperature <= -999 ? null : temperature,
                prefs.getBoolean("cache_reboot_allowed", false)
            ),
            new StatusData.RuntimeInfo(
                runtimePid < 0 ? null : runtimePid,
                Double.longBitsToDouble(prefs.getLong("cache_runtime_cpu_bits", 0)),
                prefs.getInt("cache_runtime_threads", 0),
                prefs.getString("cache_runtime_started", ""),
                prefs.getString("cache_runtime_state", "unknown"),
                prefs.getInt("cache_restart_count", 0),
                lastExit == Integer.MIN_VALUE ? null : lastExit,
                prefs.getString("cache_last_error", "")
            ),
            new StatusData.ComponentHealth(
                prefs.getString("cache_discord_health", "unknown"),
                prefs.getString("cache_database_health", "unknown"),
                prefs.getString("cache_disk_health", "unknown"),
                prefs.getInt("cache_extensions_loaded", -1),
                prefs.getInt("cache_extensions_configured", -1),
                prefs.getInt("cache_command_count", -1)
            )
        );
    }

    private static void renderLoading(
        Context context,
        AppWidgetManager manager,
        int id,
        boolean scanning
    ) {
        StatusData cached = cachedStatus(context);
        boolean compact = isCompact(manager, id);
        RemoteViews views = createViews(context, compact);
        applyTheme(context, views, compact);
        bindActions(context, views, id);
        views.setTextViewText(
            R.id.instance_text,
            BotProfileStore.active(context).name.toUpperCase(Locale.ROOT)
        );
        views.setTextViewText(R.id.status_text, scanning ? "SCANNING LAN" : "SYNCING");
        views.setTextColor(R.id.status_text, ThemeManager.palette(context).secondary);
        bindMetrics(context, views, cached, compact);
        views.setTextViewText(R.id.updated_text, scanning ? "Looking for Fate devices…" : "Refreshing…");
        manager.updateAppWidget(id, views);
    }

    private static void render(
        Context context,
        AppWidgetManager manager,
        int id,
        StatusData status,
        boolean failed
    ) {
        boolean compact = isCompact(manager, id);
        RemoteViews views = createViews(context, compact);
        applyTheme(context, views, compact);
        bindActions(context, views, id);
        boolean online = status != null && status.online && !failed;
        boolean controllerReachable = status != null && status.controllerOnline && !failed;
        String state = online ? "BOT ONLINE" : controllerReachable ? "BOT OFF" : "OFFLINE";
        views.setTextViewText(R.id.status_text, state);
        ThemeManager.Palette palette = ThemeManager.palette(context);
        views.setTextColor(
            R.id.status_text,
            online ? palette.green : controllerReachable ? palette.secondary : palette.red
        );
        bindMetrics(context, views, status, compact);
        BotProfileStore.Profile profile = BotProfileStore.active(context);
        views.setTextViewText(R.id.instance_text, profile.name.toUpperCase(Locale.ROOT));
        String health = status == null ? "No health data" : status.healthSummary;
        views.setTextViewText(R.id.health_text, health);
        String updated = status == null || status.fetchedAt == 0
            ? "No data yet"
            : (failed ? "Refresh failed • " : "Updated ") + formatTime(status.fetchedAt);
        views.setTextViewText(R.id.updated_text, updated);
        manager.updateAppWidget(id, views);
    }

    private static boolean isCompact(AppWidgetManager manager, int id) {
        Bundle options = manager.getAppWidgetOptions(id);
        int width = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 250);
        int height = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 110);
        return width < 240 || height < 105;
    }

    private static RemoteViews createViews(Context context, boolean compact) {
        return new RemoteViews(context.getPackageName(), compact ? R.layout.widget_fate_compact : R.layout.widget_fate);
    }

    private static void bindActions(Context context, RemoteViews views, int id) {
        Intent refreshIntent = new Intent(context, FateWidgetProvider.class).setAction(ACTION_REFRESH);
        PendingIntent refresh = PendingIntent.getBroadcast(
            context,
            id,
            refreshIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.refresh_button, refresh);

        Intent settingsIntent = new Intent(context, MainActivity.class)
            .putExtra(MainActivity.EXTRA_OPEN_PAGE, MainActivity.PAGE_SETTINGS);
        PendingIntent settings = PendingIntent.getActivity(
            context,
            30_000 + id,
            settingsIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.widget_settings_button, settings);

        Intent scanIntent = new Intent(context, FateWidgetProvider.class).setAction(ACTION_SCAN);
        PendingIntent scan = PendingIntent.getBroadcast(
            context,
            10_000 + id,
            scanIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.scan_button, scan);

        Intent openIntent = new Intent(context, MainActivity.class);
        PendingIntent open = PendingIntent.getActivity(
            context,
            0,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.widget_root, open);
    }

    private static void bindMetrics(Context context, RemoteViews views, StatusData status, boolean compact) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        List<String> selected = new ArrayList<>();
        if (prefs.getBoolean(PREF_METRIC_SERVERS, true)) selected.add(METRICS[0]);
        if (prefs.getBoolean(PREF_METRIC_USERS, true)) selected.add(METRICS[1]);
        if (prefs.getBoolean(PREF_METRIC_PING, true)) selected.add(METRICS[2]);
        if (prefs.getBoolean(PREF_METRIC_SHARDS, true)) selected.add(METRICS[3]);
        if (prefs.getBoolean(PREF_METRIC_COMMANDS, true)) selected.add(METRICS[4]);
        if (prefs.getBoolean(PREF_METRIC_CPU, false)) selected.add(METRICS[5]);
        if (prefs.getBoolean(PREF_METRIC_MEMORY, false)) selected.add(METRICS[6]);
        if (prefs.getBoolean(PREF_METRIC_STORAGE, false)) selected.add(METRICS[7]);
        if (prefs.getBoolean(PREF_METRIC_NETWORK, false)) selected.add(METRICS[8]);
        if (prefs.getBoolean(PREF_METRIC_RESTARTS, false)) selected.add(METRICS[9]);
        if (selected.isEmpty()) selected.add(METRICS[0]);

        int capacity = compact ? 2 : 5;
        for (int index = 0; index < capacity; index++) {
            boolean visible = index < selected.size();
            views.setViewVisibility(METRIC_PANELS[index], visible ? View.VISIBLE : View.GONE);
            if (!visible) continue;
            String metric = selected.get(index);
            views.setTextViewText(METRIC_VALUES[index], metricValue(metric, status));
            views.setTextViewText(METRIC_LABELS[index], metricLabel(metric));
        }
        views.setTextViewText(
            R.id.uptime_text,
            status == null ? "Uptime —" : "Uptime " + formatUptime(status.uptimeSeconds)
        );
    }

    private static String metricValue(String metric, StatusData status) {
        if (status == null) return "—";
        switch (metric) {
            case "servers": return compactNumber(status.servers);
            case "users": return compactNumber(status.users);
            case "ping": return status.latencyMs == null ? "—" : status.latencyMs + "ms";
            case "shards": return String.valueOf(status.shards);
            case "commands": return compactNumber(status.commandsThisMonth);
            case "cpu": return status.system.available
                ? new DecimalFormat("0.#").format(status.system.cpuPercent) + "%" : "—";
            case "memory": return status.memoryTotalBytes > 0
                ? new DecimalFormat("0.#").format(status.memoryUsedPercent) + "%" : "—";
            case "storage": return status.storageTotalBytes > 0
                ? new DecimalFormat("0.#").format(status.storageUsedPercent) + "%" : "—";
            case "network": return status.system.available
                ? compactRate(status.system.networkReceiveRate + status.system.networkSendRate) : "—";
            case "restarts": return String.valueOf(status.runtime.restartCount);
            default: return "—";
        }
    }

    private static String metricLabel(String metric) {
        switch (metric) {
            case "servers": return "SERVERS";
            case "users": return "USERS";
            case "ping": return "PING";
            case "shards": return "SHARDS";
            case "commands": return "COMMANDS";
            case "cpu": return "CPU";
            case "memory": return "RAM";
            case "storage": return "DISK";
            case "network": return "NET";
            case "restarts": return "RESTARTS";
            default: return metric.toUpperCase(Locale.ROOT);
        }
    }

    private static String compactNumber(long value) {
        if (value < 1000) return String.valueOf(value);
        double divisor = value >= 1_000_000 ? 1_000_000d : 1_000d;
        String suffix = value >= 1_000_000 ? "M" : "K";
        return new DecimalFormat(value / divisor >= 100 ? "0" : "0.#").format(value / divisor) + suffix;
    }

    private static String compactRate(long bytes) {
        if (bytes < 1024) return bytes + " B/s";
        if (bytes < 1024 * 1024) return new DecimalFormat("0.#").format(bytes / 1024d) + " K/s";
        return new DecimalFormat("0.#").format(bytes / (1024d * 1024d)) + " M/s";
    }

    private static void applyTheme(Context context, RemoteViews views, boolean compact) {
        ThemeManager.Palette palette = ThemeManager.palette(context);
        ThemeManager.applyWidgetRoot(context, views, R.id.widget_root);
        views.setTextColor(R.id.instance_text, palette.text);
        views.setTextColor(R.id.health_text, palette.muted);
        views.setTextColor(R.id.widget_settings_button, palette.muted);
        views.setTextColor(R.id.scan_button, palette.secondary);
        views.setTextColor(R.id.refresh_button, palette.text);
        if (!compact) {
            views.setTextColor(R.id.uptime_text, palette.muted);
            views.setTextColor(R.id.updated_text, palette.muted);
        }
        int capacity = compact ? 2 : 5;
        for (int index = 0; index < capacity; index++) {
            views.setInt(METRIC_PANELS[index], "setBackgroundResource", palette.widgetPanel);
            views.setTextColor(METRIC_VALUES[index], palette.text);
            views.setTextColor(METRIC_LABELS[index], palette.muted);
        }
    }

    private static String formatUptime(long seconds) {
        long days = seconds / 86400;
        long hours = (seconds % 86400) / 3600;
        long minutes = (seconds % 3600) / 60;
        if (days > 0) return days + "d " + hours + "h";
        if (hours > 0) return hours + "h " + minutes + "m";
        return minutes + "m";
    }

    private static String formatTime(long timestamp) {
        return new SimpleDateFormat("h:mm a", Locale.getDefault()).format(new Date(timestamp));
    }
}
