package cn.idi.camera;

import android.os.SystemClock;
import android.provider.Settings;
import java.util.Objects;
import org.json.JSONObject;

/** A paired device reuses the server-signed SDK credential until its bounded expiry. */
final class NativeAuthorization {
  static final String KEY = "native_authorization";
  static final long MAX_LIFETIME_MS = 23L * 60 * 60 * 1000;
  static final long REFRESH_BEFORE_MS = 60L * 60 * 1000;

  static String scope(CaptureStore store) { return store.server() + "\n" + store.device(); }

  static int boot(CaptureStore store) {
    return Settings.Global.getInt(store.context.getContentResolver(), Settings.Global.BOOT_COUNT, -1);
  }

  static void clear(CaptureStore store) {
    store.prefs.edit().remove(KEY).commit();
  }

  static void denied(CaptureStore store, String expectedScope) {
    if (!expectedScope.equals(scope(store))) return;
    clear(store);
    store.getWritableDatabase().delete("case_cache", null, null);
    store.prefs.edit().remove("selected_case").remove("last_sync").commit();
  }

  static void save(CaptureStore store, String expectedScope, JSONObject authorization) throws Exception {
    if (!expectedScope.equals(scope(store))) return;
    if (!authorization.optBoolean("ready")) {
      clear(store);
      throw new Exception(authorization.optString("message", "电脑端拍摄功能尚未就绪"));
    }
    if (!store.device().equals(authorization.getString("device_id"))
        || !BuildConfig.XHEY_SDK_VERSION.equals(authorization.getString("sdk_version")))
      throw new Exception("拍摄准备信息与当前连接不匹配，请重新同步");
    JSONObject options = authorization.getJSONObject("options");
    for (String field : new String[] {"appid", "noncestr", "signature"})
      if (options.getString(field).trim().isEmpty()) throw new Exception("拍摄准备信息不完整");
    long serverTime = authorization.getLong("server_time");
    long expiry = authorization.getLong("expires_at");
    long signedTime = options.getLong("timestamp");
    long lifetime = (Math.min(expiry, signedTime + MAX_LIFETIME_MS / 1000) - serverTime) * 1000;
    if (lifetime <= 0 || lifetime > MAX_LIFETIME_MS || signedTime > serverTime + 60)
      throw new Exception("拍摄准备信息已过期，请重新同步");
    JSONObject saved = new JSONObject(authorization.toString())
        .put("scope", expectedScope).put("received_elapsed", SystemClock.elapsedRealtime())
        .put("boot", boot(store)).put("lifetime_ms", lifetime);
    if (!store.prefs.edit().putString(KEY, saved.toString()).commit())
      throw new Exception("拍摄准备信息未保存，请检查手机空间");
  }

  static long remaining(JSONObject saved, String expectedScope, int boot, long elapsed) {
    if (!expectedScope.equals(saved.optString("scope")) || boot < 0 || boot != saved.optInt("boot", -2))
      return 0;
    long received = saved.optLong("received_elapsed", -1);
    long lifetime = saved.optLong("lifetime_ms", 0);
    if (received < 0 || elapsed < received || lifetime <= 0 || lifetime > MAX_LIFETIME_MS) return 0;
    return Math.max(0, lifetime - (elapsed - received));
  }

  static JSONObject cached(CaptureStore store) {
    if (!store.connected()) return null;
    try {
      JSONObject saved = new JSONObject(store.prefs.getString(KEY, "{}"));
      if (remaining(saved, scope(store), boot(store), SystemClock.elapsedRealtime()) > 0
          && saved.optBoolean("ready")
          && store.device().equals(saved.optString("device_id"))
          && BuildConfig.XHEY_SDK_VERSION.equals(saved.optString("sdk_version"))) return saved;
    } catch (Exception ignored) { }
    return null;
  }

  static boolean refreshDue(CaptureStore store) {
    JSONObject saved = cached(store);
    return saved == null || remaining(saved, scope(store), boot(store), SystemClock.elapsedRealtime()) <= REFRESH_BEFORE_MS;
  }

  static JSONObject session(CaptureStore store, CaptureStore.Item item) throws Exception {
    if (!Objects.equals(item.server, store.server()) || !Objects.equals(item.device, store.device()))
      throw new Exception("手机连接已改变，请返回当前案件重新拍摄");
    JSONObject saved = cached(store);
    if (saved == null) throw new Exception("拍摄准备信息已过期，请返回案件页同步后重试");
    return session(saved, item.meta, store.cases());
  }

  static JSONObject session(JSONObject saved, JSONObject meta, java.util.List<JSONObject> cases) throws Exception {
    JSONObject selected = null;
    for (JSONObject candidate : cases)
      if (meta.getString("case_id").equals(candidate.optString("id"))) { selected = candidate; break; }
    if (selected == null || !selected.optBoolean("capture_enabled") || "closed".equals(selected.optString("workflow_stage")))
      throw new Exception("本案已不在当前可拍摄项目中，请返回案件页同步");
    for (String field : new String[] {"case_number", "project_name", "location"})
      if (!meta.getString(field).equals(selected.optString(field)))
        throw new Exception("案件信息已更新，请返回案件列表重新选择");
    String group = meta.getString("photo_group"), role = meta.optString("role");
    String stage = CameraData.label(group);
    if ("待分类".equals(stage)) throw new Exception("请选择有效的拍摄环节");
    if (group.endsWith("checkin")) {
      if (!"building_checkin".equals(role) && !"door_checkin".equals(role))
        throw new Exception("请选择楼栋或入户门用途");
      stage += " · " + ("building_checkin".equals(role) ? "楼栋" : "入户门");
    } else if (!role.isEmpty() && !CameraData.role(group, "").equals(role))
      throw new Exception("拍摄环节与用途不一致");
    boolean video = "video".equals(meta.optString("media_type"));
    if (video && !saved.optBoolean("video_enabled")) throw new Exception("电脑端尚未启用录像，请同步后重试");
    JSONObject fields = new JSONObject().put("案件号", meta.getString("case_number"))
        .put("小区", meta.getString("project_name")).put("位置", meta.getString("location"))
        .put("环节", stage).put("内容", meta.getString("content"));
    JSONObject options = new JSONObject(saved.getJSONObject("options").toString()).put("customInputItems", fields);
    JSONObject result = new JSONObject().put("capture_id", meta.getString("capture_id"))
        .put("sdk_version", saved.getString("sdk_version")).put("media_type", video ? "video" : "image")
        .put("options", options);
    if (saved.optBoolean("video_enabled")) result.put("max_record_seconds", saved.optInt("max_record_seconds", 60));
    return result;
  }
}
