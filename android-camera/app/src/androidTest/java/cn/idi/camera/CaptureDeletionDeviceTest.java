package cn.idi.camera;

import static org.junit.Assert.*;
import android.app.UiAutomation;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.TextView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.work.WorkInfo;
import androidx.work.WorkManager;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Synthetic files and a loopback server only; no production capture is deleted. */
@RunWith(AndroidJUnit4.class)
public class CaptureDeletionDeviceTest {
  final Context context = ApplicationProvider.getApplicationContext();
  final CaptureStore store = CaptureStore.get(context);
  final List<String> created = new ArrayList<>();
  final String device = "delete-test-" + java.util.UUID.randomUUID();
  boolean initialized, hadDemo, oldDemo, hadNotification, oldNotification;
  boolean hadServer, hadDevice;
  String oldServer, oldDevice;

  @Before public void isolate() {
    org.junit.Assume.assumeTrue("Use an isolated profile without other pending deletion requests",
        store.items().stream().noneMatch(item -> item.state.equals("delete_pending") || item.state.equals("delete_failed")));
    hadServer = store.prefs.contains("server"); oldServer = store.server();
    hadDevice = store.prefs.contains("device"); oldDevice = store.device();
    hadDemo = store.prefs.contains("demo"); oldDemo = store.prefs.getBoolean("demo", false);
    hadNotification = store.prefs.contains("upload_notification_asked");
    oldNotification = store.prefs.getBoolean("upload_notification_asked", false);
    initialized = true;
    store.prefs.edit().putBoolean("demo", true).putBoolean("upload_notification_asked", true)
        .putString("token_" + device, "synthetic-local-delete-token").commit();
  }

  @After public void cleanup() throws Exception {
    if (!initialized) return;
    for (String id : created) {
      WorkManager.getInstance(context).cancelUniqueWork("capture-" + id).getResult().get(5, TimeUnit.SECONDS);
      WorkManager.getInstance(context).cancelUniqueWork("capture-delete-" + id).getResult().get(5, TimeUnit.SECONDS);
      store.activeCaptures.remove(id);
      // Remove only UUID fixtures created by this test, never any existing row.
      store.getWritableDatabase().delete("captures", "id=?", new String[] {id});
      File directory = store.directory(id);
      File[] files = directory.listFiles();
      if (files != null) for (File file : files) if (file.isFile()) file.delete();
      directory.delete();
    }
    android.content.SharedPreferences.Editor restore = store.prefs.edit().remove("token_" + device).remove("token_other-" + device);
    if (hadServer) restore.putString("server", oldServer); else restore.remove("server");
    if (hadDevice) restore.putString("device", oldDevice); else restore.remove("device");
    if (hadDemo) restore.putBoolean("demo", oldDemo); else restore.remove("demo");
    if (hadNotification) restore.putBoolean("upload_notification_asked", oldNotification);
    else restore.remove("upload_notification_asked");
    restore.commit();
  }

