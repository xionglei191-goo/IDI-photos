package cn.idi.camera;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

final class CameraApi {
  static final class Failure extends IOException {
    final int status;

    Failure(int status, String message) {
      super(message);
      this.status = status;
    }

    boolean retryable() {
      return status == 408 || status == 429 || status >= 500 || status >= 200 && status < 300;
    }
  }

  interface Progress { void sent(long bytes, long total) throws Exception; }
  interface Observer {
    void phase(String stage) throws Exception;
    void completed(String operation, JSONObject timing) throws Exception;
  }

  // HttpURLConnection has no write timeout. Disconnect stalled transfers and
  // cancelled workers so neither can occupy an upload slot indefinitely.
  static final class Transfer implements AutoCloseable {
    private static final java.util.concurrent.ScheduledExecutorService timer =
        java.util.concurrent.Executors.newSingleThreadScheduledExecutor(r -> {
          Thread t = new Thread(r, "idi-upload-watchdog"); t.setDaemon(true); return t;
        });
    private volatile HttpURLConnection active;
    private volatile boolean cancelled, timedOut;
    private volatile long touched, connected;
    private String captureId;
    private final long idleMillis;
    private final java.util.concurrent.ScheduledFuture<?> watch;
    Observer observer;

    void bindUpload(CaptureStore store, String id) throws IOException {
      if (captureId != null && !captureId.equals(id)) throw new IOException("传输记录不匹配");
      captureId = id;
      CaptureDeletion.track(store, id, this);
    }

    void phase(String stage) throws Exception { if (observer != null) observer.phase(stage); }

    Transfer(boolean video) {
      idleMillis = video ? 150000 : 90000;
      watch = timer.scheduleAtFixedRate(() -> {
        HttpURLConnection c = active;
        long now = android.os.SystemClock.elapsedRealtime();
        if (c != null && (now - touched > idleMillis || now - connected > 480000)) {
          timedOut = true;
          c.disconnect();
        }
      }, 5, 5, java.util.concurrent.TimeUnit.SECONDS);
    }

    synchronized void attach(HttpURLConnection c) throws IOException {
      check();
      active = c;
      timedOut = false;
      connected = touched = android.os.SystemClock.elapsedRealtime();
    }
    void touch() throws IOException { check(); touched = android.os.SystemClock.elapsedRealtime(); }
    synchronized void detach() { active = null; timedOut = false; }
    void check() throws IOException {
      if (cancelled) throw new java.io.InterruptedIOException("上传任务已暂停");
      if (timedOut) throw new SocketTimeoutException("上传连接长时间没有响应");
    }
    boolean cancelled() { return cancelled; }
    synchronized void cancel() { cancelled = true; if (active != null) active.disconnect(); }
    public void close() {
      cancel(); watch.cancel(false);
      if (captureId != null) CaptureDeletion.untrack(captureId, this);
    }
  }

  static String normalizeServer(String value) throws IOException {
    String clean = value.trim().replaceAll("/+$", "");
    try {
      URI uri = URI.create(clean);
      String host = uri.getHost();
      if (host == null
          || uri.getUserInfo() != null
          || uri.getQuery() != null
          || uri.getFragment() != null
          || !uri.getPath().isEmpty()) throw new IOException("服务器地址请只填写域名和端口，不带路径");
      if (!"https".equals(uri.getScheme())
          && !("http".equals(uri.getScheme()) && privateHost(host)))
        throw new IOException("公网连接须使用 HTTPS；HTTP 仅用于本机和局域网");
      return clean;
    } catch (IllegalArgumentException error) {
      throw new IOException("服务器地址无效", error);
    }
  }

  static boolean privateHost(String host) {
    if (host.equals("localhost") || host.equals("127.0.0.1")) return true;
    String[] blocks = host.split("\\.");
    if (blocks.length != 4) return false;
    try {
      int[] values = new int[4];
      for (int i = 0; i < 4; i++) {
        values[i] = Integer.parseInt(blocks[i]);
        if (values[i] < 0 || values[i] > 255) return false;
      }
      return values[0] == 10
          || values[0] == 192 && values[1] == 168
          || values[0] == 172 && values[1] >= 16 && values[1] <= 31;
    } catch (NumberFormatException ignored) {
      return false;
    }
  }

