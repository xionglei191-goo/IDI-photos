import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, Archive, ArrowLeft, Cable, DatabaseBackup, FolderCog, ScanLine, Settings2 } from 'lucide-react';
import { ErrorBox, useData } from './common';
import SettingsWorkspace from './SettingsWorkspace';
import Connections from './Connections';
import JobsWorkspace from './JobsWorkspace';
import RuntimeStatus from './RuntimeStatus';
import AutomaticUploads from './AutomaticUploads';
import SyncControls from './SyncControls';
import PlatformAccountSettings from './PlatformAccountSettings';
import { FollowupConfigEditor } from './ProjectFollowup';
import type { FollowupConfig } from './ProjectFollowup';
import type { Case, Job, Notify, Project, System } from './types';
import type { PlatformSession } from './platformTypes';
import type { SyncStatus } from './workflowTypes';
import './settings-center.css';

export type SettingsSection = 'connections' | 'automation' | 'recognition' | 'materials' | 'tasks' | 'backup';
export interface SettingsTarget { section: SettingsSection; tab?: string }
const SECTIONS = [
  { key: 'connections', label: '连接与收图', hint: '官网、飞书和文件夹', icon: Cable },
  { key: 'automation', label: '自动处理', hint: '同步、整理与保存开关', icon: Settings2 },
  { key: 'recognition', label: '识别与模型', hint: '文字识别、模型接口', icon: ScanLine },
  { key: 'materials', label: '项目与资料', hint: '项目、要求与资料目录', icon: FolderCog },
  { key: 'tasks', label: '后台任务', hint: '保存进度与运行记录', icon: Activity },
  { key: 'backup', label: '备份恢复', hint: '完整备份与本机信息', icon: DatabaseBackup },
] as const;

interface Props {
  target: SettingsTarget; navigate: (target: SettingsTarget) => void; back: () => void; backLabel: string;
  system: System | null; session: PlatformSession | null; projects: Project[]; cases: Case[]; currentCase?: Case; jobs: Job[];
  revision: number; refresh: () => void; notify: Notify; login: () => void; sync: () => Promise<void>;
  editProject: (project: Project) => void; newProject: () => void; openCase: (id: string) => void;
  leave: () => Promise<boolean>; registerLeave: (callback: (() => Promise<boolean>) | null) => void;
}

function AutomaticSettings(props: Props) {
  const sync = useData<SyncStatus>('/api/workflow/sync', props.revision);
  const followup = useData<{ config: FollowupConfig }>('/api/followup', props.revision);
  const [editingFollowup, setEditingFollowup] = useState(false);
  const config = followup.data?.config, schedule = sync.data?.schedule;
  const runtimeLinks = { login: props.login, settings: () => props.navigate({ section: 'recognition' }), channels: () => props.navigate({ section: 'connections' }) };
  return <div className="settings-policy-grid">
    <section className="settings-card" aria-label="自动同步设置"><h3>官网台账更新</h3><p>{schedule ? `每天 ${String(schedule.hour).padStart(2, '0')}:${String(schedule.minute).padStart(2, '0')}（北京时间）自动检查官网清单。` : '正在读取更新安排…'}手动同步保留在案件看板。</p><SyncControls value={sync.data || undefined} sync={props.sync} refresh={props.refresh} notify={props.notify} mode="policy" /><ErrorBox error={sync.error} /></section>
    <RuntimeStatus {...runtimeLinks} mode="policy" />
    <AutomaticUploads view="policy" cases={props.cases} currentCase={props.currentCase} openCase={props.openCase} leave={props.leave} refresh={props.refresh} notify={props.notify} login={props.login} />
    <section className="settings-card" aria-label="项目检查设置"><h3>项目照片检查</h3><p>检查查勘维修案件的照片内容，具体问题在项目跟进中核对。</p>{config && <p className="settings-current">自动检查：{config.auto_review ? '已开启' : '已暂停'} · 照片 AI 审核：{config.ai_review ? '已开启' : '未开启'}</p>}<button disabled={!config} onClick={() => setEditingFollowup(true)}>调整项目检查</button><ErrorBox error={followup.error} /></section>
    {editingFollowup && config && <FollowupConfigEditor config={config} close={() => setEditingFollowup(false)} saved={() => { props.refresh(); props.notify('项目检查设置已保存'); }} openSettings={() => props.navigate({ section: 'recognition' })} />}
  </div>;
}

