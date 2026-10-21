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
  FrameLayout overlay;
  View watermarkTarget;
  LinearLayout modes;
  Dialog editor;
  boolean resumed, initialEditorShown;
  final Runnable probe = this::probeWatermark;

  @Override public void onCreate(Bundle state) {
    super.onCreate(state);
    activeInstances++;
    store = CaptureStore.get(this);
    captureId = getIntent().getStringExtra(ConfiguredXheyAdapter.CAPTURE_ID);
    if (captureId == null || store.item(captureId) == null) { finish(); return; }
    initialEditorShown = state != null ? state.getBoolean("editor_shown")
        : !getIntent().getBooleanExtra(SHOW_EDITOR, true);
    renderer = findRenderer(getWindow().getDecorView());
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
    addContentView(overlay, new ViewGroup.LayoutParams(-1, -1));
    ViewCompat.setOnApplyWindowInsetsListener(overlay, (view, insets) -> {
      FrameLayout.LayoutParams params = (FrameLayout.LayoutParams) modes.getLayoutParams();
      params.topMargin = insets.getInsets(WindowInsetsCompat.Type.statusBars()).top + ui.dp(8);
      modes.setLayoutParams(params);
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
    if (renderer == null) renderer = findRenderer(getWindow().getDecorView());
    String url = renderer == null ? null : renderer.getUrl();
    if (renderer != null && url != null && url.startsWith("file:///android_asset/XheyCameraSDKAssets/index.html")) {
      renderer.evaluateJavascript(WATERMARK_BOUNDS, result -> {
        if (!resumed || isFinishing() || isDestroyed()) return;
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
          modes.setVisibility(View.VISIBLE);
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
    if (renderer == null) return;
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
            store.updateWatermark(captureId, draft);
            boolean changed = !draft.group.equals(item.meta.optString("photo_group"))
                || !draft.content.equals(item.meta.optString("content"))
                || !CameraData.role(draft.group, draft.role).equals(item.meta.optString("role"));
            if (changed) {
              // Reopen through the public signed-session contract so pixels and metadata agree.
              // Keep this capture ID; editing never creates another photo record.
              setResult(WATERMARK_EDITED, new Intent().putExtra("capture_id", captureId));
              finish();
            }
          }, () -> editor = null);
    } catch (Exception error) {
      Toast.makeText(this, "水印设置暂时无法打开，请返回后重试", Toast.LENGTH_LONG).show();
    }
  }

  private void changeMode(boolean video) {
    if (renderer == null) return;
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
    try {
      store.updateMediaType(captureId, video);
      if (video) Toast.makeText(this, "每段最长60秒，点击红色按钮开始或结束录像", Toast.LENGTH_LONG).show();
      setResult(WATERMARK_EDITED, new Intent().putExtra("capture_id", captureId));
      finish();
    } catch (Exception error) {
      Toast.makeText(this, error.getMessage(), Toast.LENGTH_LONG).show();
    }
  }

  @Override public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
    super.onRequestPermissionsResult(code, permissions, results);
    if (code == AUDIO_REQUEST) {
      if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) changeMode(true);
      else Toast.makeText(this, "录像需要麦克风权限；拍照仍可正常使用", Toast.LENGTH_LONG).show();
    }
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
    if (editor != null) editor.dismiss();
    try { super.onDestroy(); }
    finally { activeInstances--; }
  }
}
