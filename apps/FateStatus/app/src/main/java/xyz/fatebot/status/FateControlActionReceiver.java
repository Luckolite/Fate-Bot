package xyz.fatebot.status;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

import org.json.JSONObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class FateControlActionReceiver extends BroadcastReceiver {
    static final String ACTION_TOGGLE = "xyz.fatebot.status.CONTROL_TOGGLE";
    static final String ACTION_RESTART = "xyz.fatebot.status.CONTROL_RESTART";
    static final String ACTION_REFRESH = "xyz.fatebot.status.CONTROL_REFRESH";
    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();

    @Override
    public void onReceive(Context context, Intent intent) {
        Context appContext = context.getApplicationContext();
        String action = intent.getAction();
        if (ACTION_REFRESH.equals(action)) {
            FateControlWidgetProvider.refreshAll(appContext);
            return;
        }
        if (!ACTION_TOGGLE.equals(action) && !ACTION_RESTART.equals(action)) return;
        PendingResult pending = goAsync();
        FateControlWidgetProvider.showPending(
            appContext,
            ACTION_RESTART.equals(action) ? "Restarting Fate…" : "Sending service command…"
        );
        BotProfileStore.Profile profile = BotProfileStore.active(appContext);
        EXECUTOR.execute(() -> {
            SharedPreferences preferences = appContext.getSharedPreferences(
                FateWidgetProvider.PREFS,
                Context.MODE_PRIVATE
            );
            String endpoint = profile.endpoint;
            String key = ControlKeyStore.read(appContext, profile.id);
            String notice;
            boolean error = false;
            try {
                if (key.isEmpty()) throw new IllegalStateException("Add the control key in Settings.");
                StatusData current = FateApi.status(endpoint, key);
                JSONObject response;
                if (ACTION_RESTART.equals(action)) {
                    response = FateApi.request(
                        "POST",
                        endpoint,
                        "/api/v1/bot/action",
                        key,
                        new JSONObject().put("action", "restart")
                    );
                    notice = "Fate restart requested.";
                } else {
                    boolean running = !current.processRunning;
                    response = FateApi.request(
                        "POST",
                        endpoint,
                        "/api/v1/bot/state",
                        key,
                        new JSONObject().put("running", running)
                    );
                    notice = running ? "Fate start requested." : "Fate stopped.";
                }
                JSONObject status = response.optJSONObject("status");
                if (status != null && BotProfileStore.isActive(appContext, profile)) {
                    FateWidgetProvider.saveStatus(appContext, StatusData.fromJson(status), profile);
                }
            } catch (Exception failure) {
                notice = failure.getMessage() == null ? "Control request failed." : failure.getMessage();
                error = true;
            }
            if (!BotProfileStore.isActive(appContext, profile)) {
                pending.finish();
                return;
            }
            preferences.edit()
                .putString(FateControlWidgetProvider.PREF_NOTICE, notice)
                .putBoolean(FateControlWidgetProvider.PREF_NOTICE_ERROR, error)
                .putLong(FateControlWidgetProvider.PREF_NOTICE_AT, System.currentTimeMillis())
                .putString(FateControlWidgetProvider.PREF_NOTICE_PROFILE, profile.id)
                .apply();
            FateControlWidgetProvider.refreshAll(appContext);
            FateWidgetProvider.refreshAll(appContext);
            pending.finish();
        });
    }
}
