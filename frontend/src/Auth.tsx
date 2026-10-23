import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { api, ApiError, message, post } from './api';
import { ErrorBox, Loading, Modal } from './common';
import './access.css';

export interface Person {
  id: string; username: string; display_name: string; system_role: 'admin' | 'user';
  projects: Record<string, 'manager' | 'member'>;
}
const AuthContext = createContext<{ person: Person; logout: () => Promise<void> } | null>(null);
export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error('登录状态未加载');
  return value;
}

export default function AuthGate({ children }: { children: ReactNode }) {
  const [person, setPerson] = useState<Person | null>(null), [setup, setSetup] = useState(false);
  const [setupAllowed, setSetupAllowed] = useState(false);
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [username, setUsername] = useState(''), [name, setName] = useState(''), [password, setPassword] = useState('');
  useEffect(() => {
    let stopped = false;
    void (async () => {
      try {
        const status = await api<{ setup_required: boolean; setup_allowed: boolean }>('/api/auth/status');
        if (stopped) return;
        setSetup(status.setup_required);
        setSetupAllowed(status.setup_allowed === true);
        if (!status.setup_required) {
          try { const value = await api<Person>('/api/auth/me'); if (!stopped) setPerson(value); }
          catch (e) { if (!(e instanceof ApiError && e.status === 401)) throw e; }
        }
      } catch (e) { if (!stopped) setError(message(e)); }
      finally { if (!stopped) setLoading(false); }
    })();
    const expired = () => { setPerson(null); setPassword(''); setError('登录已失效，请重新登录。'); };
    window.addEventListener('idi-session-expired', expired);
    return () => { stopped = true; window.removeEventListener('idi-session-expired', expired); };
  }, []);
  useEffect(() => {
    if (!person) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void api<Person>('/api/auth/me').then(setPerson).catch(e => {
        if (e instanceof ApiError && e.status === 401) { setPerson(null); setPassword(''); setError('登录已失效，请重新登录。'); }
      });
    }, 30000);
    return () => window.clearInterval(timer);
  }, [!!person]);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      setPerson(await post<Person>(setup ? '/api/auth/setup' : '/api/auth/login',
        { username, display_name: name, password }));
      setSetup(false);
    } catch (e) { setError(message(e)); }
    finally { setPassword(''); setBusy(false); }
  }
  async function logout() {
    await post('/api/auth/logout'); setPerson(null); setPassword(''); setError('');
  }
  if (loading) return <Loading />;
  if (person) return <AuthContext.Provider value={{ person, logout }}>{children}</AuthContext.Provider>;
  return <main className="auth-page"><section className="auth-card">
    <p className="auth-brand">IDI · 维保工作台</p><h1>{setup ? setupAllowed ? '创建系统管理员' : '等待管理员初始化' : '登录工作台'}</h1>
    <p className="muted">{setup ? setupAllowed
      ? '当前为本机管理入口。创建首位管理员后，即可分配项目、人员和官网账号。'
      : '当前访问地址不能创建首位管理员。请使用部署时提供的本机管理入口；远程部署可通过已配置的 SSH 转发入口完成初始化。'
      : '使用管理员为你分配的 IDI 人员账号登录。'}</p>
    <ErrorBox error={error} />{setup && !setupAllowed ? <div className="auth-setup-notice">
      <p>管理员创建完成后，刷新此页即可使用账号登录。</p>
      <button className="primary" type="button" onClick={() => window.location.reload()}>已完成初始化，刷新登录</button>
    </div> : <form onSubmit={submit}>
      <label>人员账号<input required autoComplete="username" value={username} minLength={3} maxLength={80} onChange={e => setUsername(e.target.value)} disabled={busy} /></label>
      {setup && <label>姓名<input required value={name} maxLength={80} onChange={e => setName(e.target.value)} disabled={busy} /></label>}
      <label>密码<input required type="password" autoComplete={setup ? 'new-password' : 'current-password'} minLength={setup ? 10 : undefined} maxLength={128} value={password} onChange={e => setPassword(e.target.value)} disabled={busy} /></label>
      {setup && <small>至少10个字符；请保存好管理员账号和密码。</small>}
      <button className="primary" disabled={busy} type="submit">{busy ? '正在处理…' : setup ? '创建管理员并进入' : '登录'}</button>
    </form>}
  </section></main>;
}

export function PersonMenu() {
  const { person, logout } = useAuth();
  const [editing, setEditing] = useState(false), [oldPassword, setOldPassword] = useState(''), [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function change(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try { await post('/api/auth/password', { current_password: oldPassword, password }); setEditing(false); }
    catch (e) { setError(message(e)); }
    finally { setOldPassword(''); setPassword(''); setBusy(false); }
  }
  return <><details className="action-menu person-menu"><summary aria-label="人员账号" title={person.display_name}>{person.display_name}</summary>
    <div><span className="muted">{person.system_role === 'admin' ? '系统管理员' : '项目人员'} · {person.username}</span>
      <button onClick={() => { setError(''); setEditing(true); }}>修改密码</button>
      <button onClick={() => void logout().catch(e => setError(message(e)))}>退出登录</button>
      <ErrorBox error={!editing ? error : ''} />
    </div></details>
    {editing && <Modal title="修改人员密码" close={() => { if (!busy) setEditing(false); }}>
      <ErrorBox error={error} /><form className="access-form" onSubmit={change}>
        <label>当前密码<input required type="password" autoComplete="current-password" value={oldPassword} onChange={e => setOldPassword(e.target.value)} disabled={busy} /></label>
        <label>新密码<input required type="password" autoComplete="new-password" minLength={10} maxLength={128} value={password} onChange={e => setPassword(e.target.value)} disabled={busy} /></label>
        <p className="muted">修改后其他登录和手机配对会失效，手机需要重新配对。</p>
        <button className="primary" type="submit" disabled={busy}>{busy ? '正在保存…' : '保存新密码'}</button>
      </form>
    </Modal>}</>;
}
