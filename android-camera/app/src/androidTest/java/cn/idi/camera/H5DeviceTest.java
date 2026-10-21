package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;
import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Rect;
import android.net.Uri;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.MotionEvent;
import android.os.SystemClock;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.rule.GrantPermissionRule;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class H5DeviceTest {
  @Rule public GrantPermissionRule permissions = GrantPermissionRule.grant(
      Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION);

  @Test public void permissionOriginIsExact() {
    assertTrue(H5CameraActivity.vendorOrigin(Uri.parse("https://sdk-h5.xhey.top")));
    assertTrue(H5CameraActivity.vendorOrigin(Uri.parse("https://sdk-h5.xhey.top:443")));
    assertFalse(H5CameraActivity.vendorOrigin(Uri.parse("https://sdk-h5.xhey.top.example.com")));
    assertFalse(H5CameraActivity.vendorOrigin(Uri.parse("http://sdk-h5.xhey.top")));
    assertFalse(H5CameraActivity.vendorOrigin(Uri.parse("https://sdk-h5.xhey.top:8443")));
    assertFalse(H5CameraActivity.vendorOrigin(Uri.parse("https://user@sdk-h5.xhey.top")));
  }

  /** Explicit live smoke only. Test data and photos are synthetic and use trial quota. */
  @Test public void inspectLiveOfficialH5() throws Exception {
    assumeTrue("1".equals(InstrumentationRegistry.getArguments().getString("h5_live", "")));
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    String server = InstrumentationRegistry.getArguments().getString("server", "http://127.0.0.1:8878");
    String code = InstrumentationRegistry.getArguments().getString("pair_code", "");
    assertEquals(10, code.length());
    JSONObject pair = CameraApi.request(server, "/api/camera/pair", "", new JSONObject().put("code", code));
    store.connect(server, pair);
    JSONObject cases = CameraApi.request(server, "/api/camera/cases", store.token(store.device()), null);
    store.replaceCases(cases.getJSONArray("items"));
    JSONObject meta = CameraData.metadata(store.cases().get(0), "construction", "测试部位", "H5 SDK 联调测试，非业务照片", "").put("source", "xhey_h5");
    store.create(meta, false);
    String id = meta.getString("capture_id");
    store.state(id, "awaiting_sdk", "");
    Intent launch = new Intent(context, H5CameraActivity.class).putExtra("capture_id", id);
    File output = new File(context.getFilesDir(), "h5-verification");
    output.mkdirs();
    try (ActivityScenario<H5CameraActivity> scenario = ActivityScenario.launch(launch)) {
      Thread.sleep(35000);
      AtomicReference<String> nativeStatus = new AtomicReference<>("");
      scenario.onActivity(activity -> nativeStatus.set(activity.status.getText().toString()));
      Bitmap screenshot = InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
      assertNotNull(screenshot);
      try (FileOutputStream stream = new FileOutputStream(new File(output, "live-sdk.png"))) {
        screenshot.compress(Bitmap.CompressFormat.PNG, 100, stream);
      }
      int width = screenshot.getWidth(), height = screenshot.getHeight();
      screenshot.recycle();
      if ("1".equals(InstrumentationRegistry.getArguments().getString("take_photo", ""))) {
        // The official Flutter canvas has no accessible shutter node. This coordinate
        // is from the inspected 1080x2340 isolated emulator screenshot, not a device default.
        assertEquals(1080, width); assertEquals(2340, height);
        tap(540, 2015);
        Thread.sleep(8000);
        Bitmap after = InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
        try (FileOutputStream stream = new FileOutputStream(new File(output, "live-after-shutter.png"))) {
          after.compress(Bitmap.CompressFormat.PNG, 100, stream);
        }
        after.recycle();
        // Same inspected button position now confirms the official preview.
        tap(540, 2015);
        long deadline = SystemClock.uptimeMillis() + 20000;
        while (!store.original(id).isFile() && SystemClock.uptimeMillis() < deadline) Thread.sleep(250);
        assertTrue("Official SDK did not return a saved original: " + store.item(id).state, store.original(id).isFile());
        Files.copy(store.original(id).toPath(), new File(output, "live-original.jpg").toPath(),
            java.nio.file.StandardCopyOption.REPLACE_EXISTING);
      }
      List<String> text = new ArrayList<>();
      collect(InstrumentationRegistry.getInstrumentation().getUiAutomation().getRootInActiveWindow(), text);
      JSONObject result = new JSONObject().put("capture_id", id).put("native_status", nativeStatus.get())
          .put("screen_text", new org.json.JSONArray(text)).put("state", store.item(id).state)
          .put("scope", "official_sdk_live_load_with_synthetic_case; no_real_device_acceptance");
      if (store.original(id).isFile()) result.put("original_sha256", OfficialPhotoWriter.sha256(store.original(id)))
          .put("original_bytes", store.original(id).length());
      Files.write(new File(output, "live-sdk.json").toPath(), result.toString(2).getBytes(StandardCharsets.UTF_8));
      assertFalse("SDK load failed: " + nativeStatus.get(), store.item(id).state.equals("capture_failed"));
    }
  }

  private static void tap(float x, float y) {
    long when = SystemClock.uptimeMillis();
    MotionEvent down = MotionEvent.obtain(when, when, MotionEvent.ACTION_DOWN, x, y, 0);
    MotionEvent up = MotionEvent.obtain(when, when + 100, MotionEvent.ACTION_UP, x, y, 0);
    InstrumentationRegistry.getInstrumentation().getUiAutomation().injectInputEvent(down, true);
    InstrumentationRegistry.getInstrumentation().getUiAutomation().injectInputEvent(up, true);
    down.recycle(); up.recycle();
  }

  private static void collect(AccessibilityNodeInfo node, List<String> lines) {
    if (node == null || lines.size() > 200) return;
    CharSequence text = node.getText(), description = node.getContentDescription();
    if (text != null && text.length() > 0 || description != null && description.length() > 0) {
      Rect bounds = new Rect(); node.getBoundsInScreen(bounds);
      lines.add(node.getClassName() + " | " + (text == null ? "" : text) + " | " + (description == null ? "" : description) + " | " + bounds);
    }
    for (int i = 0; i < node.getChildCount(); i++) collect(node.getChild(i), lines);
  }
}
