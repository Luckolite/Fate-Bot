package xyz.fatebot.status;

import org.json.JSONObject;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.Callable;
import java.util.concurrent.CompletionService;
import java.util.concurrent.ExecutorCompletionService;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

final class NetworkScanner {
    interface ProgressListener {
        void onProgress(int checked, int total);
    }

    static final class Device {
        final String name;
        final String endpoint;
        final boolean controller;
        final boolean botOnline;

        Device(String name, String endpoint, boolean controller, boolean botOnline) {
            this.name = name;
            this.endpoint = endpoint;
            this.controller = controller;
            this.botOnline = botOnline;
        }

        @Override
        public String toString() {
            return name + "\n" + endpoint + (controller ? "  •  full control" : "  •  status only");
        }
    }

    private NetworkScanner() {}

    static List<Device> scan(ProgressListener listener) throws Exception {
        Set<String> hosts = subnetHosts();
        if (hosts.isEmpty()) {
            throw new IllegalStateException("Connect to a local Wi-Fi or Ethernet network first.");
        }
        ExecutorService pool = Executors.newFixedThreadPool(36);
        CompletionService<Device> completion = new ExecutorCompletionService<>(pool);
        List<Device> found = new ArrayList<>();
        try {
            for (String host : hosts) {
                completion.submit(probe(host));
            }
            int complete = 0;
            while (complete < hosts.size()) {
                Future<Device> future = completion.take();
                Device device = future.get();
                complete++;
                if (device != null) {
                    found.add(device);
                }
                if (listener != null && (complete == hosts.size() || complete % 12 == 0)) {
                    listener.onProgress(complete, hosts.size());
                }
            }
        } finally {
            pool.shutdownNow();
        }
        Collections.sort(found, (left, right) -> {
            if (left.controller != right.controller) {
                return left.controller ? -1 : 1;
            }
            return left.name.compareToIgnoreCase(right.name);
        });
        return found;
    }

    private static Callable<Device> probe(String host) {
        return () -> {
            Device controller = probeEndpoint(host, 16421, true);
            return controller != null ? controller : probeEndpoint(host, 16420, false);
        };
    }

    private static Device probeEndpoint(String host, int port, boolean expectedController) {
        String base = "http://" + host + ":" + port;
        try {
            JSONObject json = FateApi.request("GET", base, "/status", "", null, 320, 3500);
            boolean controller = "fate-control".equals(json.optString("service"));
            if (expectedController && !controller) {
                return null;
            }
            boolean looksLikeFate = controller || json.has("servers") && json.has("uptime_seconds");
            if (!looksLikeFate) {
                return null;
            }
            String name = json.optString("instance_name", controller ? "Fate controller" : "Fate bot");
            return new Device(name, base, controller, json.optBoolean("online", false));
        } catch (Exception ignored) {
            return null;
        }
    }

    private static Set<String> subnetHosts() throws Exception {
        Set<String> hosts = new LinkedHashSet<>();
        Enumeration<NetworkInterface> interfaces = NetworkInterface.getNetworkInterfaces();
        while (interfaces.hasMoreElements()) {
            NetworkInterface network = interfaces.nextElement();
            if (!network.isUp() || network.isLoopback() || network.isVirtual()) {
                continue;
            }
            Enumeration<InetAddress> addresses = network.getInetAddresses();
            while (addresses.hasMoreElements()) {
                InetAddress address = addresses.nextElement();
                if (!(address instanceof Inet4Address) || address.isLoopbackAddress()) {
                    continue;
                }
                byte[] bytes = address.getAddress();
                int first = bytes[0] & 0xff;
                int second = bytes[1] & 0xff;
                if (!(first == 10 || first == 192 && second == 168 || first == 172 && second >= 16 && second <= 31)) {
                    continue;
                }
                String prefix = first + "." + second + "." + (bytes[2] & 0xff) + ".";
                int own = bytes[3] & 0xff;
                for (int host = 1; host < 255; host++) {
                    if (host != own) {
                        hosts.add(prefix + host);
                    }
                }
            }
        }
        return hosts;
    }
}
