package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.view.SurfaceView;
import android.view.TextureView;
import android.view.PixelCopy;
import android.widget.EditText;
import android.widget.TextView;
import androidx.camera.view.PreviewView;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.rule.GrantPermissionRule;
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry;
import androidx.test.runner.lifecycle.ActivityLifecycleCallback;
import androidx.test.runner.lifecycle.Stage;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.AtomicInteger;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Opt-in preview checks; recording needs the additional startup_live_record flag. */
@RunWith(AndroidJUnit4.class)
public class CameraStartupLiveDeviceTest {
  @Rule public GrantPermissionRule permissions = GrantPermissionRule.grant(
      Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION,
      Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.RECORD_AUDIO);
  File output;

  @Test public void livePreviewEditAndModeSwitchBypassBackgroundQueue() throws Exception {
    assumeTrue(BuildConfig.DEBUG && "1".equals(InstrumentationRegistry.getArguments().getString("startup_live", "")));
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    String server = InstrumentationRegistry.getArguments().getString("server", "http://127.0.0.1:8893");
    String code = InstrumentationRegistry.getArguments().getString("pair_code", "");
    assertTrue("Use a loopback-only acceptance server", server.startsWith("http://127.0.0.1:"));
    if ("1".equals(InstrumentationRegistry.getArguments().getString("reuse_current", ""))) {
      assertTrue(store.connected()); assertEquals(server, store.server());
    } else {
      JSONObject pair = CameraApi.request(server, "/api/camera/pair", "", new JSONObject().put("code", code));
      store.connect(server, pair);
    }
    store.replaceCases(CameraApi.request(server, "/api/camera/cases", store.token(store.device()), null).getJSONArray("items"));
    NativeAuthorization.save(store, NativeAuthorization.scope(store),
        CameraApi.request(server, "/api/camera/native-authorization", store.token(store.device()), null));
    output = new File(context.getFilesDir(), "startup-verification"); output.mkdirs();
    boolean recordVideo = "1".equals(InstrumentationRegistry.getArguments().getString("startup_live_record", ""));
    JSONObject report = new JSONObject().put("scope", recordVideo
        ? "live SDK preview and video on emulator; synthetic local cases; verification disabled"
        : "live SDK preview on emulator; synthetic local cases; no capture or upload");
    AtomicInteger uncoveredCoordinator = new AtomicInteger(), uncoveredCamera = new AtomicInteger(), mainResumes = new AtomicInteger();
    java.util.ArrayList<Bitmap> transitionFrames = new java.util.ArrayList<>();
    ActivityLifecycleCallback transitions = (activity, stage) -> {
      if (stage == Stage.RESUMED && activity instanceof MainActivity) mainResumes.incrementAndGet();
      if (stage == Stage.RESUMED && activity instanceof NativeCameraActivity host) {
        if (host.transitionFrame == null || host.transitionCover == null) uncoveredCoordinator.incrementAndGet();
        else transitionFrames.add(host.transitionFrame);
      }
      if (stage == Stage.CREATED && activity instanceof EditableCameraActivity sdk && sdk.transitionCover == null)
        uncoveredCamera.incrementAndGet();
    };
    CountDownLatch release = new CountDownLatch(1), blocked = new CountDownLatch(1);
    context.startActivity(new Intent(context, MainActivity.class).putExtra("emulator_test", true)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK));
    MainActivity main = awaitActivity(MainActivity.class);
    CaptureStore.io.submit(() -> {}).get(20, TimeUnit.SECONDS);
    CaptureStore.io.execute(() -> {
      blocked.countDown();
      try { release.await(150, TimeUnit.SECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
    });
    assertTrue(blocked.await(5, TimeUnit.SECONDS));
    try {
      long started = SystemClock.uptimeMillis();
      ui(() -> {
        main.nextBackgroundSync = SystemClock.elapsedRealtime() + 3600000;
        View previous = main.root;
        main.select(store.cases().get(0));
        assertSame(previous, main.root);
        return null;
      });
      EditableCameraActivity photo = awaitActivity(EditableCameraActivity.class);
      report.put("click_to_sdk_activity_ms", SystemClock.uptimeMillis() - started);
      awaitReady(photo, 60000);
      report.put("click_to_preview_pixels_and_watermark_ready_ms", SystemClock.uptimeMillis() - started);
      report.put("background_queue_still_blocked", release.getCount() == 1);
      assertEquals(1, release.getCount());
      String id = photo.captureId;
      ui(() -> { assertNotNull(photo.editor); photo.editor.dismiss(); return null; });
      screenshot("photo-preview.png");
      ui(() -> { ActivityLifecycleMonitorRegistry.getInstance().addLifecycleCallback(transitions); return null; });
      long editStart = SystemClock.uptimeMillis();
      String editedContent = "PREVIEW-0510-" + editStart + " / 预览切换验收";
      ui(() -> { photo.editWatermark(); return null; });
      await(() -> photo.editor != null);
      ui(() -> {
        View content = find(photo.editor.getWindow().getDecorView(), v -> v instanceof EditText text
            && "填写本张照片的实际内容".contentEquals(text.getHint()));
        assertNotNull(content);
        ((EditText) content).setText(editedContent);
        View save = find(photo.editor.getWindow().getDecorView(), v -> v instanceof TextView text
            && "保存并继续拍照".contentEquals(text.getText()));
        assertNotNull(save); save.performClick(); return null;
      });
      EditableCameraActivity edited = awaitReplacement(photo);
      awaitReady(edited, 60000);
      assertEquals(id, edited.captureId);
      assertEquals(editedContent, store.item(id).meta.getString("content"));
      report.put("watermark_edit_to_ready_ms", SystemClock.uptimeMillis() - editStart);
      long videoStart = SystemClock.uptimeMillis();
      ui(() -> { find(edited.modes, v -> "切换到录像".equals(String.valueOf(v.getContentDescription()))).performClick(); return null; });
      EditableCameraActivity video = awaitReplacement(edited);
      awaitReady(video, 60000);
      assertEquals(id, video.captureId);
      assertTrue(store.item(id).video());
      screenshot("video-preview.png");
      report.put("switch_to_video_ready_ms", SystemClock.uptimeMillis() - videoStart);
      long photoStart = SystemClock.uptimeMillis();
      ui(() -> { find(video.modes, v -> "切换到拍照".equals(String.valueOf(v.getContentDescription()))).performClick(); return null; });
      EditableCameraActivity again = awaitReplacement(video);
      awaitReady(again, 60000);
      assertEquals(id, again.captureId);
      assertFalse(store.item(id).video());
      report.put("switch_back_to_photo_ready_ms", SystemClock.uptimeMillis() - photoStart);
      screenshot("photo-after-switch.png");
      EditableCameraActivity finalVideo = null;
      if (recordVideo) {
        ui(() -> { find(again.modes, v -> "切换到录像".equals(String.valueOf(v.getContentDescription()))).performClick(); return null; });
        finalVideo = awaitReplacement(again);
        awaitReady(finalVideo, 60000);
        assertEquals(id, finalVideo.captureId);
      }
      ui(() -> { ActivityLifecycleMonitorRegistry.getInstance().removeLifecycleCallback(transitions); return null; });
      assertEquals("Every intermediate coordinator must cover the cases page", 0, uncoveredCoordinator.get());
      assertEquals("The new SDK must retain the camera cover while loading", 0, uncoveredCamera.get());
      assertEquals("Switching must not resume the cases page", 0, mainResumes.get());
      assertEquals("Every edit and mode switch needs a preview cover", recordVideo ? 4 : 3, transitionFrames.size());
      for (int i = 0; i < transitionFrames.size(); i++) {
        try (FileOutputStream file = new FileOutputStream(new File(output, "transition-cover-" + (i + 1) + ".png"))) {
          transitionFrames.get(i).compress(Bitmap.CompressFormat.PNG, 100, file);
        }
      }
      report.put("covered_config_changes", transitionFrames.size()).put("uncovered_coordinator_resumes", uncoveredCoordinator.get())
          .put("uncovered_sdk_creations", uncoveredCamera.get()).put("main_resumes_during_switches", mainResumes.get());
      if (recordVideo) {
        release.countDown();
        EditableCameraActivity recording = finalVideo;
        ui(() -> { recording.renderer.evaluateJavascript(
            "document.querySelector('.capture-button').dispatchEvent(new Event('touchstart',{bubbles:true}))", null); return null; });
        await(() -> store.item(id).meta.has("video_capture_time"));
        // ARM codec translation on the x86 emulator can take several seconds
        // after the SDK's recording UI appears before its first encoded frame.
        SystemClock.sleep(12000);
        ui(() -> {
          assertNull(recording.editor);
          assertEquals(View.GONE, recording.modes.getVisibility());
          recording.renderer.evaluateJavascript(
              "document.querySelector('.capture-button').dispatchEvent(new Event('touchstart',{bubbles:true}))", null);
          return null;
        });
        long end = SystemClock.uptimeMillis() + 45000;
        while (!"uploaded".equals(store.item(id).state) && SystemClock.uptimeMillis() < end) SystemClock.sleep(200);
        assertEquals("uploaded", store.item(id).state);
        CaptureStore.Item saved = store.item(id);
        assertTrue(saved.video()); assertTrue(store.original(id).isFile());
        assertNotNull(VideoCaptureTime.validate(saved.meta.getJSONObject("video_capture_time")));
        JSONObject receipt = new JSONObject(saved.receipt);
        String hash = OfficialPhotoWriter.sha256(store.original(id));
        assertEquals(hash, receipt.getString("original_hash"));
        assertFalse(receipt.getBoolean("verification_supported"));
        android.media.MediaExtractor extractor = new android.media.MediaExtractor();
        boolean audio = false, videoTrack = false;
        try {
          extractor.setDataSource(store.original(id).getAbsolutePath());
          for (int track = 0; track < extractor.getTrackCount(); track++) {
            String mime = extractor.getTrackFormat(track).getString(android.media.MediaFormat.KEY_MIME);
            audio |= mime != null && mime.startsWith("audio/");
            videoTrack |= mime != null && mime.startsWith("video/");
          }
        } finally { extractor.release(); }
        assertTrue("Recorded MP4 must retain audio and video tracks", audio && videoTrack);
        Files.copy(store.original(id).toPath(), new File(output, "after-switch-original.mp4").toPath(),
            java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        report.put("video_after_switch", new JSONObject().put("capture_id", id).put("sha256", hash)
            .put("audio_track", audio).put("video_track", videoTrack)
            .put("video_capture_time", saved.meta.getJSONObject("video_capture_time")).put("receipt", receipt));
      } else {
        ui(() -> { again.onBackPressed(); return null; });
        await(() -> "cancelled".equals(store.item(id).state));
        assertFalse(store.original(id).exists());
        assertNull(store.prefs.getString("pending_official_capture", null));
        report.put("cancelled_without_capture", true);
      }
      report.put("same_capture_id_through_edit_and_modes", true);
    } finally {
      release.countDown();
      ui(() -> { ActivityLifecycleMonitorRegistry.getInstance().removeLifecycleCallback(transitions); return null; });
      Files.write(new File(output, "live-startup.json").toPath(), report.toString(2).getBytes(StandardCharsets.UTF_8));
      ui(() -> {
        for (Activity activity : new java.util.ArrayList<>(ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED)))
          activity.finish();
        return null;
      });
    }
  }

  private void awaitReady(EditableCameraActivity activity, long timeout) throws Exception {
    long end = SystemClock.uptimeMillis() + timeout;
    do {
      if (ui(() -> activity.transitionCover == null && activity.watermarkTarget.getVisibility() == View.VISIBLE
          && activity.preparation.getVisibility() == View.GONE) && previewHasPixels(activity)) return;
      SystemClock.sleep(100);
    } while (SystemClock.uptimeMillis() < end);
    JSONObject diagnostic = ui(() -> {
      PreviewView preview = (PreviewView) find(activity.getWindow().getDecorView(), v -> v instanceof PreviewView);
      org.json.JSONArray tree = new org.json.JSONArray();
      inspect(activity.getWindow().getDecorView(), tree);
      return new JSONObject().put("preview_view_found", preview != null)
          .put("stream", preview == null ? "unavailable" : String.valueOf(preview.getPreviewStreamState().getValue()))
          .put("watermark_visible", activity.watermarkTarget.getVisibility())
          .put("preparation_visible", activity.preparation.getVisibility()).put("views", tree);
    });
    Files.write(new File(output, "preview-diagnostic.json").toPath(), diagnostic.toString(2).getBytes(StandardCharsets.UTF_8));
    screenshot("not-ready.png");
    throw new AssertionError("Live preview and watermark were not ready");
  }

  /** The pinned SDK renders into its own SurfaceView, not CameraX PreviewView. */
  private boolean previewHasPixels(EditableCameraActivity activity) throws Exception {
    CountDownLatch copied = new CountDownLatch(1);
    java.util.concurrent.atomic.AtomicBoolean varied = new java.util.concurrent.atomic.AtomicBoolean();
    ui(() -> {
      View preview = find(activity.getWindow().getDecorView(), v -> v instanceof SurfaceView || v instanceof TextureView);
      if (preview instanceof TextureView texture && texture.isAvailable()) {
        Bitmap frame = texture.getBitmap(32, 32);
        if (frame != null) { varied.set(variedPixels(frame)); frame.recycle(); }
        copied.countDown();
      } else if (preview instanceof SurfaceView surface && surface.getHolder().getSurface().isValid()) {
        Bitmap frame = Bitmap.createBitmap(32, 32, Bitmap.Config.ARGB_8888);
        PixelCopy.request(surface, frame, code -> {
          if (code == PixelCopy.SUCCESS) varied.set(variedPixels(frame));
          frame.recycle(); copied.countDown();
        }, new Handler(Looper.getMainLooper()));
      } else copied.countDown();
      return null;
    });
    return copied.await(3, TimeUnit.SECONDS) && varied.get();
  }

  private static boolean variedPixels(Bitmap frame) {
    int low = 255, high = 0;
    for (int y = 2; y < 30; y += 3) for (int x = 2; x < 30; x += 3) {
      int pixel = frame.getPixel(x, y);
      int light = (android.graphics.Color.red(pixel) + android.graphics.Color.green(pixel) + android.graphics.Color.blue(pixel)) / 3;
      low = Math.min(low, light); high = Math.max(high, light);
    }
    return high - low > 24;
  }

  private EditableCameraActivity awaitReplacement(EditableCameraActivity old) throws Exception {
    long end = SystemClock.uptimeMillis() + 15000;
    do {
      EditableCameraActivity value = current(EditableCameraActivity.class);
      if (value != null && value != old) return value;
      SystemClock.sleep(50);
    } while (SystemClock.uptimeMillis() < end);
    throw new AssertionError("SDK did not reopen after edit or mode switch");
  }

  private <T extends Activity> T awaitActivity(Class<T> type) throws Exception {
    long end = SystemClock.uptimeMillis() + 15000;
    do { T result = current(type); if (result != null) return result; SystemClock.sleep(50); }
    while (SystemClock.uptimeMillis() < end);
    throw new AssertionError(type.getSimpleName() + " did not resume");
  }

  private <T extends Activity> T current(Class<T> type) throws Exception {
    return ui(() -> {
      for (Activity a : ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED))
        if (type.isInstance(a)) return type.cast(a);
      return null;
    });
  }

