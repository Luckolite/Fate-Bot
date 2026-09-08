package xyz.fatebot.status;

import android.content.Context;
import android.content.SharedPreferences;
import android.widget.RemoteViews;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

final class ThemeManager {
    static final String PREF_THEME = "visual_theme";
    static final String PREF_THEME_EFFECTS = "visual_theme_effects";

    static final class Palette {
        final String key;
        final String name;
        final String description;
        final int shell;
        final int background;
        final int surface;
        final int surfaceHigh;
        final int accent;
        final int secondary;
        final int tertiary;
        final int text;
        final int muted;
        final int green;
        final int red;
        final int stroke;
        final int onAccent;
        final int appBackground;
        final int widgetBackground;
        final int widgetPanel;

        Palette(
            String key,
            String name,
            String description,
            int shell,
            int background,
            int surface,
            int surfaceHigh,
            int accent,
            int secondary,
            int tertiary,
            int text,
            int muted,
            int green,
            int red,
            int stroke,
            int onAccent,
            int appBackground,
            int widgetBackground,
            int widgetPanel
        ) {
            this.key = key;
            this.name = name;
            this.description = description;
            this.shell = shell;
            this.background = background;
            this.surface = surface;
            this.surfaceHigh = surfaceHigh;
            this.accent = accent;
            this.secondary = secondary;
            this.tertiary = tertiary;
            this.text = text;
            this.muted = muted;
            this.green = green;
            this.red = red;
            this.stroke = stroke;
            this.onAccent = onAccent;
            this.appBackground = appBackground;
            this.widgetBackground = widgetBackground;
            this.widgetPanel = widgetPanel;
        }
    }

