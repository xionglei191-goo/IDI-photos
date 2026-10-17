import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Eye, EyeOff, LockKeyhole, RefreshCw, Save, Unplug } from 'lucide-react';
import { api, message, post, put } from './api';
import { dateTime, ErrorBox } from './common';
import type { Notify } from './types';
import type { PlatformAccountStatus } from './platformTypes';

const STATE: Record<string, string> = { unconfigured: '尚未设置', connecting: '正在连接官网', connected: '官网已连接', disabled: '自动连接已暂停', needs_attention: '需要处理', retry_wait: '等待自动重试', pending: '等待自动连接' };
type Leave = (() => Promise<boolean>) | null;

export default function PlatformAccountSettings({ refresh, notify, registerLeave }: {
  refresh: () => void; notify: Notify; registerLeave: (callback: Leave) => void;
}) {
  const [value, setValue] = useState<PlatformAccountStatus | null>(null);
  const [username, setUsername] = useState(''), [password, setPassword] = useState(''), [automatic, setAutomatic] = useState(true);
  const [visible, setVisible] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [touched, setTouched] = useState(false), [removing, setRemoving] = useState(false);
  const dirty = !!password || !!value && (username !== value.username || automatic !== (value.configured ? value.auto_connect : true));
  const dirtyRef = useRef(dirty), currentSession = useRef(''), refreshRef = useRef(refresh);
  dirtyRef.current = dirty; refreshRef.current = refresh;
  const accept = useCallback((next: PlatformAccountStatus, reset = false) => {
    const signature = JSON.stringify(next.session);
    if (currentSession.current && currentSession.current !== signature) refreshRef.current();
    currentSession.current = signature;
    setValue(next);
    if (reset || !dirtyRef.current) {
      setUsername(next.username); setAutomatic(next.configured ? next.auto_connect : true); setPassword('');
      setVisible(false); setTouched(false); dirtyRef.current = false;
    }
  }, []);
  useEffect(() => {
    let stopped = false, running = false;
    const abort = new AbortController();
    const load = async () => {
      if (running) return; running = true;
      try { const next = await api<PlatformAccountStatus>('/api/platform/account', { signal: abort.signal }); if (!stopped) { accept(next); setLoadError(''); } }
      catch { if (!stopped) setLoadError('暂时无法读取系统账号状态，正在重试。'); }
      finally { running = false; }
    };
    void load(); const timer = window.setInterval(() => void load(), 4000);
    return () => { stopped = true; abort.abort(); window.clearInterval(timer); };
  }, [accept]);
  useEffect(() => {
    registerLeave(async () => { if (dirty) notify('系统账号设置尚未保存，请先保存或撤销修改。', true); return !dirty && !busy; });
    return () => registerLeave(null);
  }, [dirty, busy, notify, registerLeave]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', guard); return () => window.removeEventListener('beforeunload', guard);
  }, [dirty]);
  const missingUsername = !username.trim();
  const missingPassword = !password && (!value?.password_stored || username.trim() !== value.username);
  const connecting = value?.state === 'connecting';
  async function save(event: FormEvent) {
    event.preventDefault(); setTouched(true);
    if (missingUsername || missingPassword) return;
    setBusy(true); setError('');
    try {
      const next = await put<PlatformAccountStatus>('/api/platform/account', { username, password, auto_connect: automatic });
      accept(next, true); refresh(); notify(next.state === 'connected' ? '系统账号设置已保存，官网已连接' : automatic ? '系统账号已加密保存，正在连接官网' : '系统账号已加密保存，自动连接已暂停');
    } catch (e) { setError(message(e)); }
    finally { setPassword(''); setVisible(false); setBusy(false); }
  }
  async function act(kind: 'connect' | 'disconnect' | 'forget') {
    setBusy(true); setError('');
    try {
      if (kind === 'connect') accept(await post<PlatformAccountStatus>('/api/platform/account/connect'));
      else {
        if (kind === 'disconnect') await api('/api/platform/session', { method: 'DELETE' });
        else await api('/api/platform/account', { method: 'DELETE' });
        accept(await api<PlatformAccountStatus>('/api/platform/account'), true);
        setRemoving(false); refresh(); notify(kind === 'disconnect' ? '官网已断开，自动连接已暂停' : '已移除系统账号并断开官网');
      }
    } catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  return <section className="settings-card platform-account" aria-label="系统账号设置">
    <div className="platform-account-heading"><div><h3><LockKeyhole size={17} aria-hidden="true" />系统账号（官网）</h3><p>设置一次，服务启动和登录失效时自动连接官网。</p></div><span className={`status ${value?.state === 'connected' ? 'status-completed' : ''}`} role="status">{value ? STATE[value.state] || value.state : '正在读取设置…'}</span></div>
    <ErrorBox error={error || loadError || value?.error || ''} />
    <form className="platform-account-form" onSubmit={save}>
      <div className="form-grid">
        <label>系统账号<input aria-label="系统账号" name="platform-username" autoComplete="username" maxLength={160} value={username} disabled={busy || connecting || !value} onChange={e => setUsername(e.target.value)} onBlur={() => setTouched(true)} aria-invalid={touched && missingUsername} aria-describedby="platform-username-hint" /><small id="platform-username-hint">{touched && missingUsername ? '请填写官网账号或手机号。' : '填写官网使用的账号或手机号。'}</small></label>
        <label>系统密码<div className="platform-password"><input aria-label="系统密码" name="platform-password" type={visible ? 'text' : 'password'} autoComplete="current-password" maxLength={512} value={password} disabled={busy || connecting || !value} onChange={e => setPassword(e.target.value)} placeholder={value?.password_stored ? '已加密保存；留空保留密码' : '首次设置需要填写密码'} aria-invalid={touched && missingPassword} aria-describedby="platform-password-hint" /><button type="button" className="icon-button" aria-label={visible ? '隐藏输入的密码' : '显示输入的密码'} aria-pressed={visible} disabled={!password} onClick={() => setVisible(v => !v)}>{visible ? <EyeOff size={17} aria-hidden="true" /> : <Eye size={17} aria-hidden="true" />}</button></div><small id="platform-password-hint">{touched && missingPassword ? '首次设置或更换账号时，请填写对应密码。' : value?.password_stored ? '已保存的密码不会回显；更换账号时需填写新密码。' : '密码按当前 Windows 用户加密保存，不随档案备份导出。'}</small></label>
      </div>
      <label className="check"><input type="checkbox" checked={automatic} disabled={busy || connecting || !value} onChange={e => setAutomatic(e.target.checked)} />启动时自动连接，会话失效后自动重连</label>
      <div className="row wrap platform-account-actions"><button className="primary" type="submit" disabled={busy || connecting || !value}><Save size={15} aria-hidden="true" />{busy ? '正在保存…' : automatic ? '保存并自动连接' : '保存账号设置'}</button>{dirty && <button type="button" disabled={busy} onClick={() => { if (value) accept(value, true); setError(''); }}>撤销账号修改</button>}{value?.configured && !dirty && <button type="button" disabled={busy || connecting} onClick={() => void act('connect')}><RefreshCw size={15} className={connecting ? 'spin' : ''} aria-hidden="true" />{connecting ? '正在连接…' : '立即连接'}</button>}</div>
    </form>
    {value && <div className="platform-account-detail"><p>{value.session.connected ? `当前连接账号：${value.session.account}` : '当前官网尚未连接。'}{value.auto_connect ? ' 自动连接已开启。' : ' 自动连接尚未开启。'}</p>{value.last_success_at && <p>最近自动连接成功：{dateTime(value.last_success_at)}</p>}{value.next_retry_at && <p>下次重试：{dateTime(value.next_retry_at)}</p>}<p>网络异常会间隔重试；账号密码错误或需要官网验证时停止重试，并在这里提示处理。</p></div>}
    {(value?.configured || value?.session.connected || value?.state === 'needs_attention') && <div className="row wrap platform-account-secondary">
      {value?.session.connected && <button disabled={busy || connecting || dirty} onClick={() => void act('disconnect')}><Unplug size={15} aria-hidden="true" />断开并暂停自动连接</button>}
      {!removing ? <button className="text-button" disabled={busy || connecting || dirty} onClick={() => setRemoving(true)}>移除系统账号</button> : <><span>移除后会断开官网，并清除保存的账号密码。</span><button disabled={busy} onClick={() => setRemoving(false)}>取消</button><button disabled={busy} onClick={() => void act('forget')}>确认移除</button></>}
    </div>}
  </section>;
}
