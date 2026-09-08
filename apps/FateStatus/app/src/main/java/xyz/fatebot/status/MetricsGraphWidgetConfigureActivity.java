package xyz.fatebot.status;

import android.app.Activity;
import android.appwidget.AppWidgetManager;
import android.content.Intent;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.widget.AdapterView;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

/** Launcher configuration and later reconfiguration screen for graph widgets. */
public class MetricsGraphWidgetConfigureActivity extends Activity {
    private int appWidgetId = AppWidgetManager.INVALID_APPWIDGET_ID;
    private ThemeManager.Palette palette;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setResult(RESULT_CANCELED);
        appWidgetId = getIntent().getIntExtra(
            AppWidgetManager.EXTRA_APPWIDGET_ID,
            AppWidgetManager.INVALID_APPWIDGET_ID
        );
        if (appWidgetId == AppWidgetManager.INVALID_APPWIDGET_ID) {
            finish();
            return;
        }

        palette = ThemeManager.palette(this);
        getWindow().setStatusBarColor(palette.shell);
        getWindow().setNavigationBarColor(palette.shell);
        setContentView(buildContent());
    }

    private View buildContent() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(palette.background);
        scroll.setClipToPadding(false);
        scroll.setOnApplyWindowInsetsListener((view, insets) -> {
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

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(22), dp(28), dp(22), dp(28));
        scroll.addView(root, new ScrollView.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        TextView kicker = text("CONTROL PANEL / METRICS", 11, palette.secondary, true);
        root.addView(kicker);
        TextView title = text("Choose this widget's graph", 27, palette.text, true);
        root.addView(title, marginTop(4));
        TextView intro = text(
            "Each widget remembers its own metric and timeframe. You can return here with EDIT on the widget.",
            14,
            palette.muted,
            false
        );
        root.addView(intro, marginTop(8));

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(18), dp(18), dp(18), dp(18));
        card.setBackground(roundRect(palette.surface, palette.stroke, 18));
        root.addView(card, marginTop(24));

        card.addView(text("GRAPH", 11, palette.secondary, true));
        Spinner metric = spinner(MetricsGraphWidgetProvider.METRIC_LABELS);
        metric.setSelection(MetricsGraphWidgetProvider.metricIndex(
            MetricsGraphWidgetProvider.selectedMetric(this, appWidgetId)
        ));
        card.addView(metric, marginTop(8));

        TextView metricHint = text("", 13, palette.muted, false);
        card.addView(metricHint, marginTop(8));
        updateMetricHint(metricHint, metric.getSelectedItemPosition());
        metric.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                updateMetricHint(metricHint, position);
            }
            @Override public void onNothingSelected(AdapterView<?> parent) {}
        });

        TextView timeframeLabel = text("TIMEFRAME", 11, palette.secondary, true);
        card.addView(timeframeLabel, marginTop(22));
        Spinner timeframe = spinner(MetricsGraphWidgetProvider.WINDOW_LABELS);
        timeframe.setSelection(MetricsGraphWidgetProvider.windowIndex(
            MetricsGraphWidgetProvider.selectedWindow(this, appWidgetId)
        ));
        card.addView(timeframe, marginTop(8));

        TextView auth = text(
            ControlKeyStore.read(this).isEmpty()
                ? "A control key is required. Add it in Settings before refreshing this widget."
                : "This widget will authenticate with the control key stored securely in Settings.",
            12,
            ControlKeyStore.read(this).isEmpty() ? palette.red : palette.green,
            false
        );
        root.addView(auth, marginTop(18));

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        root.addView(buttons, marginTop(24));

        Button cancel = button("CANCEL", false);
        cancel.setOnClickListener(view -> finish());
        LinearLayout.LayoutParams cancelParams = new LinearLayout.LayoutParams(0, dp(52), 1f);
        cancelParams.setMarginEnd(dp(6));
        buttons.addView(cancel, cancelParams);

        Button save = button("SAVE WIDGET", true);
        save.setOnClickListener(view -> {
            int metricPosition = Math.max(0, metric.getSelectedItemPosition());
            int windowPosition = Math.max(0, timeframe.getSelectedItemPosition());
            MetricsGraphWidgetProvider.saveSelection(
                this,
                appWidgetId,
                MetricsGraphWidgetProvider.METRIC_KEYS[metricPosition],
                MetricsGraphWidgetProvider.WINDOW_KEYS[windowPosition]
            );
            MetricsGraphWidgetProvider.refreshWidget(this, appWidgetId);
            Intent result = new Intent().putExtra(
                AppWidgetManager.EXTRA_APPWIDGET_ID,
                appWidgetId
            );
            setResult(RESULT_OK, result);
            finish();
        });
        LinearLayout.LayoutParams saveParams = new LinearLayout.LayoutParams(0, dp(52), 1.35f);
        saveParams.setMarginStart(dp(6));
        buttons.addView(save, saveParams);
        return scroll;
    }

    private Spinner spinner(String[] values) {
        Spinner spinner = new Spinner(this);
        spinner.setAdapter(new PaletteSpinnerAdapter(values));
        spinner.setPopupBackgroundDrawable(roundRect(palette.surfaceHigh, palette.stroke, 12));
        spinner.setBackground(roundRect(palette.surfaceHigh, palette.stroke, 12));
        spinner.setPadding(dp(12), 0, dp(12), 0);
        spinner.setMinimumHeight(dp(52));
        return spinner;
    }

    private Button button(String label, boolean primary) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(label);
        button.setTextSize(13);
        button.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        button.setTextColor(primary ? palette.onAccent : palette.text);
        button.setBackground(roundRect(
            primary ? palette.accent : palette.surfaceHigh,
            primary ? palette.accent : palette.stroke,
            14
        ));
        return button;
    }

    private TextView text(String value, int size, int color, boolean medium) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        view.setLineSpacing(0f, 1.08f);
        if (medium) view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        return view;
    }

    private void updateMetricHint(TextView view, int position) {
        String[] hints = {
            "See Fate's server count grow or contract.",
            "Track the total Discord users Fate can reach.",
            "See command usage volume across the selected window.",
            "Track MongoDB operations performed by Fate.",
            "Track MySQL operations performed by Fate.",
            "Measure how frequently Fate sends Discord messages."
        };
        view.setText(hints[Math.max(0, Math.min(hints.length - 1, position))]);
    }

    private GradientDrawable roundRect(int fill, int stroke, int radiusDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill);
        drawable.setCornerRadius(dp(radiusDp));
        drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    private LinearLayout.LayoutParams marginTop(int dp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        );
        params.topMargin = dp(dp);
        return params;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private final class PaletteSpinnerAdapter extends BaseAdapter {
        private final String[] values;

        PaletteSpinnerAdapter(String[] values) {
            this.values = values;
        }

        @Override public int getCount() { return values.length; }
        @Override public Object getItem(int position) { return values[position]; }
        @Override public long getItemId(int position) { return position; }

        @Override
        public View getView(int position, View convertView, ViewGroup parent) {
            return row(position, convertView, false);
        }

        @Override
        public View getDropDownView(int position, View convertView, ViewGroup parent) {
            return row(position, convertView, true);
        }

        private View row(int position, View convertView, boolean dropdown) {
            TextView view = convertView instanceof TextView ? (TextView) convertView : new TextView(
                MetricsGraphWidgetConfigureActivity.this
            );
            view.setText(values[position]);
            view.setTextColor(palette.text);
            view.setTextSize(15);
            view.setGravity(Gravity.CENTER_VERTICAL);
            view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
            view.setPadding(dp(12), dropdown ? dp(14) : 0, dp(12), dropdown ? dp(14) : 0);
            view.setBackgroundColor(dropdown ? palette.surfaceHigh : android.graphics.Color.TRANSPARENT);
            return view;
        }
    }
}
