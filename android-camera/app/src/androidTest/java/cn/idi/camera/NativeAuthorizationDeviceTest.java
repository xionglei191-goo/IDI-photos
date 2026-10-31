package cn.idi.camera;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;

import android.content.SharedPreferences;
import android.os.Build;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/** No SDK launch or network: opt-in fixtures restore the emulator's pairing and cases. */
@RunWith(AndroidJUnit4.class)
public class NativeAuthorizationDeviceTest {
  CaptureStore store;
  Map<String, ?> originalPreferences;
  JSONArray originalCases;
  JSONObject selected;

  @Before public void prepare() throws Exception {
    assumeTrue(BuildConfig.DEBUG && "1".equals(
        InstrumentationRegistry.getArguments().getString("native_authorization_fixture", "")));
    assumeTrue(Build.PRODUCT.startsWith("sdk") || Build.FINGERPRINT.contains("generic"));
    store = CaptureStore.get(ApplicationProvider.getApplicationContext());
    CaptureStore.io.submit(() -> {}).get(10, TimeUnit.SECONDS);
    assumeTrue(store.activeCaptures.isEmpty());
    for (CaptureStore.Item item : store.items())
      assumeTrue(!java.util.Arrays.asList("capturing", "rendering", "awaiting_sdk", "queued", "retry", "uploading", "verifying").contains(item.state));
    originalPreferences = new HashMap<>(store.prefs.getAll());
    originalCases = new JSONArray();
    for (JSONObject item : store.cases()) originalCases.put(new JSONObject(item.toString()));
    assertTrue(store.prefs.edit().clear().putString("server", "http://127.0.0.1:1")
        .putString("device", "auth-fixture").putString("token_auth-fixture", "fixture-token")
        .putString("token_old-pairing", "preserved-old-token").commit());
    selected = new JSONObject().put("id", "auth-case").put("case_number", "ZLBH202610280000000001")
        .put("project_name", "授权测试项目").put("location", "1栋101室")
        .put("capture_enabled", true).put("workflow_stage", "repair");
    store.replaceCases(new JSONArray().put(selected));
  }

  @After public void restore() throws Exception {
    if (originalPreferences == null) return;
    CaptureStore.io.submit(() -> {}).get(10, TimeUnit.SECONDS);
    store.replaceCases(originalCases);
    SharedPreferences.Editor editor = store.prefs.edit().clear();
    for (Map.Entry<String, ?> entry : originalPreferences.entrySet()) {
      String key = entry.getKey(); Object value = entry.getValue();
      if (value instanceof String) editor.putString(key, (String) value);
      else if (value instanceof Boolean) editor.putBoolean(key, (Boolean) value);
      else if (value instanceof Integer) editor.putInt(key, (Integer) value);
      else if (value instanceof Long) editor.putLong(key, (Long) value);
      else if (value instanceof Float) editor.putFloat(key, (Float) value);
      else if (value instanceof Set<?>) {
        Set<String> values = new HashSet<>();
        for (Object element : (Set<?>) value) values.add((String) element);
        editor.putStringSet(key, values);
      } else fail("Unexpected preference type");
    }
    assertTrue(editor.commit());
  }

  JSONObject authorization() throws Exception {
    return new JSONObject().put("ready", true).put("device_id", "auth-fixture")
        .put("sdk_version", BuildConfig.XHEY_SDK_VERSION).put("server_time", 1793174400)
        .put("expires_at", 1793257200).put("video_enabled", true).put("max_record_seconds", 60)
        .put("options", new JSONObject().put("appid", "SYNTHETIC-APP").put("timestamp", 1793174400)
            .put("noncestr", "SYNTHETIC-NONCE").put("signature", "SYNTHETIC-SIGNATURE"));
  }

  JSONObject meta() throws Exception {
    return CameraData.metadata(selected, "construction", "卫生间", "测试内容", "").put("source", "xhey_sdk");
  }

