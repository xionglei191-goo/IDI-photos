package cn.idi.camera;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.ConsoleMessage;
import android.webkit.GeolocationPermissions;
import android.webkit.PermissionRequest;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import androidx.activity.ComponentActivity;
import androidx.activity.OnBackPressedCallback;
import androidx.core.content.ContextCompat;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.webkit.JavaScriptReplyProxy;
import androidx.webkit.WebViewAssetLoader;
import androidx.webkit.WebViewClientCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import androidx.webkit.WebResourceErrorCompat;
import java.io.ByteArrayInputStream;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Collections;
import org.json.JSONObject;

/** Official H5 in a secure origin; only the bundled main frame can reach the byte bridge. */
public final class H5CameraActivity extends ComponentActivity {
  static final String LOCAL_ORIGIN = "https://appassets.androidplatform.net";
  static final String VENDOR_ORIGIN = "https://sdk-h5.xhey.top";
  static final String PAGE = LOCAL_ORIGIN + "/assets/h5-camera/index.html";
  final Handler handler = new Handler(Looper.getMainLooper());
  CaptureStore store;
  String captureId;
  WebView web;
  LinearLayout root;
  TextView status, retry;
  H5PhotoTransfer transfer;
  String userComment = "{}";
  boolean saving, ended, sessionRequested;

