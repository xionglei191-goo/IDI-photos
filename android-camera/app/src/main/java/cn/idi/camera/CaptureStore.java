package cn.idi.camera;

import android.content.ContentValues;
import android.content.Context;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import java.io.File;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

final class CaptureStore extends SQLiteOpenHelper {
  // The executor outlives Activity recreation so CameraX can always deliver its result.
  static final java.util.concurrent.ExecutorService io =
      java.util.concurrent.Executors.newSingleThreadExecutor();
  final java.util.Set<String> activeCaptures = java.util.concurrent.ConcurrentHashMap.newKeySet();
  private static CaptureStore instance;
  final Context context;
  final SharedPreferences prefs;

  static synchronized CaptureStore get(Context context) {
    if (instance == null) instance = new CaptureStore(context.getApplicationContext());
    return instance;
  }

  private CaptureStore(Context context) {
    super(context, "camera.sqlite", null, 1);
    this.context = context;
    prefs = context.getSharedPreferences("camera", Context.MODE_PRIVATE);
    setWriteAheadLoggingEnabled(true);
  }

  @Override
  public void onCreate(SQLiteDatabase db) {
    db.execSQL("CREATE TABLE case_cache(id TEXT PRIMARY KEY, data TEXT NOT NULL)");
    db.execSQL(
        "CREATE TABLE captures(id TEXT PRIMARY KEY, metadata TEXT NOT NULL, server TEXT NOT"
            + " NULL, device TEXT NOT NULL, state TEXT NOT NULL, error TEXT NOT NULL"
            + " DEFAULT '', receipt TEXT NOT NULL DEFAULT '', created_at INTEGER NOT"
            + " NULL)");
  }

  @Override
  public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {}

  File directory(String id) {
    return new File(context.getFilesDir(), "captures/" + id);
  }

  File original(String id) {
    Item item = item(id);
    return new File(directory(id), item != null && item.video() ? "original.mp4" : "original.jpg");
  }

  File stamped(String id) {
    return new File(directory(id), "watermark.jpg");
  }

  File thumbnail(String id) {
    return new File(directory(id), "thumbnail.jpg");
  }

  void create(JSONObject meta, boolean demo) throws JSONException {
    create(meta, demo ? "demo" : server(), demo ? "demo" : device());
  }

  private void create(JSONObject meta, String destination, String credential) throws JSONException {
    String id = meta.getString("capture_id");
    if (!directory(id).mkdirs() && !directory(id).isDirectory())
      throw new IllegalStateException("手机存储不可用");
    ContentValues values = new ContentValues();
    values.put("id", id);
    values.put("metadata", meta.toString());
    values.put("server", destination);
    values.put("device", credential);
    values.put("state", "capturing");
    values.put("created_at", System.currentTimeMillis());
    getWritableDatabase().insertOrThrow("captures", null, values);
  }

  // A saved photo and its successor have different identities. Keep the original
  // destination even if pairing changes, and never copy the SDK's photo identity.
  synchronized String nextOfficialPhoto(String previousId, String sessionId) throws Exception {
    Item previous = item(previousId);
    if (previous == null || previous.video() || !"xhey_sdk".equals(previous.meta.optString("source"))
        || !original(previousId).isFile() || !stamped(previousId).isFile())
      throw new Exception("当前原片尚未保存，不能开始下一张");
    String key = "next_official_photo_" + previousId;
    String nextId = prefs.getString(key, "");
    SQLiteDatabase db = getWritableDatabase();
    db.beginTransaction();
    try {
      if (nextId.isEmpty() || item(nextId) == null) {
        JSONObject old = previous.meta;
        JSONObject selected = new JSONObject().put("id", old.getString("case_id"))
            .put("case_number", old.getString("case_number"))
            .put("project_name", old.getString("project_name")).put("location", old.getString("location"));
        JSONObject meta = CameraData.metadata(selected, old.getString("photo_group"),
            old.optString("part"), old.getString("content"), old.optString("role")).put("source", "xhey_sdk");
        nextId = meta.getString("capture_id");
        create(meta, previous.server, previous.device);
        state(nextId, "awaiting_sdk", "");
      }
      if (!prefs.edit().putString(key, nextId).putString("native_series_" + sessionId, nextId)
          .putString("pending_official_capture", nextId).commit())
        throw new Exception("下一张拍摄信息未保存，请重试");
      db.setTransactionSuccessful();
      return nextId;
    } finally { db.endTransaction(); }
  }

