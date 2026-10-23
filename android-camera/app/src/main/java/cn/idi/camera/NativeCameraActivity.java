package cn.idi.camera;

import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.view.View;
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

/** Saves each SDK photo before opening the next capture in the same field session. */
public final class NativeCameraActivity extends ComponentActivity {
  CaptureStore store;
  String captureId, sessionId;
  TextView status, retry;
  boolean launched, saving, ended, requesting, pendingPhotoContinuation, showEditor = true;
  final android.os.Handler launchHandler = new android.os.Handler(android.os.Looper.getMainLooper());
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
    LinearLayout root = ui.column();
    root.setBackgroundColor(Color.rgb(23, 28, 34));
    LinearLayout header = ui.row();
    header.addView(ui.icon("back", "返回案件", Color.WHITE, this::cancel));
    ui.weight(header, ui.text("今日原生相机", 20, Color.WHITE, true));
    root.addView(header);
    status = ui.text("正在准备官方拍摄…", 16, Color.WHITE, false);
    ui.pad(status, 20);
    root.addView(status);
    retry = ui.button("重新打开官方相机", true, this::open);
    retry.setVisibility(View.GONE);
    root.addView(retry);
    setContentView(root);
    ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
      androidx.core.graphics.Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars());
      view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
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
    status.setText("正在获取本次拍摄授权…");
    store.state(captureId, "awaiting_sdk", "");
    store.activeCaptures.add(captureId);
    CaptureStore.Item item = store.item(captureId);
    final String id = captureId;
    final boolean edit = showEditor;
    CaptureStore.io.execute(() -> {
      try {
        JSONObject session = CameraApi.request(item.server, "/api/camera/native-session", store.token(item.device), item.meta);
        if (item.video() && !"video".equals(session.optString("media_type")))
          throw new Exception("电脑端尚未支持录像，请更新工作台后重试");
        Intent intent = ConfiguredXheyAdapter.intent(this, session, id, getIntent().getBooleanExtra("emulator_test", false));
        intent.putExtra(EditableCameraActivity.SHOW_EDITOR, edit);
        runOnUiThread(() -> launchAfterRelease(intent, android.os.SystemClock.uptimeMillis() + 10000));
      } catch (Exception | LinkageError error) {
        runOnUiThread(() -> {
          requesting = false;
          if (!ended && !isDestroyed()) fail("拍摄授权获取失败：" + UploadWorker.safeMessage(error));
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
      status.setText("请在官方相机中核对水印并拍摄");
      camera.launch(intent);
    } catch (RuntimeException | LinkageError error) {
      launched = false;
      fail("官方相机启动失败，请更新安装包后重试");
    }
  }

  void accept(CameraActivity.Result result, CameraActivity vendor) {
    if (saving || ended || isDestroyed()) return;
    final CapturedImage image;
    try { image = ConfiguredXheyAdapter.photo(result); }
    catch (Exception error) {
      if (vendor != null) vendor.dismiss();
      fail("官方拍摄已取消或未返回完整照片，请重新拍摄");
      return;
    }
    saving = true;
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
          if (vendor != null && !vendor.isDestroyed() && launched) {
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
    launchHandler.removeCallbacksAndMessages(null);
    EventBus.unregisterEvent(EventBus.EventName.kCameraActivityWillDismiss, listener);
    if (store != null && captureId != null && !saving) store.activeCaptures.remove(captureId);
    super.onDestroy();
  }
}
