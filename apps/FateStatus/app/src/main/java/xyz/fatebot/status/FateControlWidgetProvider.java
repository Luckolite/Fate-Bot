package xyz.fatebot.status;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.widget.RemoteViews;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class FateControlWidgetProvider extends AppWidgetProvider {
    static final String PREF_NOTICE = "control_widget_notice";
    static final String PREF_NOTICE_ERROR = "control_widget_notice_error";
    static final String PREF_NOTICE_AT = "control_widget_notice_at";
    static final String PREF_NOTICE_PROFILE = "control_widget_notice_profile";
    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();

    @Override
    public void onUpdate(Context context, AppWidgetManager manager, int[] appWidgetIds) {
        refresh(context, manager, appWidgetIds);
        SyncScheduler.schedule(context);
    }

    static void refreshAll(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        int[] ids = manager.getAppWidgetIds(
            new ComponentName(context, FateControlWidgetProvider.class)
        );
        refresh(context, manager, ids);
    }

    static void showPending(Context context, String message) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        int[] ids = manager.getAppWidgetIds(
            new ComponentName(context, FateControlWidgetProvider.class)
        );
        for (int id : ids) {
            render(context, manager, id, FateWidgetProvider.cachedStatus(context), message, false);
        }
    }

    private static void refresh(Context context, AppWidgetManager manager, int[] ids) {
        if (ids == null || ids.length == 0) return;
        Context appContext = context.getApplicationContext();
        BotProfileStore.Profile profile = BotProfileStore.active(appContext);
        StatusData cached = FateWidgetProvider.cachedStatus(appContext);
        for (int id : ids) render(appContext, manager, id, cached, "Refreshing host state…", false);
        EXECUTOR.execute(() -> {
            SharedPreferences preferences = appContext.getSharedPreferences(
                FateWidgetProvider.PREFS,
                Context.MODE_PRIVATE
            );
            StatusData status;
            String error = null;
            try {
                status = FateWidgetProvider.fetchStatus(
                    appContext,
                    profile.endpoint,
                    profile.id
                );
                if (!BotProfileStore.isActive(appContext, profile)) return;
                FateWidgetProvider.saveStatus(appContext, status, profile);
            } catch (Exception failure) {
                if (!BotProfileStore.isActive(appContext, profile)) return;
                status = cached;
                error = failure.getMessage() == null ? "FateControl is unreachable." : failure.getMessage();
            }
            String notice = preferences.getString(PREF_NOTICE, "");
            boolean noticeError = preferences.getBoolean(PREF_NOTICE_ERROR, false);
            long noticeAt = preferences.getLong(PREF_NOTICE_AT, 0);
            String noticeProfile = preferences.getString(PREF_NOTICE_PROFILE, "");
            if (!profile.id.equals(noticeProfile)) notice = "";
            if (System.currentTimeMillis() - noticeAt > 120_000L) notice = "";
            String message = error == null ? notice : error;
            boolean messageError = error != null || noticeError;
            if (!BotProfileStore.isActive(appContext, profile)) return;
            for (int id : ids) render(appContext, manager, id, status, message, messageError);
        });
    }

    private static void render(
        Context context,
        AppWidgetManager manager,
        int id,
        StatusData status,
        String notice,
        boolean noticeError
    ) {
        RemoteViews views = new RemoteViews(context.getPackageName(), R.layout.widget_fate_controls);
        ThemeManager.Palette palette = ThemeManager.palette(context);
        ThemeManager.applyWidgetRoot(context, views, R.id.control_widget_root);
        views.setTextColor(R.id.control_widget_title, palette.text);
        views.setTextColor(R.id.control_widget_host, palette.muted);
        views.setTextColor(R.id.control_widget_updated, palette.muted);
        views.setTextColor(R.id.control_widget_refresh, palette.text);
        views.setTextColor(R.id.control_widget_toggle, palette.text);
        views.setTextColor(R.id.control_widget_restart, palette.secondary);
        views.setTextViewText(R.id.control_widget_title, BotProfileStore.active(context).name);
        views.setInt(
            R.id.control_widget_restart,
            "setBackgroundResource",
            palette.widgetPanel
        );

        boolean hasKey = !ControlKeyStore.read(context).isEmpty();
        boolean controller = status != null && status.controllerOnline;
        boolean running = status != null && status.processRunning;
        String state = status != null && status.online
            ? "BOT ONLINE" : running ? "BOT STARTING" : controller ? "BOT STOPPED" : "OFFLINE";
        int stateColor = status != null && status.online
            ? palette.green : controller ? palette.secondary : palette.red;
        views.setTextViewText(R.id.control_widget_state, state);
        views.setTextColor(R.id.control_widget_state, stateColor);
        views.setTextViewText(
            R.id.control_widget_host,
            !hasKey
                ? "Add the control key in Settings"
                : status == null ? "Waiting for FateControl" : status.instanceName
        );
        views.setTextViewText(R.id.control_widget_toggle, running ? "STOP FATE" : "START FATE");
        boolean toggleEnabled = hasKey && controller && (!running || status.processOwned);
        views.setBoolean(R.id.control_widget_toggle, "setEnabled", toggleEnabled);
        views.setBoolean(R.id.control_widget_restart, "setEnabled", hasKey && controller && running);
        views.setFloat(R.id.control_widget_toggle, "setAlpha", toggleEnabled ? 1f : 0.45f);
        views.setFloat(R.id.control_widget_restart, "setAlpha", hasKey && controller && running ? 1f : 0.45f);
        String footer = notice == null || notice.isEmpty()
            ? status == null || status.fetchedAt == 0
                ? "No data yet"
                : "Updated " + new SimpleDateFormat("h:mm:ss a", Locale.getDefault()).format(new Date(status.fetchedAt))
            : notice;
        views.setTextViewText(R.id.control_widget_updated, footer);
        views.setTextColor(R.id.control_widget_updated, noticeError ? palette.red : palette.muted);
        bindActions(context, views, id);
        manager.updateAppWidget(id, views);
    }

    private static void bindActions(Context context, RemoteViews views, int id) {
        views.setOnClickPendingIntent(
            R.id.control_widget_toggle,
            action(context, id, FateControlActionReceiver.ACTION_TOGGLE)
        );
        views.setOnClickPendingIntent(
            R.id.control_widget_restart,
            action(context, 10_000 + id, FateControlActionReceiver.ACTION_RESTART)
        );
        views.setOnClickPendingIntent(
            R.id.control_widget_refresh,
            action(context, 20_000 + id, FateControlActionReceiver.ACTION_REFRESH)
        );
        PendingIntent open = PendingIntent.getActivity(
            context,
            60_000 + id,
            new Intent(context, MainActivity.class),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        views.setOnClickPendingIntent(R.id.control_widget_root, open);
    }

    private static PendingIntent action(Context context, int requestCode, String action) {
        Intent intent = new Intent(context, FateControlActionReceiver.class).setAction(action);
        return PendingIntent.getBroadcast(
            context,
            requestCode,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
    }
}
