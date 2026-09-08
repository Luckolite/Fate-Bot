using System.Diagnostics;
using System.Drawing.Drawing2D;
using System.Net.Http.Headers;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace FateControl.Desktop;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        ProfileStore store = new();
        if (args.Length == 2 && args[0] == "--preview-local"
            && Uri.TryCreate(args[1], UriKind.Absolute, out Uri? preview)
            && preview.IsLoopback)
        {
            Application.Run(new ControlPanelWindow("Local preview", preview.ToString(), store));
            return;
        }
        Application.Run(new ProfileWindow(store));
    }
}

internal sealed class DesktopProfile
{
    public string Id { get; set; } = Guid.NewGuid().ToString();
    public string Name { get; set; } = "Production bot";
    public string Endpoint { get; set; } = "http://127.0.0.1:16421";
    public string ProtectedToken { get; set; } = "";
    public override string ToString() => Name;
}

internal sealed class ProfileState
{
    public string ActiveProfileId { get; set; } = "";
    public string VisualTheme { get; set; } = "classic";
    public bool ThemeEffects { get; set; } = true;
    public List<DesktopProfile> Profiles { get; set; } = [];
}

internal sealed class ProfileStore
{
    private readonly string path = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "FateControl",
        "profiles.json"
    );
    public ProfileState State { get; private set; }

    public ProfileStore()
    {
        State = Load();
        if (State.Profiles.Count == 0)
        {
            string endpoint = Environment.GetEnvironmentVariable("FATE_CONTROL_DESKTOP_URL")
                ?? "http://127.0.0.1:16421";
            string token = LegacyToken();
            DesktopProfile profile = new()
            {
                Endpoint = ProfileWindow.NormalizeEndpoint(endpoint),
                ProtectedToken = SecretProtection.Protect(token),
            };
            State.Profiles.Add(profile);
            State.ActiveProfileId = profile.Id;
            Save();
        }
        if (Active is null)
        {
            State.ActiveProfileId = State.Profiles[0].Id;
            Save();
        }
    }

    public DesktopProfile? Active => State.Profiles.FirstOrDefault(p => p.Id == State.ActiveProfileId);

    public DesktopProfile Add()
    {
        string baseName = "New bot";
        string name = baseName;
        int suffix = 2;
        while (State.Profiles.Any(p => p.Name.Equals(name, StringComparison.OrdinalIgnoreCase)))
            name = baseName + " " + suffix++;
        DesktopProfile profile = new() { Name = name, Endpoint = "" };
        State.Profiles.Add(profile);
        State.ActiveProfileId = profile.Id;
        Save();
        return profile;
    }

    public bool DeleteActive()
    {
        DesktopProfile? active = Active;
        if (active is null || State.Profiles.Count <= 1) return false;
        int index = State.Profiles.IndexOf(active);
        State.Profiles.Remove(active);
        State.ActiveProfileId = State.Profiles[Math.Min(index, State.Profiles.Count - 1)].Id;
        Save();
        return true;
    }

    public void Activate(string id)
    {
        if (!State.Profiles.Any(p => p.Id == id)) return;
        State.ActiveProfileId = id;
        Save();
    }

    public void UpdateActive(string name, string endpoint, string token)
    {
        DesktopProfile profile = Active ?? throw new InvalidOperationException("No active profile.");
        profile.Name = string.IsNullOrWhiteSpace(name) ? profile.Name : name.Trim();
        profile.Endpoint = ProfileWindow.NormalizeEndpoint(endpoint);
        profile.ProtectedToken = SecretProtection.Protect(token.Trim());
        Save();
    }

    public void SetAppearance(string themeKey, bool effectsEnabled)
    {
        State.VisualTheme = DesktopTheme.ByKey(themeKey).Key;
        State.ThemeEffects = effectsEnabled;
        Save();
    }

    public string Token(DesktopProfile profile) => SecretProtection.Unprotect(profile.ProtectedToken);
    public string LocalToken() => LegacyToken();

    private ProfileState Load()
    {
        try
        {
            if (!File.Exists(path)) return new();
            return JsonSerializer.Deserialize<ProfileState>(File.ReadAllText(path)) ?? new();
        }
        catch (IOException) { return new(); }
        catch (UnauthorizedAccessException) { return new(); }
        catch (JsonException) { return new(); }
    }

    private void Save()
    {
        string? directory = Path.GetDirectoryName(path);
        if (directory is null) return;
        Directory.CreateDirectory(directory);
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(State, new JsonSerializerOptions
        {
            WriteIndented = true,
        }));
        File.Move(temporary, path, true);
    }

    private static string LegacyToken()
    {
        string? configured = Environment.GetEnvironmentVariable("FATE_CONTROL_TOKEN");
        if (!string.IsNullOrWhiteSpace(configured)) return configured.Trim();
        string? configuredPath = Environment.GetEnvironmentVariable("FATE_CONTROL_TOKEN_PATH");
        IEnumerable<string> candidates = new[]
        {
            configuredPath ?? "",
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "Fate", "data", "fate-control.token"),
            Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "data", "fate-control.token")),
            Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "data", "fate-control.token")),
        };
        foreach (string candidate in candidates.Where(value => !string.IsNullOrWhiteSpace(value)))
        {
            try
            {
                if (!File.Exists(candidate)) continue;
                string token = File.ReadAllText(candidate).Trim();
                if (token.Length >= 24) return token;
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        return "";
    }
}

