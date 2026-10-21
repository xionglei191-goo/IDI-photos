package cn.idi.camera;

import android.Manifest;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;
import androidx.core.content.ContextCompat;
import org.json.JSONObject;

/** A brief connection/error surface; a ready camera opens without an extra tap. */
final class OfficialCameraScreen {
  final MainActivity a;
  final Ui u;
  boolean autoOpen = true;
  TextView status, capture;
  JSONObject config;

  OfficialCameraScreen(MainActivity activity) {
    a = activity;
    u = a.ui;
  }

  View view() {
    LinearLayout page = u.column();
    u.pad(page, 20);
    LinearLayout header = u.row();
    header.addView(u.icon("back", "返回案件", Color.WHITE, () -> a.show("cases")));
    u.weight(header, u.text("今日官方拍摄", 22, Color.WHITE, true));
    header.addView(u.icon("records", "查看上传记录", Color.WHITE, () -> a.show("records")));
    page.addView(header);
    u.gap(page, 24);
    status = u.text("正在打开相机预览…", 17, Color.WHITE, false);
    page.addView(status, new LinearLayout.LayoutParams(-1, 0, 1));
    capture = u.button("重试打开相机", true, this::check);
    capture.setVisibility(View.GONE);
    page.addView(capture);
    u.gap(page, 16);
    check();
    return page;
  }

  void maybeStart() {
    if (!autoOpen || a.busy || config == null || !config.optBoolean("ready")
        || !a.getLifecycle().getCurrentState().isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) return;
    autoOpen = false;
    start();
  }

  void check() {
    capture.setEnabled(false);
    capture.setAlpha(.45f);
    autoOpen = true;
    capture.setVisibility(View.GONE);
    config = null;
    String reason = ConfiguredXheyAdapter.unavailableReason();
    if (!reason.isEmpty()) {
      status.setText(reason);
      capture.setVisibility(View.VISIBLE);
      return;
    }
    if (!a.store.connected()) {
      status.setText("请先在案件页连接并同步这台手机");
      return;
    }
    status.setText("正在检查电脑端授权…");
    String device = a.store.device(), server = a.store.server();
    a.io.execute(
        () -> {
          try {
            JSONObject result =
                CameraApi.request(server, "/api/camera/native-config", a.store.token(device), null);
            a.runOnUiThread(
                () -> {
                  if (a.isDestroyed()
                      || a.officialScreen != this
                      || !device.equals(a.store.device())) return;
                  config = result;
                  capture.setEnabled(true);
                  capture.setAlpha(1f);
                  status.setText(result.optBoolean("ready") ? "正在打开相机预览…"
                      : result.optString("message", "电脑端授权未就绪"));
                  if (result.optBoolean("ready")) maybeStart();
                  else capture.setVisibility(View.VISIBLE);
                });
          } catch (Exception error) {
            a.runOnUiThread(
                () -> {
                  if (!a.isDestroyed() && a.officialScreen == this) {
                    status.setText("无法读取官方授权：" + UploadWorker.safeMessage(error));
                    capture.setEnabled(true); capture.setAlpha(1f); capture.setVisibility(View.VISIBLE);
                  }
                });
          }
        });
  }

  void start() {
    if (a.busy || config == null || !config.optBoolean("ready")) return;
    if (a.content.trim().isEmpty() || a.group.endsWith("checkin") && a.checkinRole.isEmpty()) {
      capture.setEnabled(true); capture.setAlpha(1f); capture.setVisibility(View.VISIBLE);
      WatermarkEditor.show(a, this::start);
      return;
    }
    if (ContextCompat.checkSelfPermission(a, Manifest.permission.CAMERA)
            != PackageManager.PERMISSION_GRANTED
        || ContextCompat.checkSelfPermission(a, Manifest.permission.ACCESS_FINE_LOCATION)
            != PackageManager.PERMISSION_GRANTED) {
      capture.setEnabled(true); capture.setAlpha(1f); capture.setVisibility(View.VISIBLE);
      a.sdkPermissions.launch(
          new String[] {
            Manifest.permission.CAMERA,
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION
          });
      return;
    }
    String id = null;
    try {
      JSONObject meta = a.metadata().put("source", "xhey_sdk");
      id = meta.getString("capture_id");
      android.content.Intent intent = new android.content.Intent(a, NativeCameraActivity.class).putExtra("capture_id", id);
      a.store.create(meta, false);
      a.store.state(id, "awaiting_sdk", "");
      if (!a.store.prefs.edit().putString("pending_official_capture", id).commit())
        throw new Exception("拍摄上下文保存失败");
      a.busy = true;
      a.officialCapture.launch(intent);
    } catch (Exception | LinkageError error) {
      a.busy = false;
      if (id != null) a.store.state(id, "camera_error", "SDK 未启动，未生成照片");
      a.store.prefs.edit().remove("pending_official_capture").apply();
      status.setText("今日相机无法启动，请检查连接并重试");
      capture.setEnabled(true); capture.setAlpha(1f); capture.setVisibility(View.VISIBLE);
    }
  }
}
