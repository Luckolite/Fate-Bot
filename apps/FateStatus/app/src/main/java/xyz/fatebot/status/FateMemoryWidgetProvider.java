package xyz.fatebot.status;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.view.View;
import android.widget.RemoteViews;

import java.text.DecimalFormat;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class FateMemoryWidgetProvider extends AppWidgetProvider {
    static final String ACTION_REFRESH_MEMORY = "xyz.fatebot.status.REFRESH_MEMORY";
    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();
    private static final int[] ROWS = {R.id.memory_row_1, R.id.memory_row_2, R.id.memory_row_3, R.id.memory_row_4};
    private static final int[] LABELS = {R.id.memory_label_1, R.id.memory_label_2, R.id.memory_label_3, R.id.memory_label_4};
    private static final int[] VALUES = {R.id.memory_value_1, R.id.memory_value_2, R.id.memory_value_3, R.id.memory_value_4};
    private static final int[] BARS = {R.id.memory_bar_1, R.id.memory_bar_2, R.id.memory_bar_3, R.id.memory_bar_4};

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] appWidgetIds) {
        refresh(context, manager, appWidgetIds);
        SyncScheduler.schedule(context);
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        if (ACTION_REFRESH_MEMORY.equals(intent.getAction())) {
            refreshAll(context);
        }
    }

    static void refreshAll(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        int[] ids = manager.getAppWidgetIds(new ComponentName(context, FateMemoryWidgetProvider.class));
        refresh(context, manager, ids);
    }

    private static void refresh(Context context, AppWidgetManager manager, int[] ids) {
        if (ids == null || ids.length == 0) return;
        Context appContext = context.getApplicationContext();
        BotProfileStore.Profile profile = BotProfileStore.active(appContext);
        StatusData cached = FateWidgetProvider.cachedStatus(appContext);
        for (int id : ids) render(appContext, manager, id, cached, true);
        EXECUTOR.execute(() -> {
            StatusData status;
            boolean failed = false;
            try {
                status = FateWidgetProvider.fetchStatus(
                    appContext,
                    profile.endpoint,
                    profile.id
                );
                if (!BotProfileStore.isActive(appContext, profile)) return;
                FateWidgetProvider.saveStatus(appContext, status, profile);
            } catch (Exception error) {
                if (!BotProfileStore.isActive(appContext, profile)) return;
                status = FateWidgetProvider.cachedStatus(appContext);
                failed = true;
            }
            if (!BotProfileStore.isActive(appContext, profile)) return;
            for (int id : ids) render(appContext, manager, id, status, failed);
        });
    }

    private static void render(Context context, AppWidgetManager manager, int id, StatusData status, boolean stale) {
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.widget_fate_memory);
        ThemeManager.Palette palette = ThemeManager.palette(context);
        ThemeManager.applyWidgetRoot(context, views, R.id.memory_widget_root);
        views.setTextColor(R.id.memory_header, palette.secondary);
        views.setTextColor(R.id.memory_total, palette.text);
        views.setTextColor(R.id.memory_subtitle, palette.muted);
        views.setTextColor(R.id.memory_refresh, palette.text);
        views.setTextColor(R.id.memory_updated, palette.muted);
        for (int index = 0; index < LABELS.length; index++) {
            views.setTextColor(LABELS[index], palette.text);
            views.setTextColor(VALUES[index], palette.muted);
        }
        bindActions(context, views, id);
        long total = status == null ? 0 : status.botMemoryBytes;
        views.setTextViewText(R.id.memory_total, total > 0 ? formatBytes(total) : "—");
        String detail;
        if (total <= 0) {
            detail = stale ? "Waiting for Fate process data" : "Fate process is not running";
        } else if (status.memoryTotalBytes > 0) {
            double hostPercent = total * 100d / status.memoryTotalBytes;
            detail = "BOT PROCESS TREE  •  " + new DecimalFormat("0.0").format(hostPercent) + "% OF HOST RAM";
        } else {
            detail = "BOT PROCESS TREE";
        }
        views.setTextViewText(
            R.id.memory_subtitle,
            BotProfileStore.active(context).name.toUpperCase(Locale.ROOT) + "  •  " + detail
        );

        List<StatusData.ResourceSlice> slices = status == null ? null : status.botMemoryBreakdown;
        for (int index = 0; index < ROWS.length; index++) {
            boolean visible = slices != null && index < slices.size() && slices.get(index).bytes > 0;
            views.setViewVisibility(ROWS[index], visible ? View.VISIBLE : View.GONE);
            if (!visible) continue;
            StatusData.ResourceSlice slice = slices.get(index);
            views.setTextViewText(LABELS[index], slice.label);
            views.setTextViewText(VALUES[index], formatBytes(slice.bytes));
            int percent = total <= 0 ? 0 : (int) Math.max(1, Math.min(100, Math.round(slice.bytes * 100d / total)));
            views.setProgressBar(BARS[index], 100, percent, false);
        }
        String updated = status == null || status.fetchedAt == 0
            ? "No data yet"
            : (stale ? "Cached • " : "Updated ") + new SimpleDateFormat("h:mm a", Locale.getDefault()).format(new Date(status.fetchedAt));
        views.setTextViewText(R.id.memory_updated, updated);
        manager.updateAppWidget(id, views);
    }

    private static void bindActions(Context context, RemoteViews views, int id) {
        PendingIntent refresh = PendingIntent.getBroadcast(
            context,
            20_000 + id,
            new Intent(context, FateMemoryWidgetProvider.class).setAction(ACTION_REFRESH_MEMORY),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.memory_refresh, refresh);
        PendingIntent open = PendingIntent.getActivity(
            context,
            20_000,
            new Intent(context, MainActivity.class),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.memory_widget_root, open);
    }

    static String formatBytes(long bytes) {
        if (bytes <= 0) return "0 B";
        String[] units = {"B", "KB", "MB", "GB", "TB"};
        double value = bytes;
        int unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024;
            unit++;
        }
        String pattern = value >= 100 || unit == 0 ? "0" : value >= 10 ? "0.0" : "0.00";
        return new DecimalFormat(pattern).format(value) + " " + units[unit];
    }
}
