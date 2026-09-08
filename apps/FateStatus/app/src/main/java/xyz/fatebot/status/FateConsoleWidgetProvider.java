package xyz.fatebot.status;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.Spannable;
import android.text.SpannableStringBuilder;
import android.text.style.ForegroundColorSpan;
import android.widget.RemoteViews;

import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class FateConsoleWidgetProvider extends AppWidgetProvider {
    static final String ACTION_REFRESH_CONSOLE = "xyz.fatebot.status.REFRESH_CONSOLE";
    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] appWidgetIds) {
        refresh(context, manager, appWidgetIds);
        SyncScheduler.schedule(context);
    }

    @Override
    public void onAppWidgetOptionsChanged(
        Context context,
        AppWidgetManager manager,
        int appWidgetId,
        Bundle newOptions
    ) {
        refresh(context, manager, new int[]{appWidgetId});
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        super.onReceive(context, intent);
        if (ACTION_REFRESH_CONSOLE.equals(intent.getAction())) {
            refreshAll(context);
        }
    }

    static void refreshAll(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        int[] ids = manager.getAppWidgetIds(
            new ComponentName(context, FateConsoleWidgetProvider.class)
        );
        refresh(context, manager, ids);
    }

    private static void refresh(Context context, AppWidgetManager manager, int[] ids) {
        if (ids == null || ids.length == 0) return;
        Context appContext = context.getApplicationContext();
        BotProfileStore.Profile profile = BotProfileStore.active(appContext);
        for (int id : ids) renderLoading(appContext, manager, id);
        EXECUTOR.execute(() -> {
            String endpoint = profile.endpoint;
            String key = ControlKeyStore.read(appContext, profile.id);
            if (key.isEmpty()) {
                if (!BotProfileStore.isActive(appContext, profile)) return;
                for (int id : ids) {
                    renderMessage(
                        appContext,
                        manager,
                        id,
                        "CONTROL KEY NEEDED",
                        "Open Settings and add the FateControl key to view private console output.",
                        R.color.fate_gold
                    );
                }
                return;
            }
            try {
                JSONObject response = FateApi.request(
                    "GET",
                    endpoint,
                    "/api/v1/console?lines=100",
                    key,
                    null
                );
                String output = response.optString("text", "");
                boolean truncated = response.optBoolean("truncated", false);
                if (!BotProfileStore.isActive(appContext, profile)) return;
                for (int id : ids) {
                    renderConsole(appContext, manager, id, output, truncated);
                }
            } catch (Exception error) {
                if (!BotProfileStore.isActive(appContext, profile)) return;
                String message = error.getMessage() == null
                    ? "FateControl is unreachable."
                    : error.getMessage();
                for (int id : ids) {
                    renderMessage(
                        appContext,
                        manager,
                        id,
                        "CONSOLE OFFLINE",
                        message + "\n\nTap refresh to try again.",
                        R.color.fate_red
                    );
                }
            }
        });
    }

    private static void renderLoading(Context context, AppWidgetManager manager, int id) {
        RemoteViews views = views(context);
        bindActions(context, views, id);
        views.setTextViewText(R.id.console_widget_state, "SYNCING");
        views.setTextColor(R.id.console_widget_state, ThemeManager.palette(context).secondary);
        views.setTextViewText(R.id.console_widget_output, "Fetching Fate's latest output…");
        views.setTextViewText(R.id.console_widget_updated, "Please wait");
        manager.updateAppWidget(id, views);
    }

    private static void renderConsole(
        Context context,
        AppWidgetManager manager,
        int id,
        String output,
        boolean truncated
    ) {
        int maxLines = visibleLineCount(manager, id);
        String visible = newestLines(output, maxLines);
        RemoteViews views = views(context);
        bindActions(context, views, id);
        views.setTextViewText(R.id.console_widget_state, output.isEmpty() ? "QUIET" : "LIVE TAIL");
        ThemeManager.Palette palette = ThemeManager.palette(context);
        views.setTextColor(
            R.id.console_widget_state,
            output.isEmpty() ? palette.muted : palette.green
        );
        views.setInt(R.id.console_widget_output, "setMaxLines", maxLines);
        views.setTextViewText(
            R.id.console_widget_output,
            colorOutput(context, visible.isEmpty() ? "Fate's console is quiet." : visible)
        );
        String updated = "Updated " + new SimpleDateFormat(
            "h:mm:ss a",
            Locale.getDefault()
        ).format(new Date());
        views.setTextViewText(
            R.id.console_widget_updated,
            truncated ? updated + "  •  newest output" : updated
        );
        manager.updateAppWidget(id, views);
    }

    private static void renderMessage(
        Context context,
        AppWidgetManager manager,
        int id,
        String state,
        String message,
        int stateColor
    ) {
        RemoteViews views = views(context);
        bindActions(context, views, id);
        views.setTextViewText(R.id.console_widget_state, state);
        ThemeManager.Palette palette = ThemeManager.palette(context);
        int resolved = stateColor == R.color.fate_red
            ? palette.red : stateColor == R.color.fate_gold ? palette.secondary : palette.muted;
        views.setTextColor(R.id.console_widget_state, resolved);
        views.setTextViewText(R.id.console_widget_output, message);
        views.setTextViewText(R.id.console_widget_updated, "Tap widget to open Console");
        manager.updateAppWidget(id, views);
    }

    private static RemoteViews views(Context context) {
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.widget_fate_console);
        ThemeManager.Palette palette = ThemeManager.palette(context);
        ThemeManager.applyWidgetRoot(context, views, R.id.console_widget_root);
        views.setTextViewText(
            R.id.console_widget_header,
            BotProfileStore.active(context).name.toUpperCase(Locale.ROOT) + "  •  CONSOLE"
        );
        views.setTextColor(R.id.console_widget_header, palette.secondary);
        views.setTextColor(R.id.console_widget_refresh, palette.text);
        views.setTextColor(R.id.console_widget_output, palette.text);
        views.setTextColor(R.id.console_widget_updated, palette.muted);
        return views;
    }

    private static void bindActions(Context context, RemoteViews views, int id) {
        PendingIntent refresh = PendingIntent.getBroadcast(
            context,
            40_000 + id,
            new Intent(context, FateConsoleWidgetProvider.class)
                .setAction(ACTION_REFRESH_CONSOLE),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.console_widget_refresh, refresh);

        Intent openIntent = new Intent(context, MainActivity.class)
            .putExtra(MainActivity.EXTRA_OPEN_PAGE, MainActivity.PAGE_CONSOLE);
        PendingIntent open = PendingIntent.getActivity(
            context,
            50_000 + id,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.console_widget_root, open);
        views.setOnClickPendingIntent(R.id.console_widget_output, open);
    }

    private static int visibleLineCount(AppWidgetManager manager, int id) {
        Bundle options = manager.getAppWidgetOptions(id);
        int height = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 180);
        return Math.max(5, Math.min(30, (height - 72) / 14));
    }

    static String newestLines(String output, int maximum) {
        if (output == null || output.isEmpty()) return "";
        String[] lines = output.split("\\R", -1);
        int start = Math.max(0, lines.length - Math.max(1, maximum));
        StringBuilder visible = new StringBuilder();
        for (int index = start; index < lines.length; index++) {
            if (visible.length() > 0) visible.append('\n');
            visible.append(lines[index]);
        }
        if (visible.length() > 4_000) {
            return "…" + visible.substring(visible.length() - 3_999);
        }
        return visible.toString();
    }

    private static CharSequence colorOutput(Context context, String output) {
        SpannableStringBuilder styled = new SpannableStringBuilder(output);
        int start = 0;
        while (start < output.length()) {
            int newline = output.indexOf('\n', start);
            int end = newline < 0 ? output.length() : newline;
            String line = output.substring(start, end).toLowerCase(Locale.ROOT);
            ThemeManager.Palette palette = ThemeManager.palette(context);
            int color = line.contains("traceback") || line.contains("exception")
                || line.contains("error") || line.contains("critical")
                ? palette.red
                : line.contains("warning") || line.contains("warn")
                    ? palette.secondary
                    : palette.text;
            styled.setSpan(
                new ForegroundColorSpan(color),
                start,
                end,
                Spannable.SPAN_EXCLUSIVE_EXCLUSIVE
            );
            start = end + 1;
        }
        return styled;
    }
}