  void state(String id, String state, String error) {
    ContentValues values = new ContentValues();
    values.put("state", state);
    values.put("error", error == null ? "" : error);
    getWritableDatabase().update("captures", values, "id=?", new String[] {id});
  }

  void uploaded(String id, JSONObject receipt) {
    receipt(id, "uploaded", receipt);
  }

  // Editing is allowed only before the SDK has returned any image bytes.
  void updateWatermark(String id, WatermarkEditor.Draft draft) throws Exception {
    Item item = item(id);
    if (item == null || !item.official() || !item.state.equals("awaiting_sdk")
        || !emptyOfficialAttempt(item)) throw new Exception("这张照片已经开始保存，请重新拍摄后修改水印");
    if (!java.util.Arrays.asList(CameraData.GROUPS).contains(draft.group)
        || draft.content.trim().isEmpty() || draft.content.length() > 240 || draft.part.length() > 80
        || draft.group.endsWith("checkin") && !java.util.Arrays.asList("building_checkin", "door_checkin").contains(draft.role))
      throw new Exception("水印信息不完整，请检查环节和内容");
    item.meta.put("photo_group", draft.group).put("part", draft.part)
        .put("content", draft.content.trim()).put("role", CameraData.role(draft.group, draft.role));
    ContentValues values = new ContentValues();
    values.put("metadata", item.meta.toString());
    SQLiteDatabase db = getWritableDatabase();
    db.beginTransaction();
    try {
      if (db.update("captures", values, "id=? AND state='awaiting_sdk'", new String[] {id}) != 1)
        throw new Exception("拍摄状态已变化，请重新打开相机");
      if (draft.repeat && !prefs.edit().putString("draft_" + item.meta.getString("case_id"), draft.saved().toString()).commit())
        throw new Exception("后续照片设置未保存，请重试");
      db.setTransactionSuccessful();
    } finally { db.endTransaction(); }
  }

  void updateMediaType(String id, boolean video) throws Exception {
    Item item = item(id);
    if (item == null || !"xhey_sdk".equals(item.meta.optString("source"))
        || !item.state.equals("awaiting_sdk") || !emptyOfficialAttempt(item))
      throw new Exception("拍摄已经开始，请完成本次拍摄后切换");
    if (video) item.meta.put("media_type", "video");
    else item.meta.remove("media_type");
    ContentValues values = new ContentValues();
    values.put("metadata", item.meta.toString());
    if (getWritableDatabase().update("captures", values, "id=? AND state='awaiting_sdk'", new String[] {id}) != 1)
      throw new Exception("拍摄状态已变化，请重新打开相机");
  }

  void recoverAbandonedOfficialCaptures() {
    for (Item item : items()) {
      if (!activeCaptures.contains(item.id)) cancelEmptyOfficialCapture(item.id);
    }
  }

  // Keep the internal session for recovery, but an empty camera visit is not a photo.
  void cancelEmptyOfficialCapture(String id) {
    Item item = item(id);
    if (item == null || !emptyOfficialAttempt(item)) return;
    state(id, "cancelled", "");
    if (id.equals(prefs.getString("pending_official_capture", "")))
      prefs.edit().remove("pending_official_capture").commit();
  }

  private boolean emptyOfficialAttempt(Item item) {
    if (!item.official()) return false;
    boolean emptyState = item.state.equals("awaiting_sdk")
        || item.state.equals("camera_error") || item.state.equals("cancelled")
        || item.state.equals("capture_failed")
            && (item.error.equals("官方拍摄已取消，未生成照片")
                || item.error.equals("上次官方拍摄已中断，未生成照片，请重新拍摄")
                || item.error.equals("官方拍摄已取消或未返回完整照片，请重新拍摄"));
    if (!emptyState || !item.receipt.isEmpty()) return false;
    File directory = directory(item.id);
    File[] files = directory.listFiles();
    // Any saved or partial file is evidence of a photo attempt. Never hide it.
    return files == null ? !directory.exists() : files.length == 0;
  }

  List<Item> recordItems() {
    List<Item> result = new ArrayList<>();
    for (Item item : items()) if (!emptyOfficialAttempt(item)) result.add(item);
    return result;
  }

