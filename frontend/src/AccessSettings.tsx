import { useEffect, useRef, useState, type FormEvent } from 'react';
import { RefreshCw } from 'lucide-react';
import { api, message, patch, post, put } from './api';
import { dateTime, Empty, ErrorBox, Loading, Modal, useData } from './common';
import type { Person } from './Auth';
import type { Notify, Project } from './types';
import PlatformAccountSettings from './PlatformAccountSettings';
import RecordPagination, { useRecordPage } from './RecordPagination';
import FeishuIdentitySettings from './FeishuIdentitySettings';
import usePermissions from './usePermissions';

interface User extends Person { enabled: boolean; can_edit_account?: boolean; account_restriction?: string; protected_project_ids?: string[] }
interface ProjectAccount { configured: boolean; enabled: boolean; session: { connected: boolean } }
interface Operation {
  id: string; project_name: string; case_title: string; actor_name: string; website_account: string;
  source_users: { id: string; display_name: string }[]; operation: string; state: string; error: string; created_at: string;
}
const noop = () => {};
const stateLabels: Record<string, string> = { running: '执行中', failed: '失败', uncertain: '结果待核实', written: '官网已响应', no_write: '未写入官网', save_verified: '已保存并回读', verified: '已回读一致', saved: '已保存，待回读', submitted: '已提交，待回读', stale: '资料已变化' };
const operationLabels: Record<string, string> = { automatic_save: '自动保存照片', review_confirmation: '管理员确认', formal_submit: '正式提交', confirmed_save: '管理员保存', readback: '回读核实', withdraw_attachment: '撤回附件', withdraw_readback: '回读撤回结果' };

export function WebsiteOperations({ projectId = '', revision = 0 }: { projectId?: string; revision?: number }) {
  const records = useRecordPage<Operation>('/api/access/website-operations' + (projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''), 'idi.website-operations.page-size', revision);
  return <section className="settings-card" aria-label="官网操作记录"><div className="record-section-heading"><h3>官网操作记录</h3><button disabled={records.loading} onClick={records.refresh}><RefreshCw size={15} aria-hidden="true" />刷新记录</button></div><p className="muted">记录实际上传人员、执行或确认人员、项目官网账号和操作结果。</p><ErrorBox error={records.error} />
    {records.error ? <button className="record-retry" onClick={records.refresh}>重试读取记录</button> : !records.items.length ? records.loading ? <Loading /> : <Empty>暂无官网操作记录</Empty> : <div className="table-wrap record-table" aria-busy={records.loading}><table><thead><tr><th>时间 / 项目</th><th>案件</th><th>实际上传人员</th><th>执行 / 确认人员</th><th>官网账号</th><th>操作 / 结果</th></tr></thead><tbody>{records.items.map(row => <tr key={row.id}>
      <td>{dateTime(row.created_at)}<small>{row.project_name}</small></td><td>{row.case_title}</td><td>{row.source_users.map(user => user.display_name).join('、') || '—'}</td>
      <td>{row.actor_name}</td><td>{row.website_account || '尚未配置'}</td><td>{operationLabels[row.operation] || row.operation}<small>{stateLabels[row.state] || row.state}</small>{row.error && <small className="danger-text">{row.error}</small>}</td>
    </tr>)}</tbody></table></div>}
    <RecordPagination label="官网操作记录" {...records} />
  </section>;
}

