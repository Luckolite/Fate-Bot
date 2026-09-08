package xyz.fatebot.status;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

final class StatusData {
    static final class ResourceSlice {
        final String label;
        final long bytes;

        ResourceSlice(String label, long bytes) {
            this.label = label;
            this.bytes = bytes;
        }
    }

    static final class SystemInfo {
        final boolean available;
        final String hostname;
        final String osName;
        final String osVersion;
        final String architecture;
        final String pythonVersion;
        final String addresses;
        final long uptimeSeconds;
        final int processCount;
        final double cpuPercent;
        final int logicalCores;
        final int physicalCores;
        final Double frequencyMhz;
        final String loadAverage;
        final long networkReceivedBytes;
        final long networkSentBytes;
        final long networkReceiveRate;
        final long networkSendRate;
        final long diskReadBytes;
        final long diskWrittenBytes;
        final long diskReadRate;
        final long diskWriteRate;
        final Double temperatureCelsius;
        final boolean rebootAllowed;

        SystemInfo(
            boolean available,
            String hostname,
            String osName,
            String osVersion,
            String architecture,
            String pythonVersion,
            String addresses,
            long uptimeSeconds,
            int processCount,
            double cpuPercent,
            int logicalCores,
            int physicalCores,
            Double frequencyMhz,
            String loadAverage,
            long networkReceivedBytes,
            long networkSentBytes,
            long networkReceiveRate,
            long networkSendRate,
            long diskReadBytes,
            long diskWrittenBytes,
            long diskReadRate,
            long diskWriteRate,
            Double temperatureCelsius,
            boolean rebootAllowed
        ) {
            this.available = available;
            this.hostname = hostname;
            this.osName = osName;
            this.osVersion = osVersion;
            this.architecture = architecture;
            this.pythonVersion = pythonVersion;
            this.addresses = addresses;
            this.uptimeSeconds = uptimeSeconds;
            this.processCount = processCount;
            this.cpuPercent = cpuPercent;
            this.logicalCores = logicalCores;
            this.physicalCores = physicalCores;
            this.frequencyMhz = frequencyMhz;
            this.loadAverage = loadAverage;
            this.networkReceivedBytes = networkReceivedBytes;
            this.networkSentBytes = networkSentBytes;
            this.networkReceiveRate = networkReceiveRate;
            this.networkSendRate = networkSendRate;
            this.diskReadBytes = diskReadBytes;
            this.diskWrittenBytes = diskWrittenBytes;
            this.diskReadRate = diskReadRate;
            this.diskWriteRate = diskWriteRate;
            this.temperatureCelsius = temperatureCelsius;
            this.rebootAllowed = rebootAllowed;
        }

        static SystemInfo empty() {
            return new SystemInfo(
                false, "", "", "", "", "", "", 0, 0, 0,
                0, 0, null, "", 0, 0, 0, 0, 0, 0, 0, 0, null, false
            );
        }
    }

    static final class RuntimeInfo {
        final Integer pid;
        final double cpuPercent;
        final int threadCount;
        final String startedAt;
        final String state;
        final int restartCount;
        final Integer lastExitCode;
        final String lastError;

        RuntimeInfo(
            Integer pid,
            double cpuPercent,
            int threadCount,
            String startedAt,
            String state,
            int restartCount,
            Integer lastExitCode,
            String lastError
        ) {
            this.pid = pid;
            this.cpuPercent = cpuPercent;
            this.threadCount = threadCount;
            this.startedAt = startedAt;
            this.state = state;
            this.restartCount = restartCount;
            this.lastExitCode = lastExitCode;
            this.lastError = lastError;
        }

        static RuntimeInfo empty() {
            return new RuntimeInfo(null, 0, 0, "", "unknown", 0, null, "");
        }
    }

    static final class ComponentHealth {
        final String discord;
        final String database;
        final String disk;
        final int extensionsLoaded;
        final int extensionsConfigured;
        final int commands;

