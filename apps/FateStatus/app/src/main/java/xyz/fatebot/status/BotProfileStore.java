package xyz.fatebot.status;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

/** Named FateControl connections. The active profile feeds the app's existing connection contract. */
final class BotProfileStore {
    static final String PREF_ACTIVE_PROFILE = "bot_profile_active";
    private static final String PREF_PROFILES = "bot_profiles_v1";

    static final class Profile {
        final String id;
        final String name;
        final String endpoint;

        Profile(String id, String name, String endpoint) {
            this.id = id;
            this.name = name;
            this.endpoint = endpoint;
        }

        @Override public String toString() {
            return name;
        }
    }

    private BotProfileStore() {}

    static synchronized List<Profile> profiles(Context context) {
        ensureMigrated(context);
        return readProfiles(preferences(context));
    }

    static synchronized Profile active(Context context) {
        ensureMigrated(context);
        SharedPreferences prefs = preferences(context);
        List<Profile> profiles = readProfiles(prefs);
        String activeId = prefs.getString(PREF_ACTIVE_PROFILE, "");
        for (Profile profile : profiles) {
            if (profile.id.equals(activeId)) return profile;
        }
        Profile fallback = profiles.get(0);
        activate(context, fallback.id);
        return fallback;
    }

    static synchronized boolean isActive(Context context, String profileId) {
        ensureMigrated(context);
        return profileId != null && profileId.equals(
            preferences(context).getString(PREF_ACTIVE_PROFILE, "")
        );
    }

    static synchronized boolean isActive(Context context, Profile expected) {
        if (expected == null || !isActive(context, expected.id)) return false;
        for (Profile profile : readProfiles(preferences(context))) {
            if (profile.id.equals(expected.id)) return profile.endpoint.equals(expected.endpoint);
        }
        return false;
    }

    static synchronized Profile create(Context context, String requestedName) throws Exception {
        ensureMigrated(context);
        SharedPreferences prefs = preferences(context);
        List<Profile> profiles = readProfiles(prefs);
        String name = uniqueName(profiles, cleanName(requestedName, "New bot"), null);
        Profile profile = new Profile(
            UUID.randomUUID().toString(),
            name,
            ""
        );
        profiles.add(profile);
        writeProfiles(prefs, profiles);
        activate(context, profile.id);
        ControlKeyStore.save(context, profile.id, "");
        return profile;
    }

    static synchronized void updateActive(
        Context context,
        String requestedName,
        String endpoint,
        String controlKey
    ) throws Exception {
        ensureMigrated(context);
        SharedPreferences prefs = preferences(context);
        String activeId = prefs.getString(PREF_ACTIVE_PROFILE, "");
        List<Profile> profiles = readProfiles(prefs);
        for (int index = 0; index < profiles.size(); index++) {
            Profile existing = profiles.get(index);
            if (!existing.id.equals(activeId)) continue;
            String name = uniqueName(
                profiles,
                cleanName(requestedName, existing.name),
                existing.id
            );
            profiles.set(index, new Profile(existing.id, name, endpoint));
            writeProfiles(prefs, profiles);
            prefs.edit().putString(FateWidgetProvider.PREF_ENDPOINT, endpoint).apply();
            ControlKeyStore.save(context, existing.id, controlKey);
            return;
        }
        throw new IllegalStateException("The active bot profile no longer exists.");
    }

    static synchronized void updateActiveEndpoint(Context context, String endpoint) {
        ensureMigrated(context);
        SharedPreferences prefs = preferences(context);
        String activeId = prefs.getString(PREF_ACTIVE_PROFILE, "");
        List<Profile> profiles = readProfiles(prefs);
        for (int index = 0; index < profiles.size(); index++) {
            Profile existing = profiles.get(index);
            if (existing.id.equals(activeId)) {
                profiles.set(index, new Profile(existing.id, existing.name, endpoint));
                writeProfiles(prefs, profiles);
                prefs.edit().putString(FateWidgetProvider.PREF_ENDPOINT, endpoint).apply();
                return;
            }
        }
    }

    static synchronized void activate(Context context, String profileId) {
        ensureMigrated(context);
        SharedPreferences prefs = preferences(context);
        for (Profile profile : readProfiles(prefs)) {
            if (!profile.id.equals(profileId)) continue;
            prefs.edit()
                .putString(PREF_ACTIVE_PROFILE, profile.id)
                .putString(FateWidgetProvider.PREF_ENDPOINT, profile.endpoint)
                .apply();
            return;
        }
        throw new IllegalArgumentException("Unknown bot profile.");
    }

