package xyz.fatebot.status;

import org.json.JSONObject;
import org.json.JSONException;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URLEncoder;
import java.net.URL;
import java.nio.charset.StandardCharsets;

final class FateApi {
    private FateApi() {}

    static String normalizeBase(String input) throws Exception {
        String value = input == null ? "" : input.trim();
        if (value.isEmpty()) {
            throw new IllegalArgumentException("Address is empty");
        }
        if (!value.startsWith("http://") && !value.startsWith("https://")) {
            value = "http://" + value;
        }
        URI uri = new URI(value);
        String scheme = uri.getScheme();
        if (uri.getHost() == null
            || uri.getUserInfo() != null
            || !("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme))) {
            throw new IllegalArgumentException("Enter a valid FateControl address");
        }
        String path = uri.getPath();
        String route = path == null ? "" : path.replaceAll("/+$", "");
        boolean knownRoute = route.isEmpty()
            || route.equalsIgnoreCase("/control")
            || route.equalsIgnoreCase("/status")
            || route.regionMatches(true, 0, "/api/", 0, 5);
        if (!knownRoute) {
            throw new IllegalArgumentException(
                "Enter the FateControl server address or its /control URL"
            );
        }
        return new URI(
            uri.getScheme(),
            null,
            uri.getHost(),
            uri.getPort(),
            "",
            null,
            null
        ).toString().replaceAll("/+$", "");
    }

    static StatusData publicStatus(String base) throws Exception {
        return StatusData.fromJson(request("GET", base, "/status", "", null));
    }

    static StatusData status(String base, String controlKey) throws Exception {
        if (controlKey == null || controlKey.trim().isEmpty()) {
            return publicStatus(base);
        }
        return StatusData.fromJson(request(
            "GET",
            base,
            "/api/v1/status",
            controlKey,
            null
        ));
    }

    static String controlBase(String input) throws Exception {
        String normalized = normalizeBase(input);
        URI uri = new URI(normalized);
        if (uri.getPort() != 16420) return normalized;
        return new URI(
            uri.getScheme(),
            uri.getUserInfo(),
            uri.getHost(),
            16421,
            "",
            null,
            null
        ).toString().replaceAll("/+$", "");
    }

    static JSONObject metrics(
        String base,
        String controlKey,
        String metric,
        String window,
        String command
    ) throws Exception {
        return metrics(base, controlKey, metric, window, command, null);
    }

    static JSONObject metrics(
        String base,
        String controlKey,
        String metric,
        String window,
        String command,
        Integer bucketSeconds
    ) throws Exception {
        StringBuilder path = new StringBuilder("/api/v1/metrics?metric=")
            .append(queryValue(metric))
            .append("&window=")
            .append(queryValue(window));
        if (command != null && !command.trim().isEmpty()) {
            path.append("&command=").append(queryValue(command.trim()));
        }
        if (bucketSeconds != null && bucketSeconds > 0) {
            path.append("&bucket_seconds=").append(bucketSeconds);
        }
        return request("GET", base, path.toString(), controlKey, null, 5000, 10_000);
    }

    static String queryValue(String value) throws Exception {
        return URLEncoder.encode(value == null ? "" : value, StandardCharsets.UTF_8.name())
            .replace("+", "%20");
    }

    static JSONObject request(
        String method,
        String base,
        String path,
        String controlKey,
        JSONObject body
    ) throws Exception {
        return request(method, base, path, controlKey, body, 5000, 7000);
    }

    static JSONObject request(
        String method,
        String base,
        String path,
        String controlKey,
        JSONObject body,
        int connectTimeout,
        int readTimeout
    ) throws Exception {
        String normalized = path.startsWith("/api/") ? controlBase(base) : normalizeBase(base);
        HttpURLConnection connection = (HttpURLConnection) new URL(normalized + path).openConnection();
        connection.setConnectTimeout(connectTimeout);
        connection.setReadTimeout(readTimeout);
        connection.setRequestMethod(method);
        connection.setRequestProperty("Accept", "application/json");
        connection.setUseCaches(false);
        if (controlKey != null && !controlKey.trim().isEmpty()) {
            connection.setRequestProperty("Authorization", "Bearer " + controlKey.trim());
        }
        if (body != null) {
            byte[] data = body.toString().getBytes(StandardCharsets.UTF_8);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setFixedLengthStreamingMode(data.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(data);
            }
        }
        try {
            int code = connection.getResponseCode();
            InputStream stream = code >= 200 && code < 300
                ? connection.getInputStream()
                : connection.getErrorStream();
            String response = stream == null ? "" : readFully(stream);
            if (code < 200 || code >= 300) {
                String message = "Request failed with HTTP " + code;
                if (!response.isEmpty()) {
                    try {
                        JSONObject error = new JSONObject(response);
                        message = error.optString("error", message);
                    } catch (JSONException ignored) {
                        String plain = response.trim().replaceAll("\\s+", " ");
                        if (!plain.isEmpty()) {
                            message += ": " + plain.substring(0, Math.min(plain.length(), 180));
                        }
                    }
                }
                if (code == 404 && path.startsWith("/api/v1/")) {
                    message = "FateControl needs to be updated or restarted on the bot computer (HTTP 404).";
                }
                throw new IllegalStateException(message);
            }
            return response.isEmpty() ? new JSONObject() : new JSONObject(response);
        } finally {
            connection.disconnect();
        }
    }

    static String readFully(InputStream stream) throws Exception {
        StringBuilder text = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
            new InputStreamReader(stream, StandardCharsets.UTF_8)
        )) {
            String line;
            while ((line = reader.readLine()) != null) {
                text.append(line);
                if (text.length() > 1_000_000) {
                    throw new IllegalStateException("Response is too large");
                }
            }
        }
        return text.toString();
    }
}