  private void await(Callable<Boolean> condition) throws Exception {
    long end = SystemClock.uptimeMillis() + 10000;
    while (!ui(condition) && SystemClock.uptimeMillis() < end) SystemClock.sleep(50);
    assertTrue("Timed out waiting for UI state", ui(condition));
  }

  private static View find(View root, java.util.function.Predicate<View> test) {
    if (test.test(root)) return root;
    if (root instanceof ViewGroup group) for (int i = 0; i < group.getChildCount(); i++) {
      View result = find(group.getChildAt(i), test); if (result != null) return result;
    }
    return null;
  }

  private static void inspect(View view, org.json.JSONArray result) throws Exception {
    result.put(new JSONObject().put("class", view.getClass().getName()).put("width", view.getWidth())
        .put("height", view.getHeight()).put("visibility", view.getVisibility())
        .put("surface_view", view instanceof SurfaceView).put("texture_view", view instanceof TextureView));
    if (view instanceof ViewGroup group) for (int i = 0; i < group.getChildCount(); i++) inspect(group.getChildAt(i), result);
  }

  private static <T> T ui(Callable<T> task) throws Exception {
    CountDownLatch done = new CountDownLatch(1);
    AtomicReference<T> result = new AtomicReference<>();
    AtomicReference<Throwable> error = new AtomicReference<>();
    new Handler(Looper.getMainLooper()).post(() -> {
      try { result.set(task.call()); } catch (Throwable e) { error.set(e); } finally { done.countDown(); }
    });
    assertTrue("UI thread stalled", done.await(10, TimeUnit.SECONDS));
    if (error.get() != null) throw new AssertionError(error.get());
    return result.get();
  }

  private void screenshot(String name) throws Exception {
    Bitmap image = InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
    assertNotNull(image);
    try (FileOutputStream stream = new FileOutputStream(new File(output, name))) { image.compress(Bitmap.CompressFormat.PNG, 100, stream); }
    image.recycle();
  }
}