  private CaptureStore.Item fixture(DeleteServer server, boolean video, String state) throws Exception {
    store.prefs.edit().putString("server", server.url()).putString("device", device).commit();
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "隔离测试部位",
        "删除功能测试，非业务资料", "").put("source", "xhey_sdk")
        .put("case_number", video ? "DELETE-VIDEO-TEST" : "DELETE-PHOTO-TEST");
    if (video) meta.put("media_type", "video");
    store.create(meta, true);
    String id = meta.getString("capture_id"); created.add(id);
    Bitmap bitmap = Bitmap.createBitmap(64, 64, Bitmap.Config.ARGB_8888);
    bitmap.eraseColor(video ? Color.BLUE : Color.RED);
    ByteArrayOutputStream jpeg = new ByteArrayOutputStream();
    assertTrue(bitmap.compress(Bitmap.CompressFormat.JPEG, 90, jpeg)); bitmap.recycle();
    Files.write(store.thumbnail(id).toPath(), jpeg.toByteArray());
    Files.write(store.original(id).toPath(), video ? new byte[] {0, 0, 0, 16, 102, 116, 121, 112} : jpeg.toByteArray());
    ContentValues values = new ContentValues();
    values.put("server", server.url()); values.put("device", device); values.put("state", state);
    if (state.equals("uploaded")) values.put("receipt", uploadReceipt(id).toString());
    store.getWritableDatabase().update("captures", values, "id=?", new String[] {id});
    return store.item(id);
  }

  private static JSONObject uploadReceipt(String id) throws Exception {
    return new JSONObject().put("capture_id", id).put("photo_id", "synthetic-" + id)
        .put("state", "uploaded").put("verification_state", "not_requested");
  }

  @Test public void deletionStopsUploadAndLateCallbacksCannotRestorePhotoOrVideo() throws Exception {
    try (DeleteServer server = new DeleteServer()) {
      server.hold = new CountDownLatch(1);
      for (boolean video : new boolean[] {false, true}) {
        CaptureStore.Item item = fixture(server, video, "queued");
        String owner = "old-upload-" + item.id;
        assertTrue(store.claimUpload(item.id, owner));
        try (CameraApi.Transfer transfer = new CameraApi.Transfer(video)) {
          transfer.bindUpload(store, item.id);
          CaptureDeletion.request(context, item.id);
          assertTrue("In-flight upload must be cancelled", transfer.cancelled());
          assertEquals("delete_pending", store.item(item.id).state);
          assertFalse(store.claimUpload(item.id, "new-upload"));
          assertFalse(store.uploadUpdate(item.id, owner, new JSONObject(), "uploaded", "", uploadReceipt(item.id)));
          store.uploaded(item.id, uploadReceipt(item.id));
          store.state(item.id, "queued", "late state callback");
          assertEquals("delete_pending", store.item(item.id).state);
          UploadWorker.enqueue(context, item.id, true);
          List<WorkInfo> uploads = WorkManager.getInstance(context).getWorkInfosForUniqueWork("capture-" + item.id)
              .get(5, TimeUnit.SECONDS);
          for (WorkInfo work : uploads) assertTrue("Deleted capture cannot re-enter upload queue", work.getState().isFinished());
          assertTrue(store.original(item.id).isFile());
          assertTrue(visible(item.id));
        }
      }
      assertEquals(0, server.uploads.get());
      server.hold.countDown();
      for (String id : created) awaitState(id, "deleted");
    }
  }

  @Test public void onlyMatchingServerConfirmationHidesAndCleansFiles() throws Exception {
    try (DeleteServer server = new DeleteServer()) {
      for (boolean video : new boolean[] {false, true}) {
        CaptureStore.Item item = fixture(server, video, "uploaded");
        String hash = OfficialPhotoWriter.sha256(store.original(item.id));
        assertTrue(store.beginDeletion(item.id));
        String owner = "delete-test-owner-" + item.id;
        assertTrue(store.claimDeletion(item.id, owner));
        for (String bad : new String[] {"wrong_capture", "wrong_case", "unconfirmed"}) {
          server.reply = bad;
          try (CameraApi.Transfer transfer = new CameraApi.Transfer(false)) {
            assertThrows(UploadEngine.ReceiptError.class,
                () -> CaptureDeletion.perform(store, store.item(item.id), transfer, owner));
          }
          assertEquals("delete_pending", store.item(item.id).state);
          assertTrue(visible(item.id));
          assertEquals(hash, OfficialPhotoWriter.sha256(store.original(item.id)));
        }
        server.reply = "deleted";
        try (CameraApi.Transfer transfer = new CameraApi.Transfer(false)) {
          assertTrue(CaptureDeletion.perform(store, store.item(item.id), transfer, owner));
        }
        assertEquals("deleted", store.item(item.id).state);
        assertFalse(visible(item.id));
        assertFalse(store.directory(item.id).exists());
        assertFalse(store.claimUpload(item.id, "late-upload"));
        int before = server.deletes.get();
        CaptureDeletion.request(context, item.id);
        assertEquals(before, server.deletes.get());
      }
      assertEquals(0, server.uploads.get());
    }
  }

  @Test public void forbiddenKeepsOriginalAndOfflineIntentSurvivesRecreationThenRetries() throws Exception {
    try (DeleteServer server = new DeleteServer()) {
      server.status = 403;
      CaptureStore.Item denied = fixture(server, false, "uploaded");
      String hash = OfficialPhotoWriter.sha256(store.original(denied.id));
      CaptureDeletion.request(context, denied.id);
      awaitState(denied.id, "delete_failed");
      assertTrue(store.item(denied.id).error.contains("无删除权限"));
      assertTrue(visible(denied.id));
      assertEquals(hash, OfficialPhotoWriter.sha256(store.original(denied.id)));

      server.status = 200; server.drop = true;
      CaptureStore.Item offline = fixture(server, true, "failed");
      CaptureDeletion.request(context, offline.id);
      await(() -> store.item(offline.id).error.contains("网络"), "offline error stored");
      assertEquals("delete_pending", store.item(offline.id).state);
      try (SQLiteDatabase read = SQLiteDatabase.openDatabase(context.getDatabasePath("camera.sqlite").getPath(),
          null, SQLiteDatabase.OPEN_READONLY);
          Cursor row = read.rawQuery("SELECT state FROM captures WHERE id=?", new String[] {offline.id})) {
        assertTrue(row.moveToFirst()); assertEquals("delete_pending", row.getString(0));
      }
      try (ActivityScenario<MainActivity> activity = ActivityScenario.launch(MainActivity.class)) {
        activity.recreate();
        assertEquals("delete_pending", store.item(offline.id).state);
        assertTrue(visible(offline.id));
        assertTrue(store.original(offline.id).exists());
      }
      // Only the explicit retry follows the server becoming reachable again.
      server.drop = false;
      CaptureDeletion.retry(context, offline.id);
      awaitState(offline.id, "deleted");
      assertFalse(visible(offline.id));
      assertEquals("delete_failed", store.item(denied.id).state);
      assertTrue(store.original(denied.id).exists());
      assertEquals(0, server.uploads.get());
    }
  }

  @Test public void confirmationShowsCorrectCaseAndThumbnailBesideUploadDetailsForBothMedia() throws Exception {
    try (DeleteServer server = new DeleteServer()) {
      CaptureStore.Item photo = fixture(server, false, "uploaded"), video = fixture(server, true, "uploaded");
      try (ActivityScenario<MainActivity> activity = ActivityScenario.launch(MainActivity.class)) {
        activity.onActivity(a -> a.show("records"));
        CaptureStore.io.submit(() -> {}).get(10, TimeUnit.SECONDS);
        awaitRecordActions(activity, video.id);
        awaitDrawnFrame(activity);
        screenshot("records-actions");
        for (CaptureStore.Item item : new CaptureStore.Item[] {photo, video}) {
          awaitRecordActions(activity, item.id);
          activity.onActivity(a -> {
            View detail = a.root.findViewWithTag("upload-details:" + item.id);
            View remove = a.root.findViewWithTag("delete:" + item.id);
            assertNotNull(detail); assertNotNull(remove);
            assertSame("Delete belongs beside upload details", detail.getParent(), remove.getParent());
            ViewGroup controls = (ViewGroup) detail.getParent();
            assertTrue(controls.indexOfChild(remove) > controls.indexOfChild(detail));
            assertTrue(remove.isEnabled()); assertTrue(remove.performClick());
          });
          String media = item.video() ? "视频" : "照片";
          awaitNode("删除" + media, null);
          awaitNode("案件号：" + item.meta.getString("case_number"), null);
          AccessibilityNodeInfo thumb = awaitNode(null, "待删除" + media + "缩略图");
          assertEquals("android.widget.ImageView", String.valueOf(thumb.getClassName()));
          screenshot("confirm-" + (item.video() ? "video" : "photo"));
          click("取消");
          assertEquals("uploaded", store.item(item.id).state);
          assertTrue(store.original(item.id).exists());
        }
        assertEquals(0, server.deletes.get());
        store.activeCaptures.add(photo.id);
        activity.onActivity(a -> {
          a.recordsScreen.refresh();
          assertFalse(a.root.findViewWithTag("delete:" + photo.id).isEnabled());
        });
        store.activeCaptures.remove(photo.id);
        assertTrue(store.beginDeletion(photo.id));
        assertTrue(store.beginDeletion(video.id));
        assertTrue(store.claimDeletion(video.id, "failed-ui"));
        assertTrue(store.deletionUpdate(video.id, "failed-ui", "delete_failed", "无删除权限", null));
        activity.onActivity(a -> {
          a.recordsScreen.refresh();
          TextView pending = a.root.findViewWithTag("delete:" + photo.id);
          TextView failed = a.root.findViewWithTag("delete:" + video.id);
          assertEquals("待同步", pending.getText().toString()); assertFalse(pending.isEnabled());
          assertEquals("重试删除", failed.getText().toString()); assertTrue(failed.isEnabled());
        });
      }
    }
  }

  @Test public void confirmingDeletionStaysVisibleUntilServerAcknowledges() throws Exception {
    try (DeleteServer server = new DeleteServer()) {
      server.hold = new CountDownLatch(1);
      CaptureStore.Item photo = fixture(server, false, "uploaded");
      try (ActivityScenario<MainActivity> activity = ActivityScenario.launch(MainActivity.class)) {
        activity.onActivity(a -> { a.show("records"); a.recordsScreen.delete(photo.id); });
        awaitNode("删除照片", null); click("确认删除");
        await(() -> server.deletes.get() > 0, "delete request reaches loopback server");
        assertEquals("delete_pending", store.item(photo.id).state);
        assertTrue(visible(photo.id)); assertTrue(store.original(photo.id).exists());
        server.hold.countDown();
        awaitState(photo.id, "deleted");
        activity.onActivity(a -> { a.recordsScreen.refresh(); assertNull(a.root.findViewWithTag("delete:" + photo.id)); });
      }
    }
  }

  @Test public void changingPairingCannotDeleteOldDeviceRecords() throws Exception {
    try (DeleteServer server = new DeleteServer()) {
      CaptureStore.Item fresh = fixture(server, false, "uploaded"), pending = fixture(server, true, "uploaded");
      assertTrue(store.beginDeletion(pending.id));
      String owner = "old-device-delete";
      assertTrue(store.claimDeletion(pending.id, owner));
      store.prefs.edit().putString("device", "other-" + device)
          .putString("token_other-" + device, "synthetic-other-token").commit();
      assertThrows(Exception.class, () -> CaptureDeletion.request(context, fresh.id));
      assertThrows(Exception.class, () -> CaptureDeletion.retry(context, pending.id));
      try (CameraApi.Transfer transfer = new CameraApi.Transfer(false)) {
        assertThrows(Exception.class, () -> CaptureDeletion.perform(store, store.item(pending.id), transfer, owner));
      }
      assertEquals("uploaded", store.item(fresh.id).state);
      assertTrue(visible(fresh.id)); assertTrue(visible(pending.id));
      assertTrue(store.original(fresh.id).exists()); assertTrue(store.original(pending.id).exists());
      assertEquals(0, server.deletes.get());
    }
  }

  /** Opt-in synthetic transport test against the separately launched local-only backend. */
  @Test public void liveLocalBackendUploadDeleteAndRejectReplay() throws Exception {
    android.os.Bundle arguments = InstrumentationRegistry.getArguments();
    org.junit.Assume.assumeTrue("1".equals(arguments.getString("delete_live", "")));
    String server = CameraApi.normalizeServer(arguments.getString("server", ""));
    java.net.URI target = java.net.URI.create(server);
    assertEquals("Only a local isolated HTTP acceptance server is allowed", "http", target.getScheme());
    assertTrue("Never use production for the deletion fixture",
        java.util.Arrays.asList("127.0.0.1", "localhost", "10.0.2.2").contains(target.getHost()));
    String code = arguments.getString("pair_code", "");
    assertEquals("Supply the isolated server's one-use pairing code", 10, code.length());

    java.util.Map<String, ?> preferences = new java.util.HashMap<>(store.prefs.getAll());
    org.json.JSONArray casesBefore = new org.json.JSONArray();
    for (JSONObject item : store.cases()) casesBefore.put(new JSONObject(item.toString()));
    JSONObject evidence = new JSONObject().put("server", server).put("synthetic_image", true)
        .put("sdk_started", false).put("completed", false);
    String liveId = null;
    boolean completed = false;
    try {
      JSONObject pair = CameraApi.request(server, "/api/camera/pair", "", new JSONObject().put("code", code));
      store.connect(server, pair);
      String token = store.token(store.device());
      JSONObject config = CameraApi.request(server, "/api/camera/native-config", token, null);
      assertTrue("Local-only camera service must be ready", config.getBoolean("ready"));
      assertFalse("Do not send this synthetic image to a verifier", config.getBoolean("auto_verify"));
      JSONObject cases = CameraApi.request(server, "/api/camera/cases", token, null);
      assertTrue(cases.getJSONArray("items").length() > 0);
      store.replaceCases(cases.getJSONArray("items"));
      JSONObject meta = CameraData.metadata(store.cases().get(0), "construction", "删除集成测试部位",
          "SYNTHETIC DELETE ACCEPTANCE - NOT FIELD EVIDENCE", "").put("source", "xhey_sdk");
      String id = meta.getString("capture_id"); liveId = id; created.add(id);
      store.create(meta, false);
      Bitmap bitmap = Bitmap.createBitmap(640, 480, Bitmap.Config.ARGB_8888);
      android.graphics.Canvas canvas = new android.graphics.Canvas(bitmap);
      canvas.drawColor(Color.rgb(225, 232, 238));
      android.graphics.Paint paint = new android.graphics.Paint();
      paint.setColor(Color.BLACK); paint.setTextSize(24);
      canvas.drawText("SYNTHETIC DELETE ACCEPTANCE", 24, 80, paint);
      canvas.drawText(id, 24, 130, paint);
      ByteArrayOutputStream jpeg = new ByteArrayOutputStream();
      assertTrue(bitmap.compress(Bitmap.CompressFormat.JPEG, 90, jpeg)); bitmap.recycle();
      byte[] bytes = jpeg.toByteArray();
      OfficialPhotoWriter.save(store, id, bytes);
      store.state(id, "failed", "Synthetic fixture: direct test transport");
      String hash = OfficialPhotoWriter.sha256(store.original(id));
      JSONObject receipt = CameraApi.upload(store, store.item(id));
      assertEquals(id, receipt.getString("capture_id"));
      assertEquals("uploaded", receipt.getString("state"));
      assertEquals("not_requested", receipt.getString("verification_state"));
      assertEquals(hash, receipt.getString("original_hash"));
      store.uploaded(id, receipt);
      CaptureDeletion.request(context, id);
      awaitState(id, "deleted");
      CaptureStore.Item deleted = store.item(id);
      assertFalse(visible(id));
      assertFalse(store.directory(id).exists());
      CameraApi.Failure gone = assertThrows(CameraApi.Failure.class,
          () -> CameraApi.request(server, "/api/camera/official-captures/" + id, token, null));
      assertEquals(410, gone.status);
      int replay = replayDeletedUpload(server, token, meta, bytes);
      assertEquals("The server tombstone rejects replay of the same capture ID", 410, replay);
      evidence.put("capture_id", id).put("case_id", meta.getString("case_id"))
          .put("auto_verify", config.getBoolean("auto_verify")).put("original_hash", hash)
          .put("upload_receipt", receipt).put("delete_receipt", new JSONObject(deleted.receipt))
          .put("receipt_get_status", gone.status).put("replay_post_status", replay)
          .put("local_state", deleted.state).put("local_visible", visible(id))
          .put("local_directory_exists", store.directory(id).exists());
      completed = true;
      // Leave the deleted local tombstone for independent host inspection. No media remains.
      created.remove(id);
    } finally {
      if (liveId != null) {
        WorkManager.getInstance(context).cancelUniqueWork("capture-" + liveId).getResult().get(5, TimeUnit.SECONDS);
        WorkManager.getInstance(context).cancelUniqueWork("capture-delete-" + liveId).getResult().get(5, TimeUnit.SECONDS);
      }
      store.replaceCases(casesBefore);
      restorePreferences(preferences);
      org.json.JSONArray casesAfter = new org.json.JSONArray();
      for (JSONObject item : store.cases()) casesAfter.put(new JSONObject(item.toString()));
      boolean preferencesRestored = preferences.equals(store.prefs.getAll());
      boolean casesRestored = casesBefore.toString().equals(casesAfter.toString());
      evidence.put("completed", completed).put("preferences_restored", preferencesRestored)
          .put("cases_restored", casesRestored).put("saved_at", java.time.Instant.now().toString());
      File directory = new File(context.getFilesDir(), "deletion-verification"); directory.mkdirs();
      Files.write(new File(directory, "live-result.json").toPath(), evidence.toString(2).getBytes(StandardCharsets.UTF_8));
      assertTrue("Restore every pre-test preference", preferencesRestored);
      assertTrue("Restore every pre-test cached case", casesRestored);
    }
  }

  private void restorePreferences(java.util.Map<String, ?> snapshot) {
    android.content.SharedPreferences.Editor editor = store.prefs.edit().clear();
    for (java.util.Map.Entry<String, ?> entry : snapshot.entrySet()) {
      String key = entry.getKey(); Object value = entry.getValue();
      if (value instanceof String text) editor.putString(key, text);
      else if (value instanceof Boolean bool) editor.putBoolean(key, bool);
      else if (value instanceof Integer number) editor.putInt(key, number);
      else if (value instanceof Long number) editor.putLong(key, number);
      else if (value instanceof Float number) editor.putFloat(key, number);
      else if (value instanceof java.util.Set<?> values) {
        java.util.Set<String> strings = new java.util.HashSet<>();
        for (Object item : values) strings.add((String) item);
        editor.putStringSet(key, strings);
      } else throw new AssertionError("Unsupported preference type: " + key);
    }
    assertTrue(editor.commit());
  }

  /** Deliberately bypasses the app's deleted-record guard to verify the server's guard. */
  private static int replayDeletedUpload(String server, String token, JSONObject meta, byte[] jpeg) throws Exception {
    String boundary = "IDI-Deleted-Replay-" + java.util.UUID.randomUUID();
    ByteArrayOutputStream body = new ByteArrayOutputStream();
    body.write(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
        + "Content-Type: application/json; charset=utf-8\r\n\r\n" + meta + "\r\n--" + boundary
        + "\r\nContent-Disposition: form-data; name=\"image\"; filename=\"synthetic.jpg\"\r\n"
        + "Content-Type: image/jpeg\r\n\r\n").getBytes(StandardCharsets.UTF_8));
    body.write(jpeg); body.write(("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8));
    java.net.HttpURLConnection connection = (java.net.HttpURLConnection)
        java.net.URI.create(server + "/api/camera/official-captures").toURL().openConnection();
    try {
      connection.setInstanceFollowRedirects(false);
      connection.setConnectTimeout(10000); connection.setReadTimeout(15000);
      connection.setRequestMethod("POST"); connection.setDoOutput(true);
      connection.setRequestProperty("Authorization", "Bearer " + token);
      connection.setRequestProperty("X-IDI-Request", "1");
      connection.setRequestProperty("Accept", "application/json");
      connection.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
      connection.setFixedLengthStreamingMode(body.size());
      try (java.io.OutputStream stream = connection.getOutputStream()) { body.writeTo(stream); }
      return connection.getResponseCode();
    } finally { connection.disconnect(); }
  }

  private boolean visible(String id) { return store.recordItems().stream().anyMatch(item -> item.id.equals(id)); }
  private void awaitState(String id, String state) throws Exception { await(() -> state.equals(store.item(id).state), "state " + state); }
  private interface Condition { boolean reached() throws Exception; }
  private static void await(Condition condition, String reason) throws Exception {
    long deadline = SystemClock.elapsedRealtime() + 15000;
    while (!condition.reached() && SystemClock.elapsedRealtime() < deadline) Thread.sleep(50);
    assertTrue(reason, condition.reached());
  }

  private static AccessibilityNodeInfo awaitNode(String text, String description) throws Exception {
    UiAutomation automation = InstrumentationRegistry.getInstrumentation().getUiAutomation();
    long deadline = SystemClock.elapsedRealtime() + 5000;
    while (SystemClock.elapsedRealtime() < deadline) {
      AccessibilityNodeInfo found = find(automation.getRootInActiveWindow(), text, description);
      if (found != null && found.isVisibleToUser()) return found;
      Thread.sleep(50);
    }
    throw new AssertionError("Missing confirmation control: " + (text == null ? description : text));
  }
  private static AccessibilityNodeInfo find(AccessibilityNodeInfo node, String text, String description) {
    if (node == null) return null;
    if (text != null && text.contentEquals(node.getText() == null ? "" : node.getText())
        || description != null && description.contentEquals(node.getContentDescription() == null ? "" : node.getContentDescription())) return node;
    for (int i = 0; i < node.getChildCount(); i++) {
      AccessibilityNodeInfo found = find(node.getChild(i), text, description);
      if (found != null) return found;
    }
    return null;
  }
  private static void click(String text) throws Exception {
    AccessibilityNodeInfo target = awaitNode(text, null);
    while (target != null && !target.isClickable()) target = target.getParent();
    assertNotNull(target); assertTrue(target.performAction(AccessibilityNodeInfo.ACTION_CLICK));
  }

  private static void awaitRecordActions(ActivityScenario<MainActivity> activity, String id) throws Exception {
    awaitNode("上传记录", null);
    java.util.concurrent.atomic.AtomicBoolean ready = new java.util.concurrent.atomic.AtomicBoolean();
    java.util.concurrent.atomic.AtomicReference<String> observed = new java.util.concurrent.atomic.AtomicReference<>("not laid out");
    long deadline = SystemClock.elapsedRealtime() + 7000;
    while (!ready.get() && SystemClock.elapsedRealtime() < deadline) {
      activity.onActivity(a -> {
        if (!"records".equals(a.screen)) { observed.set("screen=" + a.screen); return; }
        View detail = a.root.findViewWithTag("upload-details:" + id);
        View remove = a.root.findViewWithTag("delete:" + id);
        if (detail == null || remove == null || !detail.isLaidOut() || !remove.isLaidOut()) {
          observed.set("controls missing or not laid out"); return;
        }
        if (detail.getParent() != remove.getParent()) { observed.set("different control rows"); return; }
        View controls = (View) detail.getParent();
        controls.requestRectangleOnScreen(new android.graphics.Rect(0, 0, controls.getWidth(), controls.getHeight()), true);
        android.graphics.Rect left = new android.graphics.Rect(), right = new android.graphics.Rect();
        boolean visible = detail.getGlobalVisibleRect(left) && remove.getGlobalVisibleRect(right);
        boolean complete = visible && left.width() >= detail.getWidth() - 1 && left.height() >= detail.getHeight() - 1
            && right.width() >= remove.getWidth() - 1 && right.height() >= remove.getHeight() - 1;
        boolean sameRow = Math.abs(left.centerY() - right.centerY()) <= 1;
        boolean separate = left.right <= right.left;
        observed.set("detail=" + left + " delete=" + right + " complete=" + complete + " sameRow=" + sameRow);
        ready.set(detail.isShown() && remove.isShown() && complete && sameRow && separate);
      });
      if (!ready.get()) Thread.sleep(50);
    }
    assertTrue("Upload details and delete must be fully visible, on one row and not overlap: " + observed.get(), ready.get());
    awaitNode("上传记录", null);
  }

  private static void awaitDrawnFrame(ActivityScenario<MainActivity> activity) throws Exception {
    CountDownLatch drawn = new CountDownLatch(1);
    activity.onActivity(a -> {
      assertEquals("records", a.screen);
      View root = a.root;
      android.view.ViewTreeObserver.OnDrawListener[] listener = new android.view.ViewTreeObserver.OnDrawListener[1];
      listener[0] = () -> root.post(() -> {
        if (root.getViewTreeObserver().isAlive()) root.getViewTreeObserver().removeOnDrawListener(listener[0]);
        // Wait until the next frame after this root's actual draw, so a screenshot
        // cannot capture the previous cases screen during the tab transition.
        root.postOnAnimation(drawn::countDown);
      });
      root.getViewTreeObserver().addOnDrawListener(listener[0]);
      root.postInvalidateOnAnimation();
    });
    assertTrue("Records root did not draw", drawn.await(10, TimeUnit.SECONDS));
    awaitNode("上传记录", null);
  }

  private void screenshot(String name) throws Exception {
    Thread.sleep(350); // Wait for AlertDialog/window animations before capturing pixels.
    File output = new File(context.getFilesDir(), "deletion-verification"); output.mkdirs();
    Bitmap image = InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
    assertNotNull(image);
    try (java.io.FileOutputStream stream = new java.io.FileOutputStream(new File(output, name + ".png"))) {
      assertTrue(image.compress(Bitmap.CompressFormat.PNG, 100, stream));
    } finally { image.recycle(); }
  }

  static final class DeleteServer implements AutoCloseable {
    final ServerSocket socket = new ServerSocket(0, 16, java.net.InetAddress.getByName("127.0.0.1"));
    final java.util.concurrent.ExecutorService executor = java.util.concurrent.Executors.newCachedThreadPool();
    final AtomicInteger deletes = new AtomicInteger(), uploads = new AtomicInteger();
    final java.util.Set<Socket> clients = java.util.concurrent.ConcurrentHashMap.newKeySet();
    volatile int status = 200;
    volatile String reply = "deleted";
    volatile boolean drop;
    volatile CountDownLatch hold;

    DeleteServer() throws Exception {
      executor.execute(() -> {
        while (!socket.isClosed()) try {
          Socket client = socket.accept(); clients.add(client); executor.execute(() -> serve(client));
        } catch (Exception stopped) { return; }
      });
    }
    String url() { return "http://127.0.0.1:" + socket.getLocalPort(); }
    void serve(Socket client) {
      try (client) {
        client.setSoTimeout(5000);
        java.io.InputStream input = client.getInputStream();
        ByteArrayOutputStream header = new ByteArrayOutputStream();
        int next; String headers = "";
        while ((next = input.read()) >= 0 && header.size() < 16384) {
          header.write(next);
          if (next == 10) { headers = header.toString(StandardCharsets.UTF_8.name()); if (headers.endsWith("\r\n\r\n")) break; }
        }
        String[] request = headers.substring(0, headers.indexOf("\r\n")).split(" ");
        int length = 0;
        java.util.regex.Matcher size = java.util.regex.Pattern.compile("(?im)^Content-Length: (\\d+)").matcher(headers);
        if (size.find()) length = Integer.parseInt(size.group(1));
        if (length > 4096) return;
        byte[] body = new byte[length];
        int received = 0;
        while (received < length) { int count = input.read(body, received, length - received); if (count < 0) return; received += count; }
        if (!"DELETE".equals(request[0])) { uploads.incrementAndGet(); return; }
        deletes.incrementAndGet();
        if (drop) return;
        CountDownLatch gate = hold;
        if (gate != null && !gate.await(15, TimeUnit.SECONDS)) return;
        String id = request[1].substring(request[1].lastIndexOf('/') + 1);
        JSONObject data = new JSONObject(new String(body, StandardCharsets.UTF_8));
        JSONObject response = status == 200 ? new JSONObject().put("capture_id", reply.equals("wrong_capture") ? "unrelated" : id)
            .put("case_id", reply.equals("wrong_case") ? "unrelated-case" : data.optString("case_id"))
            .put("state", "deleted").put("deleted", !reply.equals("unconfirmed"))
            : new JSONObject().put("detail", "synthetic permission failure");
        byte[] bytes = response.toString().getBytes(StandardCharsets.UTF_8);
        client.getOutputStream().write(("HTTP/1.1 " + status + " Test\r\nContent-Type: application/json\r\nContent-Length: "
            + bytes.length + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.UTF_8));
        client.getOutputStream().write(bytes); client.getOutputStream().flush();
      } catch (Exception ignored) { } finally { clients.remove(client); }
    }
    @Override public void close() throws Exception {
      if (hold != null) hold.countDown(); socket.close();
      for (Socket client : clients) client.close(); executor.shutdownNow();
    }
  }
}