    static synchronized boolean deleteActive(Context context) {
        ensureMigrated(context);
        SharedPreferences prefs = preferences(context);
        List<Profile> profiles = readProfiles(prefs);
        if (profiles.size() <= 1) return false;
        String activeId = prefs.getString(PREF_ACTIVE_PROFILE, "");
        Profile next = null;
        for (int index = profiles.size() - 1; index >= 0; index--) {
            Profile profile = profiles.get(index);
            if (profile.id.equals(activeId)) {
                profiles.remove(index);
                next = profiles.get(Math.min(index, profiles.size() - 1));
                break;
            }
        }
        if (next == null) return false;
        writeProfiles(prefs, profiles);
        ControlKeyStore.delete(context, activeId);
        prefs.edit()
            .putString(PREF_ACTIVE_PROFILE, next.id)
            .putString(FateWidgetProvider.PREF_ENDPOINT, next.endpoint)
            .apply();
        return true;
    }

    private static void ensureMigrated(Context context) {
        SharedPreferences prefs = preferences(context);
        List<Profile> existing = readProfiles(prefs);
        if (!existing.isEmpty()) return;

        String endpoint = prefs.getString(
            FateWidgetProvider.PREF_ENDPOINT,
            FateWidgetProvider.DEFAULT_ENDPOINT
        );
        String legacyKey = ControlKeyStore.read(context);
        Profile profile = new Profile(UUID.randomUUID().toString(), "Production bot", endpoint);
        List<Profile> profiles = new ArrayList<>();
        profiles.add(profile);
        writeProfiles(prefs, profiles);
        prefs.edit().putString(PREF_ACTIVE_PROFILE, profile.id).apply();
        if (!legacyKey.isEmpty()) {
            try {
                ControlKeyStore.save(context, profile.id, legacyKey);
            } catch (Exception ignored) {
                // The legacy key remains available under the original encrypted preference.
            }
        }
    }

    private static List<Profile> readProfiles(SharedPreferences prefs) {
        List<Profile> result = new ArrayList<>();
        try {
            JSONArray values = new JSONArray(prefs.getString(PREF_PROFILES, "[]"));
            for (int index = 0; index < values.length(); index++) {
                JSONObject value = values.optJSONObject(index);
                if (value == null) continue;
                String id = value.optString("id", "").trim();
                String name = value.optString("name", "").trim();
                String endpoint = value.optString("endpoint", "").trim();
                if (!id.isEmpty() && !name.isEmpty()) {
                    result.add(new Profile(id, name, endpoint));
                }
            }
        } catch (Exception ignored) {
            // Invalid profile metadata falls back to the legacy connection during migration.
        }
        return result;
    }

    private static void writeProfiles(SharedPreferences prefs, List<Profile> profiles) {
        JSONArray values = new JSONArray();
        for (Profile profile : profiles) {
            JSONObject value = new JSONObject();
            try {
                value.put("id", profile.id);
                value.put("name", profile.name);
                value.put("endpoint", profile.endpoint);
                values.put(value);
            } catch (Exception ignored) {
                throw new IllegalStateException("Could not save bot profiles.");
            }
        }
        prefs.edit().putString(PREF_PROFILES, values.toString()).apply();
    }

    private static String cleanName(String requested, String fallback) {
        String value = requested == null ? "" : requested.trim().replaceAll("\\s+", " ");
        if (value.isEmpty()) value = fallback;
        if (value.length() > 40) value = value.substring(0, 40).trim();
        return value;
    }

    private static String uniqueName(List<Profile> profiles, String requested, String ignoredId) {
        String candidate = requested;
        int suffix = 2;
        while (hasName(profiles, candidate, ignoredId)) candidate = requested + " " + suffix++;
        return candidate;
    }

    private static boolean hasName(List<Profile> profiles, String name, String ignoredId) {
        for (Profile profile : profiles) {
            if ((ignoredId == null || !profile.id.equals(ignoredId))
                && profile.name.equalsIgnoreCase(name)) return true;
        }
        return false;
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(FateWidgetProvider.PREFS, Context.MODE_PRIVATE);
    }
}
