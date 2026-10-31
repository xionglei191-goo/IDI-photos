package cn.idi.camera;

import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
import android.view.Gravity;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import androidx.activity.ComponentActivity;
import androidx.activity.OnBackPressedCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import com.xhey.xheycamerasdk.CameraActivity;
import com.xhey.xheycamerasdk.CapturedImage;
import com.xhey.xheycamerasdk.EventBus;
import com.xhey.xheycamerasdk.recorder.VideoInfo;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import org.json.JSONObject;

/** Saves each SDK photo before resuming the same preview with a new capture identity. */
public final class NativeCameraActivity extends ComponentActivity {
  CaptureStore store;
  String captureId, sessionId;
  TextView status, retry;
  FrameLayout root;
  android.graphics.Bitmap transitionFrame;
  FrameLayout transitionCover;
  LinearLayout feedback;
  boolean launched, saving, ended, requesting, pendingPhotoContinuation, showEditor = true;
  final android.os.Handler launchHandler = new android.os.Handler(android.os.Looper.getMainLooper());
  final Runnable slowPreparation = () -> { if (!launched && !ended && !isDestroyed()) feedback.setVisibility(View.VISIBLE); };
  ActivityResultLauncher<Intent> camera;
  final EventBus.EventBusDelegate listener = (name, payload) -> {
    if (payload instanceof EventBus.EventData.CameraActivityWillDismiss event
        && event.cameraActivity != null
        && captureId != null
        && captureId.equals(event.cameraActivity.getIntent().getStringExtra(ConfiguredXheyAdapter.CAPTURE_ID)))
      runOnUiThread(() -> accept(event.result, event.cameraActivity));
  };