export default function AccessSettings({ projects, notify, initialProjectId, focusProjectAccount = false }: { projects: Project[]; notify: Notify; initialProjectId?: string; focusProjectAccount?: boolean }) {
  const { isAdmin } = usePermissions();
  const [revision, setRevision] = useState(0), [projectId, setProjectId] = useState(initialProjectId && projects.some(p => p.id === initialProjectId) ? initialProjectId : projects[0]?.id || '');
  const accountSection = useRef<HTMLDivElement>(null);
  const users = useData<User[]>('/api/access/users', revision);
  const account = useData<ProjectAccount>(!isAdmin && projectId ? `/api/access/project-accounts/${projectId}` : null, revision);
  const [roles, setRoles] = useState<Record<string, string>>({}), [saving, setSaving] = useState(false), [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const busy = saving || refreshing || users.loading;
  const [editing, setEditing] = useState<User | 'new' | null>(null);
  useEffect(() => { setRoles(Object.fromEntries((users.data || []).map(user => [user.id, user.projects[projectId] || '']))); }, [projectId, users.data]);
  useEffect(() => { if (!users.loading) setRefreshing(false); }, [users.data, users.error, users.loading]);
  useEffect(() => { if (projects.length) setProjectId(current => current || projects[0].id); }, [projects]);
  useEffect(() => { if (initialProjectId && projects.some(p => p.id === initialProjectId)) setProjectId(initialProjectId); }, [initialProjectId]);
  useEffect(() => {
    if (focusProjectAccount && projectId === initialProjectId) {
      accountSection.current?.scrollIntoView({ block: 'start' });
      accountSection.current?.focus({ preventScroll: true });
    }
  }, [focusProjectAccount, initialProjectId, projectId]);
  const refresh = () => { setRefreshing(true); setError(''); setRevision(v => v + 1); };
  async function saveRoles() {
    setSaving(true); setError('');
    try {
      await put(`/api/access/projects/${projectId}/members`, {
        members: Object.entries(roles).filter(([, role]) => role).map(([user_id, role]) => ({ user_id, role })),
        ...(!isAdmin ? { expected_members: Object.fromEntries((users.data || []).filter(u => u.projects[projectId]).map(u => [u.id, u.projects[projectId]])) } : {}),
      });
      refresh(); notify('项目人员权限已保存，下一次请求立即生效');
    } catch (e) { setError(message(e)); }
    finally { setSaving(false); }
  }
  async function toggle(user: User) {
    setSaving(true); setError('');
    try { await patch(`/api/access/users/${user.id}`, { enabled: !user.enabled }); refresh(); notify(user.enabled ? '账号已停用，登录和手机配对已失效' : '账号已启用'); }
    catch (e) { setError(message(e)); }
    finally { setSaving(false); }
  }
  return <div className="access-settings">
    {!isAdmin && <p className="notice">可管理所负责项目的成员、工人及手机配对。项目管理员任命、跨项目账号资料和全局配置由系统管理员维护。</p>}
    <section className="settings-card"><h3>人员权限</h3><div className="table-wrap"><table><thead><tr><th>角色</th><th>范围</th><th>照片</th><th>官网正式提交</th><th>人员与账号配置</th></tr></thead><tbody>
      <tr><td>系统管理员</td><td>全部项目</td><td>上传、修改、删除</td><td>可以</td><td>可以</td></tr>
      <tr><td>项目管理员</td><td>分配的项目</td><td>上传、修改、删除</td><td>可以</td><td>管理本项目成员、工人及手机配对</td></tr>
      <tr><td>项目成员</td><td>分配的项目</td><td>APP 或网页上传、查看</td><td>不可以</td><td>不可以</td></tr>
    <tr><td>工人</td><td>明确关联的案件</td><td>现有相机拍摄、上传及处理本人记录</td><td>不可以</td><td>不可以</td></tr></tbody></table></div><p>人员照片上传后自动处理并保存官网草稿；资料齐全后的正式提交由管理员核对。</p></section>
    <section className="settings-card" aria-label="人员账号"><div className="row between"><h3>{isAdmin ? '人员账号' : '本项目人员账号'}</h3><div className="row"><button disabled={busy} onClick={refresh}>刷新人员</button><button className="primary" disabled={busy || !isAdmin && !projectId} onClick={() => setEditing('new')}>添加人员</button></div></div><ErrorBox error={error || users.error} />
      {!isAdmin && <p className="muted">当前项目：{projects.find(p => p.id === projectId)?.name || '请选择项目'}。新增人员会同时加入此项目。</p>}
      <div className="table-wrap"><table><thead><tr><th>姓名</th><th>登录账号</th><th>系统角色</th><th>状态</th><th>操作</th></tr></thead><tbody>{users.data?.filter(user => isAdmin || user.projects[projectId]).map(user => <tr key={user.id}>
        <td>{user.display_name}</td><td>{user.username}</td><td>{user.system_role === 'admin' ? '系统管理员' : '项目人员'}</td><td>{user.enabled ? '已启用' : '已停用'}</td><td><div className="row"><button disabled={busy || user.can_edit_account === false} onClick={() => setEditing(user)}>编辑 / 重设密码</button><button disabled={busy || user.can_edit_account === false} onClick={() => void toggle(user)}>{user.enabled ? '停用' : '启用'}</button></div>{user.account_restriction && <small>{user.account_restriction}</small>}</td>
      </tr>)}</tbody></table></div>
    </section>
    <section className="settings-card" aria-label="项目人员配置"><h3>项目人员配置</h3><p className="muted">工人加入项目后，需在案件列表或详情中分配案件；未分配时相机清单为空。移除项目或更改工人角色会清除原案件关联。</p><label className="inline-field">选择项目<select aria-label="选择项目" value={projectId} onChange={e => setProjectId(e.target.value)} disabled={busy}><option value="" disabled>请选择项目</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      {projectId ? <><div className="table-wrap space-top"><table><thead><tr><th>姓名 / 账号</th><th>本项目权限</th></tr></thead><tbody>{users.data?.map(user => <tr key={user.id}><td>{user.display_name}<small>{user.username}{!user.enabled && ' · 已停用'}</small></td><td>{user.system_role === 'admin' ? '系统管理员 · 全部权限' : user.protected_project_ids?.includes(projectId) ? '项目管理员 · 由系统管理员配置' : <select aria-label={`${user.display_name}的项目权限`} value={roles[user.id] || ''} disabled={busy} onChange={e => setRoles(v => ({ ...v, [user.id]: e.target.value }))}><option value="">未分配</option><option value="worker">工人 · 仅关联案件</option><option value="member">项目成员 · 上传与查看</option>{isAdmin && <option value="manager">项目管理员 · 管理与提交</option>}</select>}</td></tr>)}</tbody></table></div>
        <button className="primary space-top" disabled={busy || !users.data} onClick={() => void saveRoles()}>{saving ? '正在保存…' : '保存项目人员权限'}</button></> : <Empty>先在项目管理中建立项目</Empty>}
    </section>
    {projectId && <div ref={accountSection} tabIndex={-1} aria-label="本项目官网写入账号设置">
      <p className="muted">当前项目：{projects.find(p => p.id === projectId)?.name}</p>
      {isAdmin ? <PlatformAccountSettings key={projectId} endpoint={`/api/access/project-accounts/${projectId}`} title="本项目官网写入账号" description="本项目所有人员的照片上传、资料修改、撤回和正式提交均使用此账号。首次配置会开启自动整理与官网草稿保存；以后可在自动上传设置中暂停。" refresh={refresh} notify={notify} registerLeave={noop} />
        : <section className="settings-card"><h3>项目官网账号状态</h3><ErrorBox error={account.error} />{account.data && <p>{account.data.configured ? account.data.session.connected ? '项目官网已连接' : '项目官网等待连接' : '项目官网账号尚未配置'} · {account.data.enabled ? '项目写入已开启' : '项目写入已暂停'}</p>}</section>}
    </div>}
    <section className="settings-card"><h3>手机与人员绑定</h3><p>先建立人员账号并分配项目，再选择该人员生成手机配对码。停用人员或移除项目后，手机访问权限会立即变化。</p><div className="row"><a className="button" href="/camera-setup" target="_blank" rel="noreferrer">管理手机连接</a><a className="button" href="/camera/download">下载安卓 APP</a></div></section>
    {isAdmin && <FeishuIdentitySettings users={users.data || []} notify={notify} />}
    <WebsiteOperations projectId={projectId} revision={revision} />
    {editing && <UserEditor user={editing === 'new' ? null : editing} project={isAdmin ? undefined : projects.find(p => p.id === projectId)} close={() => setEditing(null)} saved={() => { refresh(); notify('人员账号已保存'); }} />}
  </div>;
}

function UserEditor({ user, project, close, saved }: { user: User | null; project?: Project; close: () => void; saved: () => void }) {
  const [name, setName] = useState(user?.display_name || ''), [username, setUsername] = useState(user?.username || ''), [password, setPassword] = useState('');
  const [role, setRole] = useState(user?.system_role || 'user'), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [projectRole, setProjectRole] = useState('worker');
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const data = { username, display_name: name, system_role: role, password,
        ...(project && !user ? { project_id: project.id, project_role: projectRole } : {}) };
      if (user) await patch(`/api/access/users/${user.id}`, data); else await post('/api/access/users', data);
      saved(); close();
    } catch (e) { setError(message(e)); }
    finally { setPassword(''); setBusy(false); }
  }
  return <Modal title={user ? '编辑人员账号' : '添加人员'} close={() => { if (!busy) close(); }}><ErrorBox error={error} />
    <form className="access-form" onSubmit={submit}>
      <label>姓名<input required maxLength={80} value={name} onChange={e => setName(e.target.value)} disabled={busy} /></label>
      <label>登录账号<input required minLength={3} maxLength={80} pattern="[A-Za-z0-9_.@-]+" autoComplete="off" value={username} onChange={e => setUsername(e.target.value)} disabled={busy} /></label>
      {project ? !user && <><p>加入项目：{project.name}</p><label>项目角色<select value={projectRole} onChange={e => setProjectRole(e.target.value)} disabled={busy}><option value="worker">工人 · 仅关联案件</option><option value="member">项目成员 · 上传与查看</option></select></label></>
        : <label>系统角色<select value={role} onChange={e => setRole(e.target.value as 'admin' | 'user')} disabled={busy}><option value="user">项目人员（按项目分配权限）</option><option value="admin">系统管理员（全部项目）</option></select></label>}
      <label>{user ? '重设密码（留空保留）' : '初始密码'}<input type="password" required={!user} minLength={10} maxLength={128} autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} disabled={busy} /></label>
      {user && <small>重设密码会使原登录和手机配对失效。</small>}
      <button className="primary" disabled={busy} type="submit">{busy ? '正在保存…' : '保存人员账号'}</button>
    </form>
  </Modal>;
}