  @Test public void persistedCredentialBuildsRepeatedAndEditedSessionsLocally() throws Exception {
    NativeAuthorization.save(store, NativeAuthorization.scope(store), authorization());
    assertTrue(store.prefs.contains(NativeAuthorization.KEY));
    JSONObject first = NativeAuthorization.session(NativeAuthorization.cached(store), meta(), store.cases());
    JSONObject changed = meta().put("content", "新的水印内容").put("media_type", "video");
    JSONObject second = NativeAuthorization.session(NativeAuthorization.cached(store), changed, store.cases());
    assertNotEquals(first.getString("capture_id"), second.getString("capture_id"));
    assertEquals(first.getJSONObject("options").getString("signature"), second.getJSONObject("options").getString("signature"));
    assertEquals("video", second.getString("media_type"));
    assertEquals("新的水印内容", second.getJSONObject("options").getJSONObject("customInputItems").getString("内容"));
    assertEquals("施工过程", second.getJSONObject("options").getJSONObject("customInputItems").getString("环节"));
    assertFalse(second.getJSONObject("options").has("secretKey"));
    assertFalse(NativeAuthorization.refreshDue(store));
  }

  @Test public void expiryUsesMonotonicAgeAndRejectsRebootOrAnotherScope() throws Exception {
    JSONObject saved = new JSONObject().put("scope", "server\ndevice").put("boot", 7)
        .put("received_elapsed", 1000).put("lifetime_ms", 10000);
    assertEquals(9000, NativeAuthorization.remaining(saved, "server\ndevice", 7, 2000));
    assertEquals(0, NativeAuthorization.remaining(saved, "server\ndevice", 7, 11000));
    assertEquals(0, NativeAuthorization.remaining(saved, "server\ndevice", 8, 2000));
    assertEquals(0, NativeAuthorization.remaining(saved, "server\ndevice", 7, 900));
    assertEquals(0, NativeAuthorization.remaining(saved, "other\ndevice", 7, 2000));
    saved.put("lifetime_ms", NativeAuthorization.MAX_LIFETIME_MS + 1);
    assertEquals(0, NativeAuthorization.remaining(saved, "server\ndevice", 7, 2000));
  }

  @Test public void changedPairingAndStaleAsyncResultCannotReuseAuthorization() throws Exception {
    String oldScope = NativeAuthorization.scope(store);
    NativeAuthorization.save(store, oldScope, authorization());
    store.prefs.edit().putString("device", "new-device").putString("token_new-device", "new-token").commit();
    assertNull(NativeAuthorization.cached(store));
    NativeAuthorization.save(store, oldScope, authorization());
    assertNull(NativeAuthorization.cached(store));
    NativeAuthorization.denied(store, oldScope);
    assertEquals(1, store.cases().size());
    store.prefs.edit().putString("device", "auth-fixture").putString("server", "http://127.0.0.1:2").commit();
    assertNull(NativeAuthorization.cached(store));
  }

  @Test public void revokedScopeClearsOnlyCaptureAccessAndKeepsUploadCredentials() throws Exception {
    int captures = store.items().size();
    NativeAuthorization.save(store, NativeAuthorization.scope(store), authorization());
    NativeAuthorization.denied(store, NativeAuthorization.scope(store));
    assertNull(NativeAuthorization.cached(store));
    assertTrue(store.cases().isEmpty());
    assertEquals("fixture-token", store.token("auth-fixture"));
    assertEquals("preserved-old-token", store.token("old-pairing"));
    assertEquals(captures, store.items().size());
  }

  @Test public void removedOrChangedCaseCannotGenerateAnotherCaptureSession() throws Exception {
    NativeAuthorization.save(store, NativeAuthorization.scope(store), authorization());
    JSONObject draft = meta();
    store.replaceCases(new JSONArray());
    try { NativeAuthorization.session(NativeAuthorization.cached(store), draft, store.cases()); fail(); }
    catch (Exception expected) { assertTrue(expected.getMessage().contains("可拍摄")); }
    selected.put("location", "2栋202室");
    store.replaceCases(new JSONArray().put(selected));
    try { NativeAuthorization.session(NativeAuthorization.cached(store), draft, store.cases()); fail(); }
    catch (Exception expected) { assertTrue(expected.getMessage().contains("已更新")); }
  }

  @Test public void expiringCredentialRequestsRefreshAndDisabledResponseInvalidatesIt() throws Exception {
    JSONObject response = authorization().put("expires_at", 1793174400 + 1800);
    NativeAuthorization.save(store, NativeAuthorization.scope(store), response);
    assertNotNull(NativeAuthorization.cached(store));
    assertTrue(NativeAuthorization.refreshDue(store));
    try { NativeAuthorization.save(store, NativeAuthorization.scope(store), new JSONObject().put("ready", false)); fail(); }
    catch (Exception expected) { assertTrue(expected.getMessage().contains("未就绪")); }
    assertNull(NativeAuthorization.cached(store));
  }
}
