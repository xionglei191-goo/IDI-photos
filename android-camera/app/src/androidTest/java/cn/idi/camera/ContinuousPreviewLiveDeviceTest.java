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
import android.view.PixelCopy;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.rule.GrantPermissionRule;
import androidx.test.runner.lifecycle.ActivityLifecycleCallback;
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry;
import androidx.test.runner.lifecycle.Stage;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Actual SDK photos on an isolated backend, including activity and surface checks. */
@RunWith(AndroidJUnit4.class)
public class ContinuousPreviewLiveDeviceTest {
  @Rule public GrantPermissionRule permissions = GrantPermissionRule.grant(
      Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION);

  @Test public void threePhotosReuseCameraAndReturnOnlyAfterBack() throws Exception {
    assumeTrue(BuildConfig.DEBUG && "1".equals(InstrumentationRegistry.getArguments().getString("preview_live", "")));
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    String server = InstrumentationRegistry.getArguments().getString("server", "http://127.0.0.1:8894");
    assertTrue(server.startsWith("http://127.0.0.1:"));
    if ("1".equals(InstrumentationRegistry.getArguments().getString("reuse_current", ""))) {
      assertTrue(store.connected()); assertEquals(server, store.server());
    } else {
      JSONObject pair = CameraApi.request(server, "/api/camera/pair", "", new JSONObject()
          .put("code", InstrumentationRegistry.getArguments().getString("pair_code", "")));
      store.connect(server, pair);
    }
    store.replaceCases(CameraApi.request(server, "/api/camera/cases", store.token(store.device()), null).getJSONArray("items"));
    NativeAuthorization.save(store, NativeAuthorization.scope(store),
        CameraApi.request(server, "/api/camera/native-authorization", store.token(store.device()), null));
    File output = new File(context.getFilesDir(), "continuous-preview-verification"); output.mkdirs();
    JSONObject report = new JSONObject().put("scope", "live SDK emulator; isolated synthetic cases; verification disabled");
    AtomicInteger mainResumes = new AtomicInteger(), cameraCreations = new AtomicInteger(), cameraDestructions = new AtomicInteger();
    ActivityLifecycleCallback monitor = (activity, stage) -> {
      if (activity instanceof MainActivity && stage == Stage.RESUMED) mainResumes.incrementAndGet();
      if (activity instanceof EditableCameraActivity && stage == Stage.CREATED) cameraCreations.incrementAndGet();
      if (activity instanceof EditableCameraActivity && stage == Stage.DESTROYED) cameraDestructions.incrementAndGet();
    };
    context.startActivity(new Intent(context, MainActivity.class).putExtra("emulator_test", true)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK));
    await(() -> current(MainActivity.class) != null, 15000);
    ui(() -> { MainActivity main = current(MainActivity.class); main.nextBackgroundSync = SystemClock.elapsedRealtime() + 3600000;
      main.select(store.cases().get(0)); return null; });
    await(() -> current(EditableCameraActivity.class) != null, 15000);
    EditableCameraActivity camera = ui(() -> current(EditableCameraActivity.class));
    await(() -> camera.watermarkTarget.getVisibility() == View.VISIBLE && camera.editor != null, 60000);
    ui(() -> { camera.editor.dismiss(); return null; });
    awaitPreviewPixels(camera);
    String root = camera.captureId, currentId = root;
    JSONArray photos = new JSONArray();
    ui(() -> { ActivityLifecycleMonitorRegistry.getInstance().addLifecycleCallback(monitor); return null; });
    try {
      for (int shot = 0; shot < 3; shot++) {
        final String savedId = currentId;
        long started = SystemClock.uptimeMillis();
        ui(() -> { camera.renderer.evaluateJavascript("document.querySelector('.capture-button').click()", null); return null; });
        await(() -> !camera.captureId.equals(savedId) && !camera.savingPhoto && !camera.continuingPhoto
            && camera.watermarkTarget.getVisibility() == View.VISIBLE, 45000);
        long elapsed = SystemClock.uptimeMillis() - started;
        currentId = ui(() -> camera.captureId);
        assertTrue(store.original(savedId).isFile());
        assertNotEquals(savedId, currentId);
        assertEquals(currentId, store.prefs.getString("next_official_photo_" + savedId, ""));
        ui(() -> {
          assertSame("Saving must keep the exact same SDK activity", camera, current(EditableCameraActivity.class));
          assertNull(camera.editor);
          assertEquals(View.GONE, camera.photoSaveBlocker.getVisibility());
          assertEquals(camera.captureId, camera.getIntent().getStringExtra(ConfiguredXheyAdapter.CAPTURE_ID));
          return null;
        });
        awaitPreviewPixels(camera);
        assertEquals("Cases must never resume between photos", 0, mainResumes.get());
        assertEquals(0, cameraCreations.get());
        assertEquals(0, cameraDestructions.get());
        CaptureStore.Item item = store.item(savedId);
        assertFalse(store.item(currentId).meta.has("sdk_anti_fake_code"));
        assertArrayEquals(Files.readAllBytes(store.original(savedId).toPath()), Files.readAllBytes(store.stamped(savedId).toPath()));
        photos.put(new JSONObject().put("capture_id", savedId).put("sha256", OfficialPhotoWriter.sha256(store.original(savedId)))
            .put("capture_to_next_preview_ms", elapsed).put("sdk_photo_time", item.meta.optString("sdk_photo_time")));
        Bitmap screenshot = InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
        try (FileOutputStream file = new FileOutputStream(new File(output, "after-photo-" + (shot + 1) + ".png"))) {
          screenshot.compress(Bitmap.CompressFormat.PNG, 100, file);
        } finally { screenshot.recycle(); }
      }
      report.put("photos", photos).put("main_resumes_during_capture", mainResumes.get())
          .put("sdk_recreations_during_capture", cameraCreations.get()).put("sdk_destructions_during_capture", cameraDestructions.get());
      String empty = currentId;
      ui(() -> { camera.onBackPressed(); return null; });
      await(() -> "cancelled".equals(store.item(empty).state) && current(MainActivity.class) != null, 15000);
      assertFalse(store.prefs.contains("native_series_" + root));
      assertFalse(store.prefs.contains("pending_official_capture"));
      assertFalse(store.original(empty).exists());
      for (int index = 0; index < photos.length(); index++) {
        String id = photos.getJSONObject(index).getString("capture_id");
        await(() -> "uploaded".equals(store.item(id).state), 30000);
        photos.getJSONObject(index).put("receipt", new JSONObject(store.item(id).receipt));
      }
      report.put("back_returns_to_cases", true).put("empty_cancelled", empty).put("all_three_uploaded", true);
    } finally {
      ui(() -> { ActivityLifecycleMonitorRegistry.getInstance().removeLifecycleCallback(monitor); return null; });
      Files.write(new File(output, "result.json").toPath(), report.toString(2).getBytes(StandardCharsets.UTF_8));
    }
  }

  private static <T extends Activity> T current(Class<T> type) {
    for (Activity activity : ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED))
      if (type.isInstance(activity)) return type.cast(activity);
    return null;
  }

  private static View surface(View view) {
    if (view instanceof SurfaceView) return view;
    if (view instanceof ViewGroup group) for (int i = 0; i < group.getChildCount(); i++) {
      View result = surface(group.getChildAt(i)); if (result != null) return result;
    }
    return null;
  }

  private static boolean previewPixels(EditableCameraActivity camera) throws Exception {
    CountDownLatch done = new CountDownLatch(1);
    AtomicInteger variation = new AtomicInteger();
    ui(() -> {
      SurfaceView view = (SurfaceView) surface(camera.getWindow().getDecorView()); assertNotNull(view);
      Bitmap frame = Bitmap.createBitmap(32, 32, Bitmap.Config.ARGB_8888);
      PixelCopy.request(view, frame, code -> {
        if (code == PixelCopy.SUCCESS) {
          int low = 255, high = 0;
          for (int y = 2; y < 30; y += 3) for (int x = 2; x < 30; x += 3) {
            int c = frame.getPixel(x, y);
            int light = (android.graphics.Color.red(c) + android.graphics.Color.green(c) + android.graphics.Color.blue(c)) / 3;
            low = Math.min(low, light); high = Math.max(high, light);
          }
          variation.set(high - low);
        }
        frame.recycle(); done.countDown();
      }, new Handler(Looper.getMainLooper())); return null;
    });
    return done.await(3, TimeUnit.SECONDS) && variation.get() > 24;
  }

  private static void awaitPreviewPixels(EditableCameraActivity camera) throws Exception {
    long end = SystemClock.uptimeMillis() + 60000;
    do { if (previewPixels(camera)) return; SystemClock.sleep(100); }
    while (SystemClock.uptimeMillis() < end);
    fail("Native camera surface must have preview pixels");
  }

  private static void await(Callable<Boolean> condition, long timeout) throws Exception {
    long end = SystemClock.uptimeMillis() + timeout;
    while (!ui(condition) && SystemClock.uptimeMillis() < end) SystemClock.sleep(100);
    assertTrue("Timed out waiting for continuous preview", ui(condition));
  }

  private static <T> T ui(Callable<T> work) throws Exception {
    CountDownLatch done = new CountDownLatch(1); AtomicReference<T> value = new AtomicReference<>();
    AtomicReference<Throwable> error = new AtomicReference<>();
    new Handler(Looper.getMainLooper()).post(() -> { try { value.set(work.call()); } catch (Throwable e) { error.set(e); } finally { done.countDown(); } });
    assertTrue(done.await(10, TimeUnit.SECONDS));
    if (error.get() != null) throw new AssertionError(error.get());
    return value.get();
  }
}
