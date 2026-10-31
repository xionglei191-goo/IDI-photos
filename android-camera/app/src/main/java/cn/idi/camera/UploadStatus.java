package cn.idi.camera;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.os.SystemClock;
import java.io.FileNotFoundException;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;
import java.util.Locale;
import javax.net.ssl.SSLException;
import org.json.JSONObject;

/** Local diagnostics deliberately exclude credentials, case details and image metadata. */
final class UploadStatus {
  final CaptureStore store;
  final String id, owner;
  final JSONObject info;
  final long started = SystemClock.elapsedRealtime();
  private long lastSaved;
  private long stageStarted = started;
  Runnable changed = () -> {};

  UploadStatus(CaptureStore store, CaptureStore.Item item, String owner) throws Exception {
    this.store = store;
    this.id = item.id;
    this.owner = owner;
    info = new JSONObject(item.upload.toString());
    info.put("attempt", info.optInt("attempt") + 1).put("stage", "checking")
        .put("bytes", 0).put("total", store.original(id).length()).put("elapsed_ms", 0)
        .put("started_at", System.currentTimeMillis()).put("error_code", "")
        .put("retry_at", 0).put("app_version", BuildConfig.VERSION_NAME)
        .put("network", network(store.context)).put("foreground", false)
        .put("stage_started_at", System.currentTimeMillis())
        .put("stages_ms", new JSONObject()).put("requests", new JSONObject());
    save(true);
  }

  static String network(Context context) {
    ConnectivityManager cm = context.getSystemService(ConnectivityManager.class);
    NetworkCapabilities c = cm == null ? null : cm.getNetworkCapabilities(cm.getActiveNetwork());
    if (c == null) return "offline";
    if (c.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return "wifi";
    if (c.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)) return "mobile";
    if (c.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) return "ethernet";
    return "other";
  }

  synchronized void stage(String stage) throws Exception {
    if (stage.equals(info.optString("stage"))) return;
    closeStage();
    info.put("stage", stage).put("stage_started_at", System.currentTimeMillis());
    save(true);
  }

  private void closeStage() throws Exception {
    long now = SystemClock.elapsedRealtime();
    JSONObject stages = info.getJSONObject("stages_ms");
    String previous = info.optString("stage");
    stages.put(previous, stages.optLong(previous) + now - stageStarted);
    stageStarted = now;
  }

  synchronized void timing(String operation, JSONObject timing) throws Exception {
    info.getJSONObject("requests").put(operation, timing);
    save(true);
  }

  synchronized void progress(long bytes, long total) throws Exception {
    info.put("bytes", bytes).put("total", total);
    save(bytes == total);
  }

  synchronized void foreground(boolean active) throws Exception {
    info.put("foreground", active);
    save(true);
  }

  synchronized void noteError(Throwable error) throws Exception {
    info.put("last_error_code", code(error)).put("last_error_at", System.currentTimeMillis());
    save(true);
  }

  private void save(boolean force) throws Exception {
    long now = SystemClock.elapsedRealtime();
    if (!force && now - lastSaved < 750) return;
    lastSaved = now;
    info.put("elapsed_ms", now - started).put("updated_at", System.currentTimeMillis());
    if (store.uploadUpdate(id, owner, info, null, null, null)) changed.run();
  }

  synchronized void finish(String state, String code, String error, JSONObject receipt, long delay) throws Exception {
    long elapsed = SystemClock.elapsedRealtime() - started;
    closeStage();
    info.put("stage", state).put("elapsed_ms", elapsed).put("updated_at", System.currentTimeMillis())
        .put("total_elapsed_ms", info.optLong("total_elapsed_ms") + elapsed)
        .put("error_code", code).put("retry_at", delay > 0 ? System.currentTimeMillis() + delay : 0);
    if (!code.isEmpty()) info.put("last_error_code", code).put("last_error_at", System.currentTimeMillis());
    store.uploadUpdate(id, owner, info, state, error, receipt);
  }

  static String code(Throwable error) {
    if (error instanceof CameraApi.Failure) return "HTTP_" + ((CameraApi.Failure) error).status;
    if (error instanceof UploadEngine.ReceiptError) return "RECEIPT_MISMATCH";
    if (error instanceof FileNotFoundException) return "LOCAL_FILE_MISSING";
    if (error instanceof SocketTimeoutException) return "NETWORK_TIMEOUT";
    if (error instanceof UnknownHostException) return "DNS_ERROR";
    if (error instanceof SSLException) return "TLS_ERROR";
    return "NETWORK_INTERRUPTED";
  }

  static boolean retryable(Throwable error) {
    if (error instanceof CameraApi.Failure) return ((CameraApi.Failure) error).retryable();
    return !(error instanceof UploadEngine.ReceiptError || error instanceof FileNotFoundException
        || error instanceof SSLException || error instanceof IllegalArgumentException);
  }