        ComponentHealth(
            String discord,
            String database,
            String disk,
            int extensionsLoaded,
            int extensionsConfigured,
            int commands
        ) {
            this.discord = discord;
            this.database = database;
            this.disk = disk;
            this.extensionsLoaded = extensionsLoaded;
            this.extensionsConfigured = extensionsConfigured;
            this.commands = commands;
        }

        static ComponentHealth empty() {
            return new ComponentHealth("unknown", "unknown", "unknown", -1, -1, -1);
        }
    }

    final boolean online;
    final long servers;
    final long users;
    final int shards;
    final Integer latencyMs;
    final long commandsThisMonth;
    final long uptimeSeconds;
    final long fetchedAt;
    final String service;
    final String instanceName;
    final boolean controllerOnline;
    final boolean processRunning;
    final boolean desiredRunning;
    final boolean processOwned;
    final String healthSummary;
    final String configHealth;
    final double diskFreeGb;
    final int recentErrors;
    final long memoryTotalBytes;
    final long memoryUsedBytes;
    final long memoryAvailableBytes;
    final double memoryUsedPercent;
    final List<ResourceSlice> memoryBreakdown;
    final long botMemoryBytes;
    final List<ResourceSlice> botMemoryBreakdown;
    final long storageTotalBytes;
    final long storageUsedBytes;
    final long storageFreeBytes;
    final long fateStorageBytes;
    final double storageUsedPercent;
    final List<ResourceSlice> storageBreakdown;
    final long controllerUptimeSeconds;
    final SystemInfo system;
    final RuntimeInfo runtime;
    final ComponentHealth components;

    StatusData(
        boolean online,
        long servers,
        long users,
        int shards,
        Integer latencyMs,
        long commandsThisMonth,
        long uptimeSeconds,
        long fetchedAt,
        String service,
        String instanceName,
        boolean controllerOnline,
        boolean processRunning,
        boolean desiredRunning,
        boolean processOwned,
        String healthSummary,
        String configHealth,
        double diskFreeGb,
        int recentErrors,
        long memoryTotalBytes,
        long memoryUsedBytes,
        long memoryAvailableBytes,
        double memoryUsedPercent,
        List<ResourceSlice> memoryBreakdown,
        long botMemoryBytes,
        List<ResourceSlice> botMemoryBreakdown,
        long storageTotalBytes,
        long storageUsedBytes,
        long storageFreeBytes,
        long fateStorageBytes,
        double storageUsedPercent,
        List<ResourceSlice> storageBreakdown,
        long controllerUptimeSeconds,
        SystemInfo system,
        RuntimeInfo runtime,
        ComponentHealth components
    ) {
        this.online = online;
        this.servers = servers;
        this.users = users;
        this.shards = shards;
        this.latencyMs = latencyMs;
        this.commandsThisMonth = commandsThisMonth;
        this.uptimeSeconds = uptimeSeconds;
        this.fetchedAt = fetchedAt;
        this.service = service;
        this.instanceName = instanceName;
        this.controllerOnline = controllerOnline;
        this.processRunning = processRunning;
        this.desiredRunning = desiredRunning;
        this.processOwned = processOwned;
        this.healthSummary = healthSummary;
        this.configHealth = configHealth;
        this.diskFreeGb = diskFreeGb;
        this.recentErrors = recentErrors;
        this.memoryTotalBytes = memoryTotalBytes;
        this.memoryUsedBytes = memoryUsedBytes;
        this.memoryAvailableBytes = memoryAvailableBytes;
        this.memoryUsedPercent = memoryUsedPercent;
        this.memoryBreakdown = memoryBreakdown;
        this.botMemoryBytes = botMemoryBytes;
        this.botMemoryBreakdown = botMemoryBreakdown;
        this.storageTotalBytes = storageTotalBytes;
        this.storageUsedBytes = storageUsedBytes;
        this.storageFreeBytes = storageFreeBytes;
        this.fateStorageBytes = fateStorageBytes;
        this.storageUsedPercent = storageUsedPercent;
        this.storageBreakdown = storageBreakdown;
        this.controllerUptimeSeconds = controllerUptimeSeconds;
        this.system = system;
        this.runtime = runtime;
        this.components = components;
    }

