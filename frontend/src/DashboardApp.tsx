import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, Archive, ArrowLeft, Building2, CheckCircle2, ChevronDown, ChevronRight, FileText, FolderOpen, Menu, Plus, Search, Settings, X } from 'lucide-react';
import { api, message, post } from './api';
import { Loading, PHOTO_GROUPS } from './common';
import CaseBoard from './CaseBoard';
import CaseDetail from './CaseDetail';
import PhotoWorkspace from './PhotoWorkspace';
import ImportDialog from './ImportDialog';
import Reporting, { ExportDialog } from './Reporting';
import ProjectFollowup from './ProjectFollowup';
import SettingsCenter from './SettingsCenter';
import type { SettingsTarget } from './SettingsCenter';
import WorkspaceChat from './WorkspaceChat';
import RuntimeStatus from './RuntimeStatus';
import AutomaticUploads from './AutomaticUploads';
import { LoginDialog } from './PlatformWorkspace';
import { CaseEditor, ProjectEditor } from './editors';
import type { Case, Filter, Job, Package, Project, Summary, System } from './types';
import type { PlatformSession } from './platformTypes';
import type { SyncStatus } from './workflowTypes';
import './workflow.css';

type Board = 'intake' | 'work';
type Utility = 'photos' | 'packages' | 'connectors' | 'jobs' | 'settings' | 'requirements' | 'followup' | 'recognition';
const EMPTY_FILTER: Filter = { project_id: '', case_id: '', stage: '', review: '', photo_group: '' };
const intake = (value: Case) => ['pending_intake', 'insurer_intake', 'insurer_review', 'historical_unverified'].includes(value.workflow_stage);

