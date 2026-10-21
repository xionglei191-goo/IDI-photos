package cn.idi.camera;

import android.Manifest;
import android.content.pm.PackageManager;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.view.Gravity;
import android.view.HapticFeedbackConstants;
import android.view.OrientationEventListener;
import android.view.Surface;
import android.view.View;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import androidx.camera.core.AspectRatio;
import androidx.camera.core.Camera;
import androidx.camera.core.CameraSelector;
import androidx.camera.core.ImageCapture;
import androidx.camera.core.ImageCaptureException;
import androidx.camera.core.Preview;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;
import androidx.core.content.ContextCompat;
import com.google.common.util.concurrent.ListenableFuture;
import java.util.List;
import org.json.JSONObject;

final class CameraScreen {
  final MainActivity a;
  final Ui u;
  boolean closed;
  ProcessCameraProvider provider;
  ImageCapture capture;
  PreviewView preview;
  TextView time, watermark, footer, partButton, hint;
  ImageView thumbnail;
  View shutter, flash;
  LinearLayout modes;
  String lastThumbnail = "";
  boolean flashOn;
  OrientationEventListener orientation;
  int photoRotation = Surface.ROTATION_0;

  CameraScreen(MainActivity a) {
    this.a = a;
    u = a.ui;
  }

  View view() {
    LinearLayout page = u.column();
    LinearLayout header = u.row();
    header.setPadding(u.dp(6), u.dp(6), u.dp(6), u.dp(6));
    header.addView(u.icon("back", "返回案件", Color.WHITE, () -> a.show("cases")));
    LinearLayout identity = u.column();
    identity.addView(
        u.text(
            a.selected.optString("project_name") + " · " + a.selected.optString("location"),
            16,
            Color.WHITE,
            true));
    u.gap(identity, 5);
    identity.addView(
        u.text(a.selected.optString("case_number"), 12, Color.rgb(186, 195, 204), false));
    u.weight(header, identity);
    TextView change = u.button("切换", false, () -> a.show("cases"));
    change.setTextSize(13);
    change.setTextColor(Color.WHITE);
    u.ripple(change, Ui.DARK, Color.rgb(96, 109, 120), 9);
    header.addView(change);
    page.addView(header);
    FrameLayout image = new FrameLayout(a);
    preview = new PreviewView(a);
    preview.setImplementationMode(PreviewView.ImplementationMode.COMPATIBLE);
    preview.setScaleType(PreviewView.ScaleType.FILL_CENTER);
    image.addView(preview, new FrameLayout.LayoutParams(-1, -1));
    hint = u.text("正在启动相机…", 16, Color.WHITE, false);
    hint.setGravity(Gravity.CENTER);
    image.addView(hint, new FrameLayout.LayoutParams(-1, -1));
    LinearLayout box = u.column();
    box.setPadding(u.dp(12), u.dp(6), u.dp(12), u.dp(10));
    u.surface(box, Color.argb(185, 20, 27, 34), Color.TRANSPARENT, 10);
    LinearLayout top = u.row();
    time = u.text("", 21, Color.WHITE, true);
    u.weight(top, time);
    TextView edit = u.button("改水印", false, () -> WatermarkEditor.show(a));
    edit.setTextSize(13);
    edit.setTextColor(Color.WHITE);
    u.ripple(edit, Color.TRANSPARENT, Color.rgb(156, 165, 173), 8);
    top.addView(edit);
    box.addView(top);
    watermark = u.text("", 14, Color.WHITE, false);
    watermark.setLineSpacing(u.dp(3), 1);
    box.addView(watermark);
    FrameLayout.LayoutParams watermarkParams =
        new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM | Gravity.START);
    watermarkParams.setMargins(u.dp(14), 0, u.dp(14), u.dp(14));
    image.addView(box, watermarkParams);
    page.addView(image, new LinearLayout.LayoutParams(-1, 0, 1));
    partButton = u.button("", false, () -> WatermarkEditor.show(a));
    partButton.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
    partButton.setTextSize(14);
    partButton.setTextColor(Color.WHITE);
    partButton.setMinHeight(u.dp(44));
    u.ripple(partButton, Ui.DARK, Color.TRANSPARENT, 0);
    page.addView(partButton);
    modes = u.row();
    page.addView(modes);
    refreshModes();
    LinearLayout actions = u.row();
    actions.setGravity(Gravity.CENTER);
    actions.setPadding(u.dp(24), u.dp(8), u.dp(24), u.dp(4));
    LinearLayout album = u.column();
    album.setGravity(Gravity.CENTER);
    thumbnail = new ImageView(a);
    thumbnail.setScaleType(ImageView.ScaleType.CENTER_CROP);
    u.surface(thumbnail, Color.rgb(55, 68, 78), Color.rgb(133, 145, 154), 10);
    thumbnail.setClipToOutline(true);
    album.addView(thumbnail, new LinearLayout.LayoutParams(u.dp(52), u.dp(52)));
    u.gap(album, 4);
    album.addView(u.text("本机照片", 12, Color.WHITE, false));
    album.setContentDescription("查看本机照片");
    album.setOnClickListener(v -> a.show("records"));
    actions.addView(album, new LinearLayout.LayoutParams(0, -2, 1));
    FrameLayout shoot = new FrameLayout(a);
    shutter = new View(a);
    u.ripple(shutter, Color.WHITE, Ui.ORANGE, 50);
    shutter.setContentDescription("拍照");
    shutter.setFocusable(true);
    shutter.setOnClickListener(v -> takePicture());
    FrameLayout.LayoutParams shotParams =
        new FrameLayout.LayoutParams(u.dp(78), u.dp(78), Gravity.CENTER);
    shoot.addView(shutter, shotParams);
    actions.addView(shoot, new LinearLayout.LayoutParams(0, u.dp(88), 1));
    LinearLayout flashlight = u.column();
    flashlight.setGravity(Gravity.CENTER);
    flash = u.icon("flash", "切换闪光灯", Color.WHITE, () -> toggleFlash());
    flashlight.addView(flash);
    TextView flashLabel = u.text("闪光灯", 12, Color.WHITE, false);
    flashlight.addView(flashLabel);
    actions.addView(flashlight, new LinearLayout.LayoutParams(0, -2, 1));
    page.addView(actions);
    footer = u.text("", 12, Color.rgb(182, 193, 202), false);
    footer.setGravity(Gravity.CENTER);
    footer.setPadding(0, u.dp(2), 0, u.dp(10));
    page.addView(footer);
    orientation =
        new OrientationEventListener(a) {
          @Override
          public void onOrientationChanged(int degrees) {
            if (degrees == ORIENTATION_UNKNOWN) return;
            photoRotation =
                degrees >= 45 && degrees < 135
                    ? Surface.ROTATION_270
                    : degrees >= 135 && degrees < 225
                        ? Surface.ROTATION_180
                        : degrees >= 225 && degrees < 315
                            ? Surface.ROTATION_90
                            : Surface.ROTATION_0;
          }
        };
    if (orientation.canDetectOrientation()) orientation.enable();
    tick();
    if (ContextCompat.checkSelfPermission(a, Manifest.permission.CAMERA)
        == PackageManager.PERMISSION_GRANTED) bind();
    else {
      hint.setText("允许相机权限后即可拍摄");
      hint.setOnClickListener(v -> a.permission.launch(Manifest.permission.CAMERA));
      a.permission.launch(Manifest.permission.CAMERA);
    }
    return page;
  }

  void refreshModes() {
    modes.removeAllViews();
    for (int i = 0; i < CameraData.GROUPS.length; i++) {
      String id = CameraData.GROUPS[i];
      boolean selected = id.equals(a.group);
      TextView mode =
          u.text(
              CameraData.SHORT_LABELS[i],
              12,
              selected ? Color.rgb(255, 114, 65) : Color.rgb(201, 209, 217),
              selected);
      mode.setGravity(Gravity.CENTER);
      mode.setMinHeight(u.dp(48));
      u.ripple(mode, selected ? Color.rgb(49, 31, 27) : Ui.DARK, Color.TRANSPARENT, 6);
      mode.setContentDescription(CameraData.LABELS[i]);
      mode.setFocusable(true);
      mode.setOnClickListener(
          v -> {
            a.group = id;
            a.saveDraft();
            refreshModes();
            tick();
            if (id.endsWith("checkin")) WatermarkEditor.show(a);
          });
      modes.addView(mode, new LinearLayout.LayoutParams(0, -2, 1));
    }
  }

  void bind() {
    if (closed) return;
    ListenableFuture<ProcessCameraProvider> future = ProcessCameraProvider.getInstance(a);
    future.addListener(
        () -> {
          if (closed) return;
          try {
            provider = future.get();
            provider.unbindAll();
            Preview usePreview =
                new Preview.Builder().setTargetAspectRatio(AspectRatio.RATIO_4_3).build();
            usePreview.setSurfaceProvider(preview.getSurfaceProvider());
            capture =
                new ImageCapture.Builder()
                    .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                    .setTargetAspectRatio(AspectRatio.RATIO_4_3)
                    .build();
            Camera camera =
                provider.bindToLifecycle(
                    a, CameraSelector.DEFAULT_BACK_CAMERA, usePreview, capture);
            hint.setVisibility(View.GONE);
            flash.setEnabled(camera.getCameraInfo().hasFlashUnit());
            flash.setAlpha(camera.getCameraInfo().hasFlashUnit() ? 1 : .35f);
            preview.setOnTouchListener(
                (v, event) -> {
                  if (event.getAction() == android.view.MotionEvent.ACTION_UP) {
                    camera
                        .getCameraControl()
                        .startFocusAndMetering(
                            new androidx.camera.core.FocusMeteringAction.Builder(
                                    preview
                                        .getMeteringPointFactory()
                                        .createPoint(event.getX(), event.getY()))
                                .build());
                    v.performClick();
                  }
                  return true;
                });
          } catch (Exception error) {
            hint.setText("相机启动失败，点这里重试\n" + UploadWorker.safeMessage(error));
            hint.setOnClickListener(v -> bind());
          }
        },
        ContextCompat.getMainExecutor(a));
  }

  void toggleFlash() {
    if (capture == null) return;
    flashOn = !flashOn;
    capture.setFlashMode(flashOn ? ImageCapture.FLASH_MODE_ON : ImageCapture.FLASH_MODE_OFF);
    a.toast(flashOn ? "闪光灯已开启" : "闪光灯已关闭");
  }

  void tick() {
    if (closed || watermark == null) return;
    try {
      String[] lines = CameraData.watermark(a.metadata());
      time.setText(lines[0]);
      watermark.setText(String.join("\n", java.util.Arrays.copyOfRange(lines, 1, lines.length)));
    } catch (Exception ignored) {
    }
    partButton.setText(
        "维修部位  "
            + a.part
            + (a.group.endsWith("checkin")
                ? " · "
                    + ("building_checkin".equals(a.checkinRole)
                        ? "楼栋"
                        : "door_checkin".equals(a.checkinRole) ? "入户门" : "请选择打卡用途")
                : "")
            + "   ›");
    List<CaptureStore.Item> items = a.store.items();
    int pending = 0;
    for (CaptureStore.Item item : items)
      if (!item.demo() && !item.state.equals("uploaded")) pending++;
    boolean saving = a.busy || !a.store.activeCaptures.isEmpty();
    footer.setText(saving ? "正在保存照片…" : a.demo ? "示例模式 · 照片仅存手机" : "已存手机 · " + pending + " 张待处理");
    shutter.setEnabled(!saving);
    shutter.setAlpha(saving ? .4f : 1);
    if (!items.isEmpty()) {
      CaptureStore.Item latest = items.get(0);
      if (!lastThumbnail.equals(latest.id) && a.store.thumbnail(latest.id).isFile()) {
        thumbnail.setImageBitmap(BitmapFactory.decodeFile(a.store.thumbnail(latest.id).getPath()));
        lastThumbnail = latest.id;
      }
    }
  }

  void takePicture() {
    if (a.busy || !a.store.activeCaptures.isEmpty()) return;
    if (capture == null) {
      a.toast("相机尚未就绪，请允许相机权限");
      return;
    }
    if (a.content.trim().isEmpty() || a.group.endsWith("checkin") && a.checkinRole.isEmpty()) {
      a.toast("请先填写内容并选择打卡用途");
      WatermarkEditor.show(a);
      return;
    }
    String pendingId = null;
    try {
      JSONObject meta = a.metadata();
      a.store.create(meta, a.demo);
      String id = meta.getString("capture_id");
      pendingId = id;
      boolean demoCapture = a.demo, reuse = a.repeat;
      String selectedId = a.selected.optString("id");
      a.busy = true;
      tick();
      shutter.performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY);
      capture.setTargetRotation(photoRotation);
      ImageCapture.OutputFileOptions options =
          new ImageCapture.OutputFileOptions.Builder(a.store.original(id)).build();
      a.store.activeCaptures.add(id);
      capture.takePicture(
          options,
          a.io,
          new ImageCapture.OnImageSavedCallback() {
            @Override
            public void onImageSaved(ImageCapture.OutputFileResults results) {
              a.store.state(id, "rendering", "");
              try {
                WatermarkRenderer.render(
                    a.store.original(id), a.store.stamped(id), a.store.thumbnail(id), meta);
                a.store.state(id, demoCapture ? "local" : "queued", "");
                if (!demoCapture) UploadWorker.enqueue(a, id, false);
                a.store.activeCaptures.remove(id);
                a.runOnUiThread(
                    () -> {
                      a.busy = false;
                      if (!reuse
                          && a.selected != null
                          && selectedId.equals(a.selected.optString("id"))) a.loadDraft();
                      tick();
                    });
              } catch (Exception error) {
                failed(error);
              }
            }

            @Override
            public void onError(ImageCaptureException error) {
              failed(error);
            }

            private void failed(Exception error) {
              a.store.state(id, "capture_failed", UploadWorker.safeMessage(error));
              a.store.activeCaptures.remove(id);
              a.runOnUiThread(
                  () -> {
                    a.busy = false;
                    tick();
                    a.toast("拍摄保存失败：" + UploadWorker.safeMessage(error));
                  });
            }
          });
    } catch (Exception error) {
      if (pendingId != null) {
        a.store.activeCaptures.remove(pendingId);
        a.store.state(pendingId, "capture_failed", UploadWorker.safeMessage(error));
      }
      a.busy = false;
      a.toast("无法保存：" + UploadWorker.safeMessage(error));
      tick();
    }
  }

  void close() {
    closed = true;
    if (orientation != null) orientation.disable();
    if (provider != null) provider.unbindAll();
  }
}