  void sdkInfo(String id, String comment) throws JSONException {
    JSONObject data = new JSONObject(comment).optJSONObject("data");
    if (data == null) return;
    Item item = item(id);
    if (item == null) return;
    JSONObject base = data.optJSONObject("baseInfo");
    JSONObject extension = data.optJSONObject("watermarkContentExtension");
    String time = base == null ? "" : base.optString("time", "");
    String code = extension == null ? "" : extension.optString("antiFakeCode", "");
    // Preserve the SDK callback as reported metadata, never as a verification result.
    if (!time.isEmpty() && time.length() <= 40) item.meta.put("sdk_photo_time", time);
    if (!code.isEmpty() && code.length() <= 256) item.meta.put("sdk_anti_fake_code", code);
    ContentValues values = new ContentValues();
    values.put("metadata", item.meta.toString());
    getWritableDatabase().update("captures", values, "id=?", new String[] {id});
  }

  void receipt(String id, String state, JSONObject receipt) {
    ContentValues values = new ContentValues();
    values.put("state", state);
    values.put("error", receipt.optString("error"));
    values.put("receipt", receipt.toString());
    getWritableDatabase().update("captures", values, "id=?", new String[] {id});
  }

  List<Item> items() {
    List<Item> result = new ArrayList<>();
    try (Cursor cursor =
        getReadableDatabase().rawQuery("SELECT * FROM captures ORDER BY created_at DESC", null)) {
      while (cursor.moveToNext()) result.add(new Item(cursor));
    }
    return result;
  }

  Item item(String id) {
    try (Cursor cursor =
        getReadableDatabase().rawQuery("SELECT * FROM captures WHERE id=?", new String[] {id})) {
      return cursor.moveToFirst() ? new Item(cursor) : null;
    }
  }

  void replaceCases(JSONArray items) throws JSONException {
    SQLiteDatabase db = getWritableDatabase();
    db.beginTransaction();
    try {
      db.delete("case_cache", null, null);
      for (int i = 0; i < items.length(); i++) {
        JSONObject item = items.getJSONObject(i);
        ContentValues values = new ContentValues();
        values.put("id", item.getString("id"));
        values.put("data", item.toString());
        db.insertOrThrow("case_cache", null, values);
      }
      db.setTransactionSuccessful();
    } finally {
      db.endTransaction();
    }
    prefs.edit().putLong("last_sync", System.currentTimeMillis()).apply();
  }

  List<JSONObject> cases() {
    List<JSONObject> result = new ArrayList<>();
    try (Cursor cursor =
        getReadableDatabase().rawQuery("SELECT data FROM case_cache ORDER BY rowid", null)) {
      while (cursor.moveToNext()) {
        try {
          result.add(new JSONObject(cursor.getString(0)));
        } catch (JSONException error) {
          throw new IllegalStateException(error);
        }
      }
    }
    return result;
  }

  void connect(String server, JSONObject pair) throws JSONException {
    // Pending captures keep their original destination and device credential.
    String device = pair.getString("device_id");
    prefs
        .edit()
        .putString("server", server)
        .putString("device", device)
        .putString("device_name", pair.optString("device_name"))
        .putString("user_id", pair.optString("user_id"))
        .putString("user_name", pair.optString("user_name"))
        .putString("token_" + device, pair.getString("token"))
        .putString("selected_case", "")
        .putBoolean("demo", false)
        .commit();
    getWritableDatabase().delete("case_cache", null, null);
  }

  String server() {
    return prefs.getString("server", "https://idi.xionglei.online");
  }

  String device() {
    return prefs.getString("device", "");
  }

  String token(String device) {
    return prefs.getString("token_" + device, "");
  }

  boolean connected() {
    return !device().isEmpty() && !token(device()).isEmpty();
  }

  static final class Item {
    final String id, server, device, state, error, receipt;
    final JSONObject meta;
    final long createdAt;

    Item(Cursor c) {
      id = value(c, "id");
      server = value(c, "server");
      device = value(c, "device");
      state = value(c, "state");
      error = value(c, "error");
      receipt = value(c, "receipt");
      createdAt = c.getLong(c.getColumnIndexOrThrow("created_at"));
      try {
        meta = new JSONObject(value(c, "metadata"));
      } catch (JSONException e) {
        throw new IllegalStateException(e);
      }
    }

    boolean demo() {
      return "demo".equals(device);
    }

    boolean official() {
      return "xhey_sdk".equals(meta.optString("source")) || "xhey_h5".equals(meta.optString("source"));
    }

    boolean video() {
      return "video".equals(meta.optString("media_type"));
    }

    boolean verified() {
      try {
        return official() && "verified".equals(new JSONObject(receipt).optString("verification_state"));
      } catch (JSONException ignored) {
        return false;
      }
    }

    private static String value(Cursor cursor, String key) {
      return cursor.getString(cursor.getColumnIndexOrThrow(key));
    }
  }
}