  @Override public void onCreate(Bundle state) {
    super.onCreate(state);
    WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
    store = CaptureStore.get(this);
    sessionId = getIntent().getStringExtra("capture_id");
    captureId = store.prefs.getString("native_series_" + sessionId,
        state == null ? sessionId : state.getString("capture_id", sessionId));
    CaptureStore.Item item = captureId == null ? null : store.item(captureId);
    if (item == null || !"xhey_sdk".equals(item.meta.optString("source"))) { finish(); return; }
    store.activeCaptures.add(captureId);
    ConfiguredXheyAdapter.silenceVendorLogs();
    EventBus.registerEvent(EventBus.EventName.kCameraActivityWillDismiss, listener);
    camera = registerForActivityResult(new ActivityResultContracts.StartActivityForResult(), result -> {
      launched = false;
      if (ended) return;
      if (pendingPhotoContinuation) {
        pendingPhotoContinuation = false;
        saving = false;
        continuePhotos(captureId);
        return;
      }
      if (saving) return;
      if (store.original(captureId).isFile()) { recover(); return; }
      if (result.getResultCode() == EditableCameraActivity.WATERMARK_EDITED
          && result.getData() != null
          && captureId.equals(result.getData().getStringExtra("capture_id"))) {
        showEditor = false;
        open();
        return;
      }
      if (result.getResultCode() == RESULT_OK && store.item(captureId).video())
        acceptVideo(CameraActivity.getRecordingResult());
      else if (result.getResultCode() == RESULT_OK) accept(CameraActivity.getResult(), null);
      else cancel();
    });
    Ui ui = new Ui(this);
    root = new FrameLayout(this);
    root.setBackgroundColor(Color.TRANSPARENT);
    feedback = ui.column();
    ui.pad(feedback, 16);
    ui.surface(feedback, Color.WHITE, Ui.BORDER, 16);
    feedback.setVisibility(View.GONE);
    LinearLayout header = ui.row();
    header.addView(ui.icon("back", "返回案件", Ui.MUTED, this::cancel));
    ui.weight(header, ui.text("拍摄", 20, Color.BLACK, true));
    feedback.addView(header);
    status = ui.text("正在准备相机…", 16, Ui.MUTED, false);
    ui.pad(status, 12);
    feedback.addView(status);
    retry = ui.button("重新打开官方相机", true, this::open);
    retry.setVisibility(View.GONE);
    feedback.addView(retry);
    FrameLayout.LayoutParams panel = new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM);
    panel.setMargins(ui.dp(16), ui.dp(16), ui.dp(16), ui.dp(24));
    root.addView(feedback, panel);
    setContentView(root);
    CameraPreviewTransition.attach(this);
    ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
      androidx.core.graphics.Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars());
      if (transitionFrame == null) view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
      return insets;
    });
    getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
      @Override public void handleOnBackPressed() { cancel(); }
    });
    launched = state != null && state.getBoolean("launched")
        && captureId.equals(state.getString("capture_id", sessionId));
    showEditor = captureId.equals(sessionId) && (state == null || state.getBoolean("show_editor", true));
    if (!launched) open();
  }

  private void open() {
    if (saving || ended || requesting || launched) return;
    if (store.original(captureId).isFile()) { recover(); return; }
    String problem = ConfiguredXheyAdapter.unavailableReason();
    if (!problem.isEmpty()) { fail(problem); return; }
    requesting = true;
    retry.setVisibility(View.GONE);
    status.setText("正在准备相机…");
    prepareFeedback();
    store.state(captureId, "awaiting_sdk", "");
    store.activeCaptures.add(captureId);
    CaptureStore.Item item = store.item(captureId);
    final String id = captureId;
    final boolean edit = showEditor;
    CameraLaunch.io.execute(() -> {
      try {
        JSONObject session = NativeAuthorization.session(store, item);
        if (item.video() && !"video".equals(session.optString("media_type")))
          throw new Exception("电脑端尚未支持录像，请更新工作台后重试");
        Intent intent = ConfiguredXheyAdapter.intent(this, session, id, getIntent().getBooleanExtra("emulator_test", false));
        intent.putExtra(EditableCameraActivity.SHOW_EDITOR, edit);
        runOnUiThread(() -> {
          if (ended || isDestroyed() || !id.equals(captureId)) return;
          launchAfterRelease(intent, android.os.SystemClock.uptimeMillis() + 10000);
        });
      } catch (Exception | LinkageError error) {
        runOnUiThread(() -> {
          if (!id.equals(captureId)) return;
          requesting = false;
          if (!ended && !isDestroyed()) fail("相机准备失败：" + UploadWorker.safeMessage(error));
        });
      }
    });
  }

  private void launchAfterRelease(Intent intent, long deadline) {
    if (ended || isDestroyed()) { requesting = false; return; }
    // Activity results arrive before the old activity's onDestroy. The SDK releases
    // shared native resources there; starting another camera earlier can crash JNI.
    if (!EditableCameraActivity.released()) {
      if (android.os.SystemClock.uptimeMillis() >= deadline) {
        requesting = false;
        fail("上次相机尚未关闭完成，请稍候重试");
      } else {
        status.setText("正在切换相机…");
        launchHandler.postDelayed(() -> launchAfterRelease(intent, deadline), 100);
      }
      return;
    }
    requesting = false;
    try {
      launched = true;
      launchHandler.removeCallbacks(slowPreparation);
      feedback.setVisibility(View.GONE);
      camera.launch(intent, androidx.core.app.ActivityOptionsCompat.makeCustomAnimation(this, 0, 0));
    } catch (RuntimeException | LinkageError error) {
      launched = false;
      fail("官方相机启动失败，请更新安装包后重试");
    }
  }

  void accept(CameraActivity.Result result, CameraActivity vendor) {
    if (saving || ended || isDestroyed()) return;
    if (vendor instanceof EditableCameraActivity editable && editable.continuingPhoto) return;
    // The SDK reuses its Result object. Ignore an already consumed notification.
    if (vendor != null && result != null && !result.success && result.error == null
        && result.capturedImages == null) return;
    final CapturedImage image;
    try { image = ConfiguredXheyAdapter.photo(result); }
    catch (Exception error) {
      if (vendor != null) vendor.dismiss();
      fail("官方拍摄已取消或未返回完整照片，请重新拍摄");
      return;
    }
    saving = true;
    if (vendor instanceof EditableCameraActivity editable) editable.beginPhotoSave();
    final String id = captureId;
    store.activeCaptures.add(captureId);
    store.state(captureId, "capturing", "");
    status.setText("正在保存官方原片…");
    CaptureStore.io.execute(() -> {
      try {
        persist(store, id, image);
        store.state(id, "queued", "");
        UploadWorker.enqueue(getApplicationContext(), id, false);
        runOnUiThread(() -> {
          if (vendor instanceof EditableCameraActivity editable
              && !editable.isFinishing() && !editable.isDestroyed() && launched) {
            continuePhotosInPlace(id, result, editable);
          } else if (vendor != null && !vendor.isDestroyed() && launched) {
            // Consume this SDK result before another capture can reuse its static slot.
            CameraActivity.getResult();
            pendingPhotoContinuation = true;
            vendor.dismiss();
          } else {
            saving = false;
            continuePhotos(id);
          }
        });
      } catch (Exception error) {
        store.state(id, store.original(id).isFile() ? "rendering" : "capture_failed",
            "原片保存未完成，请检查手机空间后重试");
        runOnUiThread(() -> {
          saving = false;
          if (vendor != null && !vendor.isFinishing() && !vendor.isDestroyed()) {
            new android.app.AlertDialog.Builder(vendor).setTitle("原片尚未保存")
                .setMessage("请释放手机空间后重试。退出前请确认已有原片是否保留。")
                .setPositiveButton("重试保存", (dialog, which) -> accept(result, vendor))
                .setNegativeButton("返回记录页", (dialog, which) -> { vendor.dismiss(); cancel(); })
                .setCancelable(false).show();
          } else fail("原片保存未完成，请返回记录页核对");
        });
      } finally { store.activeCaptures.remove(id); }
    });
  }

  void acceptVideo(CameraActivity.RecordingResult result) {
    if (saving || ended || isDestroyed()) return;
    final VideoInfo video;
    try { video = ConfiguredXheyAdapter.video(result); }
    catch (Exception error) { fail(error.getMessage()); return; }
    saving = true;
    final String id = captureId;
    store.activeCaptures.add(captureId);
    store.state(captureId, "capturing", "");
    status.setText("正在保存视频原片…");
    prepareFeedback();
    CaptureStore.io.execute(() -> {
      try {
        OfficialVideoWriter.save(store, id, video);
        store.state(id, "queued", "");
        UploadWorker.enqueue(getApplicationContext(), id, false);
        runOnUiThread(this::complete);
      } catch (Exception error) {
        store.state(id, "capture_failed", "视频保存未完成：" + UploadWorker.safeMessage(error));
        runOnUiThread(() -> {
          saving = false;
          status.setText("视频文件已保留，请释放手机空间后重试保存");
          retry.setText("重试保存视频");
          retry.setOnClickListener(view -> recover());
          retry.setVisibility(View.VISIBLE);
          showProblem();
        });
      } finally { store.activeCaptures.remove(id); }
    });
  }

  static void persist(CaptureStore store, String id, CapturedImage image) throws Exception {
    String comment = ConfiguredXheyAdapter.comment(image);
    File sidecar = new File(store.directory(id), "native-user-comment.json");
    File pending = new File(store.directory(id), "native-user-comment.pending");
    try (FileOutputStream out = new FileOutputStream(pending)) {
      out.write(comment.getBytes(StandardCharsets.UTF_8));
      out.getFD().sync();
    }
    Files.move(pending.toPath(), sidecar.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    store.sdkInfo(id, comment);
    OfficialPhotoWriter.save(store, id, image.getImageData());
  }

  private void recover() {
    if (saving || ended) return;
    saving = true;
    status.setText("正在恢复已保存原片…");
    prepareFeedback();
    final String id = captureId;
    store.activeCaptures.add(captureId);
    CaptureStore.io.execute(() -> {
      try {
        if (store.item(id).video()) OfficialVideoWriter.recover(store, id);
        else OfficialPhotoWriter.recover(store, id);
        CaptureStore.Item item = store.item(id);
        if (item.state.equals("awaiting_sdk") || item.state.equals("capturing")
            || item.state.equals("rendering") || item.state.equals("capture_failed"))
          store.state(id, "queued", "");
        UploadWorker.pending(getApplicationContext());
        runOnUiThread(() -> {
          saving = false;
          if (item.video()) complete(); else continuePhotos(id);
        });
      } catch (Exception error) {
        runOnUiThread(() -> { saving = false; fail("原片已保留，预览恢复失败，请返回记录页核对"); });
      } finally { store.activeCaptures.remove(id); }
    });
  }

  private void continuePhotos(String savedId) {
    if (ended || isDestroyed() || !savedId.equals(captureId)) return;
    try {
      captureId = store.nextOfficialPhoto(savedId, sessionId);
      store.activeCaptures.remove(savedId);
      store.activeCaptures.add(captureId);
      showEditor = false;
      status.setText("原片已保存，正在准备下一张…");
      Toast.makeText(this, "已保存，继续拍下一张", Toast.LENGTH_SHORT).show();
      open();
    } catch (Exception error) {
      fail("原片已保存，下一张准备失败：" + UploadWorker.safeMessage(error));
    }
  }

  private void continuePhotosInPlace(String savedId, CameraActivity.Result result, EditableCameraActivity preview) {
    if (ended || isDestroyed() || preview.isFinishing() || preview.isDestroyed()) { saving = false; return; }
    try {
      if (savedId.equals(captureId)) {
        captureId = store.nextOfficialPhoto(savedId, sessionId);
        store.activeCaptures.remove(savedId);
        store.activeCaptures.add(captureId);
      }
      // Keep the same validity checks as opening a new SDK session, without
      // closing its camera or replacing its already loaded watermark page.
      NativeAuthorization.session(store, store.item(captureId));
      showEditor = false;
      preview.continuePhoto(savedId, captureId);
      // Do not call getResult(): it nulls the static holder that the next SDK
      // callback writes into. Release only this photo's bytes after durable save.
      result.capturedImages = null;
      result.success = false;
      saving = false;
    } catch (Exception error) {
      saving = false;
      new android.app.AlertDialog.Builder(preview).setTitle("照片已保存")
          .setMessage("下一张准备失败：" + UploadWorker.safeMessage(error))
          .setPositiveButton("重试", (dialog, which) -> continuePhotosInPlace(savedId, result, preview))
          .setNegativeButton("返回案件", (dialog, which) -> { preview.finish(); cancel(); })
          .setCancelable(false).show();
    }
  }

  private void fail(String message) {
    if (ended || isDestroyed()) return;
    if (!store.original(captureId).isFile()) {
      CaptureStore.Item item = store.item(captureId);
      boolean beforePhoto = item != null
          && (item.state.equals("awaiting_sdk") || item.state.equals("camera_error"));
      store.state(captureId, beforePhoto ? "camera_error" : "capture_failed", message);
    }
    store.activeCaptures.remove(captureId);
    status.setText(message);
    retry.setText(store.original(captureId).isFile() ? "恢复已保存原片" : "重新打开官方相机");
    retry.setVisibility(View.VISIBLE);
    showProblem();
  }

  private void prepareFeedback() {
    if (transitionFrame == null) root.setBackgroundColor(Color.TRANSPARENT);
    feedback.setVisibility(View.GONE);
    launchHandler.removeCallbacks(slowPreparation);
    launchHandler.postDelayed(slowPreparation, transitionFrame == null ? 800 : 8000);
  }

  void showTransition(android.graphics.Bitmap frame) {
    clearTransition();
    transitionFrame = frame;
    root.setPadding(0, 0, 0, 0);
    root.setBackgroundColor(Color.BLACK);
    transitionCover = CameraPreviewTransition.cover(this, frame, "正在切换相机…");
    root.addView(transitionCover, 0, new FrameLayout.LayoutParams(-1, -1));
    feedback.setVisibility(View.GONE);
  }

  void clearTransition() {
    if (transitionCover != null) root.removeView(transitionCover);
    transitionCover = null;
    transitionFrame = null;
    if (root != null) {
      root.setBackgroundColor(Color.TRANSPARENT);
      ViewCompat.requestApplyInsets(root);
    }
  }

  private void showProblem() {
    launchHandler.removeCallbacks(slowPreparation);
    root.setBackgroundColor(0x66000000);
    feedback.setVisibility(View.VISIBLE);
  }

  private void complete() {
    saving = false;
    ended = true;
    store.prefs.edit().remove("native_series_" + sessionId).commit();
    if (!isDestroyed()) {
      setResult(RESULT_OK, new Intent().putExtra("capture_id", captureId));
      finish();
    }
  }

  private void cancel() {
    if (saving) { Toast.makeText(this, "正在保存原片，请稍候", Toast.LENGTH_SHORT).show(); return; }
    if (ended) return;
    ended = true;
    store.prefs.edit().remove("native_series_" + sessionId).commit();
    store.cancelEmptyOfficialCapture(captureId);
    setResult(store.original(captureId).isFile() ? RESULT_OK : RESULT_CANCELED,
        new Intent().putExtra("capture_id", captureId));
    finish();
  }

  @Override protected void onSaveInstanceState(Bundle out) {
    out.putString("capture_id", captureId);
    out.putBoolean("launched", launched);
    out.putBoolean("show_editor", showEditor);
    super.onSaveInstanceState(out);
  }

  @Override protected void onDestroy() {
    CameraPreviewTransition.detach(this);
    clearTransition();
    launchHandler.removeCallbacksAndMessages(null);
    EventBus.unregisterEvent(EventBus.EventName.kCameraActivityWillDismiss, listener);
    if (store != null && captureId != null && !saving) store.activeCaptures.remove(captureId);
    super.onDestroy();
  }
}