    static StatusData fromJson(JSONObject json) {
        JSONObject bot = json.optJSONObject("bot");
        JSONObject health = json.optJSONObject("health");
        JSONObject resources = json.optJSONObject("resources");
        JSONObject memory = resources == null ? null : resources.optJSONObject("memory");
        JSONObject storage = resources == null ? null : resources.optJSONObject("storage");
        JSONObject system = json.optJSONObject("system");
        Integer latency = json.isNull("latency_ms") ? null : json.optInt("latency_ms");
        boolean controller = "fate-control".equals(json.optString("service"));
        return new StatusData(
            json.optBoolean("online", false),
            json.optLong("servers", 0),
            json.optLong("users", 0),
            Math.max(1, json.optInt("shards", 1)),
            latency,
            json.optLong("commands_this_month", 0),
            json.optLong("uptime_seconds", 0),
            System.currentTimeMillis(),
            json.optString("service", "fate-bot"),
            json.optString("instance_name", controller ? "Fate controller" : "Fate"),
            json.optBoolean("controller_online", controller),
            bot != null ? bot.optBoolean("process_running", false) : json.optBoolean("online", false),
            bot != null ? bot.optBoolean("desired_running", false) : json.optBoolean("online", false),
            bot != null && bot.optBoolean("owned", false),
            health != null ? health.optString("summary", "Status available") : "Bot status available",
            health != null ? health.optString("config", "unknown") : "unknown",
            health != null ? health.optDouble("disk_free_gb", -1) : -1,
            health != null ? health.optInt("recent_log_errors", 0) : 0,
            memory == null ? 0 : memory.optLong("total_bytes", 0),
            memory == null ? 0 : memory.optLong("used_bytes", 0),
            memory == null ? 0 : memory.optLong("available_bytes", 0),
            memory == null ? 0 : memory.optDouble("used_percent", 0),
            parseBreakdown(memory),
            memory == null ? 0 : memory.optLong("bot_total_bytes", 0),
            parseBreakdown(memory, "bot_breakdown"),
            storage == null ? 0 : storage.optLong("total_bytes", 0),
            storage == null ? 0 : storage.optLong("used_bytes", 0),
            storage == null ? 0 : storage.optLong("free_bytes", 0),
            storage == null ? 0 : storage.optLong("fate_total_bytes", 0),
            storage == null ? 0 : storage.optDouble("used_percent", 0),
            parseBreakdown(storage),
            json.optLong("controller_uptime_seconds", 0),
            parseSystem(system),
            parseRuntime(bot),
            parseComponents(health)
        );
    }

    private static SystemInfo parseSystem(JSONObject system) {
        if (system == null || !system.has("hostname")) return SystemInfo.empty();
        JSONObject cpu = system.optJSONObject("cpu");
        JSONObject network = system.optJSONObject("network");
        JSONObject diskIo = system.optJSONObject("disk_io");
        JSONObject temperature = system.optJSONObject("temperature");
        JSONObject capabilities = system.optJSONObject("capabilities");
        JSONArray addresses = network == null ? null : network.optJSONArray("addresses");
        JSONArray load = cpu == null ? null : cpu.optJSONArray("load_average");
        return new SystemInfo(
            true,
            system.optString("hostname", "Host"),
            system.optString("os_name", "Unknown OS"),
            system.optString("os_version", ""),
            system.optString("architecture", ""),
            system.optString("python_version", ""),
            joinStrings(addresses),
            system.optLong("uptime_seconds", 0),
            system.optInt("process_count", 0),
            cpu == null ? 0 : cpu.optDouble("used_percent", 0),
            cpu == null ? 0 : cpu.optInt("logical_cores", 0),
            cpu == null ? 0 : cpu.optInt("physical_cores", 0),
            cpu == null || cpu.isNull("frequency_mhz") ? null : cpu.optDouble("frequency_mhz"),
            joinNumbers(load),
            network == null ? 0 : network.optLong("received_bytes", 0),
            network == null ? 0 : network.optLong("sent_bytes", 0),
            network == null ? 0 : network.optLong("receive_bytes_per_second", 0),
            network == null ? 0 : network.optLong("send_bytes_per_second", 0),
            diskIo == null ? 0 : diskIo.optLong("read_bytes", 0),
            diskIo == null ? 0 : diskIo.optLong("written_bytes", 0),
            diskIo == null ? 0 : diskIo.optLong("read_bytes_per_second", 0),
            diskIo == null ? 0 : diskIo.optLong("write_bytes_per_second", 0),
            temperature == null || temperature.isNull("celsius")
                ? null : temperature.optDouble("celsius"),
            capabilities != null && capabilities.optBoolean("reboot", false)
        );
    }

