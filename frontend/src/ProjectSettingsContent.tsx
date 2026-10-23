import { useState } from 'react';
import { Empty, ErrorBox, PHOTO_GROUPS, ROLES, STAGES, useData } from './common';
import { WebsiteOperations } from './AccessSettings';
import type { Project, System, Template } from './types';
import type { SettingsTarget } from './SettingsCenter';
import usePermissions from './usePermissions';

interface Account { configured: boolean; enabled: boolean; state: string; session: { connected: boolean } }

export default function ProjectSettingsContent({ target, projects, system, revision }: {
  target: SettingsTarget; projects: Project[]; system: System | null; revision: number;
}) {
  const { person, canManage } = usePermissions();
  const managed = projects.filter(p => canManage(p.id));
  const [selected, setSelected] = useState(target.projectId || managed[0]?.id || '');
  const projectId = target.projectId && canManage(target.projectId) ? target.projectId : selected;
  const account = useData<Account>(['access', 'connections'].includes(target.section) && projectId ? `/api/access/project-accounts/${projectId}` : null, revision);
  const templates = useData<Template[]>(target.section === 'materials' ? '/api/templates' : null, revision);
  return <>
    <p className="notice">当前显示你获授权的项目。人员分配、公司连接和全局策略由系统管理员维护。</p>
    {['access', 'connections'].includes(target.section) && <>
      <section className="settings-card"><h3>项目官网账号状态</h3>
        <label>选择项目<select aria-label="选择项目" value={projectId} onChange={e => setSelected(e.target.value)}>{managed.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <ErrorBox error={account.error} />{account.data && <p>{account.data.configured ? account.data.session.connected ? '项目官网已连接' : '项目官网等待连接' : '项目官网账号尚未配置'} · {account.data.enabled ? '项目写入已开启' : '项目写入已暂停'}</p>}
        {!managed.length && <Empty>当前没有可管理的项目</Empty>}
      </section>
      <section className="settings-card"><h3>IDI 水印相机</h3><p>使用管理员分配的人员账号和手机配对码拍摄本项目照片。</p><a className="button" href="/camera/download">下载安卓安装包</a></section>
      <WebsiteOperations projectId={projectId} revision={revision} />
    </>}
    {target.section === 'recognition' && <section className="settings-card"><h3>识别与模型状态</h3><p>AI 助手：{system?.recognition?.vision_ready ? '可用' : '尚未就绪，请联系系统管理员'}</p><p>本地 OCR：{system?.recognition?.local_ocr_available ? '组件已安装' : '组件未安装'}</p><p className="muted">可在工作台打开 AI 助手查询当前项目资料。接口地址、密钥和全局设置由系统管理员维护。</p></section>}
    {target.section === 'materials' && <>
      {target.tab === 'templates' ? <section className="settings-card"><h3>资料要求</h3><ErrorBox error={templates.error} />{templates.data?.map(t => <details key={t.id}><summary>{t.name}</summary><ul>{t.requirements.map((r, i) => <li key={i}>{PHOTO_GROUPS[r.photo_group || ''] || `${STAGES[r.stage]} · ${ROLES[r.role]}`} · 至少 {r.minimum} 张{r.part && ` · ${r.part}`}{r.per_visit && ' · 每次到访分别核对'}</li>)}</ul></details>)}</section>
        : <section className="settings-card"><h3>{target.tab === 'archive' ? '项目资料' : '项目管理'}</h3><div className="table-wrap"><table><thead><tr><th>项目</th><th>本项目权限</th></tr></thead><tbody>{projects.map(p => <tr key={p.id}><td>{p.name}</td><td>{person.projects[p.id] === 'manager' ? '项目管理员' : '项目成员'}</td></tr>)}</tbody></table></div>{target.tab === 'archive' && <p className="muted">从「资料工具 → 全部照片」查看和下载本项目原件，从「报送记录」下载资料包。</p>}</section>}
    </>}
    {target.section === 'backup' && <section className="settings-card"><h3>备份恢复</h3><p>系统完整备份包含全部项目，由系统管理员统一维护。你的项目照片和报送资料可在资料工具中查看、下载。</p><p className="muted">当前版本：{system?.version || '—'}</p></section>}
  </>;
}
