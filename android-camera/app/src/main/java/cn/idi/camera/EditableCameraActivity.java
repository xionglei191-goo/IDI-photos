package cn.idi.camera;

import android.app.Dialog;
import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.ViewGroup;
import android.view.Gravity;
import android.webkit.WebView;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import com.xhey.xheycamerasdk.CameraActivity;
import org.json.JSONObject;

/** Adds an accessible edit target over the official renderer's visible watermark. */
public final class EditableCameraActivity extends CameraActivity {
  static final int WATERMARK_EDITED = RESULT_FIRST_USER + 43;
  static final String SHOW_EDITOR = "idi_show_watermark_editor";
  static final String VIDEO_ENABLED = "idi_video_enabled";
  private static final int AUDIO_REQUEST = 9044;
  private static int activeInstances;
  static boolean released() { return activeInstances == 0; }
  // Read geometry only. The SDK's HTML, model, time, location and JPEG stay untouched.
  static final String WATERMARK_BOUNDS = "(() => {"
      + "const w=document.querySelector('.watermark-container');"
      + "if(!w||document.getElementById('photo-confirm-component')"
      + "||document.querySelector('.recording-time'))return null;"
      + "const r=w.getBoundingClientRect();"
      + "if(r.width<10||r.height<10||getComputedStyle(w).visibility==='hidden')return null;"
      + "const top=document.elementFromPoint(Math.max(0,Math.min(innerWidth-1,r.x+r.width/2)),"
      + "Math.max(0,Math.min(innerHeight-1,r.y+r.height/2)));"
      + "if(!top||!w.contains(top))return null;"
      + "return {x:r.x,y:r.y,width:r.width,height:r.height,viewport:innerWidth};})()";
  final Handler handler = new Handler(Looper.getMainLooper());
  CaptureStore store;
  String captureId;
  WebView renderer;
  VideoCaptureTime videoTime;
  FrameLayout overlay;
  View watermarkTarget;
  LinearLayout modes;
  TextView preparation;
  Dialog editor;
  View photoSaveBlocker;
  FrameLayout transitionCover;
  boolean resumed, initialEditorShown, savingPhoto, continuingPhoto, changingConfiguration, checkingPreviewFrame;
  android.window.OnBackInvokedCallback gestureBack;
  final Runnable continuationTimeout = () -> continuationFailed();
  final Runnable probe = this::probeWatermark;

