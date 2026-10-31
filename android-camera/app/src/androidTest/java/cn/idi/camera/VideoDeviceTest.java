package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;
import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.rule.GrantPermissionRule;
import com.xhey.xheycamerasdk.recorder.VideoInfo;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import org.json.JSONObject;
import org.junit.Rule;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class VideoDeviceTest {
  @Rule public GrantPermissionRule permissions = GrantPermissionRule.grant(
      Manifest.permission.CAMERA, Manifest.permission.ACCESS_FINE_LOCATION,
      Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.RECORD_AUDIO);

  @Test public void mediaSwitchKeepsCaptureIdentityAndCannotChangeAfterRecordingStarts() throws Exception {
    CaptureStore store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0),
        "construction", "测试部位", "录像模式测试", "").put("source", "xhey_sdk");
    store.create(meta, true);
    String id = meta.getString("capture_id");
    store.state(id, "awaiting_sdk", "");
    store.updateMediaType(id, true);
    assertTrue(store.item(id).video());
    assertEquals("original.mp4", store.original(id).getName());
    for (String field : new String[] {"capture_id", "case_id", "case_number", "location", "captured_at", "content"})
      assertEquals(meta.get(field), store.item(id).meta.get(field));
    store.updateMediaType(id, false);
    assertFalse(store.item(id).video());
    assertEquals("original.jpg", store.original(id).getName());
    store.updateMediaType(id, true);
    File partial = new File(store.directory(id), "sdk-partial.mp4");
    byte[] evidence = {1, 2, 3, 4};
    Files.write(partial.toPath(), evidence);
    assertThrows(Exception.class, () -> store.updateMediaType(id, false));
    assertThrows(Exception.class, () -> store.updateWatermark(id,
        new WatermarkEditor.Draft("completion", "客厅", "不应写入", "", false)));
    store.cancelEmptyOfficialCapture(id);
    assertTrue(store.recordItems().stream().anyMatch(item -> item.id.equals(id)));
    assertArrayEquals(evidence, Files.readAllBytes(partial.toPath()));
    store.state(id, "local", "Synthetic partial-video guard; never upload");
  }

  @Test public void completedSdkFileSurvivesActivityRecreationAndRetainsExactBytes() throws Exception {
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    JSONObject meta = CameraData.metadata(MainActivity.demoCases().get(0),
        "construction", "测试部位", "录像中断恢复测试", "").put("source", "xhey_sdk").put("media_type", "video");
    store.create(meta, true);
    String id = meta.getString("capture_id");
    File sdk = new File(store.directory(id), "sdk-interrupted.mp4");
    try (java.io.InputStream input = InstrumentationRegistry.getInstrumentation().getContext()
        .getAssets().open("sdk-video-sample.mp4")) {
      Files.copy(input, sdk.toPath());
    }
    String originalHash = OfficialPhotoWriter.sha256(sdk);
    store.state(id, "awaiting_sdk", "");
    try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
      long deadline = SystemClock.uptimeMillis() + 15000;
      while (!store.item(id).state.equals("local") && SystemClock.uptimeMillis() < deadline) Thread.sleep(100);
      assertEquals("local", store.item(id).state);
      assertTrue(store.thumbnail(id).isFile());
      assertEquals(originalHash, OfficialPhotoWriter.sha256(store.original(id)));
      assertFalse(store.stamped(id).exists());
      assertTrue(store.thumbnail(id).delete());
      store.state(id, "rendering", "");
      scenario.recreate();
      deadline = SystemClock.uptimeMillis() + 15000;
      while (!store.item(id).state.equals("local") && SystemClock.uptimeMillis() < deadline) Thread.sleep(100);
      assertEquals("local", store.item(id).state);
      assertTrue(store.thumbnail(id).length() > 0);
      assertEquals(originalHash, OfficialPhotoWriter.sha256(store.original(id)));
      assertEquals(meta.getString("case_id"), store.item(id).meta.getString("case_id"));
    }
  }

  /** Explicit live SDK test; all taps are driven from the inspected emulator screen. */
  @Test public void liveVideoRetainsBytesAndArchivesOnce() throws Exception {
    assumeTrue("1".equals(InstrumentationRegistry.getArguments().getString("video_live", "")));
    Context context = ApplicationProvider.getApplicationContext();
    CaptureStore store = CaptureStore.get(context);
    String server = InstrumentationRegistry.getArguments().getString("server", "http://127.0.0.1:8879");
    String code = InstrumentationRegistry.getArguments().getString("pair_code", "");
    assertEquals(10, code.length());
    JSONObject pair = CameraApi.request(server, "/api/camera/pair", "", new JSONObject().put("code", code));
    store.connect(server, pair);
    store.replaceCases(CameraApi.request(server, "/api/camera/cases", store.token(store.device()), null).getJSONArray("items"));
    NativeAuthorization.save(store, NativeAuthorization.scope(store),
        CameraApi.request(server, "/api/camera/native-authorization", store.token(store.device()), null));
    JSONObject meta = CameraData.metadata(store.cases().get(0), "construction", "测试部位",
        "VIDEO-050 / SDK联调，非业务资料", "").put("source", "xhey_sdk");
    store.create(meta, false);
    String id = meta.getString("capture_id");
    store.state(id, "awaiting_sdk", "");
    File output = new File(context.getFilesDir(), "video-verification");
    output.mkdirs();
    Intent launch = new Intent(context, NativeCameraActivity.class).putExtra("capture_id", id).putExtra("emulator_test", true);
    // The SDK's animated WebView never becomes idle while previewing. A
    // synchronous ActivityScenario.launch can block observation until dismissal.
    context.startActivity(launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    {
      Files.write(new File(output, "capture-id.txt").toPath(), id.getBytes(StandardCharsets.UTF_8));
      long deadline = SystemClock.uptimeMillis() + 300000;
      long nextDiagnostic = 0;
      while ((!store.item(id).video() || !store.original(id).isFile())
          && SystemClock.uptimeMillis() < deadline) {
        if (SystemClock.uptimeMillis() >= nextDiagnostic) {
          rendererDiagnostic(output, id);
          nextDiagnostic = SystemClock.uptimeMillis() + 2000;
        }
        Thread.sleep(500);
      }
      assertTrue("Must capture an actual SDK video", store.item(id).video() && store.original(id).isFile());
      Thread.sleep(2500);
      JSONObject time = store.item(id).meta.getJSONObject("video_capture_time");
      assertEquals("sdk_watermark_render", time.getString("source"));
      assertEquals("recording_start", time.getString("event"));
      assertEquals("second", time.getString("precision"));
      assertFalse(time.getString("value").isEmpty());
      assertFalse(time.getString("raw_text").isEmpty());
      assertNotNull(VideoCaptureTime.validate(time));
      String hash = OfficialPhotoWriter.sha256(store.original(id));
      OfficialVideoWriter.recover(store, id);
      assertEquals(hash, OfficialPhotoWriter.sha256(store.original(id)));
      assertTrue(store.thumbnail(id).length() > 0);
      Files.copy(store.original(id).toPath(), new File(output, "live-original.mp4").toPath(), StandardCopyOption.REPLACE_EXISTING);
      // The host restores the emulator's network after recording the queued state.
      deadline = SystemClock.uptimeMillis() + 180000;
      JSONObject receipt = null;
      while (receipt == null && SystemClock.uptimeMillis() < deadline) {
        try { receipt = CameraApi.upload(store, store.item(id)); }
        catch (Exception offline) { Thread.sleep(2000); }
      }
      assertNotNull("Video should archive after the network returns", receipt);
      assertEquals("video", receipt.getString("media_type"));
      assertEquals("uploaded", receipt.getString("state"));
      assertEquals("not_requested", receipt.getString("verification_state"));
      assertFalse(receipt.getBoolean("verification_supported"));
      assertEquals(hash, receipt.getString("original_hash"));
      JSONObject duplicate = CameraApi.upload(store, store.item(id));
      assertTrue(duplicate.getBoolean("duplicate"));
      assertEquals(receipt.getString("photo_id"), duplicate.getString("photo_id"));
      store.uploaded(id, receipt);
      // A stale SDK callback from another capture must never be attached here.
      JSONObject second = new JSONObject(meta.toString()).put("capture_id", java.util.UUID.randomUUID().toString())
          .put("media_type", "video");
      store.create(second, true);
      VideoInfo stale = new VideoInfo(); stale.filePath = store.original(id).getPath();
      assertThrows(Exception.class, () -> OfficialVideoWriter.save(store, second.getString("capture_id"), stale));
      store.state(second.getString("capture_id"), "local", "Synthetic stale-callback guard");
      assertEquals(hash, OfficialPhotoWriter.sha256(store.original(id)));
      Files.write(new File(output, "live-result.json").toPath(), new JSONObject()
          .put("capture_id", id).put("state", store.item(id).state).put("metadata", store.item(id).meta)
          .put("receipt", receipt).put("original_hash", hash).put("duplicate_receipt", duplicate)
          .toString(2).getBytes(StandardCharsets.UTF_8));
    }
  }

  /** Read only, test only: no URL query, SDK authorization, location or bridge payloads. */
  private static void rendererDiagnostic(File output, String captureId) throws Exception {
    java.util.concurrent.CountDownLatch done = new java.util.concurrent.CountDownLatch(1);
    java.util.concurrent.atomic.AtomicReference<String> snapshot = new java.util.concurrent.atomic.AtomicReference<>();
    new android.os.Handler(android.os.Looper.getMainLooper()).post(() -> {
      try {
        for (android.app.Activity current : androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
            .getInstance().getActivitiesInStage(androidx.test.runner.lifecycle.Stage.RESUMED)) {
          if (!(current instanceof EditableCameraActivity activity)
              || !captureId.equals(activity.captureId) || activity.renderer == null) continue;
          android.net.Uri url = android.net.Uri.parse(activity.renderer.getUrl() == null ? "" : activity.renderer.getUrl());
          String nativeProtocol = url.getScheme(), nativePath = url.getPath();
          activity.renderer.evaluateJavascript("""
              (() => {
                const w=document.querySelector('.watermark-container');
                const row=w&&w.querySelector('[data-tag="watermark-time"]');
                const observer=window.__idiVideoTimeObserverV1;
                const app=document.getElementById('app');
                const pending=app&&app._vnode?[app._vnode]:[];
                let model=null, visited=0, matched=0;
                while(pending.length&&visited++<512) {
                  const vnode=pending.pop();
                  if(!vnode||typeof vnode!=='object')continue;
                  if(vnode.el===w&&w) {
                    matched++;
                    model=vnode.props&&(vnode.props['watermark-model']||vnode.props.watermarkModel);
                    if(!model&&vnode.component&&vnode.component.props)model=vnode.component.props.watermarkModel;
                    if(model)break;
                  }
                  if(vnode.component&&vnode.component.subTree)pending.push(vnode.component.subTree);
                  if(Array.isArray(vnode.children))pending.push(...vnode.children);
                }
                const spans=row?row.querySelectorAll('span'):[];
                const raw=row?row.textContent.trim().slice(0,160):null;
                const text=model&&model.templateId===20&&spans.length===2?spans[1].textContent.trim():raw;
                const parsed=text&&text.replace(/^时\\s*间\\s*[：:]\\s*/,'').match(/^(20\\d{2})[.\\-/年](\\d{1,2})[.\\-/月](\\d{1,2})日?\\s*(?:(?:星期|周)[一二三四五六日天]\\s*)?(\\d{1,2}):(\\d{2}):(\\d{2})$/);
                let matches=null;
                if(parsed&&model&&typeof model.inChina==='boolean'&&Number.isFinite(model.timestamp)) {
                  const p=parsed.slice(1).map(Number), offset=model.inChina?480:-new Date(model.timestamp).getTimezoneOffset();
                  matches=Math.floor(model.timestamp/1000)===(Date.UTC(p[0],p[1]-1,p[2],p[3],p[4],p[5])-offset*60000)/1000;
                }
                const recording=document.querySelector('.recording-time');
                return {protocol:location.protocol,path:location.pathname,observer_present:!!observer,
                  observer_port:!!(observer&&observer.port),observer_sequence:observer?observer.sequence:null,
                  recording_present:!!recording,recording_counter:recording?recording.textContent.trim():null,
                  watermark_present:!!w,row_present:!!row,row_visible:!!(row&&row.getClientRects().length),
                  row_spans:spans.length,row_child_elements:row?row.childElementCount:null,
                  vnode_present:!!(app&&app._vnode),visited,matched,model_present:!!model,
                  template_id:model?model.templateId:null,template_id_type:model?typeof model.templateId:null,
                  in_china:model?model.inChina:null,in_china_type:model?typeof model.inChina:null,
                  timestamp_type:model?typeof model.timestamp:null,timestamp_error:model?!!model.timestampErrorOccured:null,
                  raw_text:raw,raw_parsed:!!parsed,model_raw_second_match:matches};
              })()
              """, result -> {
            try {
              JSONObject data = new JSONObject(result).put("native_protocol", nativeProtocol)
                  .put("native_path", nativePath).put("capture_id", captureId)
                  .put("observed_at", java.time.Instant.now().toString())
                  .put("native_observer_present", activity.videoTime != null)
                  .put("native_observer_port", activity.videoTime != null && activity.videoTime.port != null)
                  .put("native_observer_sequence", activity.videoTime == null ? 0 : activity.videoTime.sequence);
              snapshot.set(data.toString(2));
            } catch (Exception ignored) { snapshot.set("{\"state\":\"renderer_diagnostic_not_json\"}"); }
            finally { done.countDown(); }
          });
          return;
        }
      } catch (Exception ignored) { }
      done.countDown();
    });
    // A slow renderer must not stop the actual live camera test indefinitely.
    done.await(2, java.util.concurrent.TimeUnit.SECONDS);
    if (snapshot.get() == null) return;
    File pending = new File(output, "renderer-diagnostic.pending");
    Files.write(pending.toPath(), snapshot.get().getBytes(StandardCharsets.UTF_8));
    Files.move(pending.toPath(), new File(output, "renderer-diagnostic.json").toPath(),
        StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
  }
}
