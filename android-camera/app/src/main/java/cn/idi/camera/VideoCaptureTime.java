package cn.idi.camera;

import android.net.Uri;
import android.webkit.WebMessage;
import android.webkit.WebMessagePort;
import android.webkit.WebView;
import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;
import org.json.JSONObject;

/** Observes the pinned SDK renderer; never supplies or changes its clock or pixels. */
final class VideoCaptureTime implements AutoCloseable {
  static final String SDK_PATH = "/android_asset/XheyCameraSDKAssets/index.html";
  final CaptureStore store;
  final String captureId;
  final String nonce = java.util.UUID.randomUUID().toString();
  WebView renderer;
  WebMessagePort port;
  boolean installing, closed;
  int sequence;

  VideoCaptureTime(CaptureStore store, String captureId) {
    this.store = store;
    this.captureId = captureId;
    store.resetEmptyVideoTime(captureId);
  }

  static boolean sdkPage(String url) {
    if (url == null) return false;
    Uri uri = Uri.parse(url);
    return "file".equals(uri.getScheme()) && SDK_PATH.equals(uri.getPath())
        && (uri.getAuthority() == null || uri.getAuthority().isEmpty());
  }

  void probe(WebView web) {
    if (closed || installing || web == null || !sdkPage(web.getUrl())) return;
    renderer = web;
    installing = true;
    web.evaluateJavascript(script(nonce), result -> {
      installing = false;
      if (closed || !sdkPage(web.getUrl()) || !"\"connect\"".equals(result)) return;
      if (port != null) port.close();
      WebMessagePort[] channel = web.createWebMessageChannel();
      port = channel[0];
      port.setWebMessageCallback(new WebMessagePort.WebMessageCallback() {
        @Override public void onMessage(WebMessagePort source, WebMessage message) {
          if (closed || !sdkPage(web.getUrl())) return;
          accept(message.getData());
        }
      });
      // file: pages have an opaque origin. The exact local page is checked both
      // above and by the installed script; this port accepts only time observations.
      web.postWebMessage(new WebMessage(nonce, new WebMessagePort[] {channel[1]}), Uri.parse("*"));
    });
  }

  void accept(String json) {
    if (json == null || json.length() > 4096) return;
    try {
      JSONObject event = new JSONObject(json);
      int next = event.getInt("sequence");
      if (!nonce.equals(event.optString("observer")) || next <= sequence) return;
      sequence = next;
      JSONObject timing;
      try { timing = validate(event.optJSONObject("timing")); }
      catch (Exception invalidTime) { timing = null; }
      // An observed retake without a usable clock clears the previous take's
      // observation; a later clock update must not be substituted for its start.
      store.videoCaptureTime(captureId, timing);
    } catch (Exception ignored) {
      // Missing/changed SDK data remains missing and is handled by the server fallback.
    }
  }

  static JSONObject validate(JSONObject timing) throws Exception {
    if (timing == null) return null;
    int template = timing.optInt("template_id");
    if (!"sdk_watermark_render".equals(timing.optString("source"))
        || !"recording_start".equals(timing.optString("event"))
        || !"second".equals(timing.optString("precision"))
        || !BuildConfig.XHEY_SDK_VERSION.equals(timing.optString("sdk_version"))
        || (template != 10 && template != 20)) return null;
    String raw = timing.optString("raw_text");
    if (raw.isEmpty() || raw.length() > 120) return null;
    OffsetDateTime value = OffsetDateTime.parse(timing.getString("value"));
    OffsetDateTime observed = OffsetDateTime.parse(timing.getString("observed_at"));
    if (value.getYear() < 2020 || value.getYear() > 2100 || value.getNano() != 0
        || Math.abs(value.getOffset().getTotalSeconds()) > 14 * 3600) return null;
    String dateText = template == 10 ? raw.replaceFirst("^时[\\s\\u00a0]*间[\\s\\u00a0]*[：:][\\s\\u00a0]*", "") : raw;
    if (template == 10 && dateText.equals(raw)) return null;
    java.util.regex.Matcher text = java.util.regex.Pattern.compile(
        "^(20\\d{2})[.\\-/年](\\d{1,2})[.\\-/月](\\d{1,2})日?\\s*"
        + "(?:(?:星期|周)[一二三四五六日天]\\s*)?(\\d{1,2}):(\\d{2}):(\\d{2})$").matcher(dateText);
    if (!text.matches()) return null;
    java.time.LocalDateTime visible = java.time.LocalDateTime.of(
        Integer.parseInt(text.group(1)), Integer.parseInt(text.group(2)), Integer.parseInt(text.group(3)),
        Integer.parseInt(text.group(4)), Integer.parseInt(text.group(5)), Integer.parseInt(text.group(6)));
    if (!visible.equals(value.toLocalDateTime())) return null;
    // Copy only the bounded, understood fields, never arbitrary renderer content.
    return new JSONObject().put("value", value.format(DateTimeFormatter.ISO_OFFSET_DATE_TIME))
        .put("source", "sdk_watermark_render").put("event", "recording_start")
        .put("precision", "second").put("observed_at", observed.toString())
        .put("sdk_version", BuildConfig.XHEY_SDK_VERSION).put("template_id", template).put("raw_text", raw);
  }