  static String explain(Throwable error) {
    String code = code(error);
    String reason = switch (code) {
      case "HTTP_401" -> "设备连接已失效，请在设置中重新配对";
      case "HTTP_403" -> "设备或案件权限不足，请联系管理员核对";
      case "HTTP_404" -> "服务器记录或接口不存在，请联系管理员核对";
      case "HTTP_410" -> "资料已在工作台删除，不会自动重新归档";
      case "HTTP_409" -> "拍摄编号或资料内容冲突，请联系管理员核对";
      case "HTTP_413" -> "原片超过服务器大小限制，请导出原片处理";
      case "HTTP_429" -> "服务器繁忙，稍后自动重试";
      case "NETWORK_TIMEOUT" -> "网络响应超时，稍后先核对回执再补传";
      case "DNS_ERROR" -> "无法解析服务器地址，网络恢复后自动重试";
      case "TLS_ERROR" -> "安全连接失败，请检查手机时间和服务器证书";
      case "LOCAL_FILE_MISSING" -> "本机原片文件缺失，请检查手机存储";
      case "RECEIPT_MISMATCH" -> "回执编号、类型或原片哈希不符，请联系管理员核对";
      default -> error instanceof CameraApi.Failure
          ? (((CameraApi.Failure) error).retryable() ? "服务器暂时不可用，稍后自动重试" : UploadWorker.safeMessage(error))
          : "网络连接中断，网络恢复后自动重试";
    };
    return reason + "（" + code + "）";
  }

  static int percent(JSONObject info) {
    long total = info.optLong("total");
    return total <= 0 ? 0 : (int) Math.min(100, Math.max(0, info.optLong("bytes") * 100 / total));
  }

  static String uploading(JSONObject info) {
    return switch (info.optString("stage")) {
      case "checking" -> "正在核对服务器回执";
      case "reconciling" -> "响应中断，正在核对回执";
      case "connecting" -> "正在连接上传服务";
      case "awaiting_receipt" -> "传输确认中 · 等待服务器回执";
      case "sending" -> percent(info) >= 100 ? "原片已交给网络 · 传输确认中" : "传输中 " + percent(info) + "%";
      default -> "准备上传";
    };
  }

  static String detail(CaptureStore.Item item) {
    JSONObject i = item.upload;
    if (i.optInt("attempt") == 0) return "网络可用时自动上传 · 原片保留在手机";
    return String.format(Locale.CHINA, "%.1f / %.1f MB · 本次 %d 秒 · 第 %d 次",
        i.optLong("bytes") / 1048576.0, i.optLong("total") / 1048576.0,
        elapsed(item) / 1000, i.optInt("attempt"));
  }

  private static long elapsed(CaptureStore.Item item) {
    long elapsed = item.upload.optLong("elapsed_ms");
    if (item.state.equals("uploading"))
      elapsed = Math.max(elapsed, System.currentTimeMillis() - item.upload.optLong("started_at"));
    return Math.max(0, elapsed);
  }

  static String diagnostics(CaptureStore.Item item) {
    JSONObject i = item.upload;
    return "拍摄编号：" + item.id + "\n版本：" + i.optString("app_version", BuildConfig.VERSION_NAME)
        + "\n状态：" + RecordsScreen.label(item) + "\n" + detail(item)
        + "\n累计传输处理：" + (i.optLong("total_elapsed_ms")
            + (item.state.equals("uploading") ? elapsed(item) : 0)) / 1000 + " 秒"
        + "\n网络：" + i.optString("network", "unknown")
        + "\n前台传输：" + (i.optBoolean("foreground") ? "已启用" : "由系统后台任务调度")
        + "\n最近错误码：" + i.optString("last_error_code", "无")
        + timings(i)
        + "\n统计范围：从上传任务开始；不含官方相机拍摄、生成原片和 SDK 内部联网。"
        + "\n等待回执含剩余网络传输、代理和 IDI 处理，不能直接当作服务器处理时间。"
        + (item.error.isEmpty() ? "" : "\n原因：" + item.error);
  }

  private static String duration(JSONObject values, String key) {
    return values.has(key) ? String.format(Locale.CHINA, "%.3f 秒", values.optDouble(key) / 1000) : "未记录";
  }

  private static String timings(JSONObject info) {
    JSONObject requests = info.optJSONObject("requests");
    if (requests == null || requests.length() == 0) return "\n分段耗时：本次请求完成后记录（旧记录无分段数据）";
    StringBuilder text = new StringBuilder("\n\n本次请求分段（分项已包含在总耗时中）");
    String[] operations = {"upload", "receipt_reconcile", "receipt_check"};
    String[] labels = {"上传原片", "异常后找回回执", "上传前核对回执"};
    for (int n = 0; n < operations.length; n++) {
      JSONObject r = requests.optJSONObject(operations[n]);
      if (r == null) continue;
      text.append("\n").append(labels[n]).append("：").append(duration(r, "total_ms"));
      if (r.has("http_status")) text.append(" · HTTP ").append(r.optInt("http_status"));
      text.append("\n连接准备 ").append(duration(r, "connect_ms"))
          .append(" · 交给网络 ").append(duration(r, "write_ms"))
          .append("\n等待响应头 ").append(duration(r, "wait_ms"))
          .append(" · 读取回执 ").append(duration(r, "body_ms"));
      JSONObject server = r.optJSONObject("server_ms");
      if (server == null) text.append("\nIDI 服务分段：未返回（旧版服务或响应中断）");
      else {
        text.append("\nIDI 服务总计：").append(duration(server, "total"));
        String[] keys = {"receive", "dispatch", "queue", "auth", "file_read", "lock", "validate", "store", "archive", "lookup"};
        String[] names = {"接收请求体", "解析与调度", "线程排队", "鉴权", "读取原片", "等待归档锁", "资料校验", "存储与缩略图", "归档与提交", "查询回执"};
        for (int k = 0; k < keys.length; k++) if (server.has(keys[k]))
          text.append("\n  ").append(names[k]).append("：").append(duration(server, keys[k]));
      }
      text.append("\n请求追踪：").append(r.optString("server_trace", r.optString("trace")));
    }
    return text.toString();
  }
}
