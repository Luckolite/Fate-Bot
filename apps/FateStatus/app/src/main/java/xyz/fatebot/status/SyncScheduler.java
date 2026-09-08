package xyz.fatebot.status;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;

final class SyncScheduler {
    static final String PREF_INTERVAL = "sync_interval_minutes";
    static final String PREF_AUTO_DISCOVER = "auto_discover";

    private SyncScheduler() {}

    static int intervalMinutes(Context context) {
        int stored = context.getSharedPreferences(FateWidgetProvider.PREFS, Context.MODE_PRIVATE)
            .getInt(PREF_INTERVAL, 5);
        return stored == 1 ? 1 : 5;
    }

    static void schedule(Context context) {
        Context appContext = context.getApplicationContext();
        AppWidgetManager manager = AppWidgetManager.getInstance(appContext);
        boolean hasScheduledWidget = hasWidgets(manager, appContext, FateWidgetProvider.class)
            || hasWidgets(manager, appContext, FateMemoryWidgetProvider.class)
            || hasWidgets(manager, appContext, FateConsoleWidgetProvider.class)
            || hasWidgets(manager, appContext, FateControlWidgetProvider.class);
        AlarmManager alarms = (AlarmManager) appContext.getSystemService(Context.ALARM_SERVICE);
        if (alarms == null) return;
        Intent intent = new Intent(appContext, FateWidgetProvider.class)
            .setAction(FateWidgetProvider.ACTION_SCHEDULED);
        PendingIntent pending = PendingIntent.getBroadcast(
            appContext,
            4105,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        if (!hasScheduledWidget) {
            alarms.cancel(pending);
            return;
        }
        long delay = intervalMinutes(appContext) * 60_000L;
        alarms.set(
            AlarmManager.ELAPSED_REALTIME_WAKEUP,
            SystemClock.elapsedRealtime() + delay,
            pending
        );
    }

    private static boolean hasWidgets(
        AppWidgetManager manager,
        Context context,
        Class<?> provider
    ) {
        int[] ids = manager.getAppWidgetIds(new ComponentName(context, provider));
        return ids != null && ids.length > 0;
    }
}
