package xyz.fatebot.status;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

final class MetricSeries {
    static final class Point {
        final long timestampMillis;
        final String timestampLabel;
        final double value;

        Point(long timestampMillis, String timestampLabel, double value) {
            this.timestampMillis = timestampMillis;
            this.timestampLabel = timestampLabel;
            this.value = value;
        }
    }

    static final class Summary {
        final double current;
        final double total;
        final double change;
        final double changePercent;

        Summary(double current, double total, double change, double changePercent) {
            this.current = current;
            this.total = total;
            this.change = change;
            this.changePercent = changePercent;
        }
    }

    static final class Command {
        final String name;
        final double count;
        final double share;
        final List<Point> points;

        Command(String name, double count, double share, List<Point> points) {
            this.name = name;
            this.count = count;
            this.share = share;
            this.points = Collections.unmodifiableList(points);
        }
    }

    final String metric;
    final String window;
    final long bucketSeconds;
    final boolean available;
    final List<Point> points;
    final Summary summary;
    final List<Command> topCommands;

    private MetricSeries(
        String metric,
        String window,
        long bucketSeconds,
        boolean available,
        List<Point> points,
        Summary summary,
        List<Command> topCommands
    ) {
        this.metric = metric;
        this.window = window;
        this.bucketSeconds = bucketSeconds;
        this.available = available;
        this.points = Collections.unmodifiableList(points);
        this.summary = summary;
        this.topCommands = Collections.unmodifiableList(topCommands);
    }

    static MetricSeries fromJson(JSONObject json) {
        JSONObject summaryJson = json.optJSONObject("summary");
        Summary summary = summaryJson == null
            ? new Summary(Double.NaN, Double.NaN, Double.NaN, Double.NaN)
            : new Summary(
                number(summaryJson, "current"),
                number(summaryJson, "total"),
                number(summaryJson, "change"),
                number(summaryJson, "change_percent")
            );

        List<Command> commands = new ArrayList<>();
        JSONArray commandJson = json.optJSONArray("top_commands");
        if (commandJson != null) {
            for (int index = 0; index < commandJson.length(); index++) {
                JSONObject item = commandJson.optJSONObject(index);
                if (item == null) continue;
                String name = item.optString("name", "").trim();
                if (name.isEmpty()) continue;
                commands.add(new Command(
                    name,
                    number(item, "count"),
                    number(item, "share"),
                    parsePoints(item.optJSONArray("points"))
                ));
            }
        }

        return new MetricSeries(
            json.optString("metric", ""),
            json.optString("window", ""),
            json.optLong("bucket_seconds", 0L),
            json.optBoolean("available", true),
            parsePoints(json.optJSONArray("points")),
            summary,
            commands
        );
    }

    private static List<Point> parsePoints(JSONArray json) {
        List<Point> points = new ArrayList<>();
        if (json == null) return points;
        for (int index = 0; index < json.length(); index++) {
            JSONObject point = json.optJSONObject(index);
            if (point == null) continue;
            double value = number(point, "value");
            if (!Double.isFinite(value)) continue;
            Object rawTimestamp = point.opt("timestamp");
            String label = rawTimestamp == null || rawTimestamp == JSONObject.NULL
                ? "" : String.valueOf(rawTimestamp);
            points.add(new Point(timestampMillis(rawTimestamp), label, value));
        }
        return points;
    }

    private static double number(JSONObject json, String key) {
        Object value = json.opt(key);
        if (value == null || value == JSONObject.NULL) return Double.NaN;
        if (value instanceof Number) return ((Number) value).doubleValue();
        try {
            return Double.parseDouble(String.valueOf(value));
        } catch (NumberFormatException ignored) {
            return Double.NaN;
        }
    }

    private static long timestampMillis(Object raw) {
        if (raw == null || raw == JSONObject.NULL) return 0L;
        if (raw instanceof Number) {
            double value = ((Number) raw).doubleValue();
            return Math.round(Math.abs(value) < 100_000_000_000d ? value * 1000d : value);
        }
        String value = String.valueOf(raw).trim();
        if (value.isEmpty()) return 0L;
        try {
            double numeric = Double.parseDouble(value);
            return Math.round(Math.abs(numeric) < 100_000_000_000d ? numeric * 1000d : numeric);
        } catch (NumberFormatException ignored) {
            // Continue with ISO-8601 formats.
        }
        try {
            return Instant.parse(value).toEpochMilli();
        } catch (Exception ignored) {
            try {
                return OffsetDateTime.parse(value).toInstant().toEpochMilli();
            } catch (Exception ignoredAgain) {
                return 0L;
            }
        }
    }
}
