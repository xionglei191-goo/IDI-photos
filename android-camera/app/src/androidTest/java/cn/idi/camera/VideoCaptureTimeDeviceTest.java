package cn.idi.camera;

import static org.junit.Assert.*;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.Bundle;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class VideoCaptureTimeDeviceTest {
  static final String PAGE = "file:///android_asset/XheyCameraSDKAssets/index.html";

  @Test public void startTimeSurvivesPauseStopAndUploadStateWhileRetakeCannotReuseOldTime() throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0), "construction", "测试部位",
        "录像时间隔离测试", "").put("source", "xhey_sdk").put("media_type", "video");
    store.create(meta, true);
    String id = meta.getString("capture_id");
    store.state(id, "awaiting_sdk", "");
    WebView[] web = new WebView[1];
    VideoCaptureTime[] capture = new VideoCaptureTime[1];
    CountDownLatch loaded = new CountDownLatch(1);
    phase("launch attached WebView activity");
    ActivityScenario<VideoTimeProbeActivity> scenario = ActivityScenario.launch(VideoTimeProbeActivity.class);
    try {
      onMain("configure fixture", () -> {
        web[0] = VideoTimeProbeActivity.current.web;
        web[0].getSettings().setJavaScriptEnabled(false);
        web[0].getSettings().setBlockNetworkLoads(true);
        web[0].setWebViewClient(new WebViewClient() {
          @Override public void onPageFinished(WebView view, String url) { loaded.countDown(); }
        });
        // Android does not intercept android_asset URLs. Load the real local page
        // with scripts disabled, then replace only this test WebView's DOM.
        web[0].loadUrl(PAGE);
        capture[0] = new VideoCaptureTime(store, id);
      });
      assertTrue("fixture main page load", loaded.await(10, TimeUnit.SECONDS));
      onMain("enable fixture scripts", () -> web[0].getSettings().setJavaScriptEnabled(true));
      js(web[0], """
          document.body.innerHTML='<div id="app"><div class="watermark-container"><div data-tag="watermark-time"><span>拍摄时间</span><span id="clock">2026-10-28 星期三 20:31:40</span></div></div></div>';
          window.model={templateId:"20",inChina:true,timestamp:Date.parse('2026-10-28T20:31:40+08:00'),timestampErrorOccured:false};
          document.getElementById('app')._vnode={component:{subTree:{el:document.querySelector('.watermark-container'),props:{'watermark-model':model}}}};
          """);
      phase("loaded " + diagnostic(web[0]));
      onMain("install observer", () -> capture[0].probe(web[0]));
      long deadline = android.os.SystemClock.uptimeMillis() + 5000;
      while (!"true".equals(js(web[0], "Boolean(window.__idiVideoTimeObserverV1?.port)"))
          && android.os.SystemClock.uptimeMillis() < deadline) Thread.sleep(30);
      phase("observer nativePort=" + (capture[0].port != null) + " " + diagnostic(web[0]));
      assertEquals(diagnostic(web[0]), "true", js(web[0], "Boolean(window.__idiVideoTimeObserverV1?.port)"));
      assertFalse(store.item(id).meta.has("video_capture_time"));
      js(web[0], "let rec=document.createElement('div'); rec.className='recording-time';rec.textContent='00:00';document.body.append(rec)");
      awaitTime(store, id, true);
      phase("first start persisted");
      JSONObject first = store.item(id).meta.getJSONObject("video_capture_time");
      assertEquals("2026-10-28T20:31:40+08:00", first.getString("value"));
      assertEquals("2026-10-28 星期三 20:31:40", first.getString("raw_text"));
      js(web[0], "model.timestamp+=1000;clock.textContent='2026-10-28 星期三 20:31:41';rec.textContent='00:01';rec.classList.add('paused')");
      js(web[0], "rec.classList.remove('paused')");
      assertEquals(first.toString(), store.item(id).meta.getJSONObject("video_capture_time").toString());
      js(web[0], "rec.remove()");
      assertEquals(first.toString(), store.item(id).meta.getJSONObject("video_capture_time").toString());
      // A retake with no clock must clear the previous take, and a later time
      // update during that recording must not be mistaken for its start.
      js(web[0], "clock.textContent='--';rec.textContent='00:00';document.body.append(rec)");
      awaitTime(store, id, false);
      js(web[0], "clock.textContent='2026-10-28 星期三 20:31:41';rec.textContent='00:01'");
      assertFalse(store.item(id).meta.has("video_capture_time"));
      // SDK retake can reset a still-present counter within the same update.
      js(web[0], "model.timestamp+=1000;model.templateId='10';document.querySelector('[data-tag=watermark-time]').textContent='时   间：2026-10-28 20:31:42';rec.textContent='00:00'");
      awaitTime(store, id, true);
      assertEquals("2026-10-28T20:31:42+08:00", store.item(id).meta.getJSONObject("video_capture_time").getString("value"));
      assertEquals(10, store.item(id).meta.getJSONObject("video_capture_time").getInt("template_id"));
      assertEquals("时   间：2026-10-28 20:31:42", store.item(id).meta.getJSONObject("video_capture_time").getString("raw_text"));
      store.state(id, "queued", "");
      store.videoCaptureTime(id, null);
      assertTrue("Saved/queued metadata must be immutable to a late renderer callback", store.item(id).meta.has("video_capture_time"));
    } finally {
      try {
        onMain("close observer", () -> { if (capture[0] != null) capture[0].close(); });
      } finally {
        scenario.close();
        store.state(id, "cancelled", "Synthetic clock observation only; no media captured");
      }
    }
  }

  @Test public void rejectsUnknownPagesMismatchedVisibleTimeAndMissingSeconds() throws Exception {
    assertTrue(VideoCaptureTime.sdkPage(PAGE + "?appid=example"));
    assertFalse(VideoCaptureTime.sdkPage(PAGE + ".evil"));
    assertFalse(VideoCaptureTime.sdkPage("https://example.com" + VideoCaptureTime.SDK_PATH));
    assertFalse(VideoCaptureTime.sdkPage("file://untrusted" + VideoCaptureTime.SDK_PATH));
    JSONObject value = new JSONObject().put("value", "2026-10-28T20:31:40+08:00")
        .put("source", "sdk_watermark_render").put("event", "recording_start").put("precision", "second")
        .put("observed_at", "2026-10-28T12:31:40Z").put("sdk_version", BuildConfig.XHEY_SDK_VERSION)
        .put("template_id", 20).put("raw_text", "2026-10-28 星期三 20:31:40");
    assertNotNull(VideoCaptureTime.validate(value));
    assertNotNull(VideoCaptureTime.validate(new JSONObject(value.toString()).put("template_id", 10)
        .put("raw_text", "时   间：2026-10-28 20:31:40")));
    assertNull(VideoCaptureTime.validate(new JSONObject(value.toString()).put("raw_text", "2026-10-28 星期三 20:31:41")));
    assertNull(VideoCaptureTime.validate(new JSONObject(value.toString()).put("raw_text", "2026-10-28 星期三 20:31")));
    assertNull(VideoCaptureTime.validate(new JSONObject(value.toString()).put("template_id", 21)));
  }

  private static void awaitTime(CaptureStore store, String id, boolean expected) throws Exception {
    long deadline = android.os.SystemClock.uptimeMillis() + 5000;
    while (store.item(id).meta.has("video_capture_time") != expected && android.os.SystemClock.uptimeMillis() < deadline)
      Thread.sleep(30);
    assertEquals(expected, store.item(id).meta.has("video_capture_time"));
  }

  private static String js(WebView web, String script) throws Exception {
    AtomicReference<String> value = new AtomicReference<>();
    CountDownLatch result = new CountDownLatch(1);
    onMain("evaluate fixture script", () -> web.evaluateJavascript(script, answer -> {
      value.set(answer); result.countDown();
    }));
    assertTrue("JavaScript callback timed out", result.await(5, TimeUnit.SECONDS));
    return value.get();
  }

  private static String diagnostic(WebView web) throws Exception {
    AtomicReference<String> page = new AtomicReference<>();
    onMain("read page identity", () -> {
      android.net.Uri uri = android.net.Uri.parse(web.getUrl() == null ? "" : web.getUrl());
      page.set("native_protocol=" + uri.getScheme() + " native_path=" + uri.getPath());
    });
    return page.get() + " " + js(web, "JSON.stringify({protocol:location.protocol,path:location.pathname,observer:!!window.__idiVideoTimeObserverV1,port:!!window.__idiVideoTimeObserverV1?.port,sequence:window.__idiVideoTimeObserverV1?.sequence,vnode:!!document.getElementById('app')?._vnode})");
  }

  private static void phase(String message) {
    Bundle status = new Bundle();
    status.putString("stream", "[video-time] " + message + "\n");
    InstrumentationRegistry.getInstrumentation().sendStatus(2, status);
  }

  private static void onMain(String phase, Runnable action) throws Exception {
    CountDownLatch done = new CountDownLatch(1);
    AtomicReference<Throwable> failure = new AtomicReference<>();
    new Handler(Looper.getMainLooper()).post(() -> {
      try { action.run(); }
      catch (Throwable error) { failure.set(error); }
      finally { done.countDown(); }
    });
    assertTrue("Main thread timed out: " + phase, done.await(10, TimeUnit.SECONDS));
    if (failure.get() != null) throw new AssertionError(phase, failure.get());
  }
}
