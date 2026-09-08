package xyz.fatebot.status;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.animation.AccelerateDecelerateInterpolator;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.ArrayAdapter;
import android.widget.AdapterView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    static final String EXTRA_OPEN_PAGE = "open_page";
    static final int PAGE_OVERVIEW = 0;
    static final int PAGE_METRICS = 1;
    static final int PAGE_CONFIG = 2;
    static final int PAGE_CONSOLE = 3;
    static final int PAGE_SETTINGS = 4;
    private static final int LEGACY_PAGE_PANEL = 5;
    private static final String STATE_CURRENT_PAGE = "current_page";
    private int EMBER;
    private int GOLD;
    private int ROSE;
    private int GREEN;
    private int RED;
    private int TEXT;
    private int MUTED;
    private int SURFACE;
    private int SURFACE_HIGH;
    private int SHELL;
    private int STROKE;
    private ThemeManager.Palette palette;

    private final ExecutorService executor = Executors.newFixedThreadPool(4);
    private final Handler handler = new Handler(Looper.getMainLooper());
    private SharedPreferences prefs;
    private FrameLayout content;
    private ImageView ambientThemeBackground;
    private int currentPage;
    private volatile boolean statusLoadInFlight;

    private Button overviewButton;
    private LinearLayout nativeNavigation;
    private SharedControlPanel sharedControlPanel;

    private SettingsDraft pendingSettingsDraft;
    private BackupForm backupForm;
    private boolean backupExpanded;
    private boolean awaitingDriveAuthorization;

    private static final class SettingsDraft {
        final String profileId;
        final String profileName;
        final String endpoint;
        final String controlKey;
        final boolean[] metrics;
        final int intervalPosition;

        SettingsDraft(
            String profileId,
            String profileName,
            String endpoint,
            String controlKey,
            boolean[] metrics,
            int intervalPosition
        ) {
            this.profileId = profileId;
            this.profileName = profileName;
            this.endpoint = endpoint;
            this.controlKey = controlKey;
            this.metrics = metrics;
            this.intervalPosition = intervalPosition;
        }
    }

    private static final class BackupForm {
        final LinearLayout body;
        final Button expand;
        final Switch enabled;
        final Switch includeLocalFiles;
        final Switch driveEnabled;
        final EditText location;
        final EditText frequency;
        final EditText retentionDays;
        final EditText maxStorage;
        final EditText maxBackups;
        final TextView status;
        final TextView driveStatus;
        final Button save;
        final Button linkDrive;
        final Button selectFolder;
        boolean loading = true;
        boolean linked;
        boolean driveConfigured;
        String folderId = "";
        String folderPath = "";
        String revision = "";

        BackupForm(
            LinearLayout body,
            Button expand,
            Switch enabled,
            Switch includeLocalFiles,
            Switch driveEnabled,
            EditText location,
            EditText frequency,
            EditText retentionDays,
            EditText maxStorage,
            EditText maxBackups,
            TextView status,
            TextView driveStatus,
            Button save,
            Button linkDrive,
            Button selectFolder
        ) {
            this.body = body;
            this.expand = expand;
            this.enabled = enabled;
            this.includeLocalFiles = includeLocalFiles;
            this.driveEnabled = driveEnabled;
            this.location = location;
            this.frequency = frequency;
            this.retentionDays = retentionDays;
            this.maxStorage = maxStorage;
            this.maxBackups = maxBackups;
            this.status = status;
            this.driveStatus = driveStatus;
            this.save = save;
            this.linkDrive = linkDrive;
            this.selectFolder = selectFolder;
        }
    }

    private static final class MetricSpec {
        final String key;
        final String title;
        final String description;
        final String secondaryKey;
        final String primaryLabel;
        final String secondaryLabel;

        MetricSpec(String key, String title, String description) {
            this(key, title, description, null, null, null);
        }

        MetricSpec(
            String key,
            String title,
            String description,
            String secondaryKey,
            String primaryLabel,
            String secondaryLabel
        ) {
            this.key = key;
            this.title = title;
            this.description = description;
            this.secondaryKey = secondaryKey;
            this.primaryLabel = primaryLabel;
            this.secondaryLabel = secondaryLabel;
        }
    }

    private final Runnable poller = new Runnable() {
        @Override
        public void run() {
            refreshStatusCache();
            handler.postDelayed(this, SyncScheduler.intervalMinutes(MainActivity.this) * 60_000L);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(FateWidgetProvider.PREFS, Context.MODE_PRIVATE);
        BotProfileStore.active(this);
        sharedControlPanel = new SharedControlPanel(
            this,
            prefs,
            executor,
            handler,
            () -> showPage(PAGE_SETTINGS)
        );
        sharedControlPanel.prewarm();
        applyPalette();
        getWindow().setStatusBarColor(SHELL);
        getWindow().setNavigationBarColor(SHELL);
        setContentView(buildShell());
        int requestedPage = savedInstanceState == null
            ? pageFromIntent(getIntent())
            : savedInstanceState.getInt(STATE_CURRENT_PAGE, pageFromIntent(getIntent()));
        showPage(requestedPage);
        SyncScheduler.schedule(this);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        showPage(pageFromIntent(intent));
    }

    private int pageFromIntent(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data != null
            && "fatecontrol".equalsIgnoreCase(data.getScheme())
            && "backups".equalsIgnoreCase(data.getHost())) {
            awaitingDriveAuthorization = true;
            return PAGE_SETTINGS;
        }
        return clampPage(intent == null
            ? PAGE_OVERVIEW
            : intent.getIntExtra(EXTRA_OPEN_PAGE, PAGE_OVERVIEW));
    }

    private int clampPage(int page) {
        if (page == LEGACY_PAGE_PANEL) return PAGE_OVERVIEW;
        return page >= PAGE_OVERVIEW && page <= PAGE_SETTINGS ? page : PAGE_OVERVIEW;
    }

    private void applyPalette() {
        palette = ThemeManager.palette(this);
        EMBER = palette.accent;
        GOLD = palette.secondary;
        ROSE = palette.tertiary;
        GREEN = palette.green;
        RED = palette.red;
        TEXT = palette.text;
        MUTED = palette.muted;
        SURFACE = palette.surface;
        SURFACE_HIGH = palette.surfaceHigh;
        SHELL = palette.shell;
        STROKE = palette.stroke;
    }

    private View buildShell() {
        FrameLayout shell = new FrameLayout(this);
        shell.setBackgroundColor(SHELL);

        ImageView themeBackground = new ImageView(this);
        ambientThemeBackground = themeBackground;
        themeBackground.setImageResource(palette.appBackground);
        themeBackground.setScaleType(ImageView.ScaleType.CENTER_CROP);
        themeBackground.setAlpha(0.76f);
        themeBackground.setScaleX(1.04f);
        themeBackground.setScaleY(1.04f);
        themeBackground.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        shell.addView(themeBackground, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        ));
        if (ThemeManager.effectsEnabled(this)) {
            themeBackground.post(() -> animateThemeBackground(themeBackground, true));
        }
        themeBackground.addOnAttachStateChangeListener(new View.OnAttachStateChangeListener() {
            @Override public void onViewAttachedToWindow(View view) {}
            @Override public void onViewDetachedFromWindow(View view) { view.animate().cancel(); }
        });

        View shade = new View(this);
        shade.setBackgroundColor(0x3D000000);
        shade.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        shell.addView(shade, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        ));

        GearView upperGear = decorativeGear(78, false, 13_000L);
        upperGear.setAlpha(0.27f);
        FrameLayout.LayoutParams upperGearParams = new FrameLayout.LayoutParams(dp(78), dp(78));
        upperGearParams.gravity = Gravity.TOP | Gravity.END;
        upperGearParams.topMargin = dp(42);
        upperGearParams.rightMargin = dp(8);
        shell.addView(upperGear, upperGearParams);

        GearView lowerGear = decorativeGear(52, true, 9_000L);
        lowerGear.setAlpha(0.2f);
        FrameLayout.LayoutParams lowerGearParams = new FrameLayout.LayoutParams(dp(52), dp(52));
        lowerGearParams.gravity = Gravity.BOTTOM | Gravity.START;
        lowerGearParams.leftMargin = dp(10);
        lowerGearParams.bottomMargin = dp(66);
        shell.addView(lowerGear, lowerGearParams);

        LinearLayout chrome = new LinearLayout(this);
        chrome.setOrientation(LinearLayout.VERTICAL);
        chrome.setBackgroundColor(Color.TRANSPARENT);
        chrome.setOnApplyWindowInsetsListener((view, insets) -> {
            int left;
            int top;
            int right;
            int bottom;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                android.graphics.Insets systemBars = insets.getInsets(
                    WindowInsets.Type.systemBars()
                );
                left = systemBars.left;
                top = systemBars.top;
                right = systemBars.right;
                bottom = systemBars.bottom;
            } else {
                left = insets.getSystemWindowInsetLeft();
                top = insets.getSystemWindowInsetTop();
                right = insets.getSystemWindowInsetRight();
                bottom = insets.getSystemWindowInsetBottom();
            }
            view.setPadding(left, top, right, bottom);
            return insets;
        });

        content = new FrameLayout(this);
        chrome.addView(content, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            0,
            1f
        ));

        LinearLayout nav = new LinearLayout(this);
        nativeNavigation = nav;
        nav.setOrientation(LinearLayout.VERTICAL);
        nav.setPadding(dp(12), dp(8), dp(12), dp(10));
        nav.setBackgroundColor(SHELL);
        overviewButton = navButton("Back to panel", PAGE_OVERVIEW);
        LinearLayout firstRow = navigationRow();
        firstRow.addView(overviewButton, weighted());
        nav.addView(firstRow, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ));
        chrome.addView(nav, new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ));
        shell.addView(chrome, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        ));
        chrome.requestApplyInsets();
        return shell;
    }

    private void animateThemeBackground(ImageView background, boolean forward) {
        if (!background.isAttachedToWindow() || !ThemeManager.effectsEnabled(this)) return;
        background.animate()
            .scaleX(forward ? 1.11f : 1.04f)
            .scaleY(forward ? 1.11f : 1.04f)
            .translationX(forward ? dp(8) : -dp(6))
            .translationY(forward ? -dp(6) : dp(5))
            .setDuration(12_000L)
            .setInterpolator(new AccelerateDecelerateInterpolator())
            .withEndAction(() -> animateThemeBackground(background, !forward))
            .start();
    }

    private void setThemeEffectsRunning(boolean enabled) {
        ThemeManager.setEffectsEnabled(this, enabled);
        if (ambientThemeBackground == null) return;
        ambientThemeBackground.animate().cancel();
        if (enabled) {
            animateThemeBackground(ambientThemeBackground, true);
        } else {
            ambientThemeBackground.setScaleX(1.04f);
            ambientThemeBackground.setScaleY(1.04f);
            ambientThemeBackground.setTranslationX(0f);
            ambientThemeBackground.setTranslationY(0f);
        }
    }

    private LinearLayout navigationRow() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER);
        return row;
    }

    private Button navButton(String label, int page) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setTextSize(11);
        button.setTextColor(MUTED);
        button.setBackgroundColor(Color.TRANSPARENT);
        button.setMinWidth(0);
        button.setSingleLine(true);
        button.setContentDescription(label + " tab");
        button.setPadding(dp(2), 0, dp(2), 0);
        button.setOnClickListener(view -> showPage(page));
        return button;
    }

    private void showPage(int page) {
        String sharedSection = page == PAGE_METRICS ? "metrics"
            : page == PAGE_CONFIG ? "config"
            : page == PAGE_CONSOLE ? "console" : "overview";
        page = clampPage(page);
        if (page != PAGE_SETTINGS) page = PAGE_OVERVIEW;
        currentPage = page;
        content.removeAllViews();
        nativeNavigation.setVisibility(page == PAGE_OVERVIEW ? View.GONE : View.VISIBLE);
        overviewButton.setTextColor(GOLD);
        View pageView = page == PAGE_OVERVIEW
            ? sharedControlPanel.show(sharedSection)
            : buildSettings();
        content.addView(pageView);
    }

    @SuppressLint("SetTextI18n")
    private View buildSettings() {
        SettingsDraft restoredDraft = pendingSettingsDraft;
        pendingSettingsDraft = null;
        ScrollView scroll = scroll();
        LinearLayout root = pageRoot();
        addDecoratedPageHeading(
            root,
            "SETTINGS",
            "Connection & rhythm",
            "Choose a visual theme, connect to FateControl, and decide what your live widgets show.",
            true
        );

        LinearLayout appearance = card();
        appearance.addView(label("Visual theme", 18, TEXT, true));
        appearance.addView(label(
            "Selecting a theme saves and applies it immediately across the app and every Fate home-screen widget.",
            12,
            MUTED,
            false
        ), top(dp(5)));
        Spinner themeSpinner = new Spinner(this);
        themeSpinner.setAdapter(new ArrayAdapter<>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            ThemeManager.names()
        ));
        themeSpinner.setSelection(ThemeManager.indexOf(palette.key));
        themeSpinner.setBackground(rounded(SURFACE_HIGH, 14, 1, STROKE));
        ImageView themePreview = new ImageView(this);
        themePreview.setImageResource(palette.appBackground);
        themePreview.setScaleType(ImageView.ScaleType.CENTER_CROP);
        themePreview.setBackground(rounded(SURFACE_HIGH, 16, 1, STROKE));
        themePreview.setClipToOutline(true);
        LinearLayout.LayoutParams themePreviewParams = fixed(dp(154), 0);
        themePreviewParams.topMargin = dp(12);
        appearance.addView(themePreview, themePreviewParams);
        TextView themeDescription = label(palette.description, 12, MUTED, false);
        appearance.addView(themeSpinner, top(dp(10)));
        appearance.addView(themeDescription, top(dp(8)));
        Switch themeEffects = new Switch(this);
        themeEffects.setText("Ambient effects");
        themeEffects.setTextColor(TEXT);
        themeEffects.setChecked(ThemeManager.effectsEnabled(this));
        themeEffects.setPadding(0, dp(7), 0, dp(7));
        appearance.addView(themeEffects, top(dp(9)));
        appearance.addView(label(
            "Gently moves generated theme artwork. Android's animation controls can still reduce motion.",
            11,
            MUTED,
            false
        ));
        themeEffects.setOnCheckedChangeListener((button, checked) -> setThemeEffectsRunning(checked));
        root.addView(appearance, bottom(dp(12)));

        LinearLayout connection = card();
        connection.addView(label("Bot profiles", 18, TEXT, true));
        connection.addView(label(
            "Keep separate FateControl addresses and control keys for production, testing, or any other bot. Switching changes the whole app and its widgets.",
            12,
            MUTED,
            false
        ), top(dp(5)));
        List<BotProfileStore.Profile> profiles = BotProfileStore.profiles(this);
        BotProfileStore.Profile activeProfile = BotProfileStore.active(this);
        if (restoredDraft != null && !activeProfile.id.equals(restoredDraft.profileId)) {
            restoredDraft = null;
        }
        Spinner profileSpinner = new Spinner(this);
        profileSpinner.setAdapter(new ArrayAdapter<>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            profiles
        ));
        int activeProfileIndex = 0;
        for (int index = 0; index < profiles.size(); index++) {
            if (profiles.get(index).id.equals(activeProfile.id)) activeProfileIndex = index;
        }
        profileSpinner.setSelection(activeProfileIndex);
        profileSpinner.setBackground(rounded(SURFACE_HIGH, 14, 1, STROKE));
        connection.addView(profileSpinner, top(dp(12)));
        connection.addView(fieldLabel("PROFILE NAME"), top(dp(12)));
        EditText profileName = field(
            "Production bot",
            InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_WORDS
        );
        profileName.setText(restoredDraft == null ? activeProfile.name : restoredDraft.profileName);
        connection.addView(profileName);
        LinearLayout profileActions = horizontal();
        Button addProfile = secondaryButton("Add profile");
        profileActions.addView(addProfile, new LinearLayout.LayoutParams(0, dp(50), 1f));
        Button deleteProfile = secondaryButton("Delete profile");
        deleteProfile.setEnabled(profiles.size() > 1);
        deleteProfile.setAlpha(profiles.size() > 1 ? 1f : 0.45f);
        LinearLayout.LayoutParams deleteProfileParams = new LinearLayout.LayoutParams(0, dp(50), 1f);
        deleteProfileParams.leftMargin = dp(8);
        profileActions.addView(deleteProfile, deleteProfileParams);
        connection.addView(profileActions, top(dp(10)));
        addProfile.setOnClickListener(view -> {
            try {
                BotProfileStore.create(this, "New bot");
                pendingSettingsDraft = null;
                onBotProfileChanged("New profile ready. Add its address and control key.");
            } catch (Exception error) {
                toast("Could not create a bot profile.");
            }
        });
        deleteProfile.setOnClickListener(view -> new AlertDialog.Builder(this)
            .setTitle("Delete " + activeProfile.name + "?")
            .setMessage("This removes its saved address and encrypted control key from this device.")
            .setPositiveButton("Delete", (dialog, which) -> {
                if (BotProfileStore.deleteActive(this)) {
                    pendingSettingsDraft = null;
                    onBotProfileChanged("Profile deleted.");
                }
            })
            .setNegativeButton("Cancel", null)
            .show());
        profileSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                BotProfileStore.Profile selected = profiles.get(position);
                if (selected.id.equals(BotProfileStore.active(MainActivity.this).id)) return;
                BotProfileStore.activate(MainActivity.this, selected.id);
                pendingSettingsDraft = null;
                onBotProfileChanged("Switched to " + selected.name + ".");
            }
            @Override public void onNothingSelected(AdapterView<?> parent) {}
        });
        connection.addView(fieldLabel("ADDRESS"), top(dp(14)));
        EditText endpoint = field("192.168.1.20:16421", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        endpoint.setText(restoredDraft == null
            ? prefs.getString(FateWidgetProvider.PREF_ENDPOINT, FateWidgetProvider.DEFAULT_ENDPOINT)
            : restoredDraft.endpoint);
        connection.addView(endpoint);
        connection.addView(label(
            "Saved on this device and reused after closing, rebooting, or updating the app.",
            11,
            MUTED,
            false
        ), top(dp(7)));
        connection.addView(fieldLabel("CONTROL KEY"), top(dp(12)));
        EditText key = field("Stored only on this device", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        key.setText(restoredDraft == null ? ControlKeyStore.read(this) : restoredDraft.controlKey);
        connection.addView(key);
        TextView keySaveStatus = label(
            key.getText().toString().trim().isEmpty()
                ? "Enter the key to save and verify it."
                : "Saved on this device. Verifying…",
            11,
            key.getText().toString().trim().isEmpty() ? MUTED : GOLD,
            false
        );
        connection.addView(keySaveStatus, top(dp(6)));
        key.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence value, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence value, int start, int before, int count) {}
            @Override public void afterTextChanged(Editable value) {
                String enteredKey = value.toString().trim();
                if (enteredKey.isEmpty()) {
                    keySaveStatus.setText("No control key will be stored for this profile.");
                    keySaveStatus.setTextColor(MUTED);
                    return;
                }
                keySaveStatus.setText("Ready to save and test.");
                keySaveStatus.setTextColor(GOLD);
            }
        });
        if (!key.getText().toString().trim().isEmpty()) {
            Runnable pendingKeyValidation = () -> verifyControlKey(
                activeProfile,
                endpoint.getText().toString(),
                key.getText().toString().trim(),
                keySaveStatus
            );
            handler.postDelayed(pendingKeyValidation, 350L);
        }
        Button scan = secondaryButton("Find Fate on this network");
        scan.setOnClickListener(view -> scanNetwork());
        connection.addView(scan, top(dp(12)));
        root.addView(connection, bottom(dp(12)));

        root.addView(buildBackupSettings(), bottom(dp(12)));

        LinearLayout widgetMetrics = card();
        widgetMetrics.addView(label("Home widget metrics", 18, TEXT, true));
        widgetMetrics.addView(label(
            "Choose what appears on the status widget. Compact widgets show the first two enabled metrics.",
            12,
            MUTED,
            false
        ), top(dp(6)));
        Switch metricServers = settingSwitch("Servers", FateWidgetProvider.PREF_METRIC_SERVERS);
        Switch metricUsers = settingSwitch("Users", FateWidgetProvider.PREF_METRIC_USERS);
        Switch metricPing = settingSwitch("Gateway ping", FateWidgetProvider.PREF_METRIC_PING);
        Switch metricShards = settingSwitch("Shards", FateWidgetProvider.PREF_METRIC_SHARDS);
        Switch metricCommands = settingSwitch("Commands used this month", FateWidgetProvider.PREF_METRIC_COMMANDS);
        Switch metricCpu = settingSwitch("Host CPU usage", FateWidgetProvider.PREF_METRIC_CPU, false);
        Switch metricMemory = settingSwitch("Host RAM usage", FateWidgetProvider.PREF_METRIC_MEMORY, false);
        Switch metricStorage = settingSwitch("Host storage usage", FateWidgetProvider.PREF_METRIC_STORAGE, false);
        Switch metricNetwork = settingSwitch("Live network throughput", FateWidgetProvider.PREF_METRIC_NETWORK, false);
        Switch metricRestarts = settingSwitch("Controller recoveries", FateWidgetProvider.PREF_METRIC_RESTARTS, false);
        Switch[] metricToggles = {
            metricServers,
            metricUsers,
            metricPing,
            metricShards,
            metricCommands,
            metricCpu,
            metricMemory,
            metricStorage,
            metricNetwork,
            metricRestarts
        };
        if (restoredDraft != null) {
            for (int index = 0; index < metricToggles.length && index < restoredDraft.metrics.length; index++) {
                metricToggles[index].setChecked(restoredDraft.metrics[index]);
            }
        }
        widgetMetrics.addView(metricServers, top(dp(10)));
        widgetMetrics.addView(metricUsers);
        widgetMetrics.addView(metricPing);
        widgetMetrics.addView(metricShards);
        widgetMetrics.addView(metricCommands);
        widgetMetrics.addView(metricCpu);
        widgetMetrics.addView(metricMemory);
        widgetMetrics.addView(metricStorage);
        widgetMetrics.addView(metricNetwork);
        widgetMetrics.addView(metricRestarts);
        root.addView(widgetMetrics, bottom(dp(12)));

        LinearLayout rhythm = card();
        rhythm.addView(label("Sync rhythm", 18, TEXT, true));
        Spinner interval = new Spinner(this);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            new String[]{"Every minute", "Every 5 minutes"}
        );
        interval.setAdapter(adapter);
        interval.setSelection(restoredDraft == null
            ? (SyncScheduler.intervalMinutes(this) == 1 ? 0 : 1)
            : restoredDraft.intervalPosition);
        interval.setBackground(rounded(SURFACE_HIGH, 14, 1, STROKE));
        rhythm.addView(interval, top(dp(12)));
        rhythm.addView(label(
            "Profile addresses stay fixed. Use Find Fate on this network when you explicitly want to replace the active profile's address.",
            12,
            MUTED,
            false
        ), top(dp(12)));
        Button save = primaryButton("Save & test connection");
        save.setOnClickListener(view -> {
            try {
                String base = FateApi.normalizeBase(endpoint.getText().toString());
                BotProfileStore.updateActive(
                    this,
                    profileName.getText().toString(),
                    base,
                    key.getText().toString()
                );
                prefs.edit()
                    .putInt(SyncScheduler.PREF_INTERVAL, interval.getSelectedItemPosition() == 0 ? 1 : 5)
                    .putBoolean(SyncScheduler.PREF_AUTO_DISCOVER, false)
                    .putBoolean(FateWidgetProvider.PREF_METRIC_SERVERS, metricServers.isChecked())
                    .putBoolean(FateWidgetProvider.PREF_METRIC_USERS, metricUsers.isChecked())
                    .putBoolean(FateWidgetProvider.PREF_METRIC_PING, metricPing.isChecked())
                    .putBoolean(FateWidgetProvider.PREF_METRIC_SHARDS, metricShards.isChecked())
                    .putBoolean(FateWidgetProvider.PREF_METRIC_COMMANDS, metricCommands.isChecked())
                    .putBoolean(FateWidgetProvider.PREF_METRIC_CPU, metricCpu.isChecked())
                    .putBoolean(FateWidgetProvider.PREF_METRIC_MEMORY, metricMemory.isChecked())
                    .putBoolean(FateWidgetProvider.PREF_METRIC_STORAGE, metricStorage.isChecked())
                    .putBoolean(FateWidgetProvider.PREF_METRIC_NETWORK, metricNetwork.isChecked())
                    .putBoolean(FateWidgetProvider.PREF_METRIC_RESTARTS, metricRestarts.isChecked())
                    .apply();
                SyncScheduler.schedule(this);
                sharedControlPanel.invalidateAndPrewarm();
                FateWidgetProvider.refreshAll(this);
                toast("Settings saved. Testing Fate now…");
                showPage(PAGE_OVERVIEW);
            } catch (Exception error) {
                toast("That address is not valid.");
            }
        });
        themeSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                ThemeManager.Palette selectedTheme = ThemeManager.themes().get(position);
                themeDescription.setText(selectedTheme.description);
                themePreview.setImageResource(selectedTheme.appBackground);
                // Spinner dispatches the current selection when it is first attached. Comparing
                // against the active palette prevents that callback from rebuilding the page.
                if (selectedTheme.key.equals(palette.key)) return;

                boolean[] metricSelections = new boolean[metricToggles.length];
                for (int index = 0; index < metricToggles.length; index++) {
                    metricSelections[index] = metricToggles[index].isChecked();
                }
                pendingSettingsDraft = new SettingsDraft(
                    activeProfile.id,
                    profileName.getText().toString(),
                    endpoint.getText().toString(),
                    key.getText().toString(),
                    metricSelections,
                    interval.getSelectedItemPosition()
                );

                // Theme is intentionally the only Settings value persisted here. The connection,
                // rhythm, and widget controls remain drafts until Save & test is tapped.
                prefs.edit().putString(ThemeManager.PREF_THEME, selectedTheme.key).apply();
                FateWidgetProvider.refreshAll(MainActivity.this);
                applyPalette();
                getWindow().setStatusBarColor(SHELL);
                getWindow().setNavigationBarColor(SHELL);
                setContentView(buildShell());
                showPage(PAGE_SETTINGS);
                toast(selectedTheme.name + " applied");
            }

            @Override public void onNothingSelected(AdapterView<?> parent) {}
        });
        rhythm.addView(save, top(dp(16)));
        rhythm.addView(label(
            "Android may defer background work during battery-saving modes; opening the app or tapping the widget refreshes immediately.",
            11,
            MUTED,
            false
        ), top(dp(12)));
        root.addView(rhythm, bottom(dp(28)));
        scroll.addView(root);
        return scroll;
    }

    private View buildBackupSettings() {
        LinearLayout panel = card();
        LinearLayout header = horizontal();
        LinearLayout copy = new LinearLayout(this);
        copy.setOrientation(LinearLayout.VERTICAL);
        copy.addView(label("Backups", 18, TEXT, true));
        copy.addView(label(
            "MySQL, MongoDB, local state, retention, and Google Drive",
            12,
            MUTED,
            false
        ), top(dp(4)));
        header.addView(copy, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button expand = secondaryButton(backupExpanded ? "Hide" : "Open");
        header.addView(expand);
        panel.addView(header);

        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setVisibility(backupExpanded ? View.VISIBLE : View.GONE);

        Switch enabled = new Switch(this);
        enabled.setText("Automatic backups");
        enabled.setTextColor(TEXT);
        enabled.setThumbTintList(switchTint());
        enabled.setTrackTintList(trackTint());
        body.addView(enabled, top(dp(12)));

        body.addView(fieldLabel("LOCAL BACKUP FOLDER"), top(dp(12)));
        EditText location = field("./data/backups", InputType.TYPE_CLASS_TEXT);
        body.addView(location);

        body.addView(fieldLabel("FREQUENCY IN HOURS"), top(dp(12)));
        EditText frequency = field(
            "12",
            InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL
        );
        body.addView(frequency);

        body.addView(fieldLabel("RETENTION DAYS - OPTIONAL"), top(dp(12)));
        EditText retention = field("7", InputType.TYPE_CLASS_NUMBER);
        body.addView(retention);

        body.addView(fieldLabel("MAX BACKUP STORAGE IN GIB - OPTIONAL"), top(dp(12)));
        EditText maxStorage = field(
            "No storage cap",
            InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_DECIMAL
        );
        body.addView(maxStorage);

        body.addView(fieldLabel("MAX COMPLETE BACKUPS - OPTIONAL"), top(dp(12)));
        EditText maxBackups = field("No count cap", InputType.TYPE_CLASS_NUMBER);
        body.addView(maxBackups);
        body.addView(label(
            "When any enabled limit is reached, the oldest complete backup is removed first.",
            11,
            MUTED,
            false
        ), top(dp(7)));

        Switch includeLocal = new Switch(this);
        includeLocal.setText("Include Fate's local data files");
        includeLocal.setTextColor(TEXT);
        includeLocal.setThumbTintList(switchTint());
        includeLocal.setTrackTintList(trackTint());
        body.addView(includeLocal, top(dp(10)));

        body.addView(label("GOOGLE DRIVE", 10, GOLD, true), top(dp(18)));
        TextView driveStatus = label("Checking Drive link...", 12, MUTED, false);
        body.addView(driveStatus, top(dp(5)));
        Button linkDrive = secondaryButton("Link Google Drive");
        body.addView(linkDrive, top(dp(10)));
        Button selectFolder = secondaryButton("Choose remote folder");
        selectFolder.setVisibility(View.GONE);
        body.addView(selectFolder, top(dp(8)));
        Switch driveEnabled = new Switch(this);
        driveEnabled.setText("Upload each backup to Drive");
        driveEnabled.setTextColor(TEXT);
        driveEnabled.setThumbTintList(switchTint());
        driveEnabled.setTrackTintList(trackTint());
        driveEnabled.setEnabled(false);
        body.addView(driveEnabled, top(dp(8)));

        TextView status = label("Loading current backup settings...", 11, MUTED, false);
        body.addView(status, top(dp(14)));
        Button save = primaryButton("Saved");
        save.setEnabled(false);
        save.setAlpha(0.45f);
        body.addView(save, top(dp(10)));
        panel.addView(body);

        BackupForm form = new BackupForm(
            body,
            expand,
            enabled,
            includeLocal,
            driveEnabled,
            location,
            frequency,
            retention,
            maxStorage,
            maxBackups,
            status,
            driveStatus,
            save,
            linkDrive,
            selectFolder
        );
        backupForm = form;
        expand.setOnClickListener(view -> {
            backupExpanded = form.body.getVisibility() != View.VISIBLE;
            form.body.setVisibility(backupExpanded ? View.VISIBLE : View.GONE);
            form.expand.setText(backupExpanded ? "Hide" : "Open");
            form.expand.setContentDescription(
                (backupExpanded ? "Collapse" : "Expand") + " backup settings"
            );
        });
        TextWatcher watcher = new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence value, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence value, int start, int before, int count) {}
            @Override public void afterTextChanged(Editable value) {
                markBackupSettingsDirty(form);
            }
        };
        for (EditText input : new EditText[]{location, frequency, retention, maxStorage, maxBackups}) {
            input.addTextChangedListener(watcher);
        }
        enabled.setOnCheckedChangeListener((button, checked) -> markBackupSettingsDirty(form));
        includeLocal.setOnCheckedChangeListener((button, checked) -> markBackupSettingsDirty(form));
        driveEnabled.setOnCheckedChangeListener((button, checked) -> markBackupSettingsDirty(form));
        save.setOnClickListener(view -> saveBackupSettings(form));
        linkDrive.setOnClickListener(view -> startDriveAuthorization(form));
        selectFolder.setOnClickListener(view -> showDriveFolderPicker(form));
        loadBackupSettings(form, awaitingDriveAuthorization);
        return panel;
    }

    private void markBackupSettingsDirty(BackupForm form) {
        if (form.loading || backupForm != form) return;
        form.save.setEnabled(true);
        form.save.setAlpha(1f);
        form.save.setText("Save backup settings");
        form.status.setText("Unsaved backup changes.");
        form.status.setTextColor(GOLD);
    }

    private void loadBackupSettings(BackupForm form, boolean selectFolderAfterLink) {
        form.loading = true;
        form.status.setText("Loading current backup settings...");
        form.status.setTextColor(MUTED);
        String endpoint = prefs.getString(
            FateWidgetProvider.PREF_ENDPOINT,
            FateWidgetProvider.DEFAULT_ENDPOINT
        );
        String key = ControlKeyStore.read(this);
        if (key.trim().isEmpty()) {
            form.loading = false;
            form.status.setText("Add the FateControl key above to manage backups.");
            form.status.setTextColor(GOLD);
            return;
        }
        executor.execute(() -> {
            try {
                JSONObject response = FateApi.request(
                    "GET", endpoint, "/api/v1/backups/settings", key, null
                );
                JSONObject settings = response.getJSONObject("settings");
                JSONObject drive = response.optJSONObject("drive");
                String revision = response.optString("revision", "");
                runOnUiThread(() -> applyBackupSettings(
                    form,
                    settings,
                    drive,
                    revision,
                    selectFolderAfterLink
                ));
            } catch (Exception error) {
                runOnUiThread(() -> {
                    if (backupForm != form) return;
                    form.loading = false;
                    form.status.setText(readableError(error));
                    form.status.setTextColor(RED);
                });
            }
        });
    }

    private void applyBackupSettings(
        BackupForm form,
        JSONObject settings,
        JSONObject driveState,
        String revision,
        boolean selectFolderAfterLink
    ) {
        if (backupForm != form) return;
        form.loading = true;
        form.enabled.setChecked(settings.optBoolean("enabled", true));
        form.location.setText(settings.optString("location", "./data/backups"));
        form.frequency.setText(numberText(settings.opt("frequency_hours"), "12"));
        form.retentionDays.setText(nullableNumberText(settings.opt("retention_days")));
        form.maxStorage.setText(nullableNumberText(settings.opt("max_storage_gb")));
        form.maxBackups.setText(nullableNumberText(settings.opt("max_backups")));
        form.includeLocalFiles.setChecked(settings.optBoolean("include_local_files", true));
        JSONObject google = settings.optJSONObject("google_drive");
        if (google == null) google = new JSONObject();
        form.folderId = google.optString("folder_id", "");
        if ("null".equals(form.folderId)) form.folderId = "";
        form.folderPath = google.optString("folder_path", "");
        if ("null".equals(form.folderPath)) form.folderPath = "";
        form.revision = revision == null ? "" : revision;
        form.driveEnabled.setChecked(google.optBoolean("enabled", false));
        form.linked = driveState != null && driveState.optBoolean("linked", false);
        form.driveConfigured = driveState != null && driveState.optBoolean("configured", false);
        form.linkDrive.setEnabled(form.driveConfigured);
        form.linkDrive.setAlpha(form.driveConfigured ? 1f : 0.45f);
        form.linkDrive.setText(form.linked ? "Relink Google Drive" : "Link Google Drive");
        boolean hasFolder = !form.folderId.isEmpty();
        form.selectFolder.setVisibility(form.linked ? View.VISIBLE : View.GONE);
        form.selectFolder.setText(hasFolder ? "Change remote folder" : "Choose remote folder");
        form.driveEnabled.setEnabled(form.linked && hasFolder);
        form.driveEnabled.setAlpha(form.linked && hasFolder ? 1f : 0.45f);
        if (!form.driveConfigured) {
            String error = driveState == null ? "OAuth client is not configured."
                : driveState.optString("error", "OAuth client is not configured.");
            form.driveStatus.setText(error);
            form.driveStatus.setTextColor(RED);
        } else if (!form.linked) {
            form.driveStatus.setText("Not linked. OAuth credentials remain on the Fate computer.");
            form.driveStatus.setTextColor(MUTED);
        } else if (!hasFolder) {
            form.driveStatus.setText("Drive linked. Choose the remote backup folder.");
            form.driveStatus.setTextColor(GOLD);
        } else {
            form.driveStatus.setText("Linked - " + form.folderPath);
            form.driveStatus.setTextColor(GREEN);
        }
        form.loading = false;
        form.save.setEnabled(false);
        form.save.setAlpha(0.45f);
        form.save.setText("Saved");
        form.status.setText("Current backup settings loaded.");
        form.status.setTextColor(GREEN);
        if (selectFolderAfterLink && awaitingDriveAuthorization && form.linked) {
            awaitingDriveAuthorization = false;
            showDriveFolderPicker(form);
        }
    }

    private String numberText(Object value, String fallback) {
        return value == null || value == JSONObject.NULL ? fallback : String.valueOf(value);
    }

    private String nullableNumberText(Object value) {
        return value == null || value == JSONObject.NULL ? "" : String.valueOf(value);
    }

    private void saveBackupSettings(BackupForm form) {
        final JSONObject settings;
        try {
            if (form.revision.isEmpty()) {
                throw new IllegalStateException("Reload backup settings before saving.");
            }
            String location = form.location.getText().toString().trim();
            if (location.isEmpty()) throw new IllegalArgumentException("Choose a local backup folder.");
            double frequency = Double.parseDouble(form.frequency.getText().toString().trim());
            settings = new JSONObject()
                .put("enabled", form.enabled.isChecked())
                .put("location", location)
                .put("frequency_hours", frequency)
                .put("retention_days", optionalInteger(form.retentionDays))
                .put("max_storage_gb", optionalDecimal(form.maxStorage))
                .put("max_backups", optionalInteger(form.maxBackups))
                .put("include_local_files", form.includeLocalFiles.isChecked())
                .put("google_drive", new JSONObject().put("enabled", form.driveEnabled.isChecked()));
        } catch (Exception error) {
            form.status.setText("Fix backup settings: " + readableError(error));
            form.status.setTextColor(RED);
            return;
        }
        form.save.setEnabled(false);
        form.save.setAlpha(0.45f);
        form.status.setText("Saving backup settings...");
        form.status.setTextColor(GOLD);
        String endpoint = prefs.getString(FateWidgetProvider.PREF_ENDPOINT, FateWidgetProvider.DEFAULT_ENDPOINT);
        String key = ControlKeyStore.read(this);
        executor.execute(() -> {
            try {
                JSONObject response = FateApi.request(
                    "PUT",
                    endpoint,
                    "/api/v1/backups/settings",
                    key,
                    new JSONObject()
                        .put("settings", settings)
                        .put("revision", form.revision)
                );
                JSONObject saved = response.getJSONObject("settings");
                String revision = response.optString("revision", form.revision);
                runOnUiThread(() -> {
                    if (backupForm != form) return;
                    form.revision = revision;
                    form.status.setText("Saved. Fate will use this policy without a restart.");
                    form.status.setTextColor(GREEN);
                    form.save.setText("Saved");
                    form.loading = true;
                    JSONObject google = saved.optJSONObject("google_drive");
                    form.driveEnabled.setChecked(
                        google != null && google.optBoolean("enabled", false)
                    );
                    form.loading = false;
                });
            } catch (Exception error) {
                runOnUiThread(() -> {
                    if (backupForm != form) return;
                    form.status.setText(readableError(error));
                    form.status.setTextColor(RED);
                    form.save.setEnabled(true);
                    form.save.setAlpha(1f);
                    form.save.setText("Retry save");
                });
            }
        });
    }

    private Object optionalInteger(EditText field) {
        String value = field.getText().toString().trim();
        return value.isEmpty() ? JSONObject.NULL : Integer.parseInt(value);
    }

    private Object optionalDecimal(EditText field) {
        String value = field.getText().toString().trim();
        return value.isEmpty() ? JSONObject.NULL : Double.parseDouble(value);
    }

    private void startDriveAuthorization(BackupForm form) {
        String endpoint = prefs.getString(FateWidgetProvider.PREF_ENDPOINT, FateWidgetProvider.DEFAULT_ENDPOINT);
        String key = ControlKeyStore.read(this);
        form.driveStatus.setText("Creating a private Google authorization link...");
        form.driveStatus.setTextColor(GOLD);
        executor.execute(() -> {
            try {
                JSONObject response = FateApi.request(
                    "POST",
                    endpoint,
                    "/api/v1/backups/google/authorize",
                    key,
                    new JSONObject()
                );
                String url = response.getString("authorization_url");
                runOnUiThread(() -> {
                    if (backupForm != form) return;
                    awaitingDriveAuthorization = true;
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                });
            } catch (Exception error) {
                runOnUiThread(() -> {
                    if (backupForm != form) return;
                    form.driveStatus.setText(readableError(error));
                    form.driveStatus.setTextColor(RED);
                });
            }
        });
    }

    private void showDriveFolderPicker(BackupForm form) {
        if (!form.linked || backupForm != form) return;
        loadDriveFolders(
            form,
            "root",
            "My Drive",
            new ArrayList<>(),
            new ArrayList<>()
        );
    }

    private void loadDriveFolders(
        BackupForm form,
        String folderId,
        String folderPath,
        List<String> parentIds,
        List<String> parentPaths
    ) {
        form.driveStatus.setText("Loading " + folderPath + "...");
        form.driveStatus.setTextColor(GOLD);
        String endpoint = prefs.getString(FateWidgetProvider.PREF_ENDPOINT, FateWidgetProvider.DEFAULT_ENDPOINT);
        String key = ControlKeyStore.read(this);
        executor.execute(() -> {
            try {
                JSONObject response = FateApi.request(
                    "GET",
                    endpoint,
                    "/api/v1/backups/google/folders?parent_id=" + FateApi.queryValue(folderId),
                    key,
                    null
                );
                JSONArray folders = response.optJSONArray("folders");
                if (folders == null) folders = new JSONArray();
                JSONArray finalFolders = folders;
                runOnUiThread(() -> showDriveFolderDialog(
                    form,
                    folderId,
                    folderPath,
                    parentIds,
                    parentPaths,
                    finalFolders
                ));
            } catch (Exception error) {
                runOnUiThread(() -> {
                    if (backupForm != form) return;
                    form.driveStatus.setText(readableError(error));
                    form.driveStatus.setTextColor(RED);
                });
            }
        });
    }

    private void showDriveFolderDialog(
        BackupForm form,
        String folderId,
        String folderPath,
        List<String> parentIds,
        List<String> parentPaths,
        JSONArray folders
    ) {
        if (backupForm != form || currentPage != PAGE_SETTINGS) return;
        int offset = parentIds.isEmpty() ? 1 : 2;
        String[] choices = new String[folders.length() + offset];
        choices[0] = "Use this folder";
        if (!parentIds.isEmpty()) choices[1] = "Back to parent folder";
        for (int index = 0; index < folders.length(); index++) {
            choices[index + offset] = folders.optJSONObject(index).optString("name", "Unnamed folder");
        }
        new AlertDialog.Builder(this)
            .setTitle(folderPath)
            .setItems(choices, (dialog, which) -> {
                if (which == 0) {
                    selectDriveFolder(form, folderId);
                    return;
                }
                if (!parentIds.isEmpty() && which == 1) {
                    int last = parentIds.size() - 1;
                    String parentId = parentIds.get(last);
                    String parentPath = parentPaths.get(last);
                    loadDriveFolders(
                        form,
                        parentId,
                        parentPath,
                        new ArrayList<>(parentIds.subList(0, last)),
                        new ArrayList<>(parentPaths.subList(0, last))
                    );
                    return;
                }
                JSONObject selected = folders.optJSONObject(which - offset);
                if (selected == null) return;
                List<String> nextIds = new ArrayList<>(parentIds);
                List<String> nextPaths = new ArrayList<>(parentPaths);
                nextIds.add(folderId);
                nextPaths.add(folderPath);
                loadDriveFolders(
                    form,
                    selected.optString("id", ""),
                    folderPath + " / " + selected.optString("name", "Unnamed folder"),
                    nextIds,
                    nextPaths
                );
            })
            .setNegativeButton("Cancel", null)
            .show();
    }

    private void selectDriveFolder(BackupForm form, String folderId) {
        form.driveStatus.setText("Saving remote folder...");
        form.driveStatus.setTextColor(GOLD);
        String endpoint = prefs.getString(FateWidgetProvider.PREF_ENDPOINT, FateWidgetProvider.DEFAULT_ENDPOINT);
        String key = ControlKeyStore.read(this);
        executor.execute(() -> {
            try {
                JSONObject response = FateApi.request(
                    "POST",
                    endpoint,
                    "/api/v1/backups/google/folder",
                    key,
                    new JSONObject().put("folder_id", folderId)
                );
                String path = response.optString("folder_path", "My Drive");
                runOnUiThread(() -> {
                    if (backupForm != form) return;
                    form.loading = true;
                    form.folderId = folderId;
                    form.folderPath = path;
                    form.driveEnabled.setChecked(true);
                    form.driveEnabled.setEnabled(true);
                    form.driveEnabled.setAlpha(1f);
                    form.selectFolder.setText("Change remote folder");
                    form.driveStatus.setText("Linked - " + path);
                    form.driveStatus.setTextColor(GREEN);
                    form.loading = false;
                    awaitingDriveAuthorization = false;
                    toast("Google Drive backup folder selected.");
                });
            } catch (Exception error) {
                runOnUiThread(() -> {
                    if (backupForm != form) return;
                    form.driveStatus.setText(readableError(error));
                    form.driveStatus.setTextColor(RED);
                });
            }
        });
    }

    private void onBotProfileChanged(String message) {
        sharedControlPanel.invalidateAndPrewarm();
        FateWidgetProvider.refreshAll(this);
        showPage(PAGE_SETTINGS);
        toast(message);
    }

    @SuppressLint("SetTextI18n")
    private void verifyControlKey(
        BotProfileStore.Profile profile,
        String endpoint,
        String key,
        TextView result
    ) {
        executor.execute(() -> {
            try {
                JSONObject response = FateApi.request(
                    "GET",
                    endpoint,
                    "/api/v1/auth/check",
                    key,
                    null
                );
                String instance = response.optString("instance_name", "FateControl");
                runOnUiThread(() -> {
                    if (!isCurrentProfileVerification(profile, endpoint, key)) return;
                    result.setText("Verified with " + instance + ". Console widgets refreshed.");
                    result.setTextColor(GREEN);
                    FateWidgetProvider.refreshAll(this);
                    if (backupForm != null) loadBackupSettings(backupForm, false);
                });
            } catch (Exception error) {
                runOnUiThread(() -> {
                    if (!isCurrentProfileVerification(profile, endpoint, key)) return;
                    result.setText(
                        "Saved on this device, but not verified: "
                            + (error.getMessage() == null ? "FateControl is unreachable." : error.getMessage())
                    );
                    result.setTextColor(RED);
                    FateConsoleWidgetProvider.refreshAll(this);
                });
            }
        });
    }

    private boolean isCurrentProfileVerification(
        BotProfileStore.Profile profile,
        String endpoint,
        String key
    ) {
        if (currentPage != PAGE_SETTINGS || !BotProfileStore.isActive(this, profile.id)) {
            return false;
        }
        BotProfileStore.Profile active = BotProfileStore.active(this);
        try {
            if (!FateApi.normalizeBase(active.endpoint).equals(FateApi.normalizeBase(endpoint))) {
                return false;
            }
        } catch (Exception ignored) {
            return false;
        }
        return ControlKeyStore.read(this, profile.id).equals(key);
    }

    private Switch settingSwitch(String title, String preference) {
        return settingSwitch(title, preference, true);
    }

    private Switch settingSwitch(String title, String preference, boolean defaultValue) {
        Switch toggle = new Switch(this);
        toggle.setText(title);
        toggle.setTextColor(TEXT);
        toggle.setTextSize(14);
        toggle.setChecked(prefs.getBoolean(preference, defaultValue));
        toggle.setThumbTintList(switchTint());
        toggle.setTrackTintList(trackTint());
        toggle.setPadding(0, dp(5), 0, dp(5));
        return toggle;
    }

    private void refreshStatusCache() {
        if (statusLoadInFlight) return;
        statusLoadInFlight = true;
        String endpoint = prefs.getString(FateWidgetProvider.PREF_ENDPOINT, FateWidgetProvider.DEFAULT_ENDPOINT);
        String key = ControlKeyStore.read(this);
        executor.execute(() -> {
            try {
                StatusData status = FateApi.status(endpoint, key);
                FateWidgetProvider.saveStatus(this, status);
            } catch (Exception error) {
                // Profile addresses stay fixed until the user saves or explicitly chooses a
                // result from Find Fate on this network. Background refresh must not retarget it.
            } finally {
                statusLoadInFlight = false;
            }
        });
    }

    private void scanNetwork() {
        toast("Scanning this local network…");
        executor.execute(() -> {
            try {
                List<NetworkScanner.Device> devices = NetworkScanner.scan((checked, total) -> {
                    if (checked == total || checked % 48 == 0) {
                        runOnUiThread(() -> Toast.makeText(
                            this,
                            "Scanned " + checked + " of " + total + " addresses",
                            Toast.LENGTH_SHORT
                        ).show());
                    }
                });
                runOnUiThread(() -> showDevices(devices));
            } catch (Exception error) {
                runOnUiThread(() -> toast(error.getMessage()));
            }
        });
    }

    private void showDevices(List<NetworkScanner.Device> devices) {
        if (devices.isEmpty()) {
            new AlertDialog.Builder(this)
                .setTitle("No Fate devices found")
                .setMessage("Start FateControl on a computer connected to this network, then scan again. You can also enter an address manually in Settings.")
                .setPositiveButton("Open settings", (dialog, which) -> showPage(PAGE_SETTINGS))
                .setNegativeButton("Close", null)
                .show();
            return;
        }
        String[] labels = new String[devices.size()];
        for (int i = 0; i < devices.size(); i++) {
            labels[i] = devices.get(i).toString();
        }
        new AlertDialog.Builder(this)
            .setTitle("Choose a Fate device")
            .setItems(labels, (dialog, index) -> {
                NetworkScanner.Device selected = devices.get(index);
                BotProfileStore.updateActiveEndpoint(this, selected.endpoint);
                FateWidgetProvider.refreshAll(this);
                toast("Connected to " + selected.name);
                showPage(PAGE_OVERVIEW);
            })
            .setNegativeButton("Cancel", null)
            .show();
    }

    private ScrollView scroll() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(Color.TRANSPARENT);
        return scroll;
    }

    private LinearLayout pageRoot() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(18), dp(20), dp(18), dp(12));
        return root;
    }

    private void addDecoratedPageHeading(
        LinearLayout root,
        String kicker,
        String title,
        String description,
        boolean reverseGear
    ) {
        LinearLayout row = horizontal();
        LinearLayout copy = new LinearLayout(this);
        copy.setOrientation(LinearLayout.VERTICAL);
        copy.addView(label(kicker, 11, GOLD, true));
        copy.addView(label(title, 28, TEXT, true), bottom(dp(8)));
        copy.addView(label(description, 14, MUTED, false));
        row.addView(copy, new LinearLayout.LayoutParams(
            0,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            1f
        ));
        row.addView(
            decorativeGear(36, reverseGear, reverseGear ? 9_500L : 8_500L),
            new LinearLayout.LayoutParams(dp(36), dp(36))
        );
        root.addView(row, bottom(dp(18)));
    }

    private LinearLayout card() {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(18), dp(18), dp(18), dp(18));
        card.setBackground(rounded(SURFACE, 20, 1, STROKE));
        return card;
    }

    private LinearLayout horizontal() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        return row;
    }

    private GearView decorativeGear(int sizeDp, boolean reverse, long periodMs) {
        GearView gear = new GearView(this, palette.accent, palette.secondary).period(periodMs);
        if (reverse) gear.reverse();
        gear.setMinimumWidth(dp(sizeDp));
        gear.setMinimumHeight(dp(sizeDp));
        return gear;
    }

    private TextView label(String value, int size, int color, boolean bold) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextSize(size);
        text.setTextColor(color);
        if (bold) text.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        text.setLineSpacing(0, 1.12f);
        return text;
    }

    private TextView fieldLabel(String value) {
        return label(value, 10, ROSE, true);
    }

    private EditText field(String hint, int inputType) {
        EditText field = new EditText(this);
        field.setHint(hint);
        field.setHintTextColor(MUTED);
        field.setTextColor(TEXT);
        field.setTextSize(14);
        field.setInputType(inputType);
        field.setPadding(dp(14), dp(11), dp(14), dp(11));
        field.setBackground(rounded(SURFACE_HIGH, 13, 1, STROKE));
        return field;
    }

    private Button primaryButton(String value) {
        Button button = new Button(this);
        button.setText(value);
        button.setAllCaps(false);
        button.setTextColor(palette.onAccent);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setBackground(rounded(GOLD, 15, 0, 0));
        button.setMinHeight(dp(50));
        return button;
    }

    private Button secondaryButton(String value) {
        Button button = new Button(this);
        button.setText(value);
        button.setAllCaps(false);
        button.setTextColor(TEXT);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setBackground(rounded(SURFACE_HIGH, 15, 1, STROKE));
        button.setMinHeight(dp(50));
        return button;
    }

    private GradientDrawable rounded(int color, int radius, int stroke, int strokeColor) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radius));
        if (stroke > 0) drawable.setStroke(dp(stroke), strokeColor);
        return drawable;
    }

    private ColorStateList switchTint() {
        return new ColorStateList(
            new int[][]{new int[]{android.R.attr.state_checked}, new int[]{}},
            new int[]{GOLD, MUTED}
        );
    }

    private ColorStateList trackTint() {
        return new ColorStateList(
            new int[][]{new int[]{android.R.attr.state_checked}, new int[]{}},
            new int[]{(EMBER & 0x00FFFFFF) | 0x99000000, SURFACE_HIGH}
        );
    }

    private LinearLayout.LayoutParams weighted() {
        return new LinearLayout.LayoutParams(0, dp(48), 1f);
    }

    private LinearLayout.LayoutParams bottom(int margin) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        );
        params.bottomMargin = margin;
        return params;
    }

    private LinearLayout.LayoutParams top(int margin) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        );
        params.topMargin = margin;
        return params;
    }

    private LinearLayout.LayoutParams fixed(int height, int bottomMargin) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            height
        );
        params.bottomMargin = bottomMargin;
        return params;
    }

    private String readableError(Throwable error) {
        if (error == null) return "Something went wrong.";
        String message = error.getMessage();
        if (message == null || message.trim().isEmpty()) {
            return error.getClass().getSimpleName();
        }
        return message.trim();
    }

    private void toast(String message) {
        Toast.makeText(this, message == null ? "Something went wrong." : message, Toast.LENGTH_LONG).show();
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        outState.putInt(STATE_CURRENT_PAGE, clampPage(currentPage));
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.removeCallbacks(poller);
        handler.postDelayed(poller, SyncScheduler.intervalMinutes(this) * 60_000L);
        if (currentPage == PAGE_SETTINGS && awaitingDriveAuthorization && backupForm != null) {
            BackupForm form = backupForm;
            handler.postDelayed(() -> loadBackupSettings(form, true), 350L);
        }
    }

    @Override
    protected void onPause() {
        handler.removeCallbacks(poller);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (sharedControlPanel != null) sharedControlPanel.destroy();
        executor.shutdownNow();
        super.onDestroy();
    }
}