internal sealed record DesktopTheme(
    string Key, string Name, Color Background, Color Surface, Color SurfaceHigh,
    Color Accent, Color Secondary, Color Text, Color Muted, Color Stroke, Color OnAccent
)
{
    public static readonly IReadOnlyList<DesktopTheme> All = new[]
    {
        new DesktopTheme("amoled_neon", "AMOLED Neon", Color.Black, C(17,16,29), C(27,25,44), C(0,200,232), C(193,83,255), C(245,244,255), C(185,183,209), C(53,221,242), C(5,5,10)),
        new DesktopTheme("classic", "Classic", C(14,19,7), C(35,50,20), C(48,69,28), C(183,223,67), C(121,170,45), C(247,251,232), C(196,209,157), C(102,135,25), C(19,32,6)),
        new DesktopTheme("clockwork", "Clockwork", C(22,14,9), C(57,38,25), C(74,50,33), C(212,154,74), C(154,93,53), C(255,246,232), C(217,193,166), C(154,111,72), C(36,21,10)),
        new DesktopTheme("galaxy", "Galaxy", C(3,7,26), C(16,28,77), C(23,42,104), C(95,157,255), C(134,84,216), C(242,246,255), C(184,199,232), C(85,118,195), C(7,19,49)),
        new DesktopTheme("music", "Music", C(22,9,26), C(59,25,69), C(83,36,94), C(239,118,157), C(148,82,189), C(255,242,252), C(217,189,215), C(155,106,164), C(44,13,32)),
        new DesktopTheme("nature", "Nature", C(7,18,12), C(24,52,36), C(36,80,57), C(145,201,94), C(77,150,97), C(248,255,242), C(189,211,183), C(99,143,99), C(16,32,12)),
        new DesktopTheme("underwater", "Underwater", C(3,17,22), C(9,56,70), C(11,75,92), C(65,207,225), C(33,136,173), C(233,253,255), C(176,213,218), C(79,146,157), C(4,35,42)),
        new DesktopTheme("waterfall", "Waterfall", C(7,16,28), C(25,52,81), C(36,73,109), C(117,184,240), C(75,127,196), C(244,250,255), C(191,208,223), C(102,136,176), C(10,32,52)),
    };

    public static DesktopTheme ByKey(string? key) => All.FirstOrDefault(theme => theme.Key == key) ?? All[1];
    private static Color C(int red, int green, int blue) => Color.FromArgb(red, green, blue);

    public Image? Artwork()
    {
        string resource = $"FateControl.Desktop.ThemeAssets.{Key}.png";
        using Stream? stream = typeof(DesktopTheme).Assembly.GetManifestResourceStream(resource);
        if (stream is null) return null;
        using Image source = Image.FromStream(stream);
        return new Bitmap(source);
    }
}

internal sealed class AnimatedThemeArt : Control
{
    private readonly System.Windows.Forms.Timer timer = new() { Interval = 16 };
    private Image? artwork;
    private bool effectsEnabled;
    private double phase;

    public AnimatedThemeArt()
    {
        DoubleBuffered = true;
        Height = 132;
        Dock = DockStyle.Top;
        Margin = new Padding(0, 6, 0, 16);
        timer.Tick += (_, _) => { phase += 0.012; Invalidate(); };
    }

    public void SetArtwork(Image? image, bool effects)
    {
        artwork?.Dispose();
        artwork = image;
        effectsEnabled = effects;
        if (effects) timer.Start(); else timer.Stop();
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        if (artwork is null) return;
        e.Graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
        float scale = Math.Max((float)Width / artwork.Width, (float)Height / artwork.Height) * (effectsEnabled ? 1.08f : 1.04f);
        float width = artwork.Width * scale;
        float height = artwork.Height * scale;
        float travelX = Math.Max(0, width - Width) * .35f;
        float travelY = Math.Max(0, height - Height) * .2f;
        float x = (Width - width) / 2 + (effectsEnabled ? (float)Math.Sin(phase) * travelX : 0);
        float y = (Height - height) / 2 + (effectsEnabled ? (float)Math.Cos(phase * .8) * travelY : 0);
        e.Graphics.DrawImage(artwork, x, y, width, height);
        using LinearGradientBrush shade = new(ClientRectangle, Color.FromArgb(18, Color.Black), Color.FromArgb(118, Color.Black), LinearGradientMode.Horizontal);
        e.Graphics.FillRectangle(shade, ClientRectangle);
        using Pen border = new(ForeColor, 1);
        e.Graphics.DrawRectangle(border, 0, 0, Math.Max(0, Width - 1), Math.Max(0, Height - 1));
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { timer.Dispose(); artwork?.Dispose(); }
        base.Dispose(disposing);
    }
}

