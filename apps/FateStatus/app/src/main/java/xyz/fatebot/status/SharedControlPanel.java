package xyz.fatebot.status;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Handler;
import android.os.Message;
import android.util.Base64;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceError;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.URI;
import java.util.List;
import java.util.concurrent.ExecutorService;

/** A prewarmed WebView for FateControl's shared Android/Windows panel. */
final class SharedControlPanel {
    private final Activity activity;
    private final SharedPreferences prefs;
    private final ExecutorService executor;
    private final Handler handler;
    private final Runnable openNativeSettings;
    private WebView webView;
    private String loadedBase = "";
    private String loadedSection = "";
    private String requestedSection = "overview";
    private boolean ticketInFlight;
    private boolean pageReady;
    private boolean showingFailure;
    private boolean destroyed;
    private long loadGeneration;

    SharedControlPanel(
        Activity activity,
        SharedPreferences prefs,
        ExecutorService executor,
        Handler handler,
        Runnable openNativeSettings
    ) {
        this.activity = activity;
        this.prefs = prefs;
        this.executor = executor;
        this.handler = handler;
        this.openNativeSettings = openNativeSettings;
    }

    void prewarm() {
        if (destroyed || ticketInFlight) return;
        BotProfileStore.Profile profile = BotProfileStore.active(activity);
        String endpoint = profile.endpoint;
        String key = ControlKeyStore.read(activity, profile.id);
        long generation = loadGeneration;
        if (key.isEmpty()) {
            handler.post(() -> {
                if (generation == loadGeneration && BotProfileStore.isActive(activity, profile)) {
                    loadPanel(endpoint, "/control");
                }
            });
            return;
        }
        ticketInFlight = true;
        executor.execute(() -> {
            String launchPath = "/control";
            try {
                JSONObject response = FateApi.request(
                    "POST",
                    endpoint,
                    "/api/v1/control/session",
                    key,
                    null
                );
                launchPath = response.optString("launch_url", launchPath);
            } catch (Exception ignored) {
                // Loading the panel without a ticket gives it a useful offline/login state.
            }
            String finalLaunchPath = launchPath;
            handler.post(() -> {
                if (generation != loadGeneration || !BotProfileStore.isActive(activity, profile)) {
                    return;
                }
                ticketInFlight = false;
                loadPanel(endpoint, finalLaunchPath);
            });
        });
    }

    View show(String section) {
        requestedSection = normalizeSection(section);
        ensureWebView();
        applyTheme();
        detachFromParent();
        if (loadedBase.isEmpty()) prewarm();
        else openRequestedSection();
        return webView;
    }

    void invalidateAndPrewarm() {
        loadGeneration++;
        ticketInFlight = false;
        loadedBase = "";
        loadedSection = "";
        pageReady = false;
        showingFailure = false;
        if (webView != null) webView.stopLoading();
        prewarm();
    }

    void destroy() {
        destroyed = true;
        ticketInFlight = false;
        if (webView == null) return;
        detachFromParent();
        webView.stopLoading();
        webView.destroy();
        webView = null;
    }

    private void loadPanel(String endpoint, String launchPath) {
        if (destroyed) return;
        try {
            String base = FateApi.controlBase(endpoint);
            ensureWebView();
            loadedBase = base;
            loadedSection = requestedSection;
            pageReady = false;
            showingFailure = false;
            String separator = launchPath.contains("#") ? "&" : "#";
            webView.loadUrl(base + launchPath + separator + "section=" + requestedSection);
        } catch (Exception ignored) {
            loadedBase = "";
            showLoadFailure("That profile does not have a valid FateControl address.");
        }
    }

