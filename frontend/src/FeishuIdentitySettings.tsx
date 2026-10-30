import { useEffect, useState, type FormEvent } from 'react';
import { RefreshCw } from 'lucide-react';
import { api, message, put } from './api';
import { Empty, ErrorBox, Loading, Modal, useData } from './common';
import type { Person } from './Auth';
import type { Notify } from './types';

interface Binding {
  id: string; app_id: string; open_id: string; user_id: string; name: string;
  display_name: string; username: string; enabled: boolean; connector_name: string;
}
interface Group { id: string; name: string; app_id: string }
interface Member { open_id: string; name: string }
interface BindingData { bindings: Binding[]; groups: Group[] }

export default function FeishuIdentitySettings({ users, notify }: { users: (Person & { enabled: boolean })[]; notify: Notify }) {
  const [revision, setRevision] = useState(0), [group, setGroup] = useState(''), [member, setMember] = useState(''), [user, setUser] = useState('');
  const data = useData<BindingData>('/api/access/feishu-bindings', revision);
  const [members, setMembers] = useState<Member[]>([]), [loadingMembers, setLoadingMembers] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [removing, setRemoving] = useState<Binding | null>(null);
  useEffect(() => {
    let alive = true;
    setMembers([]); setMember(''); setError('');
    if (!group) { setLoadingMembers(false); return; }
    setLoadingMembers(true);
    void api<Member[]>('/api/access/feishu-bindings/groups/' + encodeURIComponent(group) + '/members')
      .then(next => { if (alive) setMembers(next); })
      .catch(e => { if (alive) setError(message(e)); })
      .finally(() => { if (alive) setLoadingMembers(false); });
    return () => { alive = false; };
  }, [group, revision]);
  async function bind(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      await put('/api/access/feishu-bindings', { connector_id: group, open_id: member, user_id: user });
      setRevision(v => v + 1); notify('飞书人员已绑定，卡片操作按该人员的项目权限校验');
    } catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  async function unbind() {
    if (!removing) return;
    setBusy(true); setError('');
    try {
      await api('/api/access/feishu-bindings/' + encodeURIComponent(removing.id), { method: 'DELETE' });
      setRemoving(null); setRevision(v => v + 1); notify('已解除飞书人员绑定，尚未执行的卡片操作将重新校验');
    } catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  }
  return <section className="settings-card" aria-label="飞书人员绑定">
    <div className="row between wrap"><h3>飞书人员绑定</h3><button disabled={busy || loadingMembers} onClick={() => setRevision(v => v + 1)}><RefreshCw size={15} />刷新绑定与成员</button></div>
    <p>绑定后，可在飞书卡片预览资料、补充字段并确认提交。正式提交仍需本项目管理员或系统管理员权限；群负责人绑定用于提醒。</p>
    <ErrorBox error={error || data.error} />
    {!data.data ? !data.error && <Loading /> : <>
      {!data.data.groups.length ? <Empty>请先在收图入口启用飞书群，再进行人员绑定。</Empty> : <form className="access-form" onSubmit={bind}>
        <label>飞书群<select required value={group} disabled={busy} onChange={e => setGroup(e.target.value)}><option value="">选择已接入的群</option>{data.data.groups.map(item => <option key={item.id} value={item.id}>{item.name} · {item.app_id}</option>)}</select></label>
        <label>飞书人员<select required value={member} disabled={busy || loadingMembers || !group} onChange={e => setMember(e.target.value)}><option value="">{loadingMembers ? '正在读取群成员…' : '选择群成员'}</option>{members.map(item => <option key={item.open_id} value={item.open_id}>{item.name} · {item.open_id}</option>)}</select></label>
        <label>对应 IDI 人员<select required value={user} disabled={busy} onChange={e => setUser(e.target.value)}><option value="">选择已启用的人员</option>{users.filter(item => item.enabled).map(item => <option key={item.id} value={item.id}>{item.display_name} · {item.username}</option>)}</select></label>
        <button className="primary" type="submit" disabled={busy || loadingMembers || !group || !member || !user}>{busy ? '正在保存…' : '保存人员绑定'}</button>
      </form>}
      <p className="muted">同一飞书应用内的各群共用此身份绑定。人员停用、项目权限撤销或解除绑定后，后续卡片操作会被拦截。</p>
      {data.data.bindings.length ? <div className="table-wrap"><table><thead><tr><th>飞书人员 / 应用</th><th>IDI 人员</th><th>状态</th><th>操作</th></tr></thead><tbody>{data.data.bindings.map(item => <tr key={item.id}>
        <td>{item.name}<small>{item.app_id}</small></td><td>{item.display_name}<small>{item.username}</small></td><td>{item.enabled ? '按当前项目权限执行' : 'IDI 人员已停用'}</td>
        <td><button disabled={busy} onClick={() => setRemoving(item)}>解除绑定</button></td>
      </tr>)}</tbody></table></div> : <Empty>尚未绑定飞书人员。卡片预览提醒可发送，正式提交按钮会提示先绑定。</Empty>}
    </>}
    {removing && <Modal title="解除飞书人员绑定" close={() => { if (!busy) setRemoving(null); }}>
      <p>解除 {removing.name} 与 {removing.display_name} 的身份绑定后，此飞书账号将无法继续通过卡片办理。</p>
      <ErrorBox error={error} /><div className="row"><button disabled={busy} onClick={() => setRemoving(null)}>取消</button><button className="danger" disabled={busy} onClick={() => void unbind()}>确认解除绑定</button></div>
    </Modal>}
  </section>;
}
