package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;
import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.rule.GrantPermissionRule;
import com.xhey.xheycamerasdk.recorder.VideoInfo;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class VideoDeviceTest {
  @Rule public GrantPermissionRule permissions = GrantPermissionRule.grant(
      Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION,
      Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.RECORD_AUDIO);

  @Test public void mediaSwitchKeepsCaptureIdentityAndCannotChangeAfterRecordingStarts() throws Exception {
    CaptureStore store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0),
        "construction", "测试部位", "录像模式测试", "").put("source", "xhey_sdk");
    store.create(meta, true);
    String id = meta.getString("capture_id");
    store.state(id, "awaiting_sdk", "");
    store.updateMediaType(id, true);
    assertTrue(store.item(id).video());
    assertEquals("original.mp4", store.original(id).getName());
    for (String field : new String[] {"capture_id", "case_id", "case_number", "location", "captured_at", "content"})
      assertEquals(meta.get(field), store.item(id).meta.get(field));
    store.updateMediaType(id, false);
    assertFalse(store.item(id).video());
    assertEquals("original.jpg", store.original(id).getName());
    store.updateMediaType(id, true);
    File partial = new File(store.directory(id), "sdk-partial.mp4");
    byte[] evidence = {1, 2, 3, 4};
    Files.write(partial.toPath(), evidence);
    assertThrows(Exception.class, () -> store.updateMediaType(id, false));
    assertThrows(Exception.class, () -> store.updateWatermark(id,
        new WatermarkEditor.Draft("completion", "客厅", "不应写入", "", false)));
    store.cancelEmptyOfficialCapture(id);
    assertTrue(store.recordItems().stream().anyMatch(item -> item.id.equals(id)));
    assertArrayEquals(evidence, Files.readAllBytes(partial.toPath()));
    store.state(id, "local", "Synthetic partial-video guard; never upload");
  }

  @Test public void completedSdkFileSurvivesActivityRecreationAndRetainsExactBytes() throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0),
        "construction", "测试部位", "录像中断恢复测试", "").put("source", "xhey_sdk").put("media_type", "video");
    store.create(meta, true);
    String id = meta.getString("capture_id");
    File sdk = new File(store.directory(id), "sdk-interrupted.mp4");
    try (java.io.InputStream input = InstrumentationRegistry.getInstrumentation().getContext()
        .getAssets().open("sdk-video-sample.mp4")) {
      Files.copy(input, sdk.toPath());
    }
    String originalHash = OfficialPhotoWriter.sha256(sdk);
    store.state(id, "awaiting_sdk", "");
    try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
      long deadline = SystemClock.uptimeMillis() + 15000;
      while (!store.item(id).state.equals("local") && SystemClock.uptimeMillis() < deadline) Thread.sleep(100);
      assertEquals("local", store.item(id).state);
      assertTrue(store.thumbnail(id).isFile());
      assertEquals(originalHash, OfficialPhotoWriter.sha256(store.original(id)));
      assertFalse(store.stamped(id).exists());
      assertTrue(store.thumbnail(id).delete());
      store.state(id, "rendering", "");
      scenario.recreate();
      deadline = SystemClock.uptimeMillis() + 15000;
      while (!store.item(id).state.equals("local") && SystemClock.uptimeMillis() < deadline) Thread.sleep(100);
      assertEquals("local", store.item(id).state);
      assertTrue(store.thumbnail(id).length() > 0);
      assertEquals(originalHash, OfficialPhotoWriter.sha256(store.original(id)));
      assertEquals(meta.getString("case_id"), store.item(id).meta.getString("case_id"));
    }
  }

  /** Explicit live SDK test; all taps are driven from the inspected emulator screen. */
  @Test public void liveVideoRetainsBytesAndArchivesOnce() throws Exception {
    assumeTrue("1".equals(InstrumentationRegistry.getArguments().getString("video_live", "")));
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    String server = InstrumentationRegistry.getArguments().getString("server", "http://127.0.0.1:8879");
    String code = InstrumentationRegistry.getArguments().getString("pair_code", "");
    assertEquals(10, code.length());
    JSONObject pair = CameraApi.request(server, "/api/camera/pair", "", new JSONObject().put("code", code));
    store.connect(server, pair);
    store.replaceCases(CameraApi.request(server, "/api/camera/cases", store.token(store.device()), null).getJSONArray("items"));
    JSONObject meta = CameraData.metadata(store.cases().get(0), "construction", "测试部位",
        "VIDEO-050 / SDK联调，非业务资料", "").put("source", "xhey_sdk");
    store.create(meta, false);
    String id = meta.getString("capture_id");
    store.state(id, "awaiting_sdk", "");
    File output = new File(context.getFilesDir(), "video-verification");
    output.mkdirs();
    Intent launch = new Intent(context, NativeCameraActivity.class).putExtra("capture_id", id).putExtra("emulator_test", true);
    try (ActivityScenario<NativeCameraActivity> scenario = ActivityScenario.launch(launch)) {
      Files.write(new File(output, "capture-id.txt").toPath(), id.getBytes(StandardCharsets.UTF_8));
      long deadline = SystemClock.uptimeMillis() + 300000;
      while ((!store.item(id).video() || !store.original(id).isFile())
          && SystemClock.uptimeMillis() < deadline) Thread.sleep(500);
      assertTrue("Must capture an actual SDK video", store.item(id).video() && store.original(id).isFile());
      Thread.sleep(2500);
      String hash = OfficialPhotoWriter.sha256(store.original(id));
      OfficialVideoWriter.recover(store, id);
      assertEquals(hash, OfficialPhotoWriter.sha256(store.original(id)));
      assertTrue(store.thumbnail(id).length() > 0);
      Files.copy(store.original(id).toPath(), new File(output, "live-original.mp4").toPath(), StandardCopyOption.REPLACE_EXISTING);
      // The host restores the emulator's network after recording the queued state.
      deadline = SystemClock.uptimeMillis() + 180000;
      JSONObject receipt = null;
      while (receipt == null && SystemClock.uptimeMillis() < deadline) {
        try { receipt = CameraApi.upload(store, store.item(id)); }
        catch (Exception offline) { Thread.sleep(2000); }
      }
      assertNotNull("Video should archive after the network returns", receipt);
      assertEquals("video", receipt.getString("media_type"));
      assertEquals("uploaded", receipt.getString("state"));
      assertEquals("not_requested", receipt.getString("verification_state"));
      assertFalse(receipt.getBoolean("verification_supported"));
      assertEquals(hash, receipt.getString("original_hash"));
      JSONObject duplicate = CameraApi.upload(store, store.item(id));
      assertTrue(duplicate.getBoolean("duplicate"));
      assertEquals(receipt.getString("photo_id"), duplicate.getString("photo_id"));
      store.uploaded(id, receipt);
      // A stale SDK callback from another capture must never be attached here.
      JSONObject second = new JSONObject(meta.toString()).put("capture_id", java.util.UUID.randomUUID().toString())
          .put("media_type", "video");
      store.create(second, true);
      VideoInfo stale = new VideoInfo(); stale.filePath = store.original(id).getPath();
      assertThrows(Exception.class, () -> OfficialVideoWriter.save(store, second.getString("capture_id"), stale));
      store.state(second.getString("capture_id"), "local", "Synthetic stale-callback guard");
      assertEquals(hash, OfficialPhotoWriter.sha256(store.original(id)));
      Files.write(new File(output, "live-result.json").toPath(), new JSONObject()
          .put("capture_id", id).put("state", store.item(id).state).put("metadata", store.item(id).meta)
          .put("receipt", receipt).put("original_hash", hash).put("duplicate_receipt", duplicate)
          .toString(2).getBytes(StandardCharsets.UTF_8));
    }
  }
}
