package cn.idi.camera;

import android.app.Activity;
import android.os.Bundle;
import android.view.ViewGroup;
import android.webkit.WebView;

/** Attached WebView fixture for instrumentation only; absent from release APKs. */
public final class VideoTimeProbeActivity extends Activity {
  static volatile VideoTimeProbeActivity current;
  WebView web;

  @Override public void onCreate(Bundle state) {
    super.onCreate(state);
    web = new WebView(this);
    web.getSettings().setJavaScriptEnabled(true);
    setContentView(web);
    current = this;
  }

  @Override public void onDestroy() {
    current = null;
    if (web.getParent() instanceof ViewGroup parent) parent.removeView(web);
    web.destroy();
    super.onDestroy();
  }
}