internal sealed class ProfileWindow : Form
{
    private readonly ProfileStore store;
    private readonly ComboBox profiles = new() { DropDownStyle = ComboBoxStyle.DropDownList };
    private readonly TextBox profileName = new();
    private readonly TextBox endpoint = new();
    private readonly TextBox token = new() { UseSystemPasswordChar = true };
    private readonly Label status = new() { AutoSize = false, Height = 48 };
    private readonly ComboBox visualTheme = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 230 };
    private readonly CheckBox themeEffects = new() { Text = "Ambient effects", AutoSize = true };
    private readonly AnimatedThemeArt themeArtwork = new();
    private bool loading;
    private bool loadingAppearance;

    public ProfileWindow(ProfileStore store)
    {
        this.store = store;
        Text = "Fate Control profiles";
        Icon = Icon.ExtractAssociatedIcon(Environment.ProcessPath ?? "") ?? SystemIcons.Application;
        MinimumSize = new Size(600, 720);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(16, 11, 15);
        ForeColor = Color.FromArgb(247, 238, 232);
        Font = new Font("Segoe UI", 10F);

        TableLayoutPanel root = new()
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(24),
            ColumnCount = 1,
            RowCount = 17,
            AutoScroll = true,
        };
        root.Controls.Add(Heading("Fate Control"));
        root.Controls.Add(Copy("Choose which bot this Windows control panel connects to. The window stays available if a server cannot load."));
        root.Controls.Add(themeArtwork);
        root.Controls.Add(FieldLabel("APPEARANCE"));
        FlowLayoutPanel appearance = new() { Dock = DockStyle.Fill, AutoSize = true, WrapContents = true };
        appearance.Controls.Add(visualTheme);
        appearance.Controls.Add(themeEffects);
        root.Controls.Add(appearance);
        root.Controls.Add(FieldLabel("BOT PROFILE"));
        root.Controls.Add(profiles);
        FlowLayoutPanel profileActions = new() { Dock = DockStyle.Fill, AutoSize = true };
        Button add = Secondary("Add profile");
        Button delete = Secondary("Delete profile");
        profileActions.Controls.Add(add);
        profileActions.Controls.Add(delete);
        root.Controls.Add(profileActions);
        root.Controls.Add(FieldLabel("PROFILE NAME"));
        root.Controls.Add(profileName);
        root.Controls.Add(FieldLabel("FATECONTROL ADDRESS"));
        root.Controls.Add(endpoint);
        root.Controls.Add(FieldLabel("CONTROL KEY"));
        root.Controls.Add(token);
        Button open = Primary("Save & open control panel");
        root.Controls.Add(open);
        root.Controls.Add(status);
        Controls.Add(root);

        foreach (Control control in new Control[] { profiles, profileName, endpoint, token })
        {
            control.Dock = DockStyle.Top;
            control.Margin = new Padding(0, 4, 0, 12);
            control.Height = 34;
        }
        token.PlaceholderText = "Stored encrypted for this Windows account";
        endpoint.PlaceholderText = "192.168.1.20:16421";
        status.ForeColor = Color.FromArgb(200, 184, 191);
        status.Margin = new Padding(0, 12, 0, 0);

        foreach (DesktopTheme theme in DesktopTheme.All) visualTheme.Items.Add(theme);
        visualTheme.DisplayMember = nameof(DesktopTheme.Name);
        loadingAppearance = true;
        visualTheme.SelectedItem = DesktopTheme.ByKey(store.State.VisualTheme);
        themeEffects.Checked = store.State.ThemeEffects;
        loadingAppearance = false;

        profiles.SelectedIndexChanged += (_, _) => SelectProfile();
        visualTheme.SelectedIndexChanged += (_, _) => SaveAppearance();
        themeEffects.CheckedChanged += (_, _) => SaveAppearance();
        add.Click += (_, _) => { store.Add(); ReloadProfiles(); };
        delete.Click += (_, _) =>
        {
            if (!store.DeleteActive())
            {
                status.Text = "Keep at least one bot profile.";
                return;
            }
            ReloadProfiles();
        };
        open.Click += async (_, _) => await SaveAndOpen(open);
        ApplyTheme();
        ReloadProfiles();
    }

    private void SaveAppearance()
    {
        if (loadingAppearance || visualTheme.SelectedItem is not DesktopTheme theme) return;
        store.SetAppearance(theme.Key, themeEffects.Checked);
        ApplyTheme();
    }

    private void ApplyTheme()
    {
        DesktopTheme theme = DesktopTheme.ByKey(store.State.VisualTheme);
        BackColor = theme.Background;
        ForeColor = theme.Text;
        themeArtwork.SetArtwork(theme.Artwork(), store.State.ThemeEffects);
        ApplyThemeTo(Controls, theme);
        themeArtwork.ForeColor = theme.Stroke;
        status.ForeColor = theme.Muted;
    }

    private static void ApplyThemeTo(Control.ControlCollection controls, DesktopTheme theme)
    {
        foreach (Control control in controls)
        {
            control.ForeColor = theme.Text;
            if (control is Button button)
            {
                bool primary = Equals(button.Tag, "primary");
                button.BackColor = primary ? theme.Accent : theme.SurfaceHigh;
                button.ForeColor = primary ? theme.OnAccent : theme.Text;
                button.FlatAppearance.BorderColor = primary ? theme.Accent : theme.Stroke;
            }
            else if (control is TextBox or ComboBox)
            {
                control.BackColor = theme.SurfaceHigh;
                control.ForeColor = theme.Text;
            }
            else if (control is Label label)
            {
                label.ForeColor = Equals(label.Tag, "accent") ? theme.Accent
                    : Equals(label.Tag, "muted") ? theme.Muted : theme.Text;
            }
            else if (control is not AnimatedThemeArt)
            {
                control.BackColor = theme.Background;
            }
            if (control.HasChildren) ApplyThemeTo(control.Controls, theme);
        }
    }

    internal static string NormalizeEndpoint(string value)
    {
        string normalized = (value ?? "").Trim().TrimEnd('/');
        if (normalized.Length == 0) return "";
        if (!normalized.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
            && !normalized.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            normalized = "http://" + normalized;
        if (!Uri.TryCreate(normalized, UriKind.Absolute, out Uri? uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)
            || string.IsNullOrWhiteSpace(uri.Host)
            || !string.IsNullOrEmpty(uri.UserInfo))
            throw new ArgumentException("Enter a valid FateControl address.");
        string route = uri.AbsolutePath.TrimEnd('/');
        bool knownRoute = route.Length == 0
            || route.Equals("/control", StringComparison.OrdinalIgnoreCase)
            || route.Equals("/status", StringComparison.OrdinalIgnoreCase)
            || route.StartsWith("/api/", StringComparison.OrdinalIgnoreCase);
        if (!knownRoute)
            throw new ArgumentException("Enter the FateControl server address or its /control URL.");
        return uri.GetLeftPart(UriPartial.Authority);
    }

    private void ReloadProfiles()
    {
        loading = true;
        profiles.DataSource = null;
        profiles.DataSource = store.State.Profiles.ToList();
        profiles.SelectedItem = store.Active;
        loading = false;
        LoadActive();
    }

    private void SelectProfile()
    {
        if (loading || profiles.SelectedItem is not DesktopProfile selected) return;
        store.Activate(selected.Id);
        LoadActive();
        status.Text = "Switched to " + selected.Name + ".";
    }

    private void LoadActive()
    {
        DesktopProfile? active = store.Active;
        if (active is null) return;
        profileName.Text = active.Name;
        endpoint.Text = active.Endpoint;
        token.Text = store.Token(active);
    }

    private async Task SaveAndOpen(Button button)
    {
        try
        {
            store.UpdateActive(profileName.Text, endpoint.Text, token.Text);
            ReloadProfiles();
            DesktopProfile profile = store.Active!;
            button.Enabled = false;
            status.Text = "Connecting to " + profile.Name + "…";
            (bool reachable, string message, ControlPanelWindow? panel) =
                await PanelLauncher.Open(profile, store.Token(profile), store);
            status.ForeColor = reachable ? Color.FromArgb(103, 211, 156) : Color.FromArgb(238, 116, 116);
            status.Text = message;
            if (reachable && panel is not null)
            {
                panel.FormClosed += (_, _) => Close();
                panel.Show();
                Hide();
            }
        }
        catch (Exception error)
        {
            status.ForeColor = Color.FromArgb(238, 116, 116);
            status.Text = error.Message;
        }
        finally { button.Enabled = true; }
    }

    private static Label Heading(string text) => new()
    {
        Text = text,
        AutoSize = true,
        Font = new Font("Segoe UI Semibold", 24F),
        Margin = new Padding(0, 0, 0, 6),
    };

    private static Label Copy(string text) => new()
    {
        Text = text,
        AutoSize = true,
        MaximumSize = new Size(470, 0),
        Tag = "muted",
        Margin = new Padding(0, 0, 0, 18),
    };

    private static Label FieldLabel(string text) => new()
    {
        Text = text,
        AutoSize = true,
        Tag = "accent",
        Font = new Font("Segoe UI Semibold", 8.5F),
    };

    private static Button Primary(string text) => new()
    {
        Text = text,
        AutoSize = false,
        Height = 44,
        Dock = DockStyle.Top,
        FlatStyle = FlatStyle.Flat,
        BackColor = Color.FromArgb(217, 124, 67),
        ForeColor = Color.FromArgb(23, 13, 8),
        Margin = new Padding(0, 10, 0, 0),
        Tag = "primary",
    };

    private static Button Secondary(string text) => new()
    {
        Text = text,
        AutoSize = true,
        FlatStyle = FlatStyle.Flat,
        BackColor = Color.FromArgb(43, 30, 37),
        ForeColor = Color.FromArgb(247, 238, 232),
        Margin = new Padding(0, 0, 8, 8),
    };
}