  static String script(String observer) {
    return """
        (() => {
          if (location.protocol !== 'file:' || location.pathname !== '/android_asset/XheyCameraSDKAssets/index.html') return null;
          const key = '__idiVideoTimeObserverV1', token = __IDI_OBSERVER_TOKEN__;
          const existing = window[key];
          if (existing && existing.token === token) return existing.port ? 'connected' : 'pending';
          if (existing) existing.close();
          const state = {token, port:null, pending:[], sequence:0, node:document.querySelector('.recording-time'), elapsed:null};
          function sample() {
            const w = document.querySelector('.watermark-container');
            const row = w && w.querySelector('[data-tag="watermark-time"]');
            if (!row || row.getClientRects().length === 0) return null;
            // This SDK ships production Vue: its renderer retains #app._vnode
            // but deliberately omits the development-only DOM component pointer.
            // Only accept the props belonging to this exact watermark element.
            const app = document.getElementById('app');
            const pending = app && app._vnode ? [app._vnode] : [];
            let model = null;
            for (let i=0; pending.length && i<512; i++) {
              const vnode = pending.pop();
              if (!vnode || typeof vnode !== 'object') continue;
              if (vnode.el === w) {
                model = vnode.props && (vnode.props['watermark-model'] || vnode.props.watermarkModel);
                if (!model && vnode.component && vnode.component.props) model=vnode.component.props.watermarkModel;
                if (model) break;
              }
              if (vnode.component && vnode.component.subTree) pending.push(vnode.component.subTree);
              if (Array.isArray(vnode.children)) pending.push(...vnode.children);
            }
            const templateId = model ? Number(model.templateId) : 0;
            if (!model || ![10,20].includes(templateId) || model.timestampErrorOccured
                || typeof model.inChina !== 'boolean' || !Number.isFinite(model.timestamp) || model.timestamp <= 0) return null;
            const parts = row.querySelectorAll('span');
            const raw = templateId===10 && row.childElementCount===0 ? row.textContent.trim()
              : templateId===20 && parts.length===2 ? parts[1].textContent.trim() : '';
            const dateText = templateId===10 ? raw.replace(/^时\\s*间\\s*[：:]\\s*/,'') : raw;
            if (templateId===10 && dateText===raw) return null;
            const match = dateText.match(/^(20\\d{2})[.\\-/年](\\d{1,2})[.\\-/月](\\d{1,2})日?\\s*(?:(?:星期|周)[一二三四五六日天]\\s*)?(\\d{1,2}):(\\d{2}):(\\d{2})$/);
            if (!match) return null;
            const offset = model.inChina ? 480 : -new Date(model.timestamp).getTimezoneOffset();
            const numbers = match.slice(1).map(Number);
            const [y,mo,d,h,mi,s] = numbers;
            const epoch = Date.UTC(y,mo-1,d,h,mi,s) - offset*60000;
            if (Math.floor(model.timestamp/1000) !== epoch/1000) return null;
            const pad = n => String(n).padStart(2,'0');
            const zone = (offset>=0?'+':'-') + pad(Math.floor(Math.abs(offset)/60)) + ':' + pad(Math.abs(offset)%60);
            return {value:`${y}-${pad(mo)}-${pad(d)}T${pad(h)}:${pad(mi)}:${pad(s)}${zone}`,
              source:'sdk_watermark_render',event:'recording_start',precision:'second',
              observed_at:new Date().toISOString(),sdk_version:'1.3.70.9b61b4a6',template_id:templateId,raw_text:raw};
          }
          function publish(timing) {
            const event = JSON.stringify({observer:token,sequence:++state.sequence,timing});
            if (state.port) state.port.postMessage(event);
            else { state.pending.push(event); if (state.pending.length>8) state.pending.shift(); }
          }
          function scan() {
            const node = document.querySelector('.recording-time');
            const digits = node && node.textContent.match(/(\\d+):(\\d{2})/);
            const elapsed = digits ? Number(digits[1])*60+Number(digits[2]) : null;
            // A pause keeps the same counter. SDK retake can remove and re-add
            // the counter within one Vue update, so also detect its reset to zero.
            if (node && (node !== state.node || (elapsed===0 && state.elapsed>0))) publish(sample());
            state.node=node;
            state.elapsed=elapsed;
          }
          const observer = new MutationObserver(scan);
          observer.observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['class']});
          function connect(event) {
            if (event.data !== token || !event.ports || event.ports.length !== 1 || state.port) return;
            state.port=event.ports[0];
            for (const pending of state.pending) state.port.postMessage(pending);
            state.pending=[];
          }
          window.addEventListener('message',connect);
          state.close=() => { observer.disconnect(); window.removeEventListener('message',connect); if(state.port) state.port.close(); };
          window[key]=state;
          return 'connect';
        })()
        """.replace("__IDI_OBSERVER_TOKEN__", JSONObject.quote(observer));
  }

  @Override public void close() {
    closed = true;
    if (port != null) port.close();
    port = null;
  }
}
