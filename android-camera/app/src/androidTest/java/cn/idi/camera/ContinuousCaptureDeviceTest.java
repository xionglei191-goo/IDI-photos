package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;
import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.os.SystemClock;
import android.view.KeyEvent;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.rule.GrantPermissionRule;
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry;
import androidx.test.runner.lifecycle.Stage;
import com.xhey.xheycamerasdk.CameraActivity;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class ContinuousCaptureDeviceTest {
  @Rule public GrantPermissionRule permissions = GrantPermissionRule.grant(
      Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION);

  private String synthetic(CaptureStore store, boolean saved) throws Exception {
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "work_checkin", "厨房", "连拍测试内容", "door_checkin")
        .put("source", "xhey_sdk").put("captured_at", "2020-01-01T00:00:00+08:00")
        .put("sdk_photo_time", "2020:01:01 00:00:00").put("sdk_anti_fake_code", "OLD-SYNTHETIC-CODE");
    store.create(meta, true);
    String id = meta.getString("capture_id");
    store.state(id, "awaiting_sdk", "");
    if (saved) {
      Bitmap bitmap = Bitmap.createBitmap(32, 32, Bitmap.Config.ARGB_8888);
      bitmap.eraseColor(android.graphics.Color.LTGRAY);
      ByteArrayOutputStream bytes = new ByteArrayOutputStream();
      assertTrue(bitmap.compress(Bitmap.CompressFormat.JPEG, 95, bytes));
      bitmap.recycle();
      OfficialPhotoWriter.save(store, id, bytes.toByteArray());
      store.state(id, "local", "Synthetic continuous-capture test; never upload");
    }
    return id;
  }

  @Test public void successorKeepsWatermarkAndDestinationButGetsIndependentIdentity() throws Exception {
    CaptureStore store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    String previous = synthetic(store, true);
    String oldMetadata = store.item(previous).meta.toString();
    byte[] original = Files.readAllBytes(store.original(previous).toPath());
    int before = store.items().size();
    String next = store.nextOfficialPhoto(previous, previous);
    assertNotEquals(previous, next);
    assertEquals(next, store.nextOfficialPhoto(previous, previous));
    assertEquals(before + 1, store.items().size());
    CaptureStore.Item child = store.item(next), parent = store.item(previous);
    assertEquals(parent.server, child.server);
    assertEquals(parent.device, child.device);
    for (String field : new String[] {"case_id", "case_number", "project_name", "location", "photo_group", "role", "part", "content", "source"})
      assertEquals(parent.meta.getString(field), child.meta.getString(field));
    assertNotEquals(parent.meta.getString("captured_at"), child.meta.getString("captured_at"));
    assertFalse(child.meta.has("sdk_photo_time"));
    assertFalse(child.meta.has("sdk_anti_fake_code"));
    assertEquals("", child.receipt);
    assertEquals("awaiting_sdk", child.state);
    assertFalse(store.original(next).exists());
    assertThrows(Exception.class, () -> store.nextOfficialPhoto(next, previous));
    store.cancelEmptyOfficialCapture(next);
    assertFalse(store.recordItems().stream().anyMatch(item -> item.id.equals(next)));
    assertTrue(store.recordItems().stream().anyMatch(item -> item.id.equals(previous)));
    assertEquals(oldMetadata, store.item(previous).meta.toString());
    assertArrayEquals(original, Files.readAllBytes(store.original(previous).toPath()));
    store.prefs.edit().remove("native_series_" + previous).commit();
  }

  @Test public void savedPhotoRecoversIntoNextCaptureAndBackEndsSession() throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    String previous = synthetic(store, true);
    try (ActivityScenario<NativeCameraActivity> scenario = ActivityScenario.launch(
        new Intent(context, NativeCameraActivity.class).putExtra("capture_id", previous))) {
      await(() -> !store.prefs.getString("native_series_" + previous, previous).equals(previous), 15000);
      String next = store.prefs.getString("native_series_" + previous, "");
      int count = store.items().size();
      scenario.onActivity(activity -> {
        assertEquals(next, activity.captureId);
        assertFalse(activity.ended);
        assertFalse(activity.showEditor);
      });
      scenario.recreate();
      scenario.onActivity(activity -> {
        assertEquals(next, activity.captureId);
        assertFalse(activity.showEditor);
        activity.getOnBackPressedDispatcher().onBackPressed();
      });
      SystemClock.sleep(700);
      assertEquals(count, store.items().size());
      assertEquals("cancelled", store.item(next).state);
      assertFalse(store.prefs.contains("native_series_" + previous));
      assertFalse(store.prefs.contains("pending_official_capture"));
      assertTrue(store.original(previous).isFile());
    }
  }

  @Test public void missingPhotoDoesNotAdvanceOrFinish() throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    String id = synthetic(store, false);
    int count = store.items().size();
    try (ActivityScenario<NativeCameraActivity> scenario = ActivityScenario.launch(
        new Intent(context, NativeCameraActivity.class).putExtra("capture_id", id))) {
      scenario.onActivity(activity -> {
        activity.accept(new CameraActivity.Result(), null);
        assertEquals(id, activity.captureId);
        assertFalse(activity.ended);
        assertFalse(activity.saving);
      });
      assertEquals(count, store.items().size());
      assertFalse(store.prefs.contains("native_series_" + id));
    }
    store.cancelEmptyOfficialCapture(id);
  }

  /** Explicit live SDK test: three manual shots, then automatic back/queue checks. */
  @Test public void liveThreePhotosStayInCameraUntilBack() throws Exception {
    assumeTrue("1".equals(InstrumentationRegistry.getArguments().getString("continuous_live", "")));
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    String server = InstrumentationRegistry.getArguments().getString("server", "http://127.0.0.1:8879");
    String code = InstrumentationRegistry.getArguments().getString("pair_code", "");
    JSONObject pair = CameraApi.request(server, "/api/camera/pair", "", new JSONObject().put("code", code));
    store.connect(server, pair);
    store.replaceCases(CameraApi.request(server, "/api/camera/cases", store.token(store.device()), null).getJSONArray("items"));
    JSONObject meta = CameraData.metadata(store.cases().get(0), "construction", "测试部位", "连续拍摄验收，非业务照片", "")
        .put("source", "xhey_sdk");
    store.create(meta, false);
    String root = meta.getString("capture_id");
    store.state(root, "awaiting_sdk", "");
    File output = new File(context.getFilesDir(), "continuous-verification"); output.mkdirs();
    ArrayList<String> saved = new ArrayList<>();
    try (ActivityScenario<NativeCameraActivity> scenario = ActivityScenario.launch(
        new Intent(context, NativeCameraActivity.class).putExtra("capture_id", root).putExtra("emulator_test", true))) {
      String current = root;
      long deadline = SystemClock.uptimeMillis() + 240000;
      while (saved.size() < 3 && SystemClock.uptimeMillis() < deadline) {
        String next = store.prefs.getString("next_official_photo_" + current, "");
        if (!next.isEmpty() && store.original(current).isFile()) {
          saved.add(current); current = next;
        }
        Files.write(new File(output, "progress.json").toPath(), new JSONObject()
            .put("saved", new JSONArray(saved)).put("current", current).put("state", store.item(current).state)
            .toString(2).getBytes(StandardCharsets.UTF_8));
        SystemClock.sleep(500);
      }
      assertEquals("Three confirmed photos must open a fourth capture automatically", 3, saved.size());
      String empty = current;
      await(() -> resumedCamera(empty), 20000);
      InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
        for (Activity activity : ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED))
          if (activity instanceof EditableCameraActivity camera) {
            assertEquals(empty, camera.captureId);
            assertNull("Subsequent shots must not reopen watermark settings", camera.editor);
            assertFalse(camera.getIntent().getBooleanExtra(EditableCameraActivity.SHOW_EDITOR, true));
          }
      });
      InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK);
      await(() -> "cancelled".equals(store.item(empty).state), 15000);
      SystemClock.sleep(1500);
      assertFalse(resumedCamera(empty));
      assertFalse(store.prefs.contains("native_series_" + root));
      JSONArray photos = new JSONArray();
      for (String id : saved) {
        await(() -> "uploaded".equals(store.item(id).state), 30000);
        CaptureStore.Item item = store.item(id);
        assertArrayEquals(Files.readAllBytes(store.original(id).toPath()), Files.readAllBytes(store.stamped(id).toPath()));
        assertEquals(meta.getString("case_id"), item.meta.getString("case_id"));
        assertFalse(item.verified());
        photos.put(new JSONObject().put("capture_id", id).put("state", item.state)
            .put("sha256", OfficialPhotoWriter.sha256(store.original(id))).put("receipt", new JSONObject(item.receipt)));
      }
      Files.write(new File(output, "result.json").toPath(), new JSONObject().put("photos", photos)
          .put("empty_cancelled", empty).put("back_ended_session", true)
          .put("scope", "emulator_synthetic_cases; SDK live; local verification disabled")
          .toString(2).getBytes(StandardCharsets.UTF_8));
    }
  }

  private static boolean resumedCamera(String id) {
    boolean[] found = {false};
    InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
      for (Activity activity : ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED))
        if (activity instanceof EditableCameraActivity camera && id.equals(camera.captureId)) found[0] = true;
    });
    return found[0];
  }

  private static void await(java.util.function.BooleanSupplier condition, long timeout) {
    long deadline = SystemClock.uptimeMillis() + timeout;
    while (!condition.getAsBoolean() && SystemClock.uptimeMillis() < deadline) SystemClock.sleep(100);
    assertTrue("Timed out waiting for capture state", condition.getAsBoolean());
  }
}
