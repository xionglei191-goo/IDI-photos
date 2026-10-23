const IDENTITY = 'idi-workspace-chat-key';
const LAST_THREAD = 'idi-workspace-chat-thread';
const SELECTION = 'idi-workspace-chat-selection-v1';
const OPEN = 'idi-workspace-chat-open';
const VALID_KEY = /^[A-Za-z0-9-]{32,80}$/;
const VALID_THREAD = /^[a-f0-9]{32}$/;
export type ChatSelection = { threadId: string; scope: string };

function readStorage(kind: 'localStorage' | 'sessionStorage', name: string) {
  try { return window[kind].getItem(name) || ''; } catch { return ''; }
}
function writeStorage(kind: 'localStorage' | 'sessionStorage', name: string, value: string) {
  try {
    if (value) window[kind].setItem(name, value); else window[kind].removeItem(name);
    return readStorage(kind, name) === value;
  } catch { return false; }
}
function readCookie(name: string) {
  try {
    const item = document.cookie.split('; ').find(value => value.startsWith(name + '='));
    return item ? decodeURIComponent(item.slice(name.length + 1)) : '';
  } catch { return ''; }
}
function writeCookie(name: string, value: string) {
  try {
    document.cookie = `${name}=${encodeURIComponent(value)}; Path=/; Max-Age=${value ? 31536000 : 0}; SameSite=Strict${location.protocol === 'https:' ? '; Secure' : ''}`;
    return readCookie(name) === value;
  } catch { return false; }
}
function stored(name: string) {
  return readStorage('localStorage', name) || readCookie(name) || readStorage('sessionStorage', name);
}
function save(name: string, value: string) {
  writeStorage('localStorage', name, value);
  writeCookie(name, value);
  writeStorage('sessionStorage', name, value);
}
export function randomChatId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(24)), v => v.toString(16).padStart(2, '0')).join('');
}

let identity: { key: string; persistent: boolean; reloadable: boolean } | undefined;
export function chatIdentity() {
  if (identity) return identity;
  const key = [readStorage('localStorage', IDENTITY), readCookie(IDENTITY), readStorage('sessionStorage', IDENTITY)].find(value => VALID_KEY.test(value)) || randomChatId();
  // Keep the existing identity. A storage failure must not silently replace it
  // on a component remount; a first-party cookie also survives lost local state.
  const local = writeStorage('localStorage', IDENTITY, key);
  const cookie = writeCookie(IDENTITY, key);
  const session = writeStorage('sessionStorage', IDENTITY, key);
  identity = { key, persistent: local || cookie, reloadable: local || cookie || session };
  return identity;
}

export function readChatSelection(owner = ''): ChatSelection | null {
  const selection = SELECTION + (owner ? '-' + owner : ''), lastThread = LAST_THREAD + (owner ? '-' + owner : '');
  for (const source of [readStorage('sessionStorage', selection), readStorage('localStorage', selection), readCookie(selection)]) {
    try {
      const value = JSON.parse(source || 'null');
      if (value && typeof value.threadId === 'string' && (!value.threadId || VALID_THREAD.test(value.threadId)) &&
        typeof value.scope === 'string' && /^(all|(?:project|case):[a-f0-9]{32})$/.test(value.scope)) return value;
    } catch { /* Try another backup, then the pre-upgrade thread pointer. */ }
  }
  const threadId = stored(lastThread);
  return VALID_THREAD.test(threadId) ? { threadId, scope: 'all' } : null;
}
export function saveChatSelection(value: ChatSelection, owner = '') {
  save(SELECTION + (owner ? '-' + owner : ''), JSON.stringify(value));
  save(LAST_THREAD + (owner ? '-' + owner : ''), value.threadId);
}
// This is tab state: opening a second tab should not force the dialog open.
export function readChatOpen(owner = '') { return readStorage('sessionStorage', OPEN + (owner ? '-' + owner : '')) === '1'; }
export function saveChatOpen(value: boolean, owner = '') { writeStorage('sessionStorage', OPEN + (owner ? '-' + owner : ''), value ? '1' : ''); }
