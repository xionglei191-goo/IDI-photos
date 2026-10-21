package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import androidx.exifinterface.media.ExifInterface;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.rule.GrantPermissionRule;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.security.MessageDigest;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class CameraDeviceTest {
  @Rule
  public GrantPermissionRule permission =
      GrantPermissionRule.grant(android.Manifest.permission.CAMERA);

  @Test
  public void rendersApprovedOrderSmallAreaAndPreservesRawExif() throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    File directory = new File(context.getFilesDir(), "verification");
    assertTrue(directory.isDirectory() || directory.mkdirs());
    File original = new File(directory, "original.jpg"),
        stamped = new File(directory, "stamped.jpg"),
        thumbnail = new File(directory, "thumbnail.jpg");
    Bitmap source = Bitmap.createBitmap(1200, 1600, Bitmap.Config.ARGB_8888);
    Canvas canvas = new Canvas(source);
    canvas.drawColor(Color.rgb(220, 215, 205));
    Paint paint = new Paint();
    paint.setColor(Color.DKGRAY);
    paint.setStrokeWidth(4);
    canvas.drawLine(300, 400, 800, 800, paint);
    try (FileOutputStream stream = new FileOutputStream(original)) {
      source.compress(Bitmap.CompressFormat.JPEG, 95, stream);
    }
    source.recycle();
    ExifInterface exif = new ExifInterface(original);
    exif.setAttribute(
        ExifInterface.TAG_ORIENTATION, String.valueOf(ExifInterface.ORIENTATION_ROTATE_90));
    exif.saveAttributes();
    String rawHash = hash(original);
    JSONObject meta =
        CameraData.metadata(MainActivity.demoCases().get(0), "construction", "卫生间", "卫生间瓷砖修补", "");
    String[] fields = CameraData.watermark(meta);
    assertTrue(fields[1].startsWith("案件号：ZLBH"));
    assertTrue(fields[2].startsWith("小区："));
    assertTrue(fields[3].startsWith("位置："));
    assertTrue(fields[4].startsWith("环节："));
    assertTrue(fields[5].startsWith("内容："));
    WatermarkLayout layout = WatermarkRenderer.render(original, stamped, thumbnail, meta);
    assertEquals(rawHash, hash(original));
    assertTrue(layout.fraction <= .061f);
    assertTrue(layout.fraction > .025f);
    Bitmap output = BitmapFactory.decodeFile(stamped.getPath());
    assertEquals(1600, output.getWidth());
    assertEquals(1200, output.getHeight());
    assertTrue(Color.red(output.getPixel(100, 100)) > 200);
    output.recycle();
    ExifInterface result = new ExifInterface(stamped);
    assertEquals(
        ExifInterface.ORIENTATION_NORMAL, result.getAttributeInt(ExifInterface.TAG_ORIENTATION, 0));
    assertNotEquals(hash(original), hash(stamped));
    assertTrue(thumbnail.length() > 0);
    JSONObject evidence =
        new JSONObject()
            .put("watermark_area_fraction", layout.fraction)
            .put("width", 1600)
            .put("height", 1200)
            .put("original_unchanged", true)
            .put("field_order", new org.json.JSONArray(fields));
    try (FileOutputStream stream =
        new FileOutputStream(new File(directory, "watermark-check.json"))) {
      stream.write(evidence.toString(2).getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }
  }

  @Test
  public void officialBytesSurviveOfflineRecreationAndMockVerification() throws Exception {
    String expectedVerification = InstrumentationRegistry.getArguments().getString("expected_verification", "verified");
    String code = InstrumentationRegistry.getArguments().getString("pair_code", "");
    assumeTrue(
        "Supply a one-use pairing code for the isolated acceptance server", code.length() == 10);
    String server =
        InstrumentationRegistry.getArguments().getString("server", "http://127.0.0.1:8876");
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    JSONObject pair =
        CameraApi.request(server, "/api/camera/pair", "", new JSONObject().put("code", code));
    store.connect(server, pair);
    JSONObject cases =
        CameraApi.request(server, "/api/camera/cases", store.token(store.device()), null);
    assertTrue(cases.getJSONArray("items").length() > 0);
    store.replaceCases(cases.getJSONArray("items"));
    String captureId;
    try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
      InstrumentationRegistry.getInstrumentation().waitForIdleSync();
      shot("01-cases.png");
      // This fixture validates offline persistence/transport. Native preview is
      // exercised separately with inspectLiveNativeCamera and real SDK authorization.
      scenario.onActivity(a -> { a.selected = a.cases().get(0); a.loadDraft(); });
      scenario.onActivity(WatermarkEditor::show);
      Thread.sleep(600);
      shot("03-watermark-editor.png");
      java.util.List<android.view.accessibility.AccessibilityNodeInfo> saveButtons =
          InstrumentationRegistry.getInstrumentation()
              .getUiAutomation()
              .getRootInActiveWindow()
              .findAccessibilityNodeInfosByText("保存并继续拍照");
      assertFalse("The edit sheet must keep its save action visible", saveButtons.isEmpty());
      android.graphics.Rect saveBounds = new android.graphics.Rect();
      saveButtons.get(0).getBoundsInScreen(saveBounds);
      assertTrue(saveButtons.get(0).isVisibleToUser());
      assertTrue(
          saveBounds.top >= 0
              && saveBounds.bottom <= context.getResources().getDisplayMetrics().heightPixels);
      InstrumentationRegistry.getInstrumentation()
          .sendKeyDownUpSync(android.view.KeyEvent.KEYCODE_BACK);
      Thread.sleep(300);
      shell("svc wifi disable");
      shell("svc data disable");
      Thread.sleep(2500);
      // Synthetic JPEG tests persistence/transport only; it is not a vendor SDK capture.
      JSONObject meta =
          CameraData.metadata(store.cases().get(0), "construction", "卫生间", "合成验收照片", "")
              .put("source", "xhey_h5");
      String id = meta.getString("capture_id");
      Bitmap synthetic = Bitmap.createBitmap(800, 1200, Bitmap.Config.ARGB_8888);
      Canvas syntheticCanvas = new Canvas(synthetic);
      syntheticCanvas.drawColor(Color.rgb(220, 218, 210));
      Paint syntheticPaint = new Paint();
      syntheticPaint.setTextSize(24);
      syntheticPaint.setColor(Color.BLACK);
      syntheticCanvas.drawText("SYNTHETIC TEST " + id, 20, 100, syntheticPaint);
      java.io.ByteArrayOutputStream bytes = new java.io.ByteArrayOutputStream();
      synthetic.compress(Bitmap.CompressFormat.JPEG, 95, bytes);
      synthetic.recycle();
      store.create(meta, false);
      OfficialPhotoWriter.save(store, id, bytes.toByteArray());
      store.sdkInfo(id, new JSONObject().put("data", new JSONObject()
          .put("baseInfo", new JSONObject().put("time", "2026:10:20 10:11:12"))
          .put("watermarkContentExtension", new JSONObject().put("antiFakeCode", "SYNTHETIC-NOT-VERIFIED"))).toString());
      assertEquals(hash(store.original(id)), hash(store.stamped(id)));
      String syntheticHash =
          java.util.HexFormat.of()
              .formatHex(MessageDigest.getInstance("SHA-256").digest(bytes.toByteArray()));
      assertEquals(syntheticHash, hash(store.original(id)));
      store.state(id, "queued", "");
      UploadWorker.enqueue(context, id, false);
      CaptureStore.Item item = store.item(id);
      captureId = item.id;
      assertEquals(item.error, "queued", item.state);
      assertTrue(store.original(item.id).isFile());
      assertTrue(store.stamped(item.id).isFile());
      String frozenTime = item.meta.getString("captured_at"),
          frozenCode = item.meta.getString("case_number");
      scenario.onActivity(a -> a.show("records"));
      Thread.sleep(500);
      shot("04-records-offline.png");
      scenario.recreate();
      CaptureStore.Item after = store.item(captureId);
      assertEquals(frozenTime, after.meta.getString("captured_at"));
      assertEquals(frozenCode, after.meta.getString("case_number"));
      assertEquals("queued", after.state);
      shell("svc wifi enable");
      shell("svc data enable");
      UploadWorker.pending(context);
      await(() -> store.item(captureId).state.equals("uploaded"), 60000);
      CaptureStore.Item uploaded = store.item(captureId);
      assertEquals(
          hash(store.original(captureId)),
          new JSONObject(uploaded.receipt).getString("original_hash"));
      assertEquals(
          hash(store.stamped(captureId)),
          new JSONObject(uploaded.receipt).getString("stamped_hash"));
      assertEquals(expectedVerification, new JSONObject(uploaded.receipt).getString("verification_state"));
      if (expectedVerification.equals("verified")) {
        assertEquals("08:00:00 · 官方时间", CameraData.recordedTime(uploaded));
        assertEquals("官方验真通过 · 已归档", RecordsScreen.label(uploaded));
      } else {
        assertFalse(uploaded.verified());
        assertEquals("10:11:12 · SDK 时间（未验真）", CameraData.recordedTime(uploaded));
        assertEquals("已归档 · 未做本地验真", RecordsScreen.label(uploaded));
        assertEquals(0, new JSONObject(uploaded.receipt).getJSONObject("verification").length());
      }
      JSONObject repeated = CameraApi.upload(store, uploaded);
      assertTrue(repeated.getBoolean("duplicate"));
      assertEquals(
          new JSONObject(uploaded.receipt).getString("photo_id"), repeated.getString("photo_id"));
      scenario.onActivity(a -> a.show("records"));
      Thread.sleep(600);
      shot("05-records-uploaded.png");
      try (FileOutputStream stream =
          new FileOutputStream(
              new File(context.getFilesDir(), "verification/capture-check.json"))) {
        stream.write(
            new JSONObject()
                .put("capture_id", captureId)
                .put("metadata", uploaded.meta)
                .put("receipt", new JSONObject(uploaded.receipt))
                .put("retry_duplicate", true)
                .put("verification_provider", expectedVerification.equals("verified") ? "mock_transport_not_live_xhey" : "not_called_local_only")
                .toString(2)
                .getBytes(java.nio.charset.StandardCharsets.UTF_8));
      }
    } finally {
      shell("svc wifi enable");
      shell("svc data enable");
    }
  }

  @Test
  public void officialOriginalIsByteIdenticalIncludingExifAndRecovery() throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    JSONObject meta =
        CameraData.metadata(MainActivity.demoCases().get(0), "construction", "", "合成保真测试", "")
            .put("source", "xhey_sdk");
    String id = meta.getString("capture_id");
    File temp = new File(context.getCacheDir(), "official-exif-test.jpg");
    Bitmap source = Bitmap.createBitmap(200, 300, Bitmap.Config.ARGB_8888);
    try (FileOutputStream out = new FileOutputStream(temp)) {
      source.compress(Bitmap.CompressFormat.JPEG, 90, out);
    }
    source.recycle();
    ExifInterface exif = new ExifInterface(temp);
    exif.setAttribute(
        ExifInterface.TAG_ORIENTATION, String.valueOf(ExifInterface.ORIENTATION_ROTATE_90));
    exif.setAttribute(ExifInterface.TAG_USER_COMMENT, "Test anti-tamper bytes; never rewrite EXIF");
    exif.saveAttributes();
    store.create(meta, true);
    OfficialPhotoWriter.save(store, id, java.nio.file.Files.readAllBytes(temp.toPath()));
    OfficialPhotoWriter.recover(store, id);
    assertEquals(hash(temp), hash(store.original(id)));
    assertEquals(hash(temp), hash(store.stamped(id)));
    assertEquals(
        ExifInterface.ORIENTATION_ROTATE_90,
        new ExifInterface(store.original(id)).getAttributeInt(ExifInterface.TAG_ORIENTATION, 0));
    Bitmap preview = BitmapFactory.decodeFile(store.thumbnail(id).getPath());
    assertEquals(300, preview.getWidth());
    assertEquals(200, preview.getHeight());
    preview.recycle();
    store.state(id, "local", "Synthetic test only");
  }

  private static void await(BooleanSupplier condition, long timeout) throws Exception {
    long end = System.currentTimeMillis() + timeout;
    while (System.currentTimeMillis() < end) {
      if (condition.getAsBoolean()) return;
      Thread.sleep(250);
    }
    fail("Condition not reached in " + timeout + "ms");
  }

  private static void shell(String command) throws Exception {
    try (android.os.ParcelFileDescriptor p =
            InstrumentationRegistry.getInstrumentation()
                .getUiAutomation()
                .executeShellCommand(command);
        FileInputStream in = new FileInputStream(p.getFileDescriptor())) {
      byte[] bytes = new byte[1024];
      while (in.read(bytes) != -1) {}
    }
  }

  private static void shot(String name) throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    File folder = new File(context.getFilesDir(), "verification");
    assertTrue(folder.isDirectory() || folder.mkdirs());
    Bitmap image = InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
    assertNotNull(image);
    try (FileOutputStream stream = new FileOutputStream(new File(folder, name))) {
      image.compress(Bitmap.CompressFormat.PNG, 100, stream);
    }
    image.recycle();
  }

  private static String hash(File file) throws Exception {
    MessageDigest digest = MessageDigest.getInstance("SHA-256");
    try (FileInputStream in = new FileInputStream(file)) {
      byte[] buffer = new byte[65536];
      int count;
      while ((count = in.read(buffer)) != -1) digest.update(buffer, 0, count);
    }
    StringBuilder result = new StringBuilder();
    for (byte b : digest.digest()) result.append(String.format("%02x", b));
    return result.toString();
  }
}