    // IDs, visible ordering, names, and descriptions intentionally match Music Sheets and
    // the Fate Control desktop panel. Persisted IDs are the cross-client theme contract.
    private static final List<Palette> THEMES = Collections.unmodifiableList(Arrays.asList(
        new Palette(
            "amoled_neon", "AMOLED Neon", "True black • cyan, violet, and pink glow",
            0xFF080710, 0xFF000000, 0xFF11101D, 0xFF1B192C,
            0xFF00C8E8, 0xFFC153FF, 0xFFFF3C91, 0xFFF5F4FF,
            0xFFB9B7D1, 0xFF75E83A, 0xFFFF3C91, 0xFF35DDF2,
            0xFF05050A, R.drawable.fate_theme_amoled_neon,
            R.drawable.widget_background_midnight, R.drawable.stat_panel_midnight
        ),
        new Palette(
            "classic", "Classic", "Lime garden • warm golden light",
            0xFF17200C, 0xFF0E1307, 0xFF233214, 0xFF30451C,
            0xFF79AA2D, 0xFFB7DF43, 0xFFE3BD69, 0xFFF7FBE8,
            0xFFC4D19D, 0xFF7ED26A, 0xFFF07872, 0xFF668719,
            0xFF132006, R.drawable.fate_theme_classic,
            R.drawable.widget_background_forest, R.drawable.stat_panel_forest
        ),
        new Palette(
            "clockwork", "Clockwork", "Copper mechanisms • turning gears",
            0xFF25170F, 0xFF160E09, 0xFF392619, 0xFF4A3221,
            0xFF9A5D35, 0xFFD49A4A, 0xFFE5BA7A, 0xFFFFF6E8,
            0xFFD9C1A6, 0xFF9FC37F, 0xFFDF7965, 0xFF9A6F48,
            0xFF24150A, R.drawable.fate_theme_clockwork,
            R.drawable.widget_background_solar, R.drawable.stat_panel_solar
        ),
        new Palette(
            "galaxy", "Galaxy", "Deep space • stars and comet trails",
            0xFF081036, 0xFF03071A, 0xFF101C4D, 0xFF172A68,
            0xFF8654D8, 0xFF5F9DFF, 0xFFC7E5FF, 0xFFF2F6FF,
            0xFFB8C7E8, 0xFF4FD2BD, 0xFFEF7698, 0xFF5576C3,
            0xFF071331, R.drawable.fate_theme_galaxy,
            R.drawable.widget_background_midnight, R.drawable.stat_panel_midnight
        ),
        new Palette(
            "music", "Music", "Orchestral color • floating notes",
            0xFF28102F, 0xFF16091A, 0xFF3B1945, 0xFF53245E,
            0xFF9452BD, 0xFFEF769D, 0xFFF2B5D5, 0xFFFFF2FC,
            0xFFD9BDD7, 0xFF5FC7AD, 0xFFF27691, 0xFF9B6AA4,
            0xFF2C0D20, R.drawable.fate_theme_music,
            R.drawable.widget_background_orchid, R.drawable.stat_panel_orchid
        ),
        new Palette(
            "nature", "Nature", "Living woodland • leaves and fireflies",
            0xFF102419, 0xFF07120C, 0xFF183424, 0xFF245039,
            0xFF4D9661, 0xFF91C95E, 0xFFD9BD72, 0xFFF8FFF2,
            0xFFBDD3B7, 0xFF68CF91, 0xFFE67C72, 0xFF638F63,
            0xFF10200C, R.drawable.fate_theme_nature,
            R.drawable.widget_background_forest, R.drawable.stat_panel_forest
        ),
        new Palette(
            "underwater", "Underwater", "Ocean light • rising bubbles",
            0xFF06232D, 0xFF031116, 0xFF093846, 0xFF0B4B5C,
            0xFF2188AD, 0xFF41CFE1, 0xFFA7E3EA, 0xFFE9FDFF,
            0xFFB0D5DA, 0xFF54C7AC, 0xFFEF7498, 0xFF4F929D,
            0xFF04232A, R.drawable.fate_theme_underwater,
            R.drawable.widget_background_frost, R.drawable.stat_panel_frost
        ),
        new Palette(
            "waterfall", "Waterfall", "Cool cascades • drifting mist and ripples",
            0xFF10243D, 0xFF07101C, 0xFF193451, 0xFF24496D,
            0xFF4B7FC4, 0xFF75B8F0, 0xFFB6D4F1, 0xFFF4FAFF,
            0xFFBFD0DF, 0xFF59C1B8, 0xFFEC7687, 0xFF6688B0,
            0xFF0A2034, R.drawable.fate_theme_waterfall,
            R.drawable.widget_background_frost, R.drawable.stat_panel_frost
        )
    ));

    private ThemeManager() {}

    static List<Palette> themes() {
        return THEMES;
    }

    static Palette palette(Context context) {
        SharedPreferences preferences = context.getSharedPreferences(
            FateWidgetProvider.PREFS,
            Context.MODE_PRIVATE
        );
        return byKey(preferences.getString(PREF_THEME, "classic"));
    }

    static Palette byKey(String key) {
        for (Palette palette : THEMES) {
            if (palette.key.equals(key)) return palette;
        }
        // Preserve a sensible visual direction for installs upgrading from the former six themes.
        if ("midnight".equals(key)) return byKey("amoled_neon");
        if ("orchid".equals(key)) return byKey("music");
        if ("forest".equals(key)) return byKey("nature");
        if ("solar".equals(key)) return byKey("clockwork");
        if ("frost".equals(key)) return byKey("waterfall");
        return byKey("classic");
    }

    static int indexOf(String key) {
        for (int index = 0; index < THEMES.size(); index++) {
            if (THEMES.get(index).key.equals(key)) return index;
        }
        return 0;
    }

