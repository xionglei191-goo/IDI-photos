package cn.idi.camera;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
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
      return status == 408 || status == 429 || status >= 500;
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
    HttpURLConnection c = connection(server, path, token, body == null ? "GET" : "POST");
    try {
      if (body != null) {
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        try (OutputStream out = c.getOutputStream()) {
          out.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
      }
      return response(c);
    } finally {
      c.disconnect();
    }
  }

  static JSONObject upload(CaptureStore store, CaptureStore.Item item) throws Exception {
    String token = store.token(item.device);
    if (token.isEmpty()) throw new Failure(401, "该照片所属的设备连接已失效，请在设置中核对");
    HttpURLConnection c =
        connection(
            item.server,
            item.official() ? "/api/camera/official-captures" : "/api/camera/captures",
            token,
            "POST");
    String boundary = "IDICamera" + item.id.replace("-", "");
    try {
      if (item.video()) c.setReadTimeout(120000);
      c.setDoOutput(true);
      c.setChunkedStreamingMode(65536);
      c.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
      try (OutputStream out = c.getOutputStream()) {
        text(
            out,
            "--"
                + boundary
                + "\r\n"
                + "Content-Disposition: form-data; name=\"metadata\"\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n\r\n"
                + item.meta
                + "\r\n");
        if (item.video()) {
          part(out, boundary, "video", store.original(item.id));
        } else if (item.official()) part(out, boundary, "image", store.original(item.id));
        else {
          part(out, boundary, "original", store.original(item.id));
          part(out, boundary, "stamped", store.stamped(item.id));
        }
        text(out, "--" + boundary + "--\r\n");
      }
      return response(c);
    } finally {
      c.disconnect();
    }
  }

  private static void part(OutputStream out, String boundary, String name, File file)
      throws IOException {
    boolean video = name.equals("video");
    if (!file.isFile() || file.length() == 0) throw new IOException("本机原片文件缺失");
    if (file.length() >= (video ? OfficialVideoWriter.MAX_BYTES : 20 * 1024 * 1024))
      throw new Failure(413, video ? "视频超过64 MiB，原片已保存在手机，请手动导出处理"
          : "照片超过20 MiB，原图已保存在手机，请手动导出处理");
    text(
        out,
        "--"
            + boundary
            + "\r\nContent-Disposition: form-data; name=\""
            + name
            + "\"; filename=\""
            + name
            + (video ? ".mp4\"\r\nContent-Type: video/mp4\r\n\r\n"
                : ".jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"));
    try (FileInputStream in = new FileInputStream(file)) {
      byte[] buffer = new byte[65536];
      int read;
      while ((read = in.read(buffer)) != -1) out.write(buffer, 0, read);
    }
    text(out, "\r\n");
  }

  private static void text(OutputStream out, String text) throws IOException {
    out.write(text.getBytes(StandardCharsets.UTF_8));
  }

  private static JSONObject response(HttpURLConnection c) throws Exception {
    int status = c.getResponseCode();
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
    if (status < 200 || status >= 300)
      throw new Failure(status, result.optString("detail", "请求失败（" + status + "）"));
    return result;
  }
}