  private static HttpURLConnection connection(
      String server, String path, String token, String method) throws IOException {
    HttpURLConnection c =
        (HttpURLConnection) URI.create(normalizeServer(server) + path).toURL().openConnection();
    c.setConnectTimeout(15000);
    c.setReadTimeout(60000);
    c.setInstanceFollowRedirects(false);
    c.setRequestMethod(method);
    c.setRequestProperty("Accept", "application/json");
    c.setRequestProperty("X-IDI-Request", "1");
    if (!token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
    return c;
  }

  static JSONObject request(String server, String path, String token, JSONObject body)
      throws Exception {
    return request(server, path, token, body, null);
  }

  static JSONObject request(String server, String path, String token, JSONObject body, Transfer transfer)
      throws Exception {
    return request(server, path, token, body, transfer, "receipt_check");
  }

  static JSONObject request(String server, String path, String token, JSONObject body, Transfer transfer, String operation)
      throws Exception {
    return request(server, path, token, body, transfer, operation, body == null ? "GET" : "POST");
  }

  static JSONObject delete(CaptureStore store, CaptureStore.Item item, Transfer transfer) throws Exception {
    CaptureDeletion.requireCurrentScope(store, item);
    String token = store.token(item.device);
    if (token.isEmpty()) throw new Failure(401, "这份资料所属连接已失效，无法同步删除");
    return request(item.server, "/api/camera/official-captures/" + item.id, token,
        new JSONObject().put("case_id", item.meta.getString("case_id")), transfer, "delete", "DELETE");
  }

  private static JSONObject request(String server, String path, String token, JSONObject body,
      Transfer transfer, String operation, String method) throws Exception {
    HttpURLConnection c = connection(server, path, token, method);
    Timing timing = new Timing(c, operation, transfer);
    try {
      if (transfer != null) transfer.attach(c);
      if (body != null) {
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        try (OutputStream out = c.getOutputStream()) {
          timing.connected();
          out.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
      } else { c.connect(); timing.connected(); }
      timing.waiting();
      return response(c, timing);
    } catch (IOException error) {
      if (transfer != null) transfer.check();
      throw error;
    } finally {
      c.disconnect();
      if (transfer != null) transfer.detach();
      timing.finish();
    }
  }

  static JSONObject upload(CaptureStore store, CaptureStore.Item item) throws Exception {
    try (Transfer transfer = new Transfer(item.video())) {
      return upload(store, item, transfer, (bytes, total) -> {});
    }
  }

  static JSONObject upload(CaptureStore store, CaptureStore.Item item, Transfer transfer, Progress progress) throws Exception {
    transfer.bindUpload(store, item.id);
    String token = store.token(item.device);
    if (token.isEmpty()) throw new Failure(401, "该照片所属的设备连接已失效，请在设置中核对");
    HttpURLConnection c =
        connection(
            item.server,
            item.official() ? "/api/camera/official-captures" : "/api/camera/captures",
            token,
            "POST");
    String boundary = "IDICamera" + item.id.replace("-", "");
    String metadata = "--" + boundary + "\r\n"
        + "Content-Disposition: form-data; name=\"metadata\"\r\n"
        + "Content-Type: application/json; charset=utf-8\r\n\r\n" + item.meta + "\r\n";
    String[] names = item.video() ? new String[] {"video"} : item.official()
        ? new String[] {"image"} : new String[] {"original", "stamped"};
    File[] files = item.official() ? new File[] {store.original(item.id)}
        : new File[] {store.original(item.id), store.stamped(item.id)};
    String ending = "--" + boundary + "--\r\n";
    long length = metadata.getBytes(StandardCharsets.UTF_8).length + ending.getBytes(StandardCharsets.UTF_8).length;
    long total = 0;
    for (int i = 0; i < files.length; i++) {
      validateFile(files[i], names[i].equals("video"));
      total += files[i].length();
      length += partHeader(boundary, names[i]).getBytes(StandardCharsets.UTF_8).length + files[i].length() + 2;
    }
    Timing timing = new Timing(c, "upload", transfer);
    try {
      transfer.attach(c);
      transfer.phase("connecting");
      if (item.video()) c.setReadTimeout(120000);
      c.setDoOutput(true);
      c.setFixedLengthStreamingMode(length);
      c.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
      try (OutputStream out = c.getOutputStream()) {
        timing.connected();
        transfer.phase("sending");
        text(out, metadata);
        long sent = 0;
        for (int i = 0; i < files.length; i++) {
          text(out, partHeader(boundary, names[i]));
          try (FileInputStream in = new FileInputStream(files[i])) {
            byte[] buffer = new byte[65536];
            int read;
            while ((read = in.read(buffer)) != -1) {
              transfer.check();
              out.write(buffer, 0, read);
              transfer.touch();
              sent += read;
              progress.sent(sent, total);
            }
          }
          text(out, "\r\n");
        }
        text(out, ending);
      }
      timing.waiting();
      transfer.phase("awaiting_receipt");
      return response(c, timing);
    } catch (IOException error) {
      transfer.check();
      throw error;
    } finally {
      c.disconnect();
      transfer.detach();
      timing.finish();
    }
  }

  static void validateFile(File file, boolean video) throws IOException {
    if (!file.isFile() || file.length() == 0) throw new java.io.FileNotFoundException("本机原片文件缺失");
    if (file.length() >= (video ? OfficialVideoWriter.MAX_BYTES : 20 * 1024 * 1024))
      throw new Failure(413, video ? "视频超过64 MiB，原片已保存在手机，请手动导出处理"
          : "照片超过20 MiB，原图已保存在手机，请手动导出处理");
  }

  private static String partHeader(String boundary, String name) {
    boolean video = name.equals("video");
    return "--"
            + boundary
            + "\r\nContent-Disposition: form-data; name=\""
            + name
            + "\"; filename=\""
            + name
            + (video ? ".mp4\"\r\nContent-Type: video/mp4\r\n\r\n"
                : ".jpg\"\r\nContent-Type: image/jpeg\r\n\r\n");
  }

  private static void text(OutputStream out, String text) throws IOException {
    out.write(text.getBytes(StandardCharsets.UTF_8));
  }

  private static JSONObject response(HttpURLConnection c, Timing timing) throws Exception {
    int status = c.getResponseCode();
    timing.headers(c, status);
    InputStream stream = status >= 200 && status < 300 ? c.getInputStream() : c.getErrorStream();
    String text = "";
    if (stream != null) {
      try (stream;
          ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
        byte[] buffer = new byte[8192];
        int count;
        while ((count = stream.read(buffer)) != -1) {
          bytes.write(buffer, 0, count);
          if (bytes.size() > 4 * 1024 * 1024) throw new IOException("服务器返回的数据过大");
        }
        text = new String(bytes.toByteArray(), StandardCharsets.UTF_8);
      }
    }
    JSONObject result;
    try {
      result = new JSONObject(text);
    } catch (Exception ignored) {
      throw new Failure(status, "服务器未返回有效数据（" + status + "），请检查地址和连接");
    }
    timing.bodyRead();
    if (status < 200 || status >= 300)
      throw new Failure(status, result.optString("detail", "请求失败（" + status + "）"));
    return result;
  }

  /** These are client observations, not proof that the server received bytes. */
  private static final class Timing {
    final long started = android.os.SystemClock.elapsedRealtime();
    final JSONObject values = new JSONObject();
    final Transfer transfer;
    final String operation;
    long phase = started;
    String current = "connect_ms";

    Timing(HttpURLConnection c, String operation, Transfer transfer) throws Exception {
      this.operation = operation;
      this.transfer = transfer;
      String trace = java.util.UUID.randomUUID().toString().replace("-", "");
      c.setRequestProperty("X-IDI-Camera-Trace", trace);
      values.put("trace", trace);
    }
    void advance(String next) throws Exception {
      long now = android.os.SystemClock.elapsedRealtime();
      if (!current.isEmpty()) values.put(current, now - phase);
      phase = now;
      current = next;
    }
    void connected() throws Exception { advance("write_ms"); }
    void waiting() throws Exception { advance("wait_ms"); }
    void headers(HttpURLConnection c, int status) throws Exception {
      advance("body_ms");
      values.put("http_status", status);
      String trace = c.getHeaderField("X-IDI-Camera-Trace");
      if (trace != null && trace.matches("[a-f0-9]{32}")) values.put("server_trace", trace);
      JSONObject server = new JSONObject();
      // Proxies can append another Server-Timing field. Preserve every field,
      // since getHeaderField(name) may return only the last one.
      StringBuilder metrics = new StringBuilder();
      for (java.util.Map.Entry<String, java.util.List<String>> entry : c.getHeaderFields().entrySet()) {
        if ("Server-Timing".equalsIgnoreCase(entry.getKey())) {
          for (String value : entry.getValue()) {
            if (metrics.length() > 0) metrics.append(',');
            metrics.append(value);
          }
        }
      }
      String header = metrics.toString();
      if (header != null && header.length() <= 4096) {
        for (String metric : header.split(",")) {
          java.util.regex.Matcher m = java.util.regex.Pattern.compile(
              "^\\s*idi_(receive|dispatch|queue|auth|file_read|lock|validate|store|archive|lookup|total);dur=([0-9]+(?:\\.[0-9]+)?)\\s*$").matcher(metric);
          if (m.matches()) {
            double duration = Double.parseDouble(m.group(2));
            if (Double.isFinite(duration) && duration <= 3600000) server.put(m.group(1), duration);
          }
        }
      }
      if (server.length() > 0) values.put("server_ms", server);
    }
    void bodyRead() throws Exception { advance(""); }
    void finish() throws Exception {
      advance("");
      values.put("total_ms", android.os.SystemClock.elapsedRealtime() - started);
      if (transfer != null && transfer.observer != null) transfer.observer.completed(operation, values);
    }
  }
}