  static String unavailableReason() {
    try {
      return WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)
          ? "" : "请更新手机的 Android System WebView 后再使用官方相机";
    } catch (RuntimeException error) {
      return "手机的 WebView 组件不可用，请更新后再试";
    }
  }

  static boolean vendorOrigin(Uri uri) {
    return "https".equals(uri.getScheme()) && "sdk-h5.xhey.top".equals(uri.getHost())
        && (uri.getPort() == -1 || uri.getPort() == 443) && uri.getUserInfo() == null;
  }

  @Override public void onCreate(Bundle state) {
    super.onCreate(state);
    WindowCompat.setDecorFitsSystemWindows(getWindow(), false);
    store = CaptureStore.get(this);
    captureId = getIntent().getStringExtra("capture_id");
    CaptureStore.Item item = captureId == null ? null : store.item(captureId);
    if (item == null || !"xhey_h5".equals(item.meta.optString("source"))) { finish(); return; }
    Ui ui = new Ui(this);
    root = ui.column();
    root.setBackgroundColor(Color.rgb(23, 28, 34));
    LinearLayout header = ui.row();
    header.addView(ui.icon("back", "返回案件", Color.WHITE, this::cancel));
    ui.weight(header, ui.text("今日官方相机", 19, Color.WHITE, true));
    root.addView(header);
    status = ui.text("正在准备官方拍摄…", 15, Color.WHITE, false);
    ui.pad(status, 16);
    root.addView(status);
    retry = ui.button("重新连接官方相机", true, this::open);
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
    if (store.original(captureId).isFile()) { recover(); return; }
    open();
  }

  private void open() {
    if (saving || ended) return;
    String problem = unavailableReason();
    if (!problem.isEmpty()) { fail(problem); return; }
    closeWeb();
    transfer = null;
    sessionRequested = false;
    store.state(captureId, "awaiting_sdk", "");
    store.activeCaptures.add(captureId);
    retry.setVisibility(View.GONE);
    status.setVisibility(View.VISIBLE);
    status.setText("正在连接今日官方相机…");
    web = new WebView(this);
    WebSettings settings = web.getSettings();
    settings.setJavaScriptEnabled(true);
    settings.setDomStorageEnabled(true);
    settings.setGeolocationEnabled(true);
    settings.setAllowFileAccess(false);
    settings.setAllowContentAccess(false);
    settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
    settings.setJavaScriptCanOpenWindowsAutomatically(false);
    settings.setSupportMultipleWindows(false);
    settings.setSafeBrowsingEnabled(true);
    final WebView current = web;
    final WebViewAssetLoader assets = new WebViewAssetLoader.Builder()
        .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this)).build();
    web.setWebViewClient(new WebViewClientCompat() {
      @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
        Uri url = request.getUrl();
        if (LOCAL_ORIGIN.equals(url.getScheme() + "://" + url.getAuthority())) {
          WebResourceResponse local = assets.shouldInterceptRequest(url);
          return local != null ? local : blocked();
        }
        // Every remote resource stays HTTPS. Native device tokens never enter this WebView.
        if (!"https".equals(url.getScheme())) return blocked();
        return null;
      }
      @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
        return request.isForMainFrame() ? !PAGE.equals(request.getUrl().toString()) : !vendorOrigin(request.getUrl());
      }
      @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
        handler.cancel();
        fail("官方相机安全连接失败，请检查网络与手机日期后重试");
      }
      @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceErrorCompat error) {
        if (request.isForMainFrame() || vendorOrigin(request.getUrl()) && "/".equals(request.getUrl().getPath()))
          fail("官方相机页面加载失败，请检查网络后重试");
      }
      @Override public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
        fail("相机网页已中断，已保存原片会保留；请重新连接");
        closeWeb();
        return true;
      }
    });
    web.setWebChromeClient(new WebChromeClient() {
      @Override public boolean onConsoleMessage(ConsoleMessage message) {
        return true; // The vendor wrapper logs its signed URL. Never forward it to logcat.
      }
      @Override public void onPermissionRequest(PermissionRequest request) {
        boolean camera = vendorOrigin(request.getOrigin())
            && ContextCompat.checkSelfPermission(H5CameraActivity.this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
        for (String resource : request.getResources())
          if (camera && PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(resource)) {
            request.grant(new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});
            return;
          }
        request.deny();
      }
      @Override public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
        boolean allowed = vendorOrigin(Uri.parse(origin))
            && ContextCompat.checkSelfPermission(H5CameraActivity.this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED;
        callback.invoke(origin, allowed, false);
      }
    });
    WebViewCompat.addWebMessageListener(web, "IDICamera", Collections.singleton(LOCAL_ORIGIN),
        (view, message, source, mainFrame, reply) -> {
          if (view != web || !mainFrame || !LOCAL_ORIGIN.equals(source.toString()) || ended) return;
          try {
            String data = message.getData();
            if (data == null || data.length() > 140000) throw new Exception("bridge size");
            handle(new JSONObject(data), reply, current);
          } catch (Exception error) { fail("官方原片传输未完成，请重新拍摄"); }
        });
    root.addView(web, new LinearLayout.LayoutParams(-1, 0, 1));
    web.loadUrl(PAGE);
    handler.postDelayed(() -> {
      if (!ended && web == current && !sessionRequested) fail("官方相机加载超时，请检查网络后重试");
    }, 45000);
  }

  private static WebResourceResponse blocked() {
    return new WebResourceResponse("text/plain", "utf-8", 403, "Blocked", Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
  }

  private void handle(JSONObject message, JavaScriptReplyProxy reply, WebView current) throws Exception {
    String type = message.getString("type");
    switch (type) {
      case "ready" -> {
        if (sessionRequested) return;
        sessionRequested = true;
        status.setText("正在获取本次拍摄授权…");
        CaptureStore.Item item = store.item(captureId);
        CaptureStore.io.execute(() -> {
          try {
            JSONObject session = CameraApi.request(item.server, "/api/camera/h5-session", store.token(item.device), item.meta);
            if (!captureId.equals(session.getString("capture_id"))) throw new Exception("拍摄授权编号不匹配");
            session.put("type", "session");
            session.getJSONObject("options").put("deviceModel", Build.MODEL).put("deviceBrand", Build.BRAND);
            runOnUiThread(() -> {
              if (ended || isDestroyed() || web != current) return;
              status.setVisibility(View.GONE);
              reply.postMessage(session.toString());
            });
          } catch (Exception error) {
            runOnUiThread(() -> { if (web == current) fail("拍摄授权获取失败：" + UploadWorker.safeMessage(error)); });
          }
        });
      }
      case "cancel" -> cancel();
      case "error" -> fail("load".equals(message.optString("code"))
          ? "官方 SDK 加载失败，请检查网络或更新安装包后重试"
          : "官方相机未返回有效照片，请检查试用授权、网络和定位后重试");
      case "photo_start" -> {
        if (!sessionRequested || transfer != null || saving) throw new Exception("duplicate transfer");
        transfer = new H5PhotoTransfer(message.getInt("chars"));
        userComment = message.optString("comment", "{}");
        if (userComment.length() > 65536) throw new Exception("comment size");
        new JSONObject(userComment);
        store.state(captureId, "capturing", "");
        acknowledge(reply, message);
      }
      case "photo_chunk" -> {
        if (transfer == null || saving) throw new Exception("missing transfer");
        transfer.append(message.getInt("chunk"), message.getString("data"));
        acknowledge(reply, message);
      }
      case "photo_end" -> {
        if (transfer == null || saving) throw new Exception("missing transfer");
        byte[] bytes = transfer.finish();
        transfer = null;
        saving = true;
        store.state(captureId, "capturing", "");
        status.setVisibility(View.VISIBLE);
        status.setText("正在保存官方原片…");
        acknowledge(reply, message);
        persist(bytes);
      }
      default -> throw new Exception("unsupported message");
    }
  }

  private static void acknowledge(JavaScriptReplyProxy reply, JSONObject message) throws Exception {
    reply.postMessage(new JSONObject().put("type", "ack").put("seq", message.getInt("seq")).toString());
  }

  private void persist(byte[] bytes) {
    CaptureStore.io.execute(() -> {
      try {
        OfficialPhotoWriter.save(store, captureId, bytes);
        Files.write(new File(store.directory(captureId), "h5-user-comment.json").toPath(), userComment.getBytes(StandardCharsets.UTF_8));
        store.sdkInfo(captureId, userComment);
        store.state(captureId, "queued", "");
        UploadWorker.enqueue(getApplicationContext(), captureId, false);
        runOnUiThread(this::complete);
      } catch (Exception error) {
        // A complete original may already exist; recovery never re-encodes it.
        store.state(captureId, store.original(captureId).isFile() ? "rendering" : "capture_failed", "官方原片保存未完成，可返回记录页恢复");
        runOnUiThread(() -> { saving = false; fail("原片保存未完成，请返回记录页核对"); });
      } finally { store.activeCaptures.remove(captureId); }
    });
  }

  private void recover() {
    saving = true;
    store.activeCaptures.add(captureId);
    CaptureStore.io.execute(() -> {
      try {
        OfficialPhotoWriter.recover(store, captureId);
        CaptureStore.Item item = store.item(captureId);
        if (item.state.equals("awaiting_sdk") || item.state.equals("capturing") || item.state.equals("rendering"))
          store.state(captureId, "queued", "");
        UploadWorker.pending(getApplicationContext());
        runOnUiThread(this::complete);
      } catch (Exception error) {
        runOnUiThread(() -> { saving = false; fail("已保存原片暂时无法打开，请返回记录页核对"); });
      } finally { store.activeCaptures.remove(captureId); }
    });
  }

  private void complete() {
    if (isDestroyed()) return;
    ended = true;
    saving = false;
    setResult(RESULT_OK, new Intent().putExtra("capture_id", captureId));
    finish();
  }

  private void fail(String message) {
    if (ended || isDestroyed() || saving) return;
    transfer = null;
    if (web != null) { web.stopLoading(); web.onPause(); web.setVisibility(View.GONE); }
    status.setVisibility(View.VISIBLE);
    status.setText(message);
    retry.setVisibility(store.original(captureId).isFile() ? View.GONE : View.VISIBLE);
    if (!store.original(captureId).isFile()) {
      CaptureStore.Item item = store.item(captureId);
      boolean beforePhoto = item != null
          && (item.state.equals("awaiting_sdk") || item.state.equals("camera_error"));
      store.state(captureId, beforePhoto ? "camera_error" : "capture_failed", message);
    }
  }

  private void cancel() {
    if (saving) { Toast.makeText(this, "正在保存原片，请稍候", Toast.LENGTH_SHORT).show(); return; }
    if (store.original(captureId).isFile()) { recover(); return; }
    ended = true;
    store.cancelEmptyOfficialCapture(captureId);
    setResult(RESULT_CANCELED, new Intent().putExtra("capture_id", captureId));
    finish();
  }

  private void closeWeb() {
    if (web == null) return;
    WebView previous = web;
    web = null;
    if (previous.getParent() instanceof ViewGroup parent) parent.removeView(previous);
    previous.stopLoading();
    previous.destroy();
  }

  @Override protected void onPause() {
    if (web != null) web.onPause();
    super.onPause();
  }

  @Override protected void onResume() {
    super.onResume();
    if (web != null) web.onResume();
  }

  @Override protected void onDestroy() {
    handler.removeCallbacksAndMessages(null);
    closeWeb();
    if (store != null && captureId != null && !saving) store.activeCaptures.remove(captureId);
    super.onDestroy();
  }
}
