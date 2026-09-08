package xyz.fatebot.status;

import android.app.PendingIntent;
import android.app.AlarmManager;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Shader;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONObject;

import java.text.DecimalFormat;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * A configurable, authenticated home-screen chart for Fate's historical metrics.
 *
 * Each widget owns its metric/window selection while series caches are shared by
 * identical selections. That keeps independently configured widgets reliable
 * without issuing duplicate requests when several show the same graph.
 */
public class MetricsGraphWidgetProvider extends AppWidgetProvider {
    static final String ACTION_REFRESH = "xyz.fatebot.status.REFRESH_METRICS_GRAPH";
    static final String ACTION_SCHEDULED = "xyz.fatebot.status.SCHEDULED_METRICS_GRAPH";
    static final String PREF_METRIC_PREFIX = "metrics_graph_widget_metric_";
    static final String PREF_WINDOW_PREFIX = "metrics_graph_widget_window_";

    private static final String PREF_CACHE_PREFIX = "metrics_graph_widget_cache_";
    private static final String PREF_CACHE_AT_PREFIX = "metrics_graph_widget_cache_at_";
    private static final String DEFAULT_METRIC = "commands";
    private static final String DEFAULT_WINDOW = "1h";
    private static final ExecutorService EXECUTOR = Executors.newFixedThreadPool(2);