    static int menuArtwork(String themeKey, String section) {
        String role = "metrics".equals(section) || "config".equals(section)
            || "backups".equals(section) || "console".equals(section)
            ? section : "overview";
        switch (byKey(themeKey).key + "_" + role) {
            case "amoled_neon_overview": return R.drawable.fate_menu_amoled_neon_overview;
            case "amoled_neon_metrics": return R.drawable.fate_menu_amoled_neon_metrics;
            case "amoled_neon_config": return R.drawable.fate_menu_amoled_neon_config;
            case "amoled_neon_backups": return R.drawable.fate_menu_amoled_neon_backups;
            case "amoled_neon_console": return R.drawable.fate_menu_amoled_neon_console;
            case "classic_overview": return R.drawable.fate_menu_classic_overview;
            case "classic_metrics": return R.drawable.fate_menu_classic_metrics;
            case "classic_config": return R.drawable.fate_menu_classic_config;
            case "classic_backups": return R.drawable.fate_menu_classic_backups;
            case "classic_console": return R.drawable.fate_menu_classic_console;
            case "clockwork_overview": return R.drawable.fate_menu_clockwork_overview;
            case "clockwork_metrics": return R.drawable.fate_menu_clockwork_metrics;
            case "clockwork_config": return R.drawable.fate_menu_clockwork_config;
            case "clockwork_backups": return R.drawable.fate_menu_clockwork_backups;
            case "clockwork_console": return R.drawable.fate_menu_clockwork_console;
            case "galaxy_overview": return R.drawable.fate_menu_galaxy_overview;
            case "galaxy_metrics": return R.drawable.fate_menu_galaxy_metrics;
            case "galaxy_config": return R.drawable.fate_menu_galaxy_config;
            case "galaxy_backups": return R.drawable.fate_menu_galaxy_backups;
            case "galaxy_console": return R.drawable.fate_menu_galaxy_console;
            case "music_overview": return R.drawable.fate_menu_music_overview;
            case "music_metrics": return R.drawable.fate_menu_music_metrics;
            case "music_config": return R.drawable.fate_menu_music_config;
            case "music_backups": return R.drawable.fate_menu_music_backups;
            case "music_console": return R.drawable.fate_menu_music_console;
            case "nature_overview": return R.drawable.fate_menu_nature_overview;
            case "nature_metrics": return R.drawable.fate_menu_nature_metrics;
            case "nature_config": return R.drawable.fate_menu_nature_config;
            case "nature_backups": return R.drawable.fate_menu_nature_backups;
            case "nature_console": return R.drawable.fate_menu_nature_console;
            case "underwater_overview": return R.drawable.fate_menu_underwater_overview;
            case "underwater_metrics": return R.drawable.fate_menu_underwater_metrics;
            case "underwater_config": return R.drawable.fate_menu_underwater_config;
            case "underwater_backups": return R.drawable.fate_menu_underwater_backups;
            case "underwater_console": return R.drawable.fate_menu_underwater_console;
            case "waterfall_overview": return R.drawable.fate_menu_waterfall_overview;
            case "waterfall_metrics": return R.drawable.fate_menu_waterfall_metrics;
            case "waterfall_config": return R.drawable.fate_menu_waterfall_config;
            case "waterfall_backups": return R.drawable.fate_menu_waterfall_backups;
            case "waterfall_console": return R.drawable.fate_menu_waterfall_console;
            default: return R.drawable.fate_menu_classic_overview;
        }
    }

    static boolean effectsEnabled(Context context) {
        return context.getSharedPreferences(FateWidgetProvider.PREFS, Context.MODE_PRIVATE)
            .getBoolean(PREF_THEME_EFFECTS, true);
    }

    static void setEffectsEnabled(Context context, boolean enabled) {
        context.getSharedPreferences(FateWidgetProvider.PREFS, Context.MODE_PRIVATE)
            .edit().putBoolean(PREF_THEME_EFFECTS, enabled).apply();
    }

    static String[] names() {
        String[] names = new String[THEMES.size()];
        for (int index = 0; index < THEMES.size(); index++) {
            names[index] = THEMES.get(index).name;
        }
        return names;
    }

    static void applyWidgetRoot(Context context, RemoteViews views, int rootId) {
        Palette palette = palette(context);
        views.setInt(rootId, "setBackgroundResource", palette.widgetBackground);
    }
}