    private void showLoadFailure(String detail) {
        if (destroyed) return;
        ensureWebView();
        showingFailure = true;
        pageReady = false;
        BotProfileStore.Profile active = BotProfileStore.active(activity);
        List<BotProfileStore.Profile> profiles = BotProfileStore.profiles(activity);
        StringBuilder choices = new StringBuilder();
        for (BotProfileStore.Profile profile : profiles) {
            choices.append("<button class='profile")
                .append(profile.id.equals(active.id) ? " active" : "")
                .append("' onclick='FateAndroid.selectProfile(")
                .append(JSONObject.quote(profile.id))
                .append(")'><strong>")
                .append(html(profile.name))
                .append("</strong><span>")
                .append(html(profile.endpoint.isEmpty() ? "Address not set" : profile.endpoint))
                .append("</span></button>");
        }
        String html = "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
            + "<style>*{box-sizing:border-box}body{margin:0;background:#100b0f;color:#f7eee8;font-family:sans-serif;padding:24px}"
            + ".card{max-width:560px;margin:8vh auto;background:#21171d;border:1px solid #4a343e;border-radius:20px;padding:22px}"
            + "h1{font-size:24px;margin:0 0 8px}p{color:#c8b8bf;line-height:1.45}.profiles{display:grid;gap:9px;margin:18px 0}"
            + "button{width:100%;text-align:left;border:1px solid #5b414c;background:#2b1e25;color:#f7eee8;border-radius:13px;padding:13px 15px}"
            + "button.active{border-color:#e7a95f;background:#38261f}button span{display:block;color:#bdaab3;font-size:12px;margin-top:4px;overflow-wrap:anywhere}"
            + ".actions{display:grid;grid-template-columns:1fr 1fr;gap:9px}.primary{background:#d97c43;border-color:#d97c43;color:#170d08;text-align:center;font-weight:700}"
            + ".secondary{text-align:center}</style></head><body><main class='card'><h1>FateControl did not load</h1><p>"
            + html(detail) + " Select another bot profile, retry this one, or edit its connection settings.</p><div class='profiles'>"
            + choices + "</div><div class='actions'><button class='primary' onclick='FateAndroid.retryPanel()'>Retry</button>"
            + "<button class='secondary' onclick='FateAndroid.openSettings()'>Profile settings</button></div></main></body></html>";
        webView.loadDataWithBaseURL("https://fate-status.local/", html, "text/html", "UTF-8", null);
    }