  @Override public void onCreate(Bundle state) {
    super.onCreate(state);
    activeInstances++;
    store = CaptureStore.get(this);
    captureId = getIntent().getStringExtra(ConfiguredXheyAdapter.CAPTURE_ID);
    if (captureId == null || store.item(captureId) == null) { finish(); return; }
    if (android.os.Build.VERSION.SDK_INT >= 33) {
      gestureBack = this::handleCameraBack;
      getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
          android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, gestureBack);
    }
    initialEditorShown = state != null ? state.getBoolean("editor_shown")
        : !getIntent().getBooleanExtra(SHOW_EDITOR, true);
    renderer = findRenderer(getWindow().getDecorView());
    if (store.item(captureId).video()) videoTime = new VideoCaptureTime(store, captureId);
    overlay = new FrameLayout(this);
    overlay.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
    watermarkTarget = new View(this);
    watermarkTarget.setBackgroundColor(Color.TRANSPARENT);
    watermarkTarget.setContentDescription("点击水印修改");
    watermarkTarget.setFocusable(true);
    watermarkTarget.setOnClickListener(view -> editWatermark());
    watermarkTarget.setVisibility(View.GONE);
    overlay.addView(watermarkTarget, new FrameLayout.LayoutParams(1, 1));
    Ui ui = new Ui(this);
    preparation = ui.text("正在准备水印…", 14, Ui.MUTED, false);
    ui.pad(preparation, 12);
    ui.surface(preparation, Color.WHITE, Ui.BORDER, 12);
    FrameLayout.LayoutParams preparationParams = new FrameLayout.LayoutParams(-2, -2, Gravity.TOP | Gravity.CENTER_HORIZONTAL);
    preparationParams.topMargin = ui.dp(12);
    overlay.addView(preparation, preparationParams);
    modes = ui.row();
    ui.surface(modes, Color.WHITE, Ui.BORDER, 12);
    boolean video = store.item(captureId).video();
    for (int i = 0; i < 2; i++) {
      boolean selectVideo = i == 1;
      TextView button = ui.chip(selectVideo ? "录像" : "拍照", video == selectVideo,
          () -> changeMode(selectVideo));
      button.setContentDescription(selectVideo ? "切换到录像" : "切换到拍照");
      modes.addView(button, new LinearLayout.LayoutParams(ui.dp(68), ui.dp(44)));
    }
    FrameLayout.LayoutParams modeParams = new FrameLayout.LayoutParams(-2, -2, Gravity.TOP | Gravity.CENTER_HORIZONTAL);
    modeParams.topMargin = ui.dp(12);
    modes.setVisibility(View.GONE);
    overlay.addView(modes, modeParams);
    photoSaveBlocker = new View(this);
    photoSaveBlocker.setClickable(true);
    photoSaveBlocker.setContentDescription("正在保存照片");
    photoSaveBlocker.setVisibility(View.GONE);
    overlay.addView(photoSaveBlocker, new FrameLayout.LayoutParams(-1, -1));
    android.graphics.Bitmap previousFrame = CameraPreviewTransition.frame(captureId);
    if (previousFrame != null) {
      transitionCover = CameraPreviewTransition.cover(this, previousFrame,
          video ? "正在切换到录像…" : "正在准备拍照预览…");
      overlay.addView(transitionCover, new FrameLayout.LayoutParams(-1, -1));
    }
    addContentView(overlay, new ViewGroup.LayoutParams(-1, -1));
    ViewCompat.setOnApplyWindowInsetsListener(overlay, (view, insets) -> {
      FrameLayout.LayoutParams params = (FrameLayout.LayoutParams) modes.getLayoutParams();
      params.topMargin = insets.getInsets(WindowInsetsCompat.Type.statusBars()).top + ui.dp(8);
      modes.setLayoutParams(params);
      FrameLayout.LayoutParams progressParams = (FrameLayout.LayoutParams) preparation.getLayoutParams();
      progressParams.topMargin = params.topMargin;
      preparation.setLayoutParams(progressParams);
      return insets;
    });
    ViewCompat.requestApplyInsets(overlay);
  }

  private static WebView findRenderer(View view) {
    if (view instanceof WebView web) return web;
    if (view instanceof ViewGroup group)
      for (int i = 0; i < group.getChildCount(); i++) {
        WebView web = findRenderer(group.getChildAt(i));
        if (web != null) return web;
      }
    return null;
  }

  private void probeWatermark() {
    if (!resumed || isFinishing() || isDestroyed() || overlay == null) return;
    if (savingPhoto) { handler.postDelayed(probe, 250); return; }
    if (renderer == null) renderer = findRenderer(getWindow().getDecorView());
    String url = renderer == null ? null : renderer.getUrl();
    if (renderer != null && VideoCaptureTime.sdkPage(url)) {
      if (videoTime != null) videoTime.probe(renderer);
      renderer.evaluateJavascript(WATERMARK_BOUNDS, result -> {
        if (!resumed || savingPhoto || isFinishing() || isDestroyed()) return;
        try {
          JSONObject bounds = new JSONObject(result);
          float scale = renderer.getWidth() / (float) bounds.getDouble("viewport");
          int[] web = new int[2], layer = new int[2];
          renderer.getLocationOnScreen(web); overlay.getLocationOnScreen(layer);
          FrameLayout.LayoutParams params = new FrameLayout.LayoutParams(
              Math.max(1, (int) Math.ceil(bounds.getDouble("width") * scale)),
              Math.max(1, (int) Math.ceil(bounds.getDouble("height") * scale)));
          params.leftMargin = web[0] - layer[0] + (int) (bounds.getDouble("x") * scale);
          params.topMargin = web[1] - layer[1] + (int) (bounds.getDouble("y") * scale);
          watermarkTarget.setLayoutParams(params);
          watermarkTarget.setVisibility(View.VISIBLE);
          preparation.setVisibility(View.GONE);
          modes.setVisibility(View.VISIBLE);
          revealPreview();
          if (continuingPhoto) {
            continuingPhoto = false;
            handler.removeCallbacks(continuationTimeout);
            photoSaveBlocker.setVisibility(View.GONE);
          }
          if (!initialEditorShown) {
            initialEditorShown = true;
            editWatermark();
          }
        } catch (Exception notVisible) {
          watermarkTarget.setVisibility(View.GONE);
          modes.setVisibility(View.GONE);
        }
      });
    }
    handler.postDelayed(probe, 250);
  }

  void editWatermark() {
    if (renderer == null || savingPhoto || continuingPhoto || changingConfiguration) return;
    renderer.evaluateJavascript("Boolean(document.querySelector('.recording-time'))", active -> {
      if ("false".equals(active) && resumed && !isFinishing() && !isDestroyed()) showWatermarkEditor();
    });
  }

  private void showWatermarkEditor() {
    if (editor != null || isFinishing() || isDestroyed()) return;
    CaptureStore.Item item = store.item(captureId);
    if (item == null || !item.state.equals("awaiting_sdk") || store.original(captureId).exists()) return;
    try {
      JSONObject caseInfo = new JSONObject().put("id", item.meta.getString("case_id"))
          .put("case_number", item.meta.getString("case_number"))
          .put("project_name", item.meta.getString("project_name"))
          .put("location", item.meta.getString("location"))
          .put("media_type", item.video() ? "video" : "image");
      for (JSONObject cached : store.cases())
        if (cached.optString("id").equals(caseInfo.optString("id")))
          caseInfo.put("parts", cached.optJSONArray("parts"));
      editor = WatermarkEditor.show(this, caseInfo, WatermarkEditor.Draft.from(item.meta),
          draft -> {
            boolean changed = !draft.group.equals(item.meta.optString("photo_group"))
                || !draft.content.equals(item.meta.optString("content"))
                || !CameraData.role(draft.group, draft.role).equals(item.meta.optString("role"));
            if (changed) {
              // Reopen through the public signed-session contract so pixels and metadata agree.
              // Keep this capture ID; editing never creates another photo record.
              changeConfiguration(() -> store.updateWatermark(captureId, draft));
            } else store.updateWatermark(captureId, draft);
          }, () -> editor = null);
    } catch (Exception error) {
      Toast.makeText(this, "水印设置暂时无法打开，请返回后重试", Toast.LENGTH_LONG).show();
    }
  }

  private void changeMode(boolean video) {
    if (renderer == null || savingPhoto || continuingPhoto || changingConfiguration) return;
    renderer.evaluateJavascript("Boolean(document.querySelector('.recording-time'))", active -> {
      if ("false".equals(active) && resumed && !isFinishing() && !isDestroyed()) changeIdleMode(video);
    });
  }

  private void changeIdleMode(boolean video) {
    CaptureStore.Item item = store.item(captureId);
    if (item == null || item.video() == video || editor != null) return;
    if (video && !getIntent().getBooleanExtra(VIDEO_ENABLED, false)) {
      Toast.makeText(this, "请先更新电脑端工作台，再使用录像", Toast.LENGTH_LONG).show();
      return;
    }
    if (video && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
      requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, AUDIO_REQUEST);
      return;
    }
    changeConfiguration(() -> store.updateMediaType(captureId, video));
  }

  private interface ConfigurationChange { void apply() throws Exception; }

  private void changeConfiguration(ConfigurationChange change) {
    if (changingConfiguration || savingPhoto || continuingPhoto) return;
    changingConfiguration = true;
    photoSaveBlocker.setVisibility(View.VISIBLE);
    // Defer one turn so the watermark dialog has closed before taking the cover.
    handler.post(() -> CameraPreviewTransition.capture(this, () -> {
      if (isFinishing() || isDestroyed()) return;
      try {
        change.apply();
        setResult(WATERMARK_EDITED, new Intent().putExtra("capture_id", captureId));
        finish();
        overridePendingTransition(0, 0);
      } catch (Exception error) { configurationFailed(error.getMessage()); }
    }, () -> configurationFailed("当前预览尚未就绪，请稍候重试")));
  }

  private void configurationFailed(String message) {
    if (isFinishing() || isDestroyed()) return;
    changingConfiguration = false;
    photoSaveBlocker.setVisibility(View.GONE);
    CameraPreviewTransition.clear(captureId);
    Toast.makeText(this, message, Toast.LENGTH_LONG).show();
  }

  private void revealPreview() {
    if (transitionCover == null || checkingPreviewFrame || !resumed) return;
    android.view.SurfaceView surface = CameraPreviewTransition.surface(getWindow().getDecorView());
    if (surface == null || !surface.getHolder().getSurface().isValid()) return;
    checkingPreviewFrame = true;
    android.graphics.Bitmap sample = android.graphics.Bitmap.createBitmap(8, 8, android.graphics.Bitmap.Config.ARGB_8888);
    android.view.PixelCopy.request(surface, sample, code -> {
      sample.recycle(); checkingPreviewFrame = false;
      if (code == android.view.PixelCopy.SUCCESS && resumed && !isFinishing() && !isDestroyed()) {
        overlay.removeView(transitionCover);
        transitionCover = null;
        CameraPreviewTransition.clear(captureId);
      }
    }, handler);
  }

  @Override public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
    super.onRequestPermissionsResult(code, permissions, results);
    if (code == AUDIO_REQUEST) {
      if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) changeMode(true);
      else Toast.makeText(this, "录像需要麦克风权限；拍照仍可正常使用", Toast.LENGTH_LONG).show();
    }
  }

  void beginPhotoSave() {
    savingPhoto = true;
    photoSaveBlocker.setVisibility(View.VISIBLE);
    watermarkTarget.setVisibility(View.GONE);
    modes.setVisibility(View.GONE);
    preparation.setText("正在保存照片…");
    preparation.setVisibility(View.VISIBLE);
  }

  void continuePhoto(String savedId, String nextId) throws Exception {
    if (!savedId.equals(captureId) && !nextId.equals(captureId))
      throw new Exception("当前拍摄编号已变化，请返回后重试");
    if (renderer == null || !VideoCaptureTime.sdkPage(renderer.getUrl()))
      throw new Exception("相机预览暂不可用，请返回后重试");
    captureId = nextId;
    getIntent().putExtra(ConfiguredXheyAdapter.CAPTURE_ID, nextId).putExtra(SHOW_EDITOR, false);
    initialEditorShown = true;
    savingPhoto = false;
    continuingPhoto = true;
    preparation.setText("照片已保存，正在恢复预览…");
    restartPhotoPreview();
  }

  private void restartPhotoPreview() {
    if (isFinishing() || isDestroyed()) return;
    continuingPhoto = true;
    photoSaveBlocker.setVisibility(View.VISIBLE);
    handler.removeCallbacks(continuationTimeout);
    handler.postDelayed(continuationTimeout, 10000);
    // The pinned vendor renderer handles startPreview by clearing its previous
    // image list and resuming its camera. Keep the exact signed URL and assets;
    // do not modify its Vue model, watermark, time, security code or image bytes.
    renderer.evaluateJavascript("(() => { if (!window.isVueAppLoaded) return false;"
        + "window.postMessage({action:'startPreview',url:window.location.href}, '*'); return true; })()", value -> {
      if (!"true".equals(value)) continuationFailed();
    });
  }

  private void continuationFailed() {
    if (!continuingPhoto || isFinishing() || isDestroyed()) return;
    continuingPhoto = false;
    handler.removeCallbacks(continuationTimeout);
    new android.app.AlertDialog.Builder(this).setTitle("照片已保存")
        .setMessage("预览尚未恢复，可以重试或返回案件。已保存照片会继续上传。")
        .setPositiveButton("重试预览", (dialog, which) -> restartPhotoPreview())
        .setNegativeButton("返回案件", (dialog, which) -> { setResult(RESULT_CANCELED); finish(); })
        .setCancelable(false).show();
  }

  // The vendor base is Activity, not ComponentActivity. API 33+ uses the
  // callback registered above; retain the vendor's legacy path on older phones.
  @android.annotation.SuppressLint("GestureBackNavigation")
  @Override public void onBackPressed() { handleCameraBack(); }

  private void handleCameraBack() {
    if (savingPhoto || changingConfiguration) {
      Toast.makeText(this, savingPhoto ? "正在保存照片，请稍候" : "正在切换相机，请稍候", Toast.LENGTH_SHORT).show();
      return;
    }
    super.onBackPressed();
  }

  @Override public void onResume() {
    super.onResume(); resumed = true;
    handler.removeCallbacks(probe); handler.post(probe);
  }

  @Override public void onPause() {
    resumed = false; handler.removeCallbacks(probe);
    super.onPause();
  }

  @Override protected void onSaveInstanceState(Bundle state) {
    state.putBoolean("editor_shown", initialEditorShown);
    super.onSaveInstanceState(state);
  }

  @Override public void onDestroy() {
    handler.removeCallbacksAndMessages(null);
    if (android.os.Build.VERSION.SDK_INT >= 33 && gestureBack != null)
      getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback(gestureBack);
    if (videoTime != null) videoTime.close();
    if (editor != null) editor.dismiss();
    try { super.onDestroy(); }
    finally { activeInstances--; }
  }
}
