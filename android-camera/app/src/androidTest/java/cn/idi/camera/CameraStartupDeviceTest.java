package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;

import android.Manifest;
import android.app.Activity;
import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.SystemClock;
import android.view.View;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.rule.GrantPermissionRule;
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry;
import androidx.test.runner.lifecycle.Stage;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Explicit emulator fixtures; intercept the vendor launch and never create a real photo. */
@RunWith(AndroidJUnit4.class)
public class CameraStartupDeviceTest {
  @Rule public GrantPermissionRule permissions = GrantPermissionRule.grant(
      Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION);
  CaptureStore store;
  Map<String, ?> preferences;
  JSONArray cases;
  JSONObject selected;
  Instrumentation instrumentation;
  Instrumentation.ActivityMonitor monitor;
  final AtomicReference<Intent> launched = new AtomicReference<>();
  final CountDownLatch launchSeen = new CountDownLatch(1);
  CountDownLatch releaseIo, releaseLaunch;

  @Before public void prepare() throws Exception {
    assumeTrue(BuildConfig.DEBUG && "1".equals(InstrumentationRegistry.getArguments().getString("startup_fixture", "")));
    assumeTrue(Build.PRODUCT.startsWith("sdk") || Build.FINGERPRINT.contains("generic"));
    instrumentation = InstrumentationRegistry.getInstrumentation();
    store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    CaptureStore.io.submit(() -> {}).get(15, TimeUnit.SECONDS);
    CameraLaunch.io.submit(() -> {}).get(10, TimeUnit.SECONDS);
    assertTrue("Use an idle emulator", store.activeCaptures.isEmpty());
    preferences = new HashMap<>(store.prefs.getAll());
    cases = new JSONArray();
    for (JSONObject value : store.cases()) cases.put(new JSONObject(value.toString()));
    store.prefs.edit().clear().putString("server", "http://127.0.0.1:1")
        .putString("device", "startup-fixture").putString("token_startup-fixture", "fixture-token").commit();
    selected = new JSONObject().put("id", "startup-case").put("title", "启动测试")
        .put("case_number", "ZLBH202610280000000003").put("project_name", "启动测试项目")
        .put("location", "1栋101室").put("capture_enabled", true).put("workflow_stage", "repair");
    store.replaceCases(new JSONArray().put(selected));
    authorize();
    monitor = new Instrumentation.ActivityMonitor() {
      @Override public Instrumentation.ActivityResult onStartActivity(Intent intent) {
        if (intent.getComponent() != null && EditableCameraActivity.class.getName().equals(intent.getComponent().getClassName())) {
          launched.set(new Intent(intent)); launchSeen.countDown();
          return new Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null);
        }
        return null;
      }
    };
    instrumentation.addMonitor(monitor);
  }

  void authorize() throws Exception {
    NativeAuthorization.save(store, NativeAuthorization.scope(store), new JSONObject().put("ready", true)
        .put("device_id", "startup-fixture").put("sdk_version", BuildConfig.XHEY_SDK_VERSION)
        .put("server_time", 1793174400).put("expires_at", 1793174400 + 23 * 3600)
        .put("video_enabled", true).put("options", new JSONObject().put("appid", "fixture")
          .put("noncestr", "fixture").put("signature", "fixture").put("timestamp", 1793174400)));
  }

  CountDownLatch block(java.util.concurrent.ExecutorService executor) throws Exception {
    CountDownLatch started = new CountDownLatch(1), release = new CountDownLatch(1);
    executor.execute(() -> { started.countDown(); try { release.await(20, TimeUnit.SECONDS); } catch (InterruptedException e) { Thread.currentThread().interrupt(); } });
    assertTrue(started.await(5, TimeUnit.SECONDS));
    return release;
  }

  @After public void restore() throws Exception {
    if (preferences == null) return;
    if (releaseIo != null) releaseIo.countDown();
    if (releaseLaunch != null) releaseLaunch.countDown();
    CameraLaunch.io.submit(() -> {}).get(10, TimeUnit.SECONDS);
    CaptureStore.io.submit(() -> {}).get(15, TimeUnit.SECONDS);
    if (monitor != null) instrumentation.removeMonitor(monitor);
    store.replaceCases(cases);
    SharedPreferences.Editor editor = store.prefs.edit().clear();
    for (Map.Entry<String, ?> entry : preferences.entrySet()) {
      String key = entry.getKey(); Object value = entry.getValue();
      if (value instanceof String v) editor.putString(key, v);
      else if (value instanceof Boolean v) editor.putBoolean(key, v);
      else if (value instanceof Integer v) editor.putInt(key, v);
      else if (value instanceof Long v) editor.putLong(key, v);
      else if (value instanceof Float v) editor.putFloat(key, v);
      else if (value instanceof Set<?> v) editor.putStringSet(key, new java.util.HashSet<>((Set<String>) v));
    }
    assertTrue(editor.commit());
  }

  @Test public void cachedLaunchKeepsPageAndBypassesBlockedBackgroundIo() throws Exception {
    releaseIo = block(CaptureStore.io);
    try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
      long start = SystemClock.uptimeMillis();
      scenario.onActivity(a -> {
        View previous = a.root;
        int count = store.items().size();
        a.select(selected);
        assertSame("No preparation page should replace the current page", previous, a.root);
        assertTrue(a.busy);
        a.select(selected);
        assertEquals("Repeated taps must not create two sessions", count + 1, store.items().size());
      });
      assertTrue("SDK launch must not wait for the blocked network/recovery queue", launchSeen.await(2500, TimeUnit.MILLISECONDS));
      assertEquals(1, releaseIo.getCount());
      assertTrue(launched.get().getBooleanExtra(EditableCameraActivity.SHOW_EDITOR, false));
      assertEquals("启动测试", ((java.util.HashMap<?, ?>) launched.get().getSerializableExtra(
          com.xhey.xheycamerasdk.CameraViewConfig.kCustomInputItems)).get("内容"));
      java.io.File output = new java.io.File(store.context.getFilesDir(), "startup-verification"); output.mkdirs();
      java.nio.file.Files.write(new java.io.File(output, "queue-check.json").toPath(),
          new JSONObject().put("click_to_sdk_launch_ms", SystemClock.uptimeMillis() - start)
              .put("background_queue_still_blocked", true).put("sdk_launch_intercepted", true).toString(2)
              .getBytes(java.nio.charset.StandardCharsets.UTF_8));
      releaseIo.countDown();
    }
  }

  @Test public void backDuringPreparationPreventsLateSdkLaunch() throws Exception {
    releaseLaunch = block(CameraLaunch.io);
    try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
      scenario.onActivity(a -> a.select(selected));
      NativeCameraActivity camera = awaitCoordinator();
      String id = camera.captureId;
      instrumentation.runOnMainSync(() -> camera.getOnBackPressedDispatcher().onBackPressed());
      releaseLaunch.countDown();
      CameraLaunch.io.submit(() -> {}).get(5, TimeUnit.SECONDS);
      SystemClock.sleep(300);
      assertNull("A cancelled preparation must never launch the SDK later", launched.get());
      assertEquals("cancelled", store.item(id).state);
    }
  }

  @Test public void missingAuthorizationKeepsPageAndCancelledSyncCannotReopen() throws Exception {
    NativeAuthorization.clear(store);
    releaseIo = block(CaptureStore.io);
    try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
      AtomicReference<OfficialCameraScreen> old = new AtomicReference<>();
      scenario.onActivity(a -> {
        View previous = a.root;
        int count = store.items().size();
        a.select(selected);
        assertSame(previous, a.root);
        assertTrue(a.officialScreen.prompt.isShowing());
        assertEquals(count, store.items().size());
        old.set(a.officialScreen);
        a.show("cases");
        assertNull(old.get().prompt);
      });
      authorize();
      scenario.onActivity(a -> old.get().synchronizedCases("已同步"));
      assertNull(launched.get());
      releaseIo.countDown();
    }
  }

  @Test public void authorizationRevokedWhilePreparingBlocksSdkAndShowsRetry() throws Exception {
    releaseLaunch = block(CameraLaunch.io);
    try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
      scenario.onActivity(a -> a.select(selected));
      NativeCameraActivity camera = awaitCoordinator();
      NativeAuthorization.denied(store, NativeAuthorization.scope(store));
      releaseLaunch.countDown();
      CameraLaunch.io.submit(() -> {}).get(5, TimeUnit.SECONDS);
      instrumentation.waitForIdleSync();
      assertNull(launched.get());
      assertEquals("camera_error", store.item(camera.captureId).state);
      instrumentation.runOnMainSync(() -> {
        assertEquals(View.VISIBLE, camera.feedback.getVisibility());
        assertEquals(View.VISIBLE, camera.retry.getVisibility());
        camera.getOnBackPressedDispatcher().onBackPressed();
      });
    }
  }

  NativeCameraActivity awaitCoordinator() throws Exception {
    AtomicReference<NativeCameraActivity> value = new AtomicReference<>();
    long end = SystemClock.uptimeMillis() + 5000;
    do {
      instrumentation.runOnMainSync(() -> {
        for (Activity a : ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED))
          if (a instanceof NativeCameraActivity camera) value.set(camera);
      });
      if (value.get() != null) return value.get();
      SystemClock.sleep(40);
    } while (SystemClock.uptimeMillis() < end);
    throw new AssertionError("Coordinator did not resume");
  }
}
