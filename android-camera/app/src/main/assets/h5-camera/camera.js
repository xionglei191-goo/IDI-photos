(() => {
  'use strict';
  const vendorOrigin = 'https://sdk-h5.xhey.top';
  const bridge = window.IDICamera;
  let camera, started = false, transferring = false, sequence = 0;
  const acknowledgements = new Map();
  const report = code => bridge.postMessage(JSON.stringify({type: 'error', code}));
  // The official wrapper accepts origin prefixes. Restrict messages to its exact
  // iframe before that listener runs; sibling frames cannot submit a photograph.
  window.addEventListener('message', event => {
    const frame = document.getElementById('xhey-camera-iframe');
    if (event.origin !== vendorOrigin || !frame || event.source !== frame.contentWindow)
      event.stopImmediatePropagation();
  }, true);
  function send(type, payload = {}) {
    const seq = sequence++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { acknowledgements.delete(seq); reject(Error('timeout')); }, 30000);
      acknowledgements.set(seq, () => { clearTimeout(timer); resolve(); });
      bridge.postMessage(JSON.stringify({type, seq, ...payload}));
    });
  }
  bridge.onmessage = event => {
    let message;
    try { message = JSON.parse(event.data); } catch { return report('bridge'); }
    if (message.type === 'ack') { acknowledgements.get(message.seq)?.(); acknowledgements.delete(message.seq); return; }
    if (message.type !== 'session' || started) return;
    started = true;
    try {
      camera = new XHeyCamera(message.options);
      camera.onLogInfo(() => {}); camera.onLogError(() => {}); camera.onLogWarn(() => {});
      camera.onCancel(() => { if (!transferring) bridge.postMessage(JSON.stringify({type:'cancel'})); });
      camera.onError(() => report('sdk'));
      camera.onSuccess(async (image, comment) => {
        if (transferring) return;
        transferring = true;
        try {
          if (typeof image !== 'string') throw Error('type');
          let base64 = image;
          if (base64.startsWith('data:')) {
            if (!/^data:image\/jpe?g;base64,/.test(base64)) throw Error('format');
            base64 = base64.substring(base64.indexOf(',') + 1);
          }
          if (!base64.length || base64.length > 27962028 || base64.length % 4 !== 0) throw Error('size');
          const details = comment && typeof comment === 'object' ? JSON.stringify(comment) : '{}';
          if (details.length > 65536) throw Error('comment');
          await send('photo_start', {chars: base64.length, comment: details});
          for (let offset = 0, chunk = 0; offset < base64.length; offset += 65536, chunk++)
            await send('photo_chunk', {chunk, data: base64.slice(offset, offset + 65536)});
          await send('photo_end');
          camera.dispose();
          document.getElementById('loading').textContent = '正在保存官方原片…';
        } catch { report('photo'); }
      });
      camera.takePhoto().catch(() => report('sdk'));
    } catch { report('sdk'); }
  };
  const script = document.createElement('script');
  script.src = 'https://static.xhey.top/sdk/prod/xhey-camera-sdk.v1.0.2.min.js';
  script.integrity = 'sha384-IfNqbSRjxxtWHBiL8DdBRctMR7RuPzybFf1Aao6WQhjalcQ9doKGbzBLQWbMKZiZ';
  script.crossOrigin = 'anonymous';
  script.onload = () => bridge.postMessage(JSON.stringify({type: 'ready'}));
  script.onerror = () => report('load');
  document.head.appendChild(script);
})();