export default function DashboardApp() {
  const [board, setBoard] = useState<Board>('intake'), [utility, setUtility] = useState<Utility | null>(() => new URLSearchParams(window.location.search).get('view') === 'followup' ? 'followup' : null), [caseId, setCaseId] = useState('');
  const [settingsTarget, setSettingsTarget] = useState<SettingsTarget>({ section: 'automation' });
  const [settingsReturn, setSettingsReturn] = useState<Utility | null>(null);
  const [source, setSource] = useState<'auto' | 'platform' | 'local'>('auto'), [listsReady, setListsReady] = useState(false);
  const activeSource = source === 'auto' ? (listsReady ? 'platform' : 'local') : source;
  const [projectId, setProjectId] = useState(''), [building, setBuilding] = useState(''), [filter, setFilter] = useState<Filter>(EMPTY_FILTER), [revision, setRevision] = useState(0);
  const [projects, setProjects] = useState<Project[]>([]), [cases, setCases] = useState<Case[]>([]), [jobs, setJobs] = useState<Job[]>([]), [summary, setSummary] = useState<Summary | null>(null), [system, setSystem] = useState<System | null>(null);
  const [selected, setSelected] = useState<string[]>([]), [expanded, setExpanded] = useState<Record<string, boolean>>({}), [sidebarOpen, setSidebarOpen] = useState(false), [connectionError, setConnectionError] = useState(''), [treeQuery, setTreeQuery] = useState('');
  const [projectEditor, setProjectEditor] = useState<Project | 'new' | null>(null), [caseEditor, setCaseEditor] = useState<Case | 'new' | null>(null), [importMode, setImportMode] = useState<string | null>(null), [exportOpen, setExportOpen] = useState(false), [parentPackage, setParentPackage] = useState<Package | null>(null);
  const [toast, setToast] = useState<{ text: string; error: boolean } | null>(null), [login, setLogin] = useState(false), [syncAfterLogin, setSyncAfterLogin] = useState(false), [session, setSession] = useState<PlatformSession | null>(null);
  const lastSignature = useRef(''), platformLeave = useRef<(() => Promise<boolean>) | null>(null);
  const registerLeave = useCallback((callback: (() => Promise<boolean>) | null) => { platformLeave.current = callback; }, []);
  const leave = useCallback(async () => !platformLeave.current || await platformLeave.current(), []);
  const refresh = useCallback(() => setRevision(v => v + 1), []);
  const notify = useCallback((text: string, error = false) => setToast({ text, error }), []);
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(null), 6500); return () => window.clearTimeout(timer); }, [toast]);
  useEffect(() => {
    const abort = new AbortController();
    Promise.all([api<Project[]>('/api/projects', { signal: abort.signal }), api<Case[]>('/api/cases', { signal: abort.signal }), api<Summary>('/api/summary', { signal: abort.signal }), api<System>('/api/system', { signal: abort.signal }), api<Job[]>('/api/jobs', { signal: abort.signal }), api<PlatformSession>('/api/platform/session', { signal: abort.signal }), api<SyncStatus>('/api/workflow/sync', { signal: abort.signal })]).then(([p, c, s, sys, j, connection, sync]) => {
      setProjects(p); setCases(c); setSummary(s); setSystem(sys); setJobs(j); setSession(connection); setConnectionError('');
      setListsReady(!!sync.lists);
      setExpanded(prev => Object.keys(prev).length || !p.length ? prev : { [p[0].id]: true, [`${p[0].id}:${c.find(v => v.project_id === p[0].id)?.building || ''}`]: true });
    }).catch(e => { if (!abort.signal.aborted) setConnectionError(message(e)); });
    return () => abort.abort();
  }, [revision]);
  useEffect(() => {
    let cancelled = false, running = false;
    const timer = window.setInterval(async () => { if (running) return; running = true; try { const [j, s] = await Promise.all([api<Job[]>('/api/jobs'), api<Summary>('/api/summary')]); if (cancelled) return; const signature = JSON.stringify([j.map(v => [v.id, v.state, v.progress]), s]); setJobs(j); setSummary(s); if (signature !== lastSignature.current) refresh(); lastSignature.current = signature; setConnectionError(''); } catch (e) { if (!cancelled) setConnectionError(message(e)); } finally { running = false; } }, 3000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [refresh]);

  async function goBoard(next: Board) { if (!await leave()) return; setBoard(next); setUtility(null); setCaseId(''); setSidebarOpen(false); }
  async function goSettings(target: SettingsTarget) { if (!await leave()) return; if (utility !== 'settings') setSettingsReturn(utility); setSettingsTarget(target); setUtility('settings'); setSidebarOpen(false); }
  async function backFromSettings() { if (await leave()) setUtility(settingsReturn); }
  async function goUtility(next: Utility) { const targets: Partial<Record<Utility, SettingsTarget>> = { settings: { section: 'automation' }, recognition: { section: 'recognition' }, requirements: { section: 'materials', tab: 'templates' }, connectors: { section: 'connections' }, jobs: { section: 'tasks', tab: 'jobs' } }; if (targets[next]) { await goSettings(targets[next]!); return; } if (!await leave()) return; setUtility(next); setSidebarOpen(false); }
  async function changeSource(next: 'platform' | 'local') { if (!await leave()) return; setSource(next); setProjectId(''); setBuilding(''); setCaseId(''); setTreeQuery(''); setUtility(null); }
  async function openCase(value: Case, preferredBoard?: Board) {
    if (!await leave()) return;
    const scopes = value.platform_lists || [];
    const currentScope = board === 'intake' ? 'intake' : 'repair';
    setCaseId(value.id); setSource(value.platform_linked ? 'platform' : 'local');
    if (preferredBoard || utility === 'followup') setBoard(preferredBoard || 'work');
    else if (!scopes.includes(currentScope)) setBoard(scopes.length ? (scopes.includes('repair') ? 'work' : 'intake') : intake(value) ? 'intake' : 'work');
    setUtility(null); setSidebarOpen(false);
    setExpanded(prev => ({ ...prev, [value.project_id]: true, [`${value.project_id}:${value.building}`]: true }));
  }
  async function selectLocation(nextProject: string, nextBuilding = '') { if (!await leave()) return; if (utility === 'followup') { setBoard('work'); setSource(cases.some(c => c.followup_eligible && c.platform_lists.includes('repair') && (!nextProject || c.project_id === nextProject)) ? 'platform' : 'local'); } setProjectId(nextProject); setBuilding(nextBuilding); setCaseId(''); setUtility(null); setSidebarOpen(false); }
  async function showPhotos(nextCase: string, group = '', review = '') {
    if (!await leave()) return; const c = cases.find(v => v.id === nextCase);
    setFilter({ ...EMPTY_FILTER, case_id: nextCase, project_id: c?.project_id || '', photo_group: group, review });
    setUtility('photos'); setSidebarOpen(false); setSelected([]);
  }
  function newCase() { if (!projects.length) { setProjectEditor('new'); notify('请先建立项目，再新建案件'); } else setCaseEditor('new'); }
  function openImport(mode = 'upload', targetCase = caseId) { if (!projects.length) { setProjectEditor('new'); notify('请先建立资料所属项目'); return; } const c = cases.find(v => v.id === targetCase); setFilter({ ...EMPTY_FILTER, project_id: c?.project_id || projectId, case_id: c?.id || '' }); setImportMode(mode); }
  async function syncLedger() { if (!await leave()) return; if (!session?.connected) { setSyncAfterLogin(true); setLogin(true); return; } try { await post('/api/workflow/sync'); refresh(); notify('官网全量对账已排队，将限速核对两张清单及案件详情'); } catch (e) { notify(message(e), true); refresh(); } }
  async function connected(value: PlatformSession) { setSession(value); if (syncAfterLogin) { try { await post('/api/workflow/sync'); notify('官网已连接，台账同步已开始'); } catch (e) { notify(message(e), true); } setSyncAfterLogin(false); } refresh(); }
  function supplement(value: Package) { setParentPackage(value); void showPhotos(value.case_id); notify(`选择本次补充资料，生成 v${value.version} 的补件版本`); }
  const currentCase = cases.find(c => c.id === caseId);
  const searchTerm = treeQuery.trim().toLowerCase();
  const treeCases = useMemo(() => cases.filter(c => (utility === 'followup' ? c.followup_eligible : (activeSource === 'platform' ? c.platform_lists?.includes(board === 'intake' ? 'intake' : 'repair') : !c.platform_linked && intake(c) === (board === 'intake'))) && (!searchTerm || [c.short_code, ...(c.short_code_aliases || []), c.platform_report_code, c.report_number, c.policy_number, c.project_name, `${c.building}-${c.room}`, c.room, c.title].some(value => value?.toLowerCase().includes(searchTerm)))), [cases, searchTerm, board, activeSource, utility]);
  const utilityNames: Partial<Record<Utility, string>> = { photos: '全部照片', packages: '报送记录' };

  return <div className="app-shell v2-shell">
    <header className="app-header"><button className="icon-button mobile-nav-toggle" aria-label="展开快速导航" onClick={() => setSidebarOpen(!sidebarOpen)}><Menu size={20} /></button><a className="brand" href="/" onClick={e => { e.preventDefault(); void goBoard('intake'); }}><Archive size={23} /><span>IDI 维保工作台</span></a><div className="header-end"><button className="text-button connection-control" onClick={() => { setSyncAfterLogin(false); setLogin(true); }}><span className={`connection-dot ${session?.connected ? '' : 'offline'}`} />{session?.connected ? '官网已连接' : '连接官网'}</button><details className="action-menu utility-menu"><summary aria-label="资料工具"><FolderOpen size={16} aria-hidden="true" /><span className="utility-label">资料工具</span></summary><div>{(Object.keys(utilityNames) as Utility[]).map(key => <button key={key} onClick={e => { e.currentTarget.closest('details')?.removeAttribute('open'); if (key === 'photos') void showPhotos(''); else void goUtility(key); }}>{utilityNames[key]}</button>)}</div></details><button className="settings-control" aria-label="系统设置" aria-current={utility === 'settings' ? 'page' : undefined} onClick={() => void goSettings(settingsTarget)}><Settings size={16} aria-hidden="true" /><span>系统设置</span></button></div></header>
    <div className="app-body"><aside className={`sidebar ${sidebarOpen ? 'is-open' : ''}`} aria-label="快速导航"><nav className="work-menu" aria-label="工作看板"><button className={!utility && board === 'intake' ? 'active' : ''} onClick={() => void goBoard('intake')}><FileText size={19} aria-hidden="true" />理赔受理</button><button className={!utility && board === 'work' ? 'active' : ''} onClick={() => void goBoard('work')}><Building2 size={19} aria-hidden="true" />查勘维修</button><button className={utility === 'followup' ? 'active' : ''} onClick={() => void goUtility('followup')}><CheckCircle2 size={19} aria-hidden="true" />项目跟进</button></nav><div className="sidebar-heading"><span>快速导航</span><button className="icon-button" aria-label="新建项目" onClick={() => setProjectEditor('new')}><Plus size={16} /></button></div><label className="tree-search"><Search size={14} /><input aria-label="快速定位案件" value={treeQuery} onChange={e => setTreeQuery(e.target.value)} placeholder="编号 / 项目 / 房号" /></label><button className={`all-case-link ${!projectId && !caseId && !utility ? 'active' : ''}`} onClick={() => void selectLocation('')}><FolderOpen size={16} />{utility === 'followup' ? '跟进案件' : '全部案件'}<span>{treeCases.length}</span></button><div className="sidebar-section-label">项目 / 楼栋 / 案件</div><div className="project-tree">{projects.filter(p => (utility !== 'followup' || cases.some(c => c.project_id === p.id && c.followup_eligible)) && (!searchTerm || treeCases.some(c => c.project_id === p.id) || p.name.toLowerCase().includes(searchTerm))).map(p => { const projectCases = treeCases.filter(c => c.project_id === p.id), buildings = [...new Set(projectCases.map(c => c.building))].sort((a, b) => a.localeCompare(b, 'zh-CN', { numeric: true })); return <div key={p.id}><div className={`project-row ${projectId === p.id && !building && !caseId ? 'active' : ''}`}><button className="tree-toggle" aria-label={`${expanded[p.id] ? '收起' : '展开'}${p.name}`} onClick={() => setExpanded(prev => ({ ...prev, [p.id]: !prev[p.id] }))}>{expanded[p.id] || searchTerm ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button><button className="project-button" onClick={() => void selectLocation(p.id)}><FolderOpen size={15} /><span>{p.name}</span><small>{projectCases.length}</small></button></div>{(expanded[p.id] || searchTerm) && <div className="building-tree">{buildings.map(block => { const key = `${p.id}:${block}`, rows = projectCases.filter(c => c.building === block); return <div key={key}><div className={`building-row ${projectId === p.id && building === block && !caseId ? 'active' : ''}`}><button className="tree-toggle" aria-label={`${expanded[key] ? '收起' : '展开'}${p.name}${block || '未填'}栋案件`} onClick={() => setExpanded(prev => ({ ...prev, [key]: !prev[key] }))}>{expanded[key] || searchTerm ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</button><button onClick={() => void selectLocation(p.id, block)}><Building2 size={14} />{block ? `${block} 栋` : '楼栋待补'}<small>{rows.length}</small></button></div>{(expanded[key] || searchTerm) && <div className="case-tree">{rows.map(c => <button key={c.id} className={caseId === c.id ? 'active' : ''} onClick={() => void openCase(c)} title={`${c.short_code || ''} · ${c.title}`}><FileText size={14} /><span><strong>{c.room ? `${c.room} 号` : c.location_detail || '位置待补'}{c.unit && ` · ${c.unit}单元`}</strong><small>{c.short_code || c.platform_report_code || c.title}</small></span></button>)}</div>}</div>; })}{!projectCases.length && <span className="tree-empty">当前看板暂无案件</span>}</div>}</div>; })}</div><div className="sidebar-footer"><small>原件保留 · OCR 自动归类</small></div></aside>
      {sidebarOpen && <button className="sidebar-scrim" aria-label="关闭快速导航" onClick={() => setSidebarOpen(false)} />}
      <main className="main-work-area"><RuntimeStatus mode="summary" openDetails={() => void goSettings({ section: 'tasks', tab: 'runtime' })} login={() => { setSyncAfterLogin(false); setLogin(true); }} settings={() => void goUtility('recognition')} channels={() => void goUtility('connectors')} openUploads={() => void goSettings({ section: 'tasks', tab: 'uploads' })} />{connectionError && <div className="connection-banner" role="alert">无法连接本机服务：{connectionError}<button onClick={refresh}>重试</button></div>}
        <AutomaticUploads suppressed={utility === 'settings'} currentCase={!utility ? currentCase : undefined} openCase={id => { const value = cases.find(c => c.id === id); if (value) void openCase(value, 'work'); }} leave={leave} refresh={refresh} notify={notify} login={() => { setSyncAfterLogin(false); setLogin(true); }} />
        {utility && utility !== 'followup' && utility !== 'settings' && <div className="utility-back"><button className="text-button" onClick={() => { void leave().then(ok => { if (ok) setUtility(null); }); }}><ArrowLeft size={14} />{currentCase ? '返回案件' : '返回看板'}</button>{utility === 'photos' && filter.photo_group && <span>{PHOTO_GROUPS[filter.photo_group]}</span>}{utility === 'photos' && filter.review === 'exceptions' && <span>待确认照片</span>}</div>}
        {!utility && !caseId && <CaseBoard board={board} source={activeSource} changeSource={next => void changeSource(next)} projectId={projectId} building={building} query={treeQuery} onQueryChange={setTreeQuery} revision={revision} summary={summary} jobs={jobs} openCase={v => void openCase(v)} showPhotos={(id, group, review) => void showPhotos(id, group, review)} sync={syncLedger} refresh={refresh} notify={notify} create={newCase} importFiles={() => openImport('upload', '')} clearFilter={() => void selectLocation('')} />}
        {!utility && caseId && (currentCase ? <CaseDetail key={`${caseId}:${board}`} board={board} value={currentCase} cases={cases} revision={revision} notify={notify} back={() => void goBoard(board)} goWork={() => void openCase(currentCase, 'work')} edit={setCaseEditor} refresh={refresh} showPhotos={(id, group, review) => void showPhotos(id, group, review)} importFiles={id => openImport('upload', id)} registerLeave={registerLeave} leave={leave} /> : <Loading />)}
        {utility === 'photos' && <PhotoWorkspace filter={filter} cases={cases} projects={projects} revision={revision} selected={selected} setSelected={setSelected} importFiles={() => openImport('upload', filter.case_id)} exportFiles={() => setExportOpen(true)} refresh={refresh} notify={notify} visionReady={!!system?.recognition.vision_ready} />}
        {utility === 'packages' && <Reporting revision={revision} cases={cases} selected={selected} createExport={() => setExportOpen(true)} supplement={supplement} refresh={refresh} notify={notify} />}
        {utility === 'followup' && <ProjectFollowup projects={projects} cases={cases} refresh={refresh} notify={notify} openCase={value => void openCase(value)} openSettings={() => void goUtility('recognition')} openAutomation={() => void goSettings({ section: 'automation' })} />}
        {utility === 'settings' && <SettingsCenter target={settingsTarget} navigate={target => void goSettings(target)} back={() => void backFromSettings()} backLabel={settingsReturn === 'followup' ? '返回项目跟进' : settingsReturn ? '返回资料' : currentCase ? '返回案件' : '返回工作台'} system={system} session={session} projects={projects} cases={cases} currentCase={currentCase} jobs={jobs} revision={revision} refresh={refresh} notify={notify} login={() => { setSyncAfterLogin(false); setLogin(true); }} sync={syncLedger} editProject={setProjectEditor} newProject={() => setProjectEditor('new')} openCase={id => { const value = cases.find(c => c.id === id); if (value) void openCase(value, 'work'); }} leave={leave} registerLeave={registerLeave} />}
      </main>
    </div>
    {toast && <div className={`toast ${toast.error ? 'toast-error' : ''}`} role={toast.error ? 'alert' : 'status'}>{toast.error ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}<span>{toast.text}</span><button className="icon-button" aria-label="关闭提示" onClick={() => setToast(null)}><X size={16} /></button></div>}
    {projectEditor && <ProjectEditor project={projectEditor === 'new' ? undefined : projectEditor} close={() => setProjectEditor(null)} saved={p => { setProjects(prev => [...prev.filter(v => v.id !== p.id), p]); refresh(); }} notify={notify} />}
    {caseEditor && <CaseEditor value={caseEditor === 'new' ? undefined : caseEditor} projects={projects} projectId={projectId} close={() => setCaseEditor(null)} saved={v => { refresh(); void openCase(v); }} notify={notify} />}
    {importMode && <ImportDialog projects={projects} cases={cases} projectId={filter.project_id} caseId={filter.case_id} close={() => setImportMode(null)} submitted={refresh} notify={notify} initialMode={importMode} />}
    {exportOpen && <ExportDialog ids={selected} parentId={parentPackage?.id} close={() => setExportOpen(false)} submitted={() => { refresh(); setUtility('packages'); setParentPackage(null); }} notify={notify} />}
    {login && <LoginDialog close={() => setLogin(false)} connected={s => void connected(s)} configure={() => { setLogin(false); setSyncAfterLogin(false); void goSettings({ section: 'connections' }); }} />}
    <WorkspaceChat projects={projects} currentCase={!utility ? currentCase : undefined} projectId={projectId} openCase={async id => { const value = cases.find(c => c.id === id) || await api<Case>('/api/cases/' + id); await openCase(value); }} openSettings={() => void goUtility('recognition')} />
  </div>;
}