export default function SettingsCenter(props: Props) {
  const { target, navigate, system, projects, cases, jobs, revision, refresh, notify, login, editProject, newProject, registerLeave } = props;
  const accountLeave = useRef<(() => Promise<boolean>) | null>(null), workspaceLeave = useRef<(() => Promise<boolean>) | null>(null);
  const registerAccountLeave = useCallback((callback: (() => Promise<boolean>) | null) => { accountLeave.current = callback; }, []);
  const registerWorkspaceLeave = useCallback((callback: (() => Promise<boolean>) | null) => { workspaceLeave.current = callback; }, []);
  useEffect(() => {
    registerLeave(async () => (!accountLeave.current || await accountLeave.current()) && (!workspaceLeave.current || await workspaceLeave.current()));
    return () => registerLeave(null);
  }, [registerLeave]);
  const section = SECTIONS.find(item => item.key === target.section) || SECTIONS[0];
  const materialTab = ['projects', 'templates', 'archive'].includes(target.tab || '') ? target.tab! : 'projects';
  const taskTab = ['uploads', 'runtime', 'jobs'].includes(target.tab || '') ? target.tab! : 'uploads';
  const existingTab = target.section === 'recognition' || target.section === 'backup' ? target.section : materialTab;
  const hasExistingPanel = ['recognition', 'materials', 'backup'].includes(target.section);
  return <section className="workspace settings-center" aria-label="系统设置">
    <div className="page-heading"><div><h1>系统设置</h1><p>连接、自动处理和运行记录集中管理</p></div><button onClick={props.back}><ArrowLeft size={15} aria-hidden="true" />{props.backLabel}</button></div>
    <div className="settings-layout">
      <nav className="settings-nav" aria-label="设置分类">{SECTIONS.map(item => <button key={item.key} aria-label={item.label} className={target.section === item.key ? 'active' : ''} aria-current={target.section === item.key ? 'page' : undefined} onClick={() => navigate({ section: item.key })}><item.icon size={18} aria-hidden="true" /><span><strong>{item.label}</strong><small>{item.hint}</small></span></button>)}</nav>
      <div className="settings-content">
        <div className="settings-section-heading"><h2>{section.label}</h2><p>{section.hint}</p></div>
        <div hidden={target.section !== 'connections'}><PlatformAccountSettings refresh={refresh} notify={notify} registerLeave={registerAccountLeave} /></div>
        {target.section === 'connections' && <Connections embedded projects={projects} cases={cases} revision={revision} refresh={refresh} notify={notify} />}
        {target.section === 'automation' && <AutomaticSettings {...props} />}
        {target.section === 'materials' && <div className="settings-subnav" role="group" aria-label="资料管理分类">{[['projects', '项目管理'], ['templates', '资料要求'], ['archive', '资料目录']].map(([tab, label]) => <button key={tab} aria-pressed={materialTab === tab} onClick={() => navigate({ section: 'materials', tab })}>{label}</button>)}</div>}
        {target.section === 'tasks' && <><div className="settings-subnav" role="group" aria-label="后台任务分类">{[['uploads', '保存进度'], ['runtime', '运行详情'], ['jobs', '任务记录']].map(([tab, label]) => <button key={tab} aria-pressed={taskTab === tab} onClick={() => navigate({ section: 'tasks', tab })}>{label}</button>)}</div>
          {taskTab === 'uploads' && <AutomaticUploads view="progress" currentCase={props.currentCase} openCase={props.openCase} leave={props.leave} refresh={refresh} notify={notify} login={login} />}
          {taskTab === 'runtime' && <RuntimeStatus login={login} settings={() => navigate({ section: 'recognition' })} channels={() => navigate({ section: 'connections' })} />}
          {taskTab === 'jobs' && <JobsWorkspace embedded jobs={jobs} refresh={refresh} notify={notify} />}
        </>}
        <div hidden={!hasExistingPanel}><SettingsWorkspace embedded initialTab={existingTab} system={system} projects={projects} revision={revision} refresh={refresh} notify={notify} editProject={editProject} newProject={newProject} registerLeave={registerWorkspaceLeave} /></div>
        {target.section === 'backup' && <p className="settings-help"><Archive size={15} aria-hidden="true" />备份包含案件、照片原件和报送记录。</p>}
      </div>
    </div>
  </section>;
}