    private static RuntimeInfo parseRuntime(JSONObject bot) {
        if (bot == null) return RuntimeInfo.empty();
        Integer pid = bot.isNull("pid") ? null : bot.optInt("pid");
        Integer exitCode = bot.isNull("last_exit_code") ? null : bot.optInt("last_exit_code");
        return new RuntimeInfo(
            pid,
            bot.optDouble("cpu_percent", 0),
            bot.optInt("thread_count", 0),
            bot.optString("started_at", ""),
            bot.optString("state", "unknown"),
            bot.optInt("restart_count", 0),
            exitCode,
            bot.optString("last_error", "")
        );
    }

    private static ComponentHealth parseComponents(JSONObject health) {
        if (health == null) return ComponentHealth.empty();
        return new ComponentHealth(
            health.optString("discord", "unknown"),
            health.optString("database", "unknown"),
            health.optString("disk", "unknown"),
            health.isNull("extensions_loaded") ? -1 : health.optInt("extensions_loaded", -1),
            health.isNull("extensions_configured") ? -1 : health.optInt("extensions_configured", -1),
            health.isNull("commands") ? -1 : health.optInt("commands", -1)
        );
    }

    private static String joinStrings(JSONArray values) {
        if (values == null) return "";
        StringBuilder output = new StringBuilder();
        for (int index = 0; index < values.length(); index++) {
            String value = values.optString(index, "");
            if (value.isEmpty()) continue;
            if (output.length() > 0) output.append("  •  ");
            output.append(value);
        }
        return output.toString();
    }

    private static String joinNumbers(JSONArray values) {
        if (values == null) return "";
        StringBuilder output = new StringBuilder();
        for (int index = 0; index < values.length(); index++) {
            if (output.length() > 0) output.append(" / ");
            output.append(values.optDouble(index, 0));
        }
        return output.toString();
    }

    private static List<ResourceSlice> parseBreakdown(JSONObject resource) {
        return parseBreakdown(resource, "breakdown");
    }

    private static List<ResourceSlice> parseBreakdown(JSONObject resource, String key) {
        if (resource == null) {
            return Collections.emptyList();
        }
        JSONArray values = resource.optJSONArray(key);
        if (values == null) {
            return Collections.emptyList();
        }
        List<ResourceSlice> result = new ArrayList<>();
        for (int index = 0; index < values.length(); index++) {
            JSONObject value = values.optJSONObject(index);
            if (value != null) {
                result.add(new ResourceSlice(
                    value.optString("label", "Other"),
                    Math.max(0, value.optLong("bytes", 0))
                ));
            }
        }
        return Collections.unmodifiableList(result);
    }

    static String encodeBreakdown(List<ResourceSlice> values) {
        JSONArray encoded = new JSONArray();
        for (ResourceSlice value : values) {
            try {
                encoded.put(new JSONObject().put("label", value.label).put("bytes", value.bytes));
            } catch (Exception ignored) {
                // One malformed cache item should not prevent the remaining data from being saved.
            }
        }
        return encoded.toString();
    }

    static List<ResourceSlice> decodeBreakdown(String value) {
        if (value == null || value.isEmpty()) {
            return Collections.emptyList();
        }
        try {
            return parseBreakdown(new JSONObject().put("breakdown", new JSONArray(value)));
        } catch (Exception ignored) {
            return Collections.emptyList();
        }
    }
}
