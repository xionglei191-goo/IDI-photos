package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;
import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.os.Parcel;
import android.os.SystemClock;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.rule.GrantPermissionRule;
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry;
import androidx.test.runner.lifecycle.Stage;
import com.xhey.xheycamerasdk.CameraActivity;
import com.xhey.xheycamerasdk.CameraConfigMode;
import com.xhey.xheycamerasdk.CameraViewConfig;
import com.xhey.xheycamerasdk.CapturedImage;
import com.xhey.xheycamerasdk.UserComment;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class NativeSdkDeviceTest {
  @Rule public GrantPermissionRule permissions = GrantPermissionRule.grant(
      Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION);

  @Test public void signedNativeIntentPreservesCredentialsAndFiveFieldsAcrossParcel() throws Exception {
    JSONObject fields = new JSONObject().put("案件号", "ZLBH202610200000000001")
        .put("小区", "铂悦府（测试）").put("位置", "1幢201室").put("环节", "施工 · 楼栋")
        .put("内容", "测试：\"保留引号\"\n第二行");
    JSONObject session = new JSONObject().put("capture_id", "synthetic-contract")
        .put("sdk_version", BuildConfig.XHEY_SDK_VERSION).put("options", new JSONObject()
          .put("appid", "SYNTHETIC-APP").put("noncestr", "SYNTHETIC-NONCE").put("timestamp", 1792456000)
          .put("signature", "SYNTHETIC-SIGNATURE").put("groupWatermarkId", "SYNTHETIC-TEMPLATE")
          .put("customInputItems", fields));
    try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
      scenario.onActivity(activity -> {
        try {
          Intent original = ConfiguredXheyAdapter.intent(activity, session, "synthetic-contract", false);
          assertEquals(EditableCameraActivity.class.getName(), original.getComponent().getClassName());
          Parcel parcel = Parcel.obtain();
          original.writeToParcel(parcel, 0); parcel.setDataPosition(0);
          Intent intent = Intent.CREATOR.createFromParcel(parcel); parcel.recycle();
          assertEquals("SYNTHETIC-APP", intent.getStringExtra(CameraViewConfig.kAppid));
          assertEquals("SYNTHETIC-NONCE", intent.getStringExtra(CameraViewConfig.kNoncestr));
          assertEquals(1792456000, intent.getIntExtra(CameraViewConfig.kTimestamp, 0));
          assertEquals("SYNTHETIC-SIGNATURE", intent.getStringExtra(CameraViewConfig.kSignature));
          assertFalse(intent.hasExtra(CameraViewConfig.kSecretKey));
          assertEquals(1, intent.getIntExtra(CameraViewConfig.kMaxImageCount, 0));
          assertTrue(intent.getBooleanExtra(CameraViewConfig.kNeedPhotoConfirm, false));
          assertTrue(intent.getBooleanExtra(CameraViewConfig.kInterceptOnEmulator, false));
          assertEquals(CameraConfigMode.Photo.getValue(), intent.getIntExtra(CameraViewConfig.kCameraMode, -1));
          assertEquals("SYNTHETIC-TEMPLATE", intent.getStringExtra(CameraViewConfig.kGroupWatermarkId));
          java.util.HashMap<?, ?> actualFields = (java.util.HashMap<?, ?>) intent.getSerializableExtra(CameraViewConfig.kCustomInputItems);
          assertNotNull(actualFields);
          assertEquals(5, actualFields.size());
          for (String field : ConfiguredXheyAdapter.FIELDS) assertEquals(fields.getString(field), actualFields.get(field));
          assertFalse(actualFields.containsKey("时间"));
          assertFalse(actualFields.containsKey("地点"));
          assertEquals("file:///android_asset/idi-native-camera", intent.getStringExtra(CameraViewConfig.kResourceDir));
          JSONObject videoSession = new JSONObject(session.toString()).put("media_type", "video").put("max_record_seconds", 60);
          Intent videoIntent = ConfiguredXheyAdapter.intent(activity, videoSession, "synthetic-contract", false);
          assertEquals(CameraConfigMode.Video.getValue(), videoIntent.getIntExtra(CameraViewConfig.kCameraMode, -1));
          assertEquals(60, videoIntent.getIntExtra(CameraViewConfig.kMaxRecordDuration, -1));
          assertEquals(4_000_000, videoIntent.getIntExtra(CameraViewConfig.kVideoBitrate, -1));
          assertEquals(com.xhey.xheycamerasdk.camera.VideoResolution.P720.getValue(),
              videoIntent.getStringExtra(CameraViewConfig.kVideoResolution));
          assertEquals(activity.store.directory("synthetic-contract").getAbsolutePath(),
              videoIntent.getStringExtra(CameraViewConfig.kVideoSaveDir));
          assertFalse(videoIntent.hasExtra(CameraViewConfig.kSecretKey));
        } catch (Exception error) { throw new AssertionError(error); }
      });
    }
  }

  @Test public void nativeResultSavesExactBytesAndUnverifiedSdkMetadata() throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "测试部位", "原生回调合成测试", "").put("source", "xhey_sdk");
    store.create(meta, false);
    String id = meta.getString("capture_id");
    Bitmap bitmap = Bitmap.createBitmap(320, 240, Bitmap.Config.ARGB_8888);
    bitmap.eraseColor(android.graphics.Color.LTGRAY);
    ByteArrayOutputStream stream = new ByteArrayOutputStream();
    assertTrue(bitmap.compress(Bitmap.CompressFormat.JPEG, 95, stream)); bitmap.recycle();
    byte[] bytes = stream.toByteArray();
    String raw = new JSONObject().put("data", new JSONObject()
        .put("baseInfo", new JSONObject().put("time", "2026:10:20 11:12:13"))
        .put("watermarkContentExtension", new JSONObject().put("antiFakeCode", "SYNTHETIC-NOT-VERIFIED"))).toString();
    CapturedImage image = new CapturedImage(bytes, new UserComment(raw));
    CameraActivity.Result result = new CameraActivity.Result();
    result.success = true; result.capturedImages = new ArrayList<>(); result.capturedImages.add(image);
    assertSame(image, ConfiguredXheyAdapter.photo(result));
    NativeCameraActivity.persist(store, id, image);
    store.state(id, "local", "Synthetic contract test only; never upload");
    assertArrayEquals(bytes, Files.readAllBytes(store.original(id).toPath()));
    assertArrayEquals(bytes, Files.readAllBytes(store.stamped(id).toPath()));
    assertEquals("2026:10:20 11:12:13", store.item(id).meta.getString("sdk_photo_time"));
    assertEquals("SYNTHETIC-NOT-VERIFIED", store.item(id).meta.getString("sdk_anti_fake_code"));
    assertFalse(store.item(id).verified());
    result.success = false;
    assertThrows(Exception.class, () -> ConfiguredXheyAdapter.photo(result));
    result.success = true; result.capturedImages.add(image);
    assertThrows(Exception.class, () -> ConfiguredXheyAdapter.photo(result));
    result.capturedImages.clear(); result.error = new Error("Synthetic error");
    assertThrows(Exception.class, () -> ConfiguredXheyAdapter.photo(result));
  }

  @Test public void watermarkEditsKeepOneCaptureAndFixedCaseContext() throws Exception {
    CaptureStore store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "原部位", "原内容", "")
        .put("source", "xhey_sdk");
    store.create(meta, true);
    String id = meta.getString("capture_id"), key = "draft_" + meta.getString("case_id");
    String oldDraft = store.prefs.getString(key, null);
    store.state(id, "awaiting_sdk", "");
    int before = store.items().size();
    try {
      String content = "新内容：引号\"与 + 空格\n第二行";
      store.updateWatermark(id, new WatermarkEditor.Draft("work_checkin", "厨房", content, "door_checkin", true));
      CaptureStore.Item actual = store.item(id);
      assertEquals(before, store.items().size());
      assertEquals("awaiting_sdk", actual.state);
      assertFalse(store.recordItems().stream().anyMatch(item -> item.id.equals(id)));
      for (String fixed : new String[] {"capture_id", "case_id", "case_number", "project_name", "location", "captured_at", "source"})
        assertEquals(meta.getString(fixed), actual.meta.getString(fixed));
      assertEquals("work_checkin", actual.meta.getString("photo_group"));
      assertEquals("door_checkin", actual.meta.getString("role"));
      assertEquals("door_checkin", WatermarkEditor.Draft.from(actual.meta).role);
      assertEquals("厨房", actual.meta.getString("part"));
      assertEquals(content, actual.meta.getString("content"));
      assertEquals(content, new JSONObject(store.prefs.getString(key, "{}")).getString("content"));
      String previous = actual.meta.toString();
      assertThrows(Exception.class, () -> store.updateWatermark(id,
          new WatermarkEditor.Draft("work_checkin", "厨房", "有效内容", "", true)));
      assertEquals(previous, store.item(id).meta.toString());
      store.updateWatermark(id, new WatermarkEditor.Draft("completion", "客厅", "仅本张", "door_checkin", false));
      assertEquals("finished", store.item(id).meta.getString("role"));
      assertEquals("", WatermarkEditor.Draft.from(store.item(id).meta).role);
      assertEquals(content, new JSONObject(store.prefs.getString(key, "{}")).getString("content"));
      store.cancelEmptyOfficialCapture(id);
      assertFalse(store.recordItems().stream().anyMatch(item -> item.id.equals(id)));
    } finally {
      android.content.SharedPreferences.Editor prefs = store.prefs.edit();
      if (oldDraft == null) prefs.remove(key); else prefs.putString(key, oldDraft);
      prefs.commit();
    }
  }

  @Test public void watermarkEditingCannotChangeSavedOrPartiallySavedPhotos() throws Exception {
    CaptureStore store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    for (String file : new String[] {"original.jpg", "official.pending"}) {
      JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "部位", "已拍内容", "")
          .put("source", "xhey_sdk");
      store.create(meta, true);
      String id = meta.getString("capture_id");
      store.state(id, "awaiting_sdk", "");
      byte[] evidence = {1, 2, 3, 4};
      File saved = new File(store.directory(id), file);
      Files.write(saved.toPath(), evidence);
      assertThrows(Exception.class, () -> store.updateWatermark(id,
          new WatermarkEditor.Draft("completion", "其他", "不能改写", "", false)));
      assertEquals("已拍内容", store.item(id).meta.getString("content"));
      assertArrayEquals(evidence, Files.readAllBytes(saved.toPath()));
      store.state(id, "local", "Synthetic editing guard test; never upload");
    }
  }

  @Test public void launcherReentryKeepsOriginalActivityAndPendingCapture() throws Exception {
    CaptureStore store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "测试", "启动恢复测试", "")
        .put("source", "xhey_sdk");
    String id = meta.getString("capture_id");
    java.util.concurrent.atomic.AtomicReference<MainActivity> original = new java.util.concurrent.atomic.AtomicReference<>();
    try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
      store.create(meta, true);
      store.activeCaptures.add(id);
      store.state(id, "awaiting_sdk", "");
      store.prefs.edit().putString("pending_official_capture", id).commit();
      scenario.onActivity(activity -> {
        original.set(activity);
        activity.startActivity(new Intent(activity, MainActivity.class)
            .setAction(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER));
      });
      java.util.concurrent.atomic.AtomicBoolean returned = new java.util.concurrent.atomic.AtomicBoolean();
      long deadline = SystemClock.uptimeMillis() + 5000;
      do {
        InstrumentationRegistry.getInstrumentation().waitForIdleSync();
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> returned.set(
            ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(Stage.RESUMED).contains(original.get())));
        if (!returned.get()) Thread.sleep(100);
      } while (!returned.get() && SystemClock.uptimeMillis() < deadline);
      assertTrue("Launcher must return to the existing Activity", returned.get());
      assertEquals("awaiting_sdk", store.item(id).state);
      assertEquals(id, store.prefs.getString("pending_official_capture", ""));
    } finally {
      store.activeCaptures.remove(id);
      store.prefs.edit().remove("pending_official_capture").commit();
      if (store.item(id) != null) store.state(id, "local", "Synthetic recovery test; never upload");
    }
  }

  @Test public void coldRecoveryKeepsSavedBytesAndLiveSdkRequests() throws Exception {
    CaptureStore store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    ArrayList<String> ids = new ArrayList<>();
    Bitmap bitmap = Bitmap.createBitmap(24, 24, Bitmap.Config.ARGB_8888);
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    bitmap.compress(Bitmap.CompressFormat.JPEG, 95, out); bitmap.recycle();
    byte[] bytes = out.toByteArray();
    try {
      for (int i = 0; i < 4; i++) {
        JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "测试", "中断恢复测试", "")
            .put("source", "xhey_sdk");
        store.create(meta, true);
        String id = meta.getString("capture_id"); ids.add(id);
        store.state(id, "awaiting_sdk", "");
      }
      Files.write(store.original(ids.get(1)).toPath(), bytes);
      store.activeCaptures.add(ids.get(2));
      store.state(ids.get(3), "uploaded", "");
      store.prefs.edit().putString("pending_official_capture", ids.get(0)).commit();
      store.recoverAbandonedOfficialCaptures();
      assertEquals("cancelled", store.item(ids.get(0)).state);
      assertFalse(RecordsScreen.pending(store.item(ids.get(0))));
      assertFalse(store.recordItems().stream().anyMatch(item -> item.id.equals(ids.get(0))));
      assertEquals("awaiting_sdk", store.item(ids.get(1)).state);
      assertArrayEquals(bytes, Files.readAllBytes(store.original(ids.get(1)).toPath()));
      assertEquals("awaiting_sdk", store.item(ids.get(2)).state);
      assertEquals("uploaded", store.item(ids.get(3)).state);
      assertFalse(store.prefs.contains("pending_official_capture"));
    } finally {
      for (String id : ids) {
        store.activeCaptures.remove(id);
        store.state(id, "local", "Synthetic recovery test; never upload");
      }
      store.prefs.edit().remove("pending_official_capture").commit();
    }
  }

  @Test public void openingAndCancellingWithoutPhotoDoesNotAddARecord() throws Exception {
    CaptureStore store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    int before = store.recordItems().size();
    ArrayList<String> ids = new ArrayList<>();
    try {
      for (String source : new String[] {"xhey_sdk", "xhey_h5"}) {
        JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "测试", "未拍摄退出", "")
            .put("source", source);
        store.create(meta, true);
        String id = meta.getString("capture_id"); ids.add(id);
        store.state(id, "awaiting_sdk", "");
        store.prefs.edit().putString("pending_official_capture", id).commit();
        assertEquals(before, store.recordItems().size());
        store.cancelEmptyOfficialCapture(id);
        assertEquals("cancelled", store.item(id).state);
        assertFalse(store.prefs.contains("pending_official_capture"));
        assertFalse(RecordsScreen.pending(store.item(id)));
        assertFalse(RecordsScreen.failed(store.item(id)));
        assertEquals(before, store.recordItems().size());
        assertTrue("Internal session is retained without deleting files", store.directory(id).isDirectory());
      }
      // A network/configuration error before the SDK returns a photo is shown in
      // the camera screen, not misreported as a failed photo in the upload list.
      store.state(ids.get(0), "camera_error", "拍摄授权获取失败：测试网络错误");
      assertEquals(before, store.recordItems().size());
      store.cancelEmptyOfficialCapture(ids.get(0));
      assertEquals("cancelled", store.item(ids.get(0)).state);
    } finally {
      store.prefs.edit().remove("pending_official_capture").commit();
      for (String id : ids) store.cancelEmptyOfficialCapture(id);
    }
  }

  @Test public void legacyEmptyVisitsDisappearWhileRealPhotoFailuresRemain() throws Exception {
    CaptureStore store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    ArrayList<String> ids = new ArrayList<>();
    try {
      for (int i = 0; i < 7; i++) {
        JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "测试", "旧记录迁移", "")
            .put("source", "xhey_sdk");
        store.create(meta, true);
        String id = meta.getString("capture_id"); ids.add(id);
        store.state(id, "capture_failed", "官方拍摄已取消，未生成照片");
      }
      store.state(ids.get(1), "capture_failed", "上次官方拍摄已中断，未生成照片，请重新拍摄");
      byte[] preserved = new byte[] {1, 2, 3, 4};
      Files.write(store.original(ids.get(2)).toPath(), preserved);
      File partial = new File(store.directory(ids.get(3)), "official.pending");
      Files.write(partial.toPath(), preserved);
      store.state(ids.get(4), "capture_failed", "原片保存未完成，请检查手机空间后重试");
      store.state(ids.get(5), "failed", "连接中断，照片仍在手机");
      store.receipt(ids.get(6), "capture_failed", new JSONObject()
          .put("error", "官方拍摄已取消，未生成照片").put("photo_id", "synthetic-receipt"));
      store.recoverAbandonedOfficialCaptures();
      for (int i = 0; i < 2; i++) {
        String id = ids.get(i);
        assertEquals("cancelled", store.item(id).state);
        assertFalse(store.recordItems().stream().anyMatch(item -> item.id.equals(id)));
      }
      for (int i = 2; i < ids.size(); i++) {
        String id = ids.get(i);
        assertTrue("A photo/save/upload failure must remain visible",
            store.recordItems().stream().anyMatch(item -> item.id.equals(id)));
        store.cancelEmptyOfficialCapture(id);
        assertTrue(RecordsScreen.failed(store.item(id)));
      }
      assertArrayEquals(preserved, Files.readAllBytes(store.original(ids.get(2)).toPath()));
      assertArrayEquals(preserved, Files.readAllBytes(partial.toPath()));
    } finally {
      for (String id : ids) store.state(id, "local", "Synthetic record migration test; never upload");
    }
  }

  /** Explicit live load. Manual taps use the inspected current device screen. */
  @Test public void inspectLiveNativeCamera() throws Exception {
    assumeTrue("1".equals(InstrumentationRegistry.getArguments().getString("native_live", "")));
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    String server = InstrumentationRegistry.getArguments().getString("server", "http://127.0.0.1:8878");
    String code = InstrumentationRegistry.getArguments().getString("pair_code", "");
    assertEquals(10, code.length());
    JSONObject pair = CameraApi.request(server, "/api/camera/pair", "", new JSONObject().put("code", code));
    store.connect(server, pair);
    JSONObject cases = CameraApi.request(server, "/api/camera/cases", store.token(store.device()), null);
    store.replaceCases(cases.getJSONArray("items"));
    JSONObject meta = CameraData.metadata(store.cases().get(0), "construction", "测试部位", "原生 SDK 联调，非业务照片", "").put("source", "xhey_sdk");
    store.create(meta, false);
    String id = meta.getString("capture_id"); store.state(id, "awaiting_sdk", "");
    Intent launch = new Intent(context, NativeCameraActivity.class).putExtra("capture_id", id).putExtra("emulator_test", true);
    File output = new File(context.getFilesDir(), "native-verification"); output.mkdirs();
    try (ActivityScenario<NativeCameraActivity> scenario = ActivityScenario.launch(launch)) {
      Thread.sleep(25000);
      screenshot(new File(output, "live-native.png"));
      Files.write(new File(output, "live-state.json").toPath(), new JSONObject()
          .put("capture_id", id).put("state", store.item(id).state)
          .put("scope", "synthetic_case_on_emulator; local_verification_disabled").toString(2).getBytes(StandardCharsets.UTF_8));
      long deadline = SystemClock.uptimeMillis() + 180000;
      while (!store.original(id).isFile() && SystemClock.uptimeMillis() < deadline) Thread.sleep(500);
      if (store.original(id).isFile()) {
        Thread.sleep(5000);
        Files.copy(store.original(id).toPath(), new File(output, "live-original.jpg").toPath(), StandardCopyOption.REPLACE_EXISTING);
        CaptureStore.Item item = store.item(id);
        Files.write(new File(output, "live-result.json").toPath(), new JSONObject()
            .put("capture_id", id).put("state", item.state).put("metadata", item.meta)
            .put("receipt", item.receipt).put("original_hash", OfficialPhotoWriter.sha256(store.original(id)))
            .toString(2).getBytes(StandardCharsets.UTF_8));
        assertFalse(item.verified());
      }
      assertFalse("Native SDK failed to load", store.item(id).state.equals("capture_failed")
          || store.item(id).state.equals("camera_error"));
    }
  }

  private static void screenshot(File file) throws Exception {
    Bitmap image = InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
    assertNotNull(image);
    try (FileOutputStream out = new FileOutputStream(file)) { image.compress(Bitmap.CompressFormat.PNG, 100, out); }
    image.recycle();
  }
}
