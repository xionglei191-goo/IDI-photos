package cn.idi.camera;

import android.Manifest;
import android.app.AlertDialog;
import android.content.pm.PackageManager;
import androidx.core.content.ContextCompat;
import org.json.JSONObject;

/** Launches from the current page; only missing preparation or errors need a dialog. */
final class OfficialCameraScreen {
  final MainActivity a;
  boolean autoOpen = true, closed;
  JSONObject config;
  AlertDialog prompt;

  OfficialCameraScreen(MainActivity activity) { a = activity; }

  void maybeStart() {
    if (closed || !autoOpen || a.busy || config == null || !config.optBoolean("ready")
        || !a.getLifecycle().getCurrentState().isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) return;
    autoOpen = false;
    start();
  }

  void check() {
    if (closed || a.busy) return;
    dismissPrompt();
    autoOpen = true;
    config = null;
    String reason = ConfiguredXheyAdapter.unavailableReason();
    if (!reason.isEmpty()) { problem(reason); return; }
    if (!a.store.connected()) { problem("请先在案件页连接并同步这台手机"); return; }
    config = NativeAuthorization.cached(a.store);
    if (config != null) maybeStart();
    else {
      prompt = new AlertDialog.Builder(a).setTitle("准备拍摄")
          .setMessage("正在同步拍摄准备信息…")
          .setNegativeButton("取消", (dialog, which) -> a.show("cases"))
          .setOnCancelListener(dialog -> a.show("cases")).show();
      a.sync();
    }
  }

  void synchronizedCases(String message) {
    if (closed || a.busy) return;
    config = NativeAuthorization.cached(a.store);
    if (config == null) problem(message);
    else { dismissPrompt(); maybeStart(); }
  }

  void permissionDenied() {
    problem("今日官方拍摄需要相机和精确位置权限，请允许后再拍摄");
  }

  void start() {
    if (closed || a.busy || config == null || !config.optBoolean("ready")) return;
    if (a.content.trim().isEmpty() || a.group.endsWith("checkin") && a.checkinRole.isEmpty()) {
      WatermarkEditor.show(a, this::start);
      return;
    }
    if (ContextCompat.checkSelfPermission(a, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED
        || ContextCompat.checkSelfPermission(a, Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
      a.sdkPermissions.launch(new String[] {Manifest.permission.CAMERA,
          Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION});
      return;
    }
    String id = null;
    try {
      JSONObject meta = a.metadata().put("source", "xhey_sdk");
      id = meta.getString("capture_id");
      android.content.Intent intent = new android.content.Intent(a, NativeCameraActivity.class).putExtra("capture_id", id);
      // This test override cannot disable vendor checks in a release build.
      intent.putExtra("emulator_test", BuildConfig.DEBUG && a.getIntent().getBooleanExtra("emulator_test", false));
      a.store.create(meta, false);
      a.store.state(id, "awaiting_sdk", "");
      if (!a.store.prefs.edit().putString("pending_official_capture", id).commit())
        throw new Exception("拍摄上下文保存失败");
      dismissPrompt();
      a.busy = true;
      a.officialCapture.launch(intent);
    } catch (Exception | LinkageError error) {
      a.busy = false;
      if (id != null) a.store.state(id, "camera_error", "SDK 未启动，未生成照片");
      a.store.prefs.edit().remove("pending_official_capture").apply();
      problem("今日相机无法启动，请检查连接并重试");
    }
  }

  private void problem(String message) {
    if (closed || a.isFinishing() || a.isDestroyed()) return;
    autoOpen = false;
    dismissPrompt();
    prompt = new AlertDialog.Builder(a).setTitle("暂时无法拍摄").setMessage(message)
        .setPositiveButton("重试", (dialog, which) -> check())
        .setNegativeButton("返回案件", (dialog, which) -> a.show("cases"))
        .setOnCancelListener(dialog -> a.show("cases")).show();
  }

  private void dismissPrompt() {
    if (prompt != null) { prompt.dismiss(); prompt = null; }
  }

  void close() { closed = true; autoOpen = false; dismissPrompt(); }
}
