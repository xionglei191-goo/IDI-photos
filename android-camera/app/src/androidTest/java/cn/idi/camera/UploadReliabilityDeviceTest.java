package cn.idi.camera;

import static org.junit.Assert.*;
import android.Manifest;
import android.app.NotificationManager;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.os.SystemClock;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.rule.GrantPermissionRule;
import androidx.work.WorkManager;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class UploadReliabilityDeviceTest {
  @Rule public GrantPermissionRule notifications = GrantPermissionRule.grant(Manifest.permission.POST_NOTIFICATIONS);
  final Context context = ApplicationProvider.getApplicationContext();
  final CaptureStore store = CaptureStore.get(context);

  private CaptureStore.Item photo(FaultServer server, int size) throws Exception {
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "厨房", "上传故障测试", "")
        .put("source", "xhey_sdk");
    store.create(meta, true);
    String id = meta.getString("capture_id");
    byte[] bytes = new byte[size];
    new java.util.Random(84).nextBytes(bytes);
    Files.write(store.original(id).toPath(), bytes);
    ContentValues values = new ContentValues();
    values.put("server", server.url()); values.put("device", "upload-test-device"); values.put("state", "failed");
    store.getWritableDatabase().update("captures", values, "id=?", new String[] {id});
    store.prefs.edit().putString("token_upload-test-device", "synthetic-local-token").commit();
    server.receipts.put(id, new JSONObject().put("capture_id", id).put("media_type", "image")
        .put("original_hash", OfficialPhotoWriter.sha256(store.original(id)))
        .put("state", "uploaded").put("verification_state", "not_requested").put("photo_id", "test-" + id));
    return store.item(id);
  }

  private JSONObject attempt(CaptureStore.Item item) throws Exception {
    String owner = java.util.UUID.randomUUID().toString();
    assertTrue(store.claimUpload(item.id, owner));
    UploadStatus status = new UploadStatus(store, store.item(item.id), owner);
    try (CameraApi.Transfer transfer = new CameraApi.Transfer(false)) {
      JSONObject receipt = UploadEngine.transfer(store, item, transfer, status);
      status.finish("uploaded", "", "", receipt, 0);
      return receipt;
    } catch (Exception error) {
      status.finish(UploadStatus.retryable(error) ? "retry" : "failed", UploadStatus.code(error),
          UploadStatus.explain(error), null, 15000);
      throw error;
    }
  }

  @Test public void lostUploadResponseIsRecoveredWithoutSendingImageTwice() throws Exception {
    try (FaultServer server = new FaultServer()) {
      server.dropResponse = true;
      CaptureStore.Item item = photo(server, 600000);
      String metadata = item.meta.toString(), hash = OfficialPhotoWriter.sha256(store.original(item.id));
      assertEquals(item.id, attempt(item).getString("capture_id"));
      assertEquals(1, server.posts.get());
      assertEquals(2, server.gets.get());
      assertEquals("uploaded", store.item(item.id).state);
      assertEquals(100, UploadStatus.percent(store.item(item.id).upload));
      assertEquals(hash, OfficialPhotoWriter.sha256(store.original(item.id)));
      assertEquals(metadata, store.item(item.id).meta.toString());
      assertEquals(600000, server.imageBytes.get(item.id).length);
      assertArrayEquals(Files.readAllBytes(store.original(item.id).toPath()), server.imageBytes.get(item.id));
    }
  }

  @Test public void existingReceiptAndDeletedRecordNeverReupload() throws Exception {
    try (FaultServer server = new FaultServer()) {
      CaptureStore.Item existing = photo(server, 128);
      server.received.put(existing.id, true);
      assertEquals(existing.id, attempt(existing).getString("capture_id"));
      assertEquals(0, server.posts.get());
      CaptureStore.Item deleted = photo(server, 128);
      server.getStatus = 410;
      CameraApi.Failure failure = assertThrows(CameraApi.Failure.class, () -> attempt(deleted));
      assertEquals(410, failure.status);
      assertEquals("failed", store.item(deleted.id).state);
      assertEquals("HTTP_410", store.item(deleted.id).upload.optString("error_code"));
      assertEquals(0, server.posts.get());
    }
  }

  @Test public void actualReadTimeoutFindsServerReceiptWithoutSecondPost() throws Exception {
    try (FaultServer server = new FaultServer()) {
      server.hold = new CountDownLatch(1);
      server.holdSeconds = 75;
      CaptureStore.Item item = photo(server, 500000);
      long started = SystemClock.elapsedRealtime();
      attempt(item);
      long elapsed = SystemClock.elapsedRealtime() - started;
      assertTrue("Exercise the real 60-second read timeout", elapsed >= 55000 && elapsed < 74000);
      assertEquals(1, server.posts.get());
      assertEquals(2, server.gets.get());
      assertEquals("uploaded", store.item(item.id).state);
      assertEquals("NETWORK_TIMEOUT", store.item(item.id).upload.optString("last_error_code"));
      JSONObject requests = store.item(item.id).upload.getJSONObject("requests");
      assertTrue(requests.getJSONObject("upload").getLong("wait_ms") >= 55000);
      assertEquals(200, requests.getJSONObject("receipt_reconcile").getInt("http_status"));
    }
  }

  @Test public void unavailableLookupRetriesPastFiveAttemptsWithoutBlindUpload() throws Exception {
    try (FaultServer server = new FaultServer()) {
      CaptureStore.Item item = photo(server, 128);
      server.getStatus = 503;
      for (int i = 0; i < 7; i++) {
        CaptureStore.Item current = store.item(item.id);
        assertThrows(CameraApi.Failure.class, () -> attempt(current));
        assertEquals("retry", store.item(item.id).state);
      }
      assertEquals(7, store.item(item.id).upload.optInt("attempt"));
      assertEquals(0, server.posts.get());
      server.getStatus = 0;
      attempt(store.item(item.id));
      assertEquals(1, server.posts.get());
      assertEquals("uploaded", store.item(item.id).state);
      assertEquals("HTTP_503", store.item(item.id).upload.optString("last_error_code"));
    }
  }

  @Test public void wrongHashAndPermissionErrorsStopAutomaticRetry() throws Exception {
    try (FaultServer server = new FaultServer()) {
      CaptureStore.Item wrong = photo(server, 128);
      server.received.put(wrong.id, true);
      server.receipts.get(wrong.id).put("original_hash", "wrong");
      assertThrows(UploadEngine.ReceiptError.class, () -> attempt(wrong));
      assertEquals("failed", store.item(wrong.id).state);
      for (int status : new int[] {401, 403, 409, 413}) {
        CaptureStore.Item item = photo(server, 128);
        server.getStatus = status;
        assertThrows(CameraApi.Failure.class, () -> attempt(item));
        assertEquals("failed", store.item(item.id).state);
        assertEquals("HTTP_" + status, store.item(item.id).upload.optString("error_code"));
      }
      assertEquals(0, server.posts.get());
    }
  }

  @Test public void migrationPreservesOldRecordAndCancelledAttemptCannotOverwriteNewOwner() throws Exception {
    try (SQLiteDatabase db = SQLiteDatabase.create(null)) {
      db.execSQL("CREATE TABLE captures(id TEXT PRIMARY KEY,metadata TEXT,receipt TEXT,state TEXT)");
      db.execSQL("INSERT INTO captures VALUES ('old','untouched','original-receipt','queued')");
      store.onUpgrade(db, 1, 2);
      try (Cursor c = db.rawQuery("SELECT metadata,receipt,upload_info,upload_owner FROM captures WHERE id='old'", null)) {
        assertTrue(c.moveToFirst()); assertEquals("untouched", c.getString(0));
        assertEquals("original-receipt", c.getString(1)); assertEquals("{}", c.getString(2)); assertEquals("", c.getString(3));
      }
    }
    try (FaultServer server = new FaultServer()) {
      CaptureStore.Item item = photo(server, 128);
      assertTrue(store.claimUpload(item.id, "old"));
      assertTrue(store.claimUpload(item.id, "new"));
      assertTrue(store.uploadUpdate(item.id, "new", new JSONObject(), "uploaded", "", server.receipts.get(item.id)));
      assertFalse(store.uploadUpdate(item.id, "old", new JSONObject(), "retry", "late failure", null));
      assertEquals("uploaded", store.item(item.id).state);
      assertFalse(UploadStatus.diagnostics(store.item(item.id)).contains("synthetic-local-token"));
    }
  }

  @Test public void workManagerLimitsTwoTransfersAndShowsForegroundProgress() throws Exception {
    List<String> ids = new ArrayList<>();
    try (FaultServer server = new FaultServer(); ActivityScenario<MainActivity> activity = ActivityScenario.launch(MainActivity.class)) {
      server.hold = new CountDownLatch(1);
      for (int i = 0; i < 3; i++) {
        CaptureStore.Item item = photo(server, 400000);
        ids.add(item.id);
        store.state(item.id, "queued", "");
        UploadWorker.enqueue(context, item.id, true);
      }
      await(() -> server.posts.get() >= 2, 30000);
      SystemClock.sleep(1400);
      assertEquals(2, server.posts.get());
      assertEquals(2, server.peak.get());
      assertEquals(2, ids.stream().filter(id -> store.item(id).state.equals("uploading")).count());
      assertTrue(ids.stream().anyMatch(id -> store.item(id).upload.optBoolean("foreground")));
      assertTrue(context.getSystemService(NotificationManager.class).getActiveNotifications().length > 0);
      for (String id : ids) if (store.item(id).state.equals("uploading")) {
        assertEquals("awaiting_receipt", store.item(id).upload.optString("stage"));
        assertEquals(100, UploadStatus.percent(store.item(id).upload));
        assertTrue(RecordsScreen.label(store.item(id)).contains("等待服务器回执"));
        assertFalse(RecordsScreen.label(store.item(id)).contains("100%"));
      }
      activity.onActivity(a -> a.show("records"));
      activity.recreate();
      SystemClock.sleep(900);
      screenshot("progress.png");
      activity.onActivity(a -> a.recordsScreen.diagnostics(ids.get(0)));
      SystemClock.sleep(400);
      screenshot("diagnostics.png");
      server.hold.countDown();
      await(() -> ids.stream().allMatch(id -> store.item(id).state.equals("uploaded")), 30000);
      assertEquals(3, server.posts.get());
      assertEquals(2, server.peak.get());
    } finally {
      for (String id : ids) WorkManager.getInstance(context).cancelUniqueWork("capture-" + id).getResult().get();
    }
  }

  @Test public void networkRecoveryRestartsWaitingWorkAndKeepsArchiveIdentity() throws Exception {
    try (FaultServer server = new FaultServer(); ActivityScenario<MainActivity> activity = ActivityScenario.launch(MainActivity.class)) {
      CaptureStore.Item item = photo(server, 200000);
      server.getStatus = 503;
      store.state(item.id, "queued", "");
      UploadWorker.enqueue(context, item.id, true);
      await(() -> store.item(item.id).state.equals("retry") && !UploadWorker.active(item.id), 30000);
      assertEquals(0, server.posts.get());
      server.getStatus = 0;
      UploadWorker.networkRecovered(context);
      await(() -> store.item(item.id).state.equals("uploaded"), 12000);
      assertEquals(1, server.posts.get());
      assertTrue(store.item(item.id).upload.optInt("attempt") >= 2);
    }
  }

  interface Condition { boolean ready() throws Exception; }
  @Test public void delayedResponseHasSeparateClientAndServerTimings() throws Exception {
    try (FaultServer server = new FaultServer()) {
      server.delayMillis = 1300;
      server.timingHeader = "idi_receive;dur=10.2, idi_store;dur=1200, idi_total;dur=1250, idi_invalid;dur=999";
      CaptureStore.Item item = photo(server, 500000);
      attempt(item);
      JSONObject requests = store.item(item.id).upload.getJSONObject("requests");
      assertEquals(404, requests.getJSONObject("receipt_check").getInt("http_status"));
      JSONObject post = requests.getJSONObject("upload");
      assertTrue(post.getLong("wait_ms") >= 1200);
      assertTrue(post.getLong("write_ms") < post.getLong("wait_ms"));
      assertEquals(1200, post.getJSONObject("server_ms").getInt("store"));
      assertFalse(post.getJSONObject("server_ms").has("invalid"));
      assertEquals(post.getString("trace"), post.getString("server_trace"));
      assertTrue(UploadStatus.diagnostics(store.item(item.id)).contains("存储与缩略图"));
      assertFalse(UploadStatus.diagnostics(store.item(item.id)).contains("synthetic-local-token"));
      assertEquals(1, server.posts.get());
      try (ActivityScenario<MainActivity> activity = ActivityScenario.launch(MainActivity.class)) {
        activity.onActivity(a -> { a.show("records"); a.recordsScreen.diagnostics(item.id); });
        androidx.test.platform.app.InstrumentationRegistry.getInstrumentation().waitForIdleSync();
        SystemClock.sleep(1800);
        screenshot("timing-diagnostics.png");
      }
    }
  }
  static void screenshot(String name) throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    java.io.File folder = new java.io.File(context.getFilesDir(), "upload-verification");
    assertTrue(folder.isDirectory() || folder.mkdirs());
    android.graphics.Bitmap image = androidx.test.platform.app.InstrumentationRegistry.getInstrumentation()
        .getUiAutomation().takeScreenshot();
    assertNotNull(image);
    try (java.io.FileOutputStream out = new java.io.FileOutputStream(new java.io.File(folder, name))) {
      image.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out);
    }
    image.recycle();
  }
  static void await(Condition condition, long timeout) throws Exception {
    long end = SystemClock.elapsedRealtime() + timeout;
    while (!condition.ready() && SystemClock.elapsedRealtime() < end) SystemClock.sleep(100);
    assertTrue("Timed out", condition.ready());
  }

  static final class FaultServer implements AutoCloseable {
    final ServerSocket socket = new ServerSocket(0);
    final java.util.concurrent.ExecutorService executor = Executors.newCachedThreadPool();
    final Map<String, JSONObject> receipts = new ConcurrentHashMap<>();
    final Map<String, Boolean> received = new ConcurrentHashMap<>();
    final Map<String, byte[]> imageBytes = new ConcurrentHashMap<>();
    final AtomicInteger gets = new AtomicInteger(), posts = new AtomicInteger(), concurrent = new AtomicInteger(), peak = new AtomicInteger();
    volatile int getStatus;
    volatile boolean dropResponse;
    volatile CountDownLatch hold;
    volatile int holdSeconds = 30;
    volatile int delayMillis;
    volatile String timingHeader = "";

    FaultServer() throws Exception {
      executor.execute(() -> {
        while (!socket.isClosed()) try {
          Socket client = socket.accept(); executor.execute(() -> serve(client));
        } catch (Exception ignored) { }
      });
    }
    String url() { return "http://127.0.0.1:" + socket.getLocalPort(); }

    private void serve(Socket client) {
      boolean post = false;
      try (client) {
        client.setSoTimeout(15000);
        InputStream input = client.getInputStream();
        ByteArrayOutputStream header = new ByteArrayOutputStream();
        int c; String headers;
        while ((c = input.read()) >= 0) {
          header.write(c);
          if (header.size() > 65536) throw new Exception("Headers too long");
          if (c == '\n' && header.toString("UTF-8").endsWith("\r\n\r\n")) break;
        }
        headers = header.toString("UTF-8");
        String path = headers.split(" ")[1];
        int status = 200; JSONObject result;
        if (headers.startsWith("GET ")) {
          gets.incrementAndGet();
          String id = path.substring(path.lastIndexOf('/') + 1);
          if (getStatus != 0) status = getStatus;
          else if (!received.containsKey(id)) status = 404;
          result = status == 200 ? receipts.get(id) : new JSONObject().put("detail", "synthetic HTTP " + status);
        } else {
          post = true; posts.incrementAndGet();
          int active = concurrent.incrementAndGet(); peak.accumulateAndGet(active, Math::max);
          Matcher length = Pattern.compile("(?im)^Content-Length: (\\d+)").matcher(headers);
          if (!length.find()) throw new Exception("Expected exact content length");
          byte[] body = input.readNBytes(Integer.parseInt(length.group(1)));
          Matcher capture = Pattern.compile("\"capture_id\":\"([^\"]+)\"").matcher(new String(body, StandardCharsets.ISO_8859_1));
          if (!capture.find()) throw new Exception("No capture ID");
          String id = capture.group(1);
          String raw = new String(body, StandardCharsets.ISO_8859_1);
          int imageStart = raw.indexOf("Content-Type: image/jpeg\r\n\r\n") + "Content-Type: image/jpeg\r\n\r\n".length();
          int imageEnd = raw.lastIndexOf("\r\n--IDICamera");
          imageBytes.put(id, java.util.Arrays.copyOfRange(body, imageStart, imageEnd));
          received.put(id, true); result = receipts.get(id);
          if (hold != null) hold.await(holdSeconds, TimeUnit.SECONDS);
          if (delayMillis > 0) SystemClock.sleep(delayMillis);
          if (dropResponse) { dropResponse = false; return; }
        }
        byte[] body = result.toString().getBytes(StandardCharsets.UTF_8);
        Matcher trace = Pattern.compile("(?im)^X-IDI-Camera-Trace: ([a-f0-9]{32})").matcher(headers);
        String timing = timingHeader.isEmpty() ? "" : "Server-Timing: " + (post ? timingHeader : "idi_total;dur=4.5") + "\r\n";
        if (!timing.isEmpty()) timing += "Server-Timing: cfCacheStatus;desc=DYNAMIC\r\n";
        if (trace.find()) timing += "X-IDI-Camera-Trace: " + trace.group(1) + "\r\n";
        client.getOutputStream().write(("HTTP/1.1 " + status + " Test\r\nContent-Type: application/json\r\nContent-Length: "
            + body.length + "\r\n" + timing + "Connection: close\r\n\r\n").getBytes(StandardCharsets.UTF_8));
        client.getOutputStream().write(body); client.getOutputStream().flush();
      } catch (Exception ignored) { } finally { if (post) concurrent.decrementAndGet(); }
    }
    public void close() throws Exception { if (hold != null) hold.countDown(); socket.close(); executor.shutdownNow(); }
  }
}