internal static class PanelLauncher
{
    public static async Task<(bool, string, ControlPanelWindow?)> Open(
        DesktopProfile profile,
        string token,
        ProfileStore store
    )
    {
        if (string.IsNullOrWhiteSpace(profile.Endpoint))
            return (false, "Set an address for this profile first.", null);
        try
        {
            using HttpClient client = new() { Timeout = TimeSpan.FromSeconds(5) };
            using HttpResponseMessage probe = await client.GetAsync(
                profile.Endpoint + "/control",
                HttpCompletionOption.ResponseHeadersRead
            );
            if (!probe.IsSuccessStatusCode)
                return (false, profile.Name + " returned HTTP " + (int)probe.StatusCode
                    + " for its control panel. Check the saved address and update FateControl.", null);
            string launchUrl = profile.Endpoint + "/control";
            string? ticketPath = null;
            if (!string.IsNullOrWhiteSpace(token))
            {
                ticketPath = await CreateLaunchTicket(client, profile.Endpoint, token);
            }
            if (ticketPath is null && IsLoopback(profile.Endpoint))
            {
                string localToken = store.LocalToken();
                if (!string.IsNullOrWhiteSpace(localToken)
                    && !string.Equals(localToken, token, StringComparison.Ordinal))
                    ticketPath = await CreateLaunchTicket(client, profile.Endpoint, localToken);
            }
            if (ticketPath is not null)
            {
                launchUrl = profile.Endpoint + (ticketPath.StartsWith('/') ? ticketPath : "/" + ticketPath);
            }
            else if (!string.IsNullOrWhiteSpace(token))
            {
                return (false, "The saved control key was rejected by " + profile.Name + ".", null);
            }
            launchUrl = AddAppearanceParameters(
                launchUrl,
                store.State.VisualTheme,
                store.State.ThemeEffects
            );
            ControlPanelWindow panel = new(profile.Name, launchUrl, store);
            return (true, "Opened " + profile.Name + ".", panel);
        }
        catch (HttpRequestException error) { return (false, "Could not load " + profile.Name + ": " + error.Message, null); }
        catch (TaskCanceledException) { return (false, "Could not load " + profile.Name + ": the connection timed out.", null); }
        catch (JsonException) { return (false, "The server responded, but its sign-in response was invalid.", null); }
    }

    private static async Task<string?> CreateLaunchTicket(HttpClient client, string endpoint, string token)
    {
        using HttpRequestMessage request = new(HttpMethod.Post, endpoint + "/api/v1/control/session");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        using HttpResponseMessage response = await client.SendAsync(request);
        if (!response.IsSuccessStatusCode) return null;
        using JsonDocument payload = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        if (!payload.RootElement.TryGetProperty("launch_url", out JsonElement path)) return null;
        string? value = path.GetString();
        return string.IsNullOrWhiteSpace(value) ? null : value;
    }

    private static bool IsLoopback(string endpoint) =>
        Uri.TryCreate(endpoint, UriKind.Absolute, out Uri? uri) && uri.IsLoopback;

    internal static string AddAppearanceParameters(string launchUrl, string theme, bool effectsEnabled)
    {
        int fragmentIndex = launchUrl.IndexOf('#');
        string baseUrl = fragmentIndex >= 0 ? launchUrl[..fragmentIndex] : launchUrl;
        string fragment = fragmentIndex >= 0 ? launchUrl[fragmentIndex..] : "";
        string separator = baseUrl.Contains('?') ? "&" : "?";
        string appearance = "theme=" + Uri.EscapeDataString(theme)
            + "&effects=" + (effectsEnabled ? "on" : "off");
        return baseUrl + separator + appearance + fragment;
    }

}