    static final String[] METRIC_KEYS = {
        "servers", "users", "commands", "mongo_calls", "mysql_calls", "messages_sent"
    };
    static final String[] METRIC_LABELS = {
        "Servers", "Users", "Commands used", "MongoDB calls", "MySQL calls", "Messages sent"
    };
    static final String[] WINDOW_KEYS = {
        "1m", "5m", "15m", "1h", "6h", "12h", "24h", "7d"
    };
    static final String[] WINDOW_LABELS = {
        "1 minute", "5 minutes", "15 minutes", "1 hour",
        "6 hours", "12 hours", "24 hours", "7 days"
    };

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] appWidgetIds) {
        refresh(context, manager, appWidgetIds);
        schedule(context);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (!ACTION_REFRESH.equals(action)
            && !ACTION_SCHEDULED.equals(action)
            && !Intent.ACTION_BOOT_COMPLETED.equals(action)) {
            super.onReceive(context, intent);
            return;
        }

        int requestedId = intent.getIntExtra(
            AppWidgetManager.EXTRA_APPWIDGET_ID,
            AppWidgetManager.INVALID_APPWIDGET_ID
        );
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        int[] ids;
        if (requestedId == AppWidgetManager.INVALID_APPWIDGET_ID) {
            ids = manager.getAppWidgetIds(
                new ComponentName(context, MetricsGraphWidgetProvider.class)
            );
        } else {
            ids = new int[] {requestedId};
        }
        BroadcastReceiver.PendingResult pendingResult = goAsync();
        refresh(context, manager, ids, pendingResult::finish);
        schedule(context);
    }

    @Override
    public void onAppWidgetOptionsChanged(
        Context context,
        AppWidgetManager manager,
        int appWidgetId,
        Bundle newOptions
    ) {
        super.onAppWidgetOptionsChanged(context, manager, appWidgetId, newOptions);
        renderCached(context, manager, appWidgetId, false, null);
    }

    @Override
    public void onDeleted(Context context, int[] appWidgetIds) {
        SharedPreferences.Editor editor = preferences(context).edit();
        for (int id : appWidgetIds) {
            editor.remove(PREF_METRIC_PREFIX + id);
            editor.remove(PREF_WINDOW_PREFIX + id);
        }
        editor.apply();
        schedule(context);
        super.onDeleted(context, appWidgetIds);
    }

    @Override
    public void onDisabled(Context context) {
        cancelSchedule(context);
        super.onDisabled(context);
    }

    static void refreshAll(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        int[] ids = manager.getAppWidgetIds(
            new ComponentName(context, MetricsGraphWidgetProvider.class)
        );
        refresh(context, manager, ids);
        schedule(context);
    }

    static void refreshWidget(Context context, int appWidgetId) {
        refresh(
            context,
            AppWidgetManager.getInstance(context),
            new int[] {appWidgetId}
        );
        schedule(context);
    }

    static void saveSelection(Context context, int appWidgetId, String metric, String window) {
        preferences(context).edit()
            .putString(PREF_METRIC_PREFIX + appWidgetId, validMetric(metric))
            .putString(PREF_WINDOW_PREFIX + appWidgetId, validWindow(window))
            .apply();
    }

    static String selectedMetric(Context context, int appWidgetId) {
        return validMetric(preferences(context).getString(
            PREF_METRIC_PREFIX + appWidgetId,
            DEFAULT_METRIC
        ));
    }

    static String selectedWindow(Context context, int appWidgetId) {
        return validWindow(preferences(context).getString(
            PREF_WINDOW_PREFIX + appWidgetId,
            DEFAULT_WINDOW
        ));
    }

    static int metricIndex(String key) {
        for (int i = 0; i < METRIC_KEYS.length; i++) {
            if (METRIC_KEYS[i].equals(key)) return i;
        }
        return indexOf(METRIC_KEYS, DEFAULT_METRIC);
    }

    static int windowIndex(String key) {
        for (int i = 0; i < WINDOW_KEYS.length; i++) {
            if (WINDOW_KEYS[i].equals(key)) return i;
        }
        return indexOf(WINDOW_KEYS, DEFAULT_WINDOW);
    }

    private static void refresh(Context context, AppWidgetManager manager, int[] ids) {
        refresh(context, manager, ids, null);
    }

    private static void refresh(
        Context context,
        AppWidgetManager manager,
        int[] ids,
        Runnable completion
    ) {
        if (ids == null || ids.length == 0) {
            if (completion != null) completion.run();
            return;
        }
        Context appContext = context.getApplicationContext();
        BotProfileStore.Profile profile = BotProfileStore.active(appContext);
        for (int id : ids) renderCached(appContext, manager, id, true, null);

        int[] stableIds = ids.clone();
        EXECUTOR.execute(() -> {
            try {
                fetchAndRender(appContext, manager, stableIds, profile);
            } finally {
                if (completion != null) completion.run();
            }
        });
    }

    private static void schedule(Context context) {
        Context appContext = context.getApplicationContext();
        AppWidgetManager manager = AppWidgetManager.getInstance(appContext);
        int[] ids = manager.getAppWidgetIds(
            new ComponentName(appContext, MetricsGraphWidgetProvider.class)
        );
        if (ids == null || ids.length == 0) {
            cancelSchedule(appContext);
            return;
        }
        AlarmManager alarms = (AlarmManager) appContext.getSystemService(Context.ALARM_SERVICE);
        if (alarms == null) return;
        alarms.set(
            AlarmManager.ELAPSED_REALTIME_WAKEUP,
            SystemClock.elapsedRealtime() + SyncScheduler.intervalMinutes(appContext) * 60_000L,
            scheduledIntent(appContext)
        );
    }

    private static void cancelSchedule(Context context) {
        AlarmManager alarms = (AlarmManager) context.getSystemService(Context.ALARM_SERVICE);
        if (alarms != null) alarms.cancel(scheduledIntent(context));
    }

    private static PendingIntent scheduledIntent(Context context) {
        return PendingIntent.getBroadcast(
            context,
            51_105,
            new Intent(context, MetricsGraphWidgetProvider.class).setAction(ACTION_SCHEDULED),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
    }

    private static void fetchAndRender(
        Context context,
        AppWidgetManager manager,
        int[] ids,
        BotProfileStore.Profile profile
    ) {
        Map<String, List<Integer>> groups = new LinkedHashMap<>();
        for (int id : ids) {
            String metric = selectedMetric(context, id);
            String window = selectedWindow(context, id);
            groups.computeIfAbsent(metric + "|" + window, unused -> new ArrayList<>()).add(id);
        }

        String endpoint = profile.endpoint;
        String controlKey = ControlKeyStore.read(context, profile.id);

        for (Map.Entry<String, List<Integer>> group : groups.entrySet()) {
            int separator = group.getKey().indexOf('|');
            String metric = group.getKey().substring(0, separator);
            String window = group.getKey().substring(separator + 1);
            SeriesSnapshot snapshot = null;
            String error = null;
            try {
                if (controlKey.isEmpty()) {
                    throw new IllegalStateException("Add the control key in Settings to load metrics.");
                }
                JSONObject payload = FateApi.metrics(
                    endpoint,
                    controlKey,
                    metric,
                    window,
                    null
                );
                long fetchedAt = System.currentTimeMillis();
                MetricSeries parsed = MetricSeries.fromJson(payload);
                if (!parsed.available) {
                    throw new IllegalStateException(
                        "FateControl's metric store is unavailable. Restart the updated backend."
                    );
                }
                if ((!parsed.metric.isEmpty() && !metric.equals(parsed.metric))
                    || (!parsed.window.isEmpty() && !window.equals(parsed.window))) {
                    throw new IllegalArgumentException(
                        "FateControl returned a different metric window."
                    );
                }
                snapshot = new SeriesSnapshot(parsed, fetchedAt);
                if (!BotProfileStore.isActive(context, profile)) return;
                saveCache(context, profile, metric, window, payload, fetchedAt);
            } catch (Exception failure) {
                if (!BotProfileStore.isActive(context, profile)) return;
                error = usefulMessage(failure);
                snapshot = readCache(context, metric, window);
            }
            if (!BotProfileStore.isActive(context, profile)) return;
            for (int id : group.getValue()) {
                render(context, manager, id, snapshot, false, error);
            }
        }
    }

    private static void renderCached(
        Context context,
        AppWidgetManager manager,
        int appWidgetId,
        boolean refreshing,
        String error
    ) {
        String metric = selectedMetric(context, appWidgetId);
        String window = selectedWindow(context, appWidgetId);
        render(context, manager, appWidgetId, readCache(context, metric, window), refreshing, error);
    }

    private static void render(
        Context context,
        AppWidgetManager manager,
        int appWidgetId,
        SeriesSnapshot snapshot,
        boolean refreshing,
        String error
    ) {
        MetricSeries series = snapshot == null ? null : snapshot.series;
        String metric = selectedMetric(context, appWidgetId);
        String window = selectedWindow(context, appWidgetId);
        boolean compact = isCompact(manager, appWidgetId);
        ThemeManager.Palette palette = ThemeManager.palette(context);
        RemoteViews views = new RemoteViews(
            context.getPackageName(),
            compact
                ? R.layout.widget_fate_metrics_graph_compact
                : R.layout.widget_fate_metrics_graph
        );

        ThemeManager.applyWidgetRoot(context, views, R.id.metrics_widget_root);
        views.setInt(R.id.metrics_widget_graph, "setBackgroundResource", palette.widgetPanel);
        views.setTextColor(R.id.metrics_widget_kicker, palette.secondary);
        views.setTextColor(R.id.metrics_widget_title, palette.text);
        views.setTextColor(R.id.metrics_widget_value, metricColor(metric, palette));
        views.setTextColor(R.id.metrics_widget_change, palette.muted);
        views.setTextColor(R.id.metrics_widget_total, palette.muted);
        views.setTextColor(R.id.metrics_widget_window, palette.secondary);
        views.setTextColor(R.id.metrics_widget_edit, palette.text);
        views.setTextColor(R.id.metrics_widget_refresh, palette.text);
        views.setTextColor(R.id.metrics_widget_status, palette.muted);

        views.setTextViewText(
            R.id.metrics_widget_kicker,
            BotProfileStore.active(context).name.toUpperCase(Locale.ROOT) + "  •  METRICS"
        );
        views.setTextViewText(R.id.metrics_widget_title, metricLabel(metric));
        views.setTextViewText(R.id.metrics_widget_window, window.toUpperCase(Locale.US));
        views.setContentDescription(
            R.id.metrics_widget_graph,
            metricLabel(metric) + " over " + windowLabel(window)
        );

        if (series == null) {
            views.setTextViewText(R.id.metrics_widget_value, "—");
            views.setTextViewText(R.id.metrics_widget_change, "Waiting for samples");
            views.setTextViewText(R.id.metrics_widget_total, "No metric data yet");
            views.setTextViewText(
                R.id.metrics_widget_status,
                error == null ? (refreshing ? "Refreshing…" : "Tap refresh to load") : error
            );
            views.setTextColor(
                R.id.metrics_widget_status,
                error == null ? palette.muted : palette.red
            );
            views.setImageViewBitmap(
                R.id.metrics_widget_graph,
                renderEmptyGraph(context, manager, appWidgetId, palette, refreshing, compact)
            );
        } else {
            views.setTextViewText(R.id.metrics_widget_value, formatNumber(current(series)));
            views.setTextViewText(R.id.metrics_widget_change, formatChange(series));
            views.setTextColor(
                R.id.metrics_widget_change,
                series.summary.change > 0d
                    ? palette.green
                    : series.summary.change < 0d ? palette.red : palette.muted
            );
            views.setTextViewText(
                R.id.metrics_widget_total,
                "Total " + formatNumber(total(series)) + "  •  " + series.points.size() + " samples"
            );
            String status = error == null
                ? (refreshing ? "Refreshing…" : "Updated " + formatUpdated(snapshot.fetchedAt))
                : "Cached • " + error;
            views.setTextViewText(R.id.metrics_widget_status, status);
            views.setTextColor(R.id.metrics_widget_status, error == null ? palette.muted : palette.red);
            views.setImageViewBitmap(
                R.id.metrics_widget_graph,
                renderGraph(
                    context,
                    manager,
                    appWidgetId,
                    series,
                    metricColor(metric, palette),
                    palette,
                    compact
                )
            );
        }

        views.setViewVisibility(R.id.metrics_widget_status, View.VISIBLE);
        bindActions(context, views, appWidgetId);
        manager.updateAppWidget(appWidgetId, views);
    }

    private static void bindActions(Context context, RemoteViews views, int appWidgetId) {
        Intent refreshIntent = new Intent(context, MetricsGraphWidgetProvider.class)
            .setAction(ACTION_REFRESH)
            .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, appWidgetId);
        PendingIntent refresh = PendingIntent.getBroadcast(
            context,
            80_000 + appWidgetId,
            refreshIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.metrics_widget_refresh, refresh);

        Intent configureIntent = new Intent(context, MetricsGraphWidgetConfigureActivity.class)
            .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, appWidgetId)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent configure = PendingIntent.getActivity(
            context,
            100_000 + appWidgetId,
            configureIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.metrics_widget_edit, configure);
        views.setOnClickPendingIntent(R.id.metrics_widget_window, configure);

        Intent openIntent = new Intent(context, MainActivity.class)
            .putExtra(MainActivity.EXTRA_OPEN_PAGE, MainActivity.PAGE_METRICS)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent open = PendingIntent.getActivity(
            context,
            90_000 + appWidgetId,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.metrics_widget_root, open);
        views.setOnClickPendingIntent(R.id.metrics_widget_graph, open);
    }

    private static Bitmap renderGraph(
        Context context,
        AppWidgetManager manager,
        int appWidgetId,
        MetricSeries series,
        int lineColor,
        ThemeManager.Palette palette,
        boolean compact
    ) {
        int[] size = chartSize(context, manager, appWidgetId, compact);
        Bitmap bitmap = Bitmap.createBitmap(size[0], size[1], Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bitmap);
        canvas.drawColor(Color.TRANSPARENT);

        float density = context.getResources().getDisplayMetrics().density;
        float left = (compact ? 5f : 40f) * density;
        float top = (compact ? 5f : 11f) * density;
        float right = size[0] - (compact ? 5f : 9f) * density;
        float bottom = size[1] - (compact ? 5f : 19f) * density;
        float plotWidth = Math.max(1f, right - left);
        float plotHeight = Math.max(1f, bottom - top);

        Paint grid = new Paint(Paint.ANTI_ALIAS_FLAG);
        grid.setColor(withAlpha(palette.stroke, 105));
        grid.setStrokeWidth(Math.max(1f, density * 0.75f));
        for (int i = 0; i <= 3; i++) {
            float y = top + plotHeight * i / 3f;
            canvas.drawLine(left, y, right, y, grid);
        }
        for (int i = 0; i <= 4; i++) {
            float x = left + plotWidth * i / 4f;
            canvas.drawLine(x, top, x, bottom, grid);
        }

        List<MetricSeries.Point> plotPoints = new ArrayList<>(series.points);
        boolean timeScale = plotPoints.size() > 1;
        for (MetricSeries.Point point : plotPoints) {
            if (point.timestampMillis <= 0L) {
                timeScale = false;
                break;
            }
        }
        if (timeScale) {
            plotPoints.sort((first, second) -> Long.compare(
                first.timestampMillis,
                second.timestampMillis
            ));
        }

        if (plotPoints.isEmpty()) {
            drawCenteredMessage(canvas, size, "No samples in this window", palette.muted, density);
            return bitmap;
        }

        double min = Double.POSITIVE_INFINITY;
        double max = Double.NEGATIVE_INFINITY;
        for (MetricSeries.Point point : plotPoints) {
            if (!Double.isFinite(point.value)) continue;
            min = Math.min(min, point.value);
            max = Math.max(max, point.value);
        }
        if (!Double.isFinite(min) || !Double.isFinite(max)) {
            drawCenteredMessage(canvas, size, "No numeric samples", palette.muted, density);
            return bitmap;
        }
        if (Math.abs(max - min) < 0.000001d) {
            double padding = Math.max(1d, Math.abs(max) * 0.08d);
            min -= padding;
            max += padding;
        } else {
            double padding = (max - min) * 0.08d;
            min -= padding;
            max += padding;
        }

        Path line = new Path();
        Path area = new Path();
        List<float[]> coordinates = new ArrayList<>();
        int count = plotPoints.size();
        long firstPlotTime = timeScale ? plotPoints.get(0).timestampMillis : 0L;
        long lastPlotTime = timeScale ? plotPoints.get(count - 1).timestampMillis : 0L;
        long timeSpan = lastPlotTime - firstPlotTime;
        for (int i = 0; i < count; i++) {
            MetricSeries.Point point = plotPoints.get(i);
            float x;
            if (timeScale && timeSpan > 0L) {
                x = left + plotWidth * (point.timestampMillis - firstPlotTime) / timeSpan;
            } else {
                x = count == 1
                    ? left + plotWidth / 2f
                    : left + plotWidth * i / (count - 1f);
            }
            float y = (float) (bottom - ((point.value - min) / (max - min)) * plotHeight);
            y = Math.max(top, Math.min(bottom, y));
            coordinates.add(new float[] {x, y});
            if (i == 0) {
                line.moveTo(x, y);
                area.moveTo(x, bottom);
                area.lineTo(x, y);
            } else {
                line.lineTo(x, y);
                area.lineTo(x, y);
            }
        }
        float lastX = coordinates.get(coordinates.size() - 1)[0];
        area.lineTo(lastX, bottom);
        area.close();

        Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
        fill.setShader(new LinearGradient(
            0f,
            top,
            0f,
            bottom,
            withAlpha(lineColor, 115),
            withAlpha(lineColor, 8),
            Shader.TileMode.CLAMP
        ));
        canvas.drawPath(area, fill);

        Paint stroke = new Paint(Paint.ANTI_ALIAS_FLAG);
        stroke.setStyle(Paint.Style.STROKE);
        stroke.setStrokeJoin(Paint.Join.ROUND);
        stroke.setStrokeCap(Paint.Cap.ROUND);
        stroke.setStrokeWidth(Math.max(2.2f * density, 3f));
        stroke.setColor(lineColor);
        canvas.drawPath(line, stroke);

        Paint dot = new Paint(Paint.ANTI_ALIAS_FLAG);
        dot.setColor(palette.text);
        float radius = Math.max(2.2f * density, 3f);
        if (coordinates.size() <= 18) {
            for (float[] coordinate : coordinates) {
                canvas.drawCircle(coordinate[0], coordinate[1], radius, dot);
                dot.setColor(lineColor);
                canvas.drawCircle(coordinate[0], coordinate[1], radius * 0.56f, dot);
                dot.setColor(palette.text);
            }
        } else {
            float[] last = coordinates.get(coordinates.size() - 1);
            canvas.drawCircle(last[0], last[1], radius * 1.2f, dot);
            dot.setColor(lineColor);
            canvas.drawCircle(last[0], last[1], radius * 0.7f, dot);
        }

        if (!compact) {
            Paint label = new Paint(Paint.ANTI_ALIAS_FLAG);
            label.setColor(palette.muted);
            label.setTextSize(Math.max(8f * density, 10f));
            label.setTypeface(android.graphics.Typeface.create(
                "sans",
                android.graphics.Typeface.NORMAL
            ));
            label.setTextAlign(Paint.Align.RIGHT);
            canvas.drawText(formatNumber(max), left - 5f * density, top + 4f * density, label);
            canvas.drawText(formatNumber(min), left - 5f * density, bottom, label);

            label.setTextAlign(Paint.Align.LEFT);
            long firstTimestamp = plotPoints.get(0).timestampMillis;
            long lastTimestamp = plotPoints.get(plotPoints.size() - 1).timestampMillis;
            canvas.drawText(formatAxisTime(firstTimestamp), left, size[1] - 4f * density, label);
            label.setTextAlign(Paint.Align.RIGHT);
            canvas.drawText(formatAxisTime(lastTimestamp), right, size[1] - 4f * density, label);
        }
        return bitmap;
    }

    private static Bitmap renderEmptyGraph(
        Context context,
        AppWidgetManager manager,
        int appWidgetId,
        ThemeManager.Palette palette,
        boolean refreshing,
        boolean compact
    ) {
        int[] size = chartSize(context, manager, appWidgetId, compact);
        Bitmap bitmap = Bitmap.createBitmap(size[0], size[1], Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bitmap);
        canvas.drawColor(Color.TRANSPARENT);
        float density = context.getResources().getDisplayMetrics().density;
        Paint grid = new Paint(Paint.ANTI_ALIAS_FLAG);
        grid.setColor(withAlpha(palette.stroke, 90));
        grid.setStrokeWidth(Math.max(1f, density * 0.75f));
        float inset = 10f * density;
        for (int i = 1; i <= 3; i++) {
            float y = inset + (size[1] - inset * 2f) * i / 4f;
            canvas.drawLine(inset, y, size[0] - inset, y, grid);
        }
        drawCenteredMessage(
            canvas,
            size,
            compact
                ? (refreshing ? "Refreshing…" : "No samples")
                : refreshing ? "Refreshing metric…" : "No metric samples yet",
            palette.muted,
            density
        );
        return bitmap;
    }

    private static void drawCenteredMessage(
        Canvas canvas,
        int[] size,
        String message,
        int color,
        float density
    ) {
        Paint text = new Paint(Paint.ANTI_ALIAS_FLAG);
        text.setColor(color);
        text.setTextSize(Math.max(11f * density, 14f));
        text.setTextAlign(Paint.Align.CENTER);
        canvas.drawText(
            message,
            size[0] / 2f,
            size[1] / 2f - (text.ascent() + text.descent()) / 2f,
            text
        );
    }

    private static int[] chartSize(
        Context context,
        AppWidgetManager manager,
        int appWidgetId,
        boolean compact
    ) {
        Bundle options = manager.getAppWidgetOptions(appWidgetId);
        int widthDp = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 250);
        int heightDp = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 180);
        float density = context.getResources().getDisplayMetrics().density;
        // Keep the bitmap comfortably under Android's RemoteViews binder budget.
        int width = clamp(Math.round(widthDp * density), 180, 640);
        float graphHeightDp = compact
            ? Math.max(42, heightDp * 0.3f)
            : Math.max(72, heightDp * 0.45f);
        int height = clamp(
            Math.round(graphHeightDp * density),
            compact ? 42 : 80,
            compact ? 150 : 220
        );
        return new int[] {width, height};
    }

    private static boolean isCompact(AppWidgetManager manager, int appWidgetId) {
        Bundle options = manager.getAppWidgetOptions(appWidgetId);
        int width = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 250);
        int height = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 180);
        return width < 230 || height < 165;
    }

    private static void saveCache(
        Context context,
        BotProfileStore.Profile profile,
        String metric,
        String window,
        JSONObject payload,
        long fetchedAt
    ) {
        String cacheKey = cacheKey(profile, metric, window);
        preferences(context).edit()
            .putString(PREF_CACHE_PREFIX + cacheKey, payload.toString())
            .putLong(PREF_CACHE_AT_PREFIX + cacheKey, fetchedAt)
            .apply();
    }

    private static SeriesSnapshot readCache(Context context, String metric, String window) {
        SharedPreferences prefs = preferences(context);
        String cacheKey = cacheKey(BotProfileStore.active(context), metric, window);
        String raw = prefs.getString(PREF_CACHE_PREFIX + cacheKey, "");
        if (raw.isEmpty()) return null;
        try {
            return new SeriesSnapshot(
                MetricSeries.fromJson(new JSONObject(raw)),
                prefs.getLong(PREF_CACHE_AT_PREFIX + cacheKey, 0L)
            );
        } catch (Exception ignored) {
            return null;
        }
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(FateWidgetProvider.PREFS, Context.MODE_PRIVATE);
    }

    private static String cacheKey(BotProfileStore.Profile profile, String metric, String window) {
        return profile.id + "_" + Integer.toHexString(profile.endpoint.hashCode())
            + "_" + metric + "_" + window;
    }

    private static String validMetric(String value) {
        return indexOf(METRIC_KEYS, value) >= 0 ? value : DEFAULT_METRIC;
    }

    private static String validWindow(String value) {
        return indexOf(WINDOW_KEYS, value) >= 0 ? value : DEFAULT_WINDOW;
    }

    private static int indexOf(String[] values, String wanted) {
        if (wanted == null) return -1;
        for (int i = 0; i < values.length; i++) {
            if (values[i].equals(wanted)) return i;
        }
        return -1;
    }

    private static String metricLabel(String metric) {
        return METRIC_LABELS[metricIndex(metric)];
    }

    private static String windowLabel(String window) {
        return WINDOW_LABELS[windowIndex(window)];
    }

    private static int metricColor(String metric, ThemeManager.Palette palette) {
        switch (metric) {
            case "servers": return palette.accent;
            case "users": return palette.secondary;
            case "commands": return palette.tertiary;
            case "mongo_calls": return palette.green;
            case "mysql_calls": return blend(palette.accent, palette.secondary, 0.52f);
            case "messages_sent": return blend(palette.secondary, palette.text, 0.36f);
            default: return palette.accent;
        }
    }

    private static int blend(int first, int second, float amount) {
        float inverse = 1f - amount;
        return Color.rgb(
            Math.round(Color.red(first) * inverse + Color.red(second) * amount),
            Math.round(Color.green(first) * inverse + Color.green(second) * amount),
            Math.round(Color.blue(first) * inverse + Color.blue(second) * amount)
        );
    }

    private static int withAlpha(int color, int alpha) {
        return Color.argb(alpha, Color.red(color), Color.green(color), Color.blue(color));
    }

    private static String formatNumber(double value) {
        if (!Double.isFinite(value)) return "—";
        double absolute = Math.abs(value);
        if (absolute >= 1_000_000_000d) return decimal(value / 1_000_000_000d) + "B";
        if (absolute >= 1_000_000d) return decimal(value / 1_000_000d) + "M";
        if (absolute >= 1_000d) return decimal(value / 1_000d) + "K";
        if (Math.abs(value - Math.rint(value)) < 0.000001d) {
            return new DecimalFormat("#,##0").format(value);
        }
        return new DecimalFormat("#,##0.##").format(value);
    }

    private static String decimal(double value) {
        return new DecimalFormat(Math.abs(value) >= 100d ? "0" : "0.#").format(value);
    }

    private static String formatChange(MetricSeries series) {
        double change = series.summary.change;
        double changePercent = series.summary.changePercent;
        String prefix = change > 0d ? "+" : "";
        String amount = prefix + formatNumber(change);
        if (!Double.isFinite(changePercent)) return amount + " this window";
        String percentPrefix = changePercent > 0d ? "+" : "";
        return amount + "  •  " + percentPrefix
            + new DecimalFormat("0.#").format(changePercent) + "%";
    }

    private static double current(MetricSeries series) {
        if (Double.isFinite(series.summary.current)) return series.summary.current;
        return series.points.isEmpty() ? Double.NaN : series.points.get(series.points.size() - 1).value;
    }

    private static double total(MetricSeries series) {
        if (Double.isFinite(series.summary.total)) return series.summary.total;
        double total = 0d;
        boolean found = false;
        for (MetricSeries.Point point : series.points) {
            if (!Double.isFinite(point.value)) continue;
            total += point.value;
            found = true;
        }
        return found ? total : Double.NaN;
    }

    private static String formatUpdated(long timestamp) {
        if (timestamp <= 0L) return "recently";
        return new SimpleDateFormat("h:mm a", Locale.getDefault()).format(new Date(timestamp));
    }

    private static String formatAxisTime(long timestamp) {
        if (timestamp <= 0L) return "";
        return new SimpleDateFormat("h:mm", Locale.getDefault()).format(new Date(timestamp));
    }

    private static String usefulMessage(Exception error) {
        String message = error.getMessage();
        if (message == null || message.trim().isEmpty()) return "Metrics are unavailable.";
        message = message.trim().replaceAll("\\s+", " ");
        return message.substring(0, Math.min(120, message.length()));
    }

    private static int clamp(int value, int minimum, int maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    private static final class SeriesSnapshot {
        final MetricSeries series;
        final long fetchedAt;

        SeriesSnapshot(MetricSeries series, long fetchedAt) {
            this.series = series;
            this.fetchedAt = fetchedAt;
        }
    }
}