    private static String html(String value) {
        if (value == null) return "";
        return value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\"", "&quot;")
            .replace("'", "&#39;");
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void ensureWebView() {
        if (webView != null || destroyed) return;
        webView = new WebView(activity);
        webView.setBackgroundColor(Color.rgb(16, 11, 15));
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setLoadsImagesAutomatically(true);
        settings.setSupportMultipleWindows(true);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        settings.setUserAgentString(settings.getUserAgentString() + " FateStatus/SharedPanel");
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false);
        webView.setWebViewClient(new PanelWebViewClient());
        webView.addJavascriptInterface(new AndroidBridge(), "FateAndroid");
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onCreateWindow(
                WebView view,
                boolean isDialog,
                boolean isUserGesture,
                Message resultMessage
            ) {
                if (!isUserGesture) return false;
                WebView child = new WebView(activity);
                child.setWebViewClient(new WebViewClient() {
                    @Override
                    public boolean shouldOverrideUrlLoading(
                        WebView view,
                        WebResourceRequest request
                    ) {
                        openExternal(request.getUrl());
                        view.destroy();
                        return true;
                    }

                    @Override
                    public boolean shouldOverrideUrlLoading(WebView view, String url) {
                        openExternal(Uri.parse(url));
                        view.destroy();
                        return true;
                    }
                });
                WebView.WebViewTransport transport = (WebView.WebViewTransport) resultMessage.obj;
                transport.setWebView(child);
                resultMessage.sendToTarget();
                return true;
            }
        });
    }

    private void openRequestedSection() {
        if (webView == null || !pageReady) return;
        loadedSection = requestedSection;
        webView.evaluateJavascript(
            "window.FatePanel&&window.FatePanel.openSection(" + JSONObject.quote(requestedSection) + ");"
                + "window.FateNativeApplySectionArt&&window.FateNativeApplySectionArt("
                + JSONObject.quote(requestedSection) + ")",
            null
        );
    }

    private void applyTheme() {
        if (webView == null) return;
        ThemeManager.Palette palette = ThemeManager.palette(activity);
        webView.setBackgroundColor(palette.background);
        if (!pageReady) return;
        String artwork = artworkDataUri(palette.appBackground);
        String artworkBase = "/__fate_native_art/";
        StringBuilder script = new StringBuilder("(()=>{const root=document.documentElement,s=root.style;");
        addCssVariable(script, "--background", cssColor(palette.background));
        addCssVariable(script, "--background-rgb", cssRgb(palette.background));
        addCssVariable(script, "--shell", cssColor(palette.shell));
        addCssVariable(script, "--shell-rgb", cssRgb(palette.shell));
        addCssVariable(script, "--surface", cssColor(palette.surface));
        addCssVariable(script, "--surface-rgb", cssRgb(palette.surface));
        addCssVariable(script, "--surface-high", cssColor(palette.surfaceHigh));
        addCssVariable(script, "--surface-high-rgb", cssRgb(palette.surfaceHigh));
        addCssVariable(script, "--stroke", cssColor(palette.stroke));
        addCssVariable(script, "--text", cssColor(palette.text));
        addCssVariable(script, "--muted", cssColor(palette.muted));
        addCssVariable(script, "--muted-rgb", cssRgb(palette.muted));
        addCssVariable(script, "--gold", cssColor(palette.secondary));
        addCssVariable(script, "--gold-rgb", cssRgb(palette.secondary));
        addCssVariable(script, "--ember", cssColor(palette.accent));
        addCssVariable(script, "--ember-rgb", cssRgb(palette.accent));
        addCssVariable(script, "--tertiary", cssColor(palette.tertiary));
        addCssVariable(script, "--green", cssColor(palette.green));
        addCssVariable(script, "--green-rgb", cssRgb(palette.green));
        addCssVariable(script, "--red", cssColor(palette.red));
        addCssVariable(script, "--red-rgb", cssRgb(palette.red));
        addCssVariable(script, "--on-accent", cssColor(palette.onAccent));
        addCssVariable(script, "--native-theme-art", "url(\"" + artwork + "\")");
        script.append("window.FateNativeThemeArt={overview:")
            .append(JSONObject.quote(artworkBase + "overview.webp?theme=" + palette.key))
            .append(",metrics:").append(JSONObject.quote(artworkBase + "metrics.webp?theme=" + palette.key))
            .append(",config:").append(JSONObject.quote(artworkBase + "config.webp?theme=" + palette.key))
            .append(",backups:").append(JSONObject.quote(artworkBase + "backups.webp?theme=" + palette.key))
            .append(",console:").append(JSONObject.quote(artworkBase + "console.webp?theme=" + palette.key))
            .append("};")
            .append("window.FateNativeApplySectionArt=section=>{")
            .append("const role=window.FateNativeThemeArt[section]?section:'overview';")
            .append("root.dataset.nativeSection=role;")
            .append("s.setProperty('--native-section-art','url(\\\"'+window.FateNativeThemeArt[role]+'\\\")');")
            .append("};");
        String nativeCss = nativePanelCss(palette);
        script.append("let style=document.getElementById('fate-native-theme-style');")
            .append("if(!style){style=document.createElement('style');style.id='fate-native-theme-style';document.head.append(style);}")
            .append("style.textContent=")
            .append(JSONObject.quote(nativeCss))
            .append(";")
            .append("document.querySelectorAll('.panel-section').forEach(section=>{")
            .append("let banner=section.querySelector('.native-theme-banner');")
            .append("if(!banner){banner=document.createElement('div');banner.className='native-theme-banner';banner.setAttribute('aria-hidden','true');section.prepend(banner);}")
            .append("});");
        script.append("localStorage.setItem('fate-control.visual-theme.v1',")
            .append(JSONObject.quote(palette.key))
            .append(");localStorage.setItem('fate-control.theme-effects.v1',")
            .append(JSONObject.quote(ThemeManager.effectsEnabled(activity) ? "on" : "off"))
            .append(");if(window.applyVisualTheme)window.applyVisualTheme(")
            .append(JSONObject.quote(palette.key))
            .append(");window.FateNativeApplySectionArt(")
            .append(JSONObject.quote(requestedSection))
            .append(");")
            .append("if(!window.FateNativeThemeObserver){window.FateNativeThemeObserver=new MutationObserver(()=>{")
            .append("const active=document.querySelector('.section-nav button[aria-current=\\\"page\\\"]');")
            .append("if(active&&active.dataset.section)window.FateNativeApplySectionArt(active.dataset.section);")
            .append("});window.FateNativeThemeObserver.observe(document.body,{subtree:true,attributes:true,attributeFilter:['aria-current']});}");
        script.append("})();");
        webView.evaluateJavascript(script.toString(), null);
    }

    private String artworkDataUri(int resource) {
        try (
            InputStream input = activity.getResources().openRawResource(resource);
            ByteArrayOutputStream output = new ByteArrayOutputStream()
        ) {
            byte[] buffer = new byte[16 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) output.write(buffer, 0, count);
            return "data:image/webp;base64," + Base64.encodeToString(
                output.toByteArray(),
                Base64.NO_WRAP
            );
        } catch (Exception ignored) {
            return "";
        }
    }

    private static String nativePanelCss(ThemeManager.Palette palette) {
        String background = cssRgba(palette.background, 0.48f);
        String surface = cssRgba(palette.surface, 0.50f);
        String surfaceHigh = cssRgba(palette.surfaceHigh, 0.62f);
        return "body{isolation:isolate!important;background-color:" + cssColor(palette.background) + "!important;}"
            + "body::before{content:''!important;position:fixed!important;inset:-5%!important;z-index:0!important;"
            + "background:linear-gradient(" + cssRgba(palette.background, 0.28f) + "," + cssRgba(palette.background, 0.58f) + "),var(--native-section-art) center/cover no-repeat!important;"
            + "opacity:1!important;transform:scale(1.04);pointer-events:none!important;will-change:transform;}"
            + ".app-shell{position:relative;z-index:1;}"
            + ".topbar{padding:14px 16px!important;border:1px solid var(--stroke)!important;border-radius:18px!important;"
            + "background:linear-gradient(90deg," + surfaceHigh + "," + background + "),var(--native-section-art) center/cover no-repeat!important;"
            + "box-shadow:var(--shadow-soft)!important;}"
            + ".native-theme-banner{position:relative;height:128px;margin:0 0 14px;overflow:hidden;border:1px solid var(--stroke);border-radius:18px;"
            + "background:linear-gradient(90deg," + cssRgba(palette.background, 0.04f) + "," + cssRgba(palette.background, 0.48f) + "),var(--native-section-art) center/cover no-repeat;"
            + "box-shadow:var(--shadow-soft);transition:background-image .18s ease;}"
            + ".native-theme-banner::after{content:'';position:absolute;inset:0;border-radius:inherit;box-shadow:inset 0 0 0 1px " + cssRgba(palette.secondary, 0.24f) + ";}"
            + ".card,.metric-layout-row,.host-load-grid>div{background:linear-gradient(145deg," + surfaceHigh + "," + surface + ")!important;backdrop-filter:blur(9px);}"
            + ".card{position:relative;overflow:hidden;}"
            + ".card:not(.status-card)::after{content:'';position:absolute;inset:0 auto 0 0;width:3px;background:linear-gradient(var(--gold),var(--ember));opacity:.68;pointer-events:none;}"
            + ".button:not(.primary),select,input:not([type='checkbox']),textarea,.top-command-button,.folder-list button{background:" + cssRgba(palette.surfaceHigh, 0.58f) + "!important;backdrop-filter:blur(8px);}"
            + ".config-secret-value,.sticky-actions{background:" + cssRgba(palette.shell, 0.68f) + "!important;backdrop-filter:blur(10px);}"
            + ".console-output{background:" + cssRgba(palette.background, 0.52f) + "!important;backdrop-filter:blur(9px);}"
            + ".section-nav{background:" + cssRgba(palette.shell, 0.78f) + "!important;backdrop-filter:blur(14px);}"
            + "dialog{background:" + surfaceHigh + "!important;backdrop-filter:blur(16px);}"
            + "dialog:not(#theme-dialog) .dialog-card::before{content:''!important;min-height:106px!important;margin:-22px -22px 2px!important;"
            + "border-bottom:1px solid var(--stroke)!important;border-radius:19px 19px 0 0!important;"
            + "background:linear-gradient(90deg," + cssRgba(palette.background, 0.12f) + "," + cssRgba(palette.background, 0.58f) + "),var(--native-section-art) center/cover no-repeat!important;}"
            + "[data-effects='on'] body::before{animation:nativeThemeDrift 18s ease-in-out infinite alternate;}"
            + "[data-effects='off'] body::before{animation:none!important;}"
            + "@keyframes nativeThemeDrift{from{transform:scale(1.04) translate3d(-.4%,-.3%,0)}to{transform:scale(1.1) translate3d(.4%,.3%,0)}}"
            + "@media(prefers-reduced-motion:reduce){body::before{animation:none!important}.native-theme-banner{transition:none!important}}";
    }

    private static void addCssVariable(StringBuilder script, String name, String value) {
        script.append("s.setProperty(")
            .append(JSONObject.quote(name))
            .append(',')
            .append(JSONObject.quote(value))
            .append(");");
    }

    private static String cssColor(int color) {
        return String.format(java.util.Locale.ROOT, "#%06X", color & 0xFFFFFF);
    }

    private static String cssRgb(int color) {
        return ((color >> 16) & 0xFF) + "," + ((color >> 8) & 0xFF) + "," + (color & 0xFF);
    }

    private static String cssRgba(int color, float alpha) {
        return String.format(
            java.util.Locale.ROOT,
            "rgba(%d,%d,%d,%.2f)",
            (color >> 16) & 0xff,
            (color >> 8) & 0xff,
            color & 0xff,
            alpha
        );
    }

    private void detachFromParent() {
        if (webView == null) return;
        if (webView.getParent() instanceof ViewGroup) {
            ((ViewGroup) webView.getParent()).removeView(webView);
        }
    }

    private boolean isPanelOrigin(Uri uri) {
        if (uri == null || loadedBase.isEmpty()) return false;
        try {
            URI base = new URI(loadedBase);
            int basePort = base.getPort() >= 0 ? base.getPort() : defaultPort(base.getScheme());
            int uriPort = uri.getPort() >= 0 ? uri.getPort() : defaultPort(uri.getScheme());
            return base.getScheme().equalsIgnoreCase(uri.getScheme())
                && base.getHost().equalsIgnoreCase(uri.getHost())
                && basePort == uriPort;
        } catch (Exception ignored) {
            return false;
        }
    }

    private void openExternal(Uri uri) {
        if (uri == null) return;
        try {
            activity.startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception ignored) {
            // The panel keeps its current state when no external browser is available.
        }
    }

    private static int defaultPort(String scheme) {
        return "https".equalsIgnoreCase(scheme) ? 443 : 80;
    }

    private static String normalizeSection(String value) {
        if ("metrics".equals(value)
            || "config".equals(value)
            || "backups".equals(value)
            || "console".equals(value)) {
            return value;
        }
        return "overview";
    }

    private final class AndroidBridge {
        @JavascriptInterface
        public void sectionChanged(String section) {
            handler.post(() -> {
                if (destroyed) return;
                String normalized = normalizeSection(section);
                requestedSection = normalized;
                loadedSection = normalized;
            });
        }

        @JavascriptInterface
        public void openSettings() {
            handler.post(openNativeSettings);
        }

        @JavascriptInterface
        public void setTheme(String themeKey) {
            ThemeManager.Palette selected = ThemeManager.byKey(themeKey);
            prefs.edit().putString(ThemeManager.PREF_THEME, selected.key).apply();
            handler.post(() -> {
                FateWidgetProvider.refreshAll(activity);
                activity.recreate();
            });
        }

        @JavascriptInterface
        public void setThemeEffects(boolean enabled) {
            ThemeManager.setEffectsEnabled(activity, enabled);
            handler.post(activity::recreate);
        }

        @JavascriptInterface
        public void selectProfile(String profileId) {
            handler.post(() -> {
                try {
                    BotProfileStore.activate(activity, profileId);
                    FateWidgetProvider.refreshAll(activity);
                    invalidateAndPrewarm();
                } catch (Exception ignored) {
                    showLoadFailure("That profile could not be selected.");
                }
            });
        }

        @JavascriptInterface
        public void retryPanel() {
            handler.post(SharedControlPanel.this::invalidateAndPrewarm);
        }
    }

    private final class PanelWebViewClient extends WebViewClient {
        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            String path = uri == null ? null : uri.getPath();
            String prefix = "/__fate_native_art/";
            if (path != null && path.startsWith(prefix) && path.endsWith(".webp")) {
                String role = path.substring(prefix.length(), path.length() - ".webp".length());
                int resource = ThemeManager.menuArtwork(ThemeManager.palette(activity).key, role);
                try {
                    return new WebResourceResponse(
                        "image/webp",
                        null,
                        activity.getResources().openRawResource(resource)
                    );
                } catch (Exception ignored) {
                    return null;
                }
            }
            return super.shouldInterceptRequest(view, request);
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            if (isPanelOrigin(uri)) return false;
            openExternal(uri);
            return true;
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            Uri uri = Uri.parse(url);
            if (isPanelOrigin(uri)) return false;
            openExternal(uri);
            return true;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (showingFailure) return;
            pageReady = true;
            applyTheme();
            if (!requestedSection.equals(loadedSection)) openRequestedSection();
        }

        @Override
        public void onReceivedError(
            WebView view,
            WebResourceRequest request,
            WebResourceError error
        ) {
            if (!request.isForMainFrame() || showingFailure || !isPanelOrigin(request.getUrl())) return;
            CharSequence description = error == null ? null : error.getDescription();
            showLoadFailure(description == null
                ? "The selected FateControl server is unreachable."
                : description.toString());
        }

        @Override
        public void onReceivedHttpError(
            WebView view,
            WebResourceRequest request,
            WebResourceResponse errorResponse
        ) {
            if (!request.isForMainFrame() || showingFailure || !isPanelOrigin(request.getUrl())) return;
            int status = errorResponse == null ? 0 : errorResponse.getStatusCode();
            showLoadFailure(status > 0
                ? "The selected server returned HTTP " + status + "."
                : "The selected FateControl server rejected the request.");
        }
    }
}