internal sealed class ControlPanelWindow : Form
{
    private readonly ProfileStore store;
    private readonly string launchUrl;
    private readonly WebView2 web = new() { Dock = DockStyle.Fill };
    private readonly ComboBox themes = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 180 };
    private readonly CheckBox effects = new() { Text = "Ambient effects", AutoSize = true };
    private readonly Button refresh = new() { Text = "Refresh", AutoSize = true, FlatStyle = FlatStyle.Flat };
    private readonly Panel toolbar = new() { Dock = DockStyle.Fill, Height = 52, Padding = new Padding(12, 9, 12, 7) };
    private bool loadingAppearance;
    private string? hostStyleSheetId;

    public ControlPanelWindow(string profileName, string launchUrl, ProfileStore store)
    {
        this.store = store;
        this.launchUrl = launchUrl;
        Text = "Fate Control — " + profileName;
        Icon = Icon.ExtractAssociatedIcon(Environment.ProcessPath ?? "") ?? SystemIcons.Application;
        StartPosition = FormStartPosition.CenterScreen;
        WindowState = FormWindowState.Maximized;
        MinimumSize = new Size(900, 620);

        foreach (DesktopTheme theme in DesktopTheme.All) themes.Items.Add(theme);
        themes.DisplayMember = nameof(DesktopTheme.Name);
        loadingAppearance = true;
        themes.SelectedItem = DesktopTheme.ByKey(store.State.VisualTheme);
        effects.Checked = store.State.ThemeEffects;
        loadingAppearance = false;

        FlowLayoutPanel controls = new() { Dock = DockStyle.Fill, WrapContents = false, AutoSize = true };
        Label label = new() { Text = "Theme", AutoSize = true, Margin = new Padding(0, 7, 8, 0), Tag = "accent" };
        themes.Margin = new Padding(0, 0, 12, 0);
        effects.Margin = new Padding(0, 6, 16, 0);
        controls.Controls.Add(label);
        controls.Controls.Add(themes);
        controls.Controls.Add(effects);
        controls.Controls.Add(refresh);
        toolbar.Controls.Add(controls);

        TableLayoutPanel root = new() { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1 };
        root.RowStyles.Add(new RowStyle(SizeType.Absolute, 52));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        root.Controls.Add(toolbar, 0, 0);
        root.Controls.Add(web, 0, 1);
        Controls.Add(root);

        themes.SelectedIndexChanged += async (_, _) => await SaveAndApplyAppearance();
        effects.CheckedChanged += async (_, _) => await SaveAndApplyAppearance();
        refresh.Click += (_, _) => web.Reload();
        Shown += async (_, _) => await InitializeWebView();
        ApplyChrome();
    }

    private async Task InitializeWebView()
    {
        try
        {
            string userDataFolder = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "FateControl",
                "WebView2"
            );
            Directory.CreateDirectory(userDataFolder);
            CoreWebView2Environment environment = await CoreWebView2Environment.CreateAsync(
                browserExecutableFolder: null,
                userDataFolder: userDataFolder
            );
            await web.EnsureCoreWebView2Async(environment);
            web.CoreWebView2.Settings.AreDevToolsEnabled = false;
            web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            web.CoreWebView2.AddWebResourceRequestedFilter(
                "*://*/__fate_desktop_art/*",
                CoreWebView2WebResourceContext.Image
            );
            web.CoreWebView2.WebResourceRequested += ServeThemeArtwork;
            web.CoreWebView2.NavigationStarting += (_, _) => hostStyleSheetId = null;
            web.CoreWebView2.NavigationCompleted += async (_, _) => await InjectTheme();
            web.Source = new Uri(launchUrl);
        }
        catch (Exception error)
        {
            MessageBox.Show(this, "The embedded Fate Control view could not start: " + error.Message,
                "Fate Control", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private void ServeThemeArtwork(object? sender, CoreWebView2WebResourceRequestedEventArgs request)
    {
        if (web.CoreWebView2 is null || !Uri.TryCreate(request.Request.Uri, UriKind.Absolute, out Uri? uri)) return;
        string[] parts = uri.AbsolutePath.Trim('/').Split('/', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length != 3 || parts[0] != "__fate_desktop_art") return;
        string theme = SanitizeArtworkPart(parts[1]);
        string role = SanitizeArtworkPart(Path.GetFileNameWithoutExtension(parts[2]));
        string name = $"FateControl.Desktop.MenuWeb.{theme}_{role}.webp";
        Stream? stream = typeof(ControlPanelWindow).Assembly.GetManifestResourceStream(name);
        if (stream is null) return;
        request.Response = web.CoreWebView2.Environment.CreateWebResourceResponse(
            stream,
            200,
            "OK",
            "Content-Type: image/webp\r\nCache-Control: no-store"
        );
    }

    private static string SanitizeArtworkPart(string value) => new(
        value.Where(character => char.IsAsciiLetterOrDigit(character) || character == '_').ToArray()
    );

    private async Task SaveAndApplyAppearance()
    {
        if (loadingAppearance || themes.SelectedItem is not DesktopTheme theme) return;
        store.SetAppearance(theme.Key, effects.Checked);
        ApplyChrome();
        await InjectTheme();
    }

    private void ApplyChrome()
    {
        DesktopTheme theme = DesktopTheme.ByKey(store.State.VisualTheme);
        BackColor = theme.Background;
        toolbar.BackColor = theme.Surface;
        toolbar.ForeColor = theme.Text;
        themes.BackColor = theme.SurfaceHigh;
        themes.ForeColor = theme.Text;
        effects.BackColor = theme.Surface;
        effects.ForeColor = theme.Text;
        refresh.BackColor = theme.SurfaceHigh;
        refresh.ForeColor = theme.Text;
        refresh.FlatAppearance.BorderColor = theme.Stroke;
        foreach (Control control in toolbar.Controls.Cast<Control>().SelectMany(Flatten))
            if (Equals(control.Tag, "accent")) control.ForeColor = theme.Accent;
    }

    private static IEnumerable<Control> Flatten(Control root)
    {
        yield return root;
        foreach (Control child in root.Controls)
            foreach (Control descendant in Flatten(child)) yield return descendant;
    }

    private async Task InjectTheme()
    {
        if (web.CoreWebView2 is null) return;
        DesktopTheme theme = DesktopTheme.ByKey(store.State.VisualTheme);
        string css = BuildPanelCss(theme);
        string script = "(()=>{const root=document.documentElement;"
            + "let backdrop=document.getElementById('fate-desktop-backdrop');if(!backdrop){backdrop=document.createElement('div');backdrop.id='fate-desktop-backdrop';backdrop.setAttribute('aria-hidden','true');document.body.prepend(backdrop);}"
            + "window.FateDesktopApplyArt=role=>{const roles=['overview','metrics','config','backups','console','settings'];if(!roles.includes(role))role='overview';for(const item of roles)root.classList.remove('fate-art-'+item);root.classList.add('fate-art-'+role);};"
            + "document.querySelectorAll('.panel-section').forEach(section=>{let banner=section.querySelector('.native-theme-banner');if(!banner){banner=document.createElement('div');banner.className='native-theme-banner';banner.setAttribute('aria-hidden','true');section.prepend(banner);}});"
            + "const sync=()=>{const active=document.querySelector('.section-nav button[aria-current=\\\"page\\\"]');window.FateDesktopApplyArt(active?.dataset?.section||'overview');};sync();"
            + "if(!window.FateDesktopObserver){window.FateDesktopObserver=new MutationObserver(sync);window.FateDesktopObserver.observe(document.body,{subtree:true,attributes:true,attributeFilter:['aria-current']});}"
            + "const columnKey='fate-control.metric-columns.v1',columnOptions=['auto','2','3','4','5'];let columnValue=localStorage.getItem(columnKey)||'auto';if(!columnOptions.includes(columnValue))columnValue='auto';let columnSelect=document.getElementById('metrics-columns');if(!columnSelect){const toolbar=document.querySelector('.metrics-toolbar');if(toolbar){const label=document.createElement('label');label.className='compact-field metric-columns-field';label.append(document.createTextNode('Columns'));columnSelect=document.createElement('select');columnSelect.id='metrics-columns';for(const value of columnOptions){const option=document.createElement('option');option.value=value;option.textContent=value==='auto'?'Auto':value;columnSelect.append(option);}label.append(columnSelect);toolbar.insertBefore(label,document.getElementById('customize-metrics'));}}const applyColumns=value=>{root.dataset.metricColumns=value;if(value==='auto')root.style.removeProperty('--metric-column-count');else root.style.setProperty('--metric-column-count',value);if(columnSelect)columnSelect.value=value;};if(columnSelect&&!columnSelect.dataset.fateDesktopBound){columnSelect.dataset.fateDesktopBound='true';columnSelect.addEventListener('change',()=>{localStorage.setItem(columnKey,columnSelect.value);applyColumns(columnSelect.value);});}applyColumns(columnValue);"
            + "if(!document.getElementById('metrics-show-hidden')&&typeof state!=='undefined'&&typeof loadAllMetrics==='function'){state.showHiddenMetrics=false;const baseLoadAllMetrics=loadAllMetrics;const decorateHiddenMetrics=()=>{const metrics=typeof registeredMetrics==='function'?registeredMetrics():state.metricOrder;document.querySelectorAll('#metric-grid>.metric-card').forEach(card=>{const hidden=state.metricHidden.has(card.dataset.metric);card.classList.toggle('metric-card-hidden-preview',hidden);let badge=card.querySelector('.metric-hidden-badge');if(!badge){badge=document.createElement('span');badge.className='metric-hidden-badge';badge.textContent='Hidden';badge.hidden=true;card.querySelector('.metric-header>div')?.append(badge);}badge.hidden=!hidden;});const summary=document.getElementById('metrics-layout-summary'),count=state.metricHidden.size,displayed=document.querySelectorAll('#metric-grid>.metric-card').length;if(summary)summary.textContent=displayed+' of '+metrics.length+' graphs displayed'+(count?(state.showHiddenMetrics?' · '+count+' hidden '+(count===1?'graph':'graphs')+' revealed':' · '+count+' hidden'):'')+' · drag cards to rearrange · layout saved on this device';};loadAllMetrics=async()=>{const hidden=state.metricHidden;let pending;if(state.showHiddenMetrics)state.metricHidden=new Set();try{pending=baseLoadAllMetrics();}finally{state.metricHidden=hidden;}decorateHiddenMetrics();await pending;decorateHiddenMetrics();};const toolbar=document.querySelector('.metrics-toolbar');if(toolbar){const label=document.createElement('label');label.className='toggle-row compact metrics-hidden-toggle';const input=document.createElement('input');input.id='metrics-show-hidden';input.type='checkbox';const copy=document.createElement('span');copy.textContent='Show hidden';label.append(input,copy);toolbar.insertBefore(label,document.getElementById('customize-metrics'));input.addEventListener('change',()=>{state.showHiddenMetrics=input.checked;loadAllMetrics();});}const editor=document.querySelector('.metric-editor-card');if(editor){const block=document.createElement('div');block.className='metric-editor-visibility';const copy=document.createElement('div');copy.innerHTML='<p class=\"eyebrow\">VISIBILITY</p><p class=\"muted\" id=\"metric-editor-visibility-copy\"></p>';const button=document.createElement('button');button.type='button';button.id='toggle-metric-hidden';button.className='button danger';block.append(copy,button);editor.insertBefore(block,document.getElementById('metric-editor-error'));const update=()=>{const metric=state.editingMetric,hidden=state.metricEditorFromCustomizer?state.metricLayoutDraft?.hidden.has(metric):state.metricHidden.has(metric);button.textContent=hidden?'Unhide metric':'Hide metric';button.classList.toggle('danger',!hidden);copy.lastElementChild.textContent=hidden?'Return this graph to the normal Metrics view.':'Hide this graph while keeping its settings and saved position.';};const baseOpenMetricSettings=openMetricSettings;openMetricSettings=(metric,fromCustomizer=false)=>{baseOpenMetricSettings(metric,fromCustomizer);update();};button.addEventListener('click',async()=>{const metric=state.editingMetric,hidden=state.metricEditorFromCustomizer?state.metricLayoutDraft?.hidden:state.metricHidden,order=state.metricEditorFromCustomizer?state.metricLayoutDraft?.order:state.metricOrder;if(!metric||!hidden||!order)return;const windowValue=document.getElementById('metric-editor-window').value,bucket=Number(document.getElementById('metric-editor-bucket').value.trim());if(typeof validMetricBucket==='function'&&!validMetricBucket(windowValue,bucket)){document.getElementById('metric-editor-error').textContent=metricBucketRule(windowValue);return;}if(!hidden.has(metric)&&!order.some(item=>item!==metric&&!hidden.has(item))){document.getElementById('metric-editor-error').textContent='Keep at least one graph visible.';return;}if(hidden.has(metric))hidden.delete(metric);else hidden.add(metric);if(!state.metricEditorFromCustomizer&&typeof saveMetricLayout==='function')saveMetricLayout();await commitMetricSettings();});}}"
            + "localStorage.setItem('fate-control.visual-theme.v1'," + JsonSerializer.Serialize(theme.Key) + ");localStorage.setItem('fate-control.theme-effects.v1'," + JsonSerializer.Serialize(store.State.ThemeEffects ? "on" : "off") + ");"
            + "})();";
        try
        {
            await ApplyHostStyleSheet(css);
            await web.ExecuteScriptAsync(script);
        }
        catch (InvalidOperationException) { }
        catch (Exception) { }
    }

    private async Task ApplyHostStyleSheet(string css)
    {
        if (web.CoreWebView2 is null) return;
        await web.CoreWebView2.CallDevToolsProtocolMethodAsync("Page.enable", "{}");
        await web.CoreWebView2.CallDevToolsProtocolMethodAsync("DOM.enable", "{}");
        await web.CoreWebView2.CallDevToolsProtocolMethodAsync("CSS.enable", "{}");
        if (string.IsNullOrWhiteSpace(hostStyleSheetId))
        {
            string frameTree = await web.CoreWebView2.CallDevToolsProtocolMethodAsync("Page.getFrameTree", "{}");
            using JsonDocument framePayload = JsonDocument.Parse(frameTree);
            string frameId = framePayload.RootElement.GetProperty("frameTree").GetProperty("frame").GetProperty("id").GetString()
                ?? throw new InvalidOperationException("The control panel frame is unavailable.");
            string created = await web.CoreWebView2.CallDevToolsProtocolMethodAsync(
                "CSS.createStyleSheet",
                JsonSerializer.Serialize(new { frameId })
            );
            using JsonDocument createdPayload = JsonDocument.Parse(created);
            hostStyleSheetId = createdPayload.RootElement.GetProperty("styleSheetId").GetString();
        }
        await web.CoreWebView2.CallDevToolsProtocolMethodAsync(
            "CSS.setStyleSheetText",
            JsonSerializer.Serialize(new { styleSheetId = hostStyleSheetId, text = css })
        );
    }

    private static string BuildPanelCss(DesktopTheme theme)
    {
        string background = Rgba(theme.Background, .78);
        string surface = Rgba(theme.Surface, .76);
        string high = Rgba(theme.SurfaceHigh, .82);
        string variables = ":root{--background:" + Hex(theme.Background) + ";--background-rgb:" + Rgb(theme.Background)
            + ";--surface:" + Hex(theme.Surface) + ";--surface-rgb:" + Rgb(theme.Surface)
            + ";--surface-high:" + Hex(theme.SurfaceHigh) + ";--surface-high-rgb:" + Rgb(theme.SurfaceHigh)
            + ";--stroke:" + Hex(theme.Stroke) + ";--text:" + Hex(theme.Text) + ";--muted:" + Hex(theme.Muted)
            + ";--gold:" + Hex(theme.Accent) + ";--gold-rgb:" + Rgb(theme.Accent)
            + ";--ember:" + Hex(theme.Secondary) + ";--ember-rgb:" + Rgb(theme.Secondary)
            + ";--on-accent:" + Hex(theme.OnAccent) + ";}"
            + ArtClass(theme.Key, "overview", "overview") + ArtClass(theme.Key, "metrics", "metrics")
            + ArtClass(theme.Key, "config", "config") + ArtClass(theme.Key, "backups", "backups")
            + ArtClass(theme.Key, "console", "console") + ArtClass(theme.Key, "settings", "config");
        return variables + "html,body{background-color:" + Hex(theme.Background) + "!important;}body{isolation:isolate!important;}"
            + "body::before{display:none!important;}"
            + "#fate-desktop-backdrop{position:fixed!important;inset:-1%!important;z-index:0!important;background-color:" + Hex(theme.Background) + "!important;background-image:linear-gradient(180deg," + Rgba(theme.Background,.46) + " 0%," + Rgba(theme.Background,.62) + " 54%," + Rgba(theme.Background,.78) + " 100%),var(--desktop-section-art)!important;background-position:center!important;background-size:cover!important;background-repeat:no-repeat!important;filter:saturate(.84) contrast(1.04)!important;box-shadow:inset 0 0 220px rgba(0,0,0,.62)!important;pointer-events:none!important;}"
            + ".app-shell{position:relative!important;z-index:1!important;}"
            + ".topbar{border:0!important;background:transparent!important;box-shadow:none!important;backdrop-filter:none!important;}"
            + ".topbar>div:first-child{display:block!important;flex:0 1 auto!important;width:fit-content!important;max-width:calc(100% - 250px)!important;min-width:0!important;padding:13px 22px 16px!important;overflow:hidden!important;border:1px solid " + Rgba(theme.Accent,.38) + "!important;border-radius:24px!important;clip-path:inset(0 round 24px)!important;background:linear-gradient(105deg," + high + "," + background + ")!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 13px 36px rgba(0,0,0,.32)!important;backdrop-filter:blur(16px) saturate(1.08)!important;}"
            + ".topbar-actions{padding:5px!important;overflow:hidden!important;border:1px solid " + Rgba(theme.Accent,.28) + "!important;border-radius:999px!important;background:" + Rgba(theme.Surface,.62) + "!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 10px 30px rgba(0,0,0,.24)!important;}"
            + "@media(max-width:760px){.topbar>div:first-child{width:100%!important;max-width:100%!important;border-radius:20px!important;clip-path:inset(0 round 20px)!important;}.topbar-actions{margin-left:auto!important;}}"
            + ".topbar h1,.section-heading h2{text-shadow:0 2px 12px rgba(0,0,0,.72)!important;}"
            + ".native-theme-banner{display:none!important;}"
            + ".panel-section.active{padding:18px!important;border:1px solid " + Rgba(theme.Stroke,.56) + "!important;border-radius:22px!important;background:linear-gradient(160deg," + Rgba(theme.Background,.54) + "," + Rgba(theme.Background,.36) + ")!important;box-shadow:0 24px 70px rgba(0,0,0,.28)!important;backdrop-filter:blur(4px) saturate(1.04)!important;}"
            + ".card,.metric-layout-row,.host-load-grid>div{border-color:" + Rgba(theme.Accent,.30) + "!important;background:linear-gradient(145deg," + high + "," + surface + ")!important;box-shadow:0 12px 32px rgba(0,0,0,.28)!important;backdrop-filter:blur(15px) saturate(1.08)!important;}"
            + ".metric-card,.metric-card *{-webkit-user-select:none!important;user-select:none!important;}.metric-card{cursor:grab!important;}.metric-card.metric-card-dragging{cursor:grabbing!important;}"
            + ".metrics-hidden-toggle{min-height:42px!important;padding:0 10px!important;border:1px solid " + Rgba(theme.Accent,.25) + "!important;border-radius:12px!important;background:" + Rgba(theme.Background,.38) + "!important;white-space:nowrap!important;}.metric-card-hidden-preview{opacity:.68!important;border-style:dashed!important;filter:saturate(.7)!important;}.metric-card-hidden-preview:hover{opacity:.9!important;}.metric-hidden-badge{display:inline-flex;width:fit-content;margin-top:7px;padding:3px 7px;border:1px solid " + Rgba(theme.Accent,.38) + ";border-radius:999px;color:" + Hex(theme.Accent) + ";background:" + Rgba(theme.Background,.48) + ";font-size:.65rem;font-weight:800;letter-spacing:.07em;text-transform:uppercase;}.metric-hidden-badge[hidden]{display:none!important;}.metric-editor-visibility{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:12px;border:1px solid " + Rgba(theme.Accent,.22) + ";border-radius:14px;background:" + Rgba(theme.Background,.3) + ";}.metric-editor-visibility p{margin:0;}.metric-editor-visibility .muted{margin-top:4px;font-size:.76rem;}"
            + ".button:not(.primary),select,input:not([type='checkbox']),textarea,.top-command-button,.folder-list button{background:" + Rgba(theme.SurfaceHigh,.74) + "!important;backdrop-filter:blur(10px)!important;}"
            + ".console-output{background:" + Rgba(theme.Background,.82) + "!important;box-shadow:inset 0 1px 0 rgba(255,255,255,.035)!important;backdrop-filter:blur(14px)!important;}"
            + ".section-nav{border-color:" + Rgba(theme.Accent,.36) + "!important;background:" + Rgba(theme.Surface,.92) + "!important;box-shadow:0 16px 48px rgba(0,0,0,.48)!important;backdrop-filter:blur(18px) saturate(1.08)!important;}"
            + "@media(min-width:1101px){.metric-grid{grid-template-columns:repeat(var(--metric-column-count,3),minmax(0,1fr))!important;}:root[data-active-section='metrics'][data-metric-columns='4'] .app-shell{width:min(1580px,100%)!important;}:root[data-active-section='metrics'][data-metric-columns='5'] .app-shell{width:min(1880px,100%)!important;}}"
            + "@media(max-width:1100px) and (min-width:761px){.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))!important;}}"
            + "@media(max-width:760px){.metric-grid{grid-template-columns:1fr!important;}}";
    }

    private static string MenuArtwork(string theme, string role) =>
        $"/__fate_desktop_art/{theme}/{role}.webp";

    private static string ArtClass(string theme, string cssRole, string imageRole) =>
        $":root.fate-art-{cssRole}{{--desktop-section-art:url(\"{MenuArtwork(theme, imageRole)}\");--section-art:url(\"{MenuArtwork(theme, imageRole)}\");}}";
    private static string Hex(Color color) => $"#{color.R:X2}{color.G:X2}{color.B:X2}";
    private static string Rgb(Color color) => $"{color.R},{color.G},{color.B}";
    private static string Rgba(Color color, double alpha) => $"rgba({color.R},{color.G},{color.B},{alpha:0.00})";
}

internal static class SecretProtection
{
    [StructLayout(LayoutKind.Sequential)]
    private struct DataBlob { public int Length; public IntPtr Data; }

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CryptProtectData(
        ref DataBlob input, string description, IntPtr entropy, IntPtr reserved,
        IntPtr prompt, int flags, out DataBlob output
    );

    [DllImport("crypt32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CryptUnprotectData(
        ref DataBlob input, IntPtr description, IntPtr entropy, IntPtr reserved,
        IntPtr prompt, int flags, out DataBlob output
    );

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);

    public static string Protect(string value)
    {
        if (string.IsNullOrEmpty(value)) return "";
        return Convert.ToBase64String(Transform(Encoding.UTF8.GetBytes(value), true));
    }

    public static string Unprotect(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return "";
        try { return Encoding.UTF8.GetString(Transform(Convert.FromBase64String(value), false)); }
        catch (Exception) { return ""; }
    }

    private static byte[] Transform(byte[] input, bool protect)
    {
        DataBlob source = new() { Length = input.Length, Data = Marshal.AllocHGlobal(input.Length) };
        try
        {
            Marshal.Copy(input, 0, source.Data, input.Length);
            bool ok = protect
                ? CryptProtectData(ref source, "FateControl profile key", IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, 0, out DataBlob output)
                : CryptUnprotectData(ref source, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, 0, out output);
            if (!ok) throw new InvalidOperationException("Windows could not protect the control key.");
            try
            {
                byte[] result = new byte[output.Length];
                Marshal.Copy(output.Data, result, 0, output.Length);
                return result;
            }
            finally { LocalFree(output.Data); }
        }
        finally { Marshal.FreeHGlobal(source.Data); }
    }
}
