import { useEffect, useState } from 'react';
import { ArrowRight, ChevronLeft, ChevronRight, FolderInput, Plus, RefreshCw, Search, SlidersHorizontal } from 'lucide-react';
import { queryString } from './api';
import { dateTime, Empty, ErrorBox, Loading, place, useData, useDebounce, WORKFLOW_STAGES } from './common';
import type { Case, Job, Summary } from './types';
import type { BoardData } from './workflowTypes';
import WorkBoardTable from './WorkBoardTable';

export default function CaseBoard({ board, source, changeSource, projectId, building, revision, summary, jobs, openCase, showPhotos, sync, create, importFiles, clearFilter }: {
  board: 'intake' | 'work'; source: 'platform' | 'local'; changeSource: (source: 'platform' | 'local') => void; projectId: string; building: string; revision: number; summary: Summary | null; jobs: Job[];
  openCase: (value: Case) => void; showPhotos: (caseId: string, group?: string, review?: string) => void;
  sync: () => void; create: () => void; importFiles: () => void; clearFilter: () => void;
}) {
  const [query, setQuery] = useState(''), delayed = useDebounce(query), [status, setStatus] = useState(''), [page, setPage] = useState(1), [archived, setArchived] = useState(false);
  useEffect(() => { setStatus(''); setPage(1); setArchived(false); setQuery(''); }, [board, source]);
  useEffect(() => setPage(1), [projectId, building, delayed, status, archived]);
  const result = useData<BoardData>('/api/workflow/board?' + queryString({ board, source, project_id: projectId, building, query: delayed, status, page, archived }), revision);
  const data = result.data, runningSync = jobs.find(j => j.kind === 'platform_sync' && ['queued', 'running'].includes(j.state));
  const stats = board === 'intake' ? ['pending_intake', 'insurer_review', 'insurer_intake', 'historical_unverified'] : ['survey', 'repair', 'acceptance', 'settlement'];
  const availableStages = source === 'platform' ? Object.keys(WORKFLOW_STAGES) : [...stats, ...(archived ? ['closed', 'direct_payment', 'noncoverage'] : [])];
  const pages = Math.max(1, Math.ceil((data?.total || 0) / 30));
  useEffect(() => { if (page > pages && !result.loading) setPage(pages); }, [page, pages, result.loading]);
  return <section className="workspace board-workspace">
    <div className="page-heading"><div><div className="eyebrow">案件工作台</div><h1>{board === 'intake' ? '理赔受理' : '查勘维修'}</h1><p>{source === 'platform' ? (board === 'intake' ? '保留官网理赔受理全部记录，点击进度卡片筛选待办案件' : '与官网查勘维修清单对应，按五类照片核对现场资料') : '本地新建及样例案件，关联官网后按官网清单展示'}</p></div><div className="row wrap"><button onClick={importFiles}><FolderInput size={16} />导入照片</button><button onClick={create}><Plus size={16} />新建案件</button><button className="primary" disabled={!!runningSync} onClick={sync}><RefreshCw size={16} className={runningSync ? 'spin' : ''} />{runningSync ? '同步中' : '同步官网台账'}</button></div></div>
    <div className={`board-stats ${board === 'intake' ? 'intake-stats' : ''}`}>
      {stats.map(key => <button aria-pressed={status === key} className={`stat-card ${status === key ? 'selected' : ''}`} key={key} onClick={() => setStatus(status === key ? '' : key)}><span>{WORKFLOW_STAGES[key]}</span><strong>{data?.counts[key] || 0}</strong><small>{key === 'insurer_review' ? '直接赔付或非保险责任分支' : key === 'pending_intake' ? '等待施工 / 维保单位办理' : key === 'insurer_intake' ? '当前处理人为保险公司' : key === 'historical_unverified' ? '旧记录缺少受理结果，仅查看' : '点击查看案件'}</small></button>)}
      {board === 'intake' && <div className="stat-card sync-card"><span>最近完整同步</span><strong className="sync-time">{data?.sync.last_complete ? dateTime(data.sync.last_complete.finished_at) : '尚未同步'}</strong><small>{runningSync ? `正在读取 ${runningSync.progress} / ${runningSync.total || '…'} 个案件` : data?.sync.last_complete ? `${data.sync.last_complete.total} 个官网案件` : '同步后自动保留编号与办理进度'}</small></div>}
    </div>
    {!!summary?.exceptions && <div className="exception-banner"><span><b>{summary.exceptions} 项照片待确认</b> · 编号、位置或分类需要核对</span><button className="text-button" onClick={() => showPhotos('', '', 'exceptions')}>去确认 <ArrowRight size={14} /></button></div>}
    {data?.sync.latest && ['failed', 'partial'].includes(data.sync.latest.state) && <div className="notice">最近台账同步{data.sync.latest.state === 'partial' ? '部分完成' : '未完成'}：{data.sync.latest.error}。已保存的案件和照片仍可使用。</div>}
    {board === 'intake' && !!data?.counts.historical_unverified && <p className="historical-notice">历史待核实记录的报案日期早于 2026-09-24，缺少受理结果且未进入查勘维修清单。记录完整保留，不计入维保待办；已有有效受理结果的早期案件仍按实际流程展示。</p>}
    <div className="board-table-panel">
      <div className="board-source-row"><div className="board-source-tabs" role="group" aria-label="案件来源"><button className={source === 'platform' ? 'active' : ''} aria-pressed={source === 'platform'} onClick={() => changeSource('platform')}>官网清单</button><button className={source === 'local' ? 'active' : ''} aria-pressed={source === 'local'} onClick={() => changeSource('local')}>本地案件</button></div><span className="board-source-total">{source === 'platform' ? `官网清单：${data?.source_total ?? '—'} 案` : '本地新增与样例，不计入官网总数'}</span></div>
      <div className="toolbar board-toolbar"><label className="search-box"><Search size={16} /><input aria-label="搜索案件" value={query} onChange={e => setQuery(e.target.value)} placeholder="短编号、报修编号、项目、房号或问题" /></label><label className="inline-field"><SlidersHorizontal size={15} /><select aria-label="筛选案件进度" value={status} onChange={e => setStatus(e.target.value)}><option value="">全部进度</option>{availableStages.map(key => <option value={key} key={key}>{WORKFLOW_STAGES[key]}</option>)}</select></label>{source === 'local' && board === 'work' && <label className="check"><input type="checkbox" checked={archived} onChange={e => { setArchived(e.target.checked); setStatus(''); }} />含已结束</label>}{(projectId || building) && <button className="text-button" onClick={clearFilter}>清除导航筛选</button>}<span className="muted board-total" role="status" aria-live="polite">当前显示 {data?.total ?? '—'} 案</span></div>
      <ErrorBox error={result.error} />
      {board === 'work' && <p className="board-photo-note">每案上行官网、下行本地。官网数量来自最近同步的附件快照；“—”表示无独立栏位，视频和 PDF 不计入照片。点击本地数量查看原件。共用打卡分别计入查勘和施工用途，原件总数不重复。</p>}
      {result.loading && !data ? <Loading /> : !data?.items.length ? <Empty action={<button onClick={sync}>连接并同步官网台账</button>}>{status || query || projectId ? '没有符合当前条件的案件' : board === 'intake' ? '当前没有受理记录' : '受理结果为现场查勘的案件会出现在这里'}</Empty> : <div className="table-wrap board-table-wrap">
        {board === 'work' ? <WorkBoardTable items={data.items} openCase={openCase} showPhotos={showPhotos} /> : <table className="board-table">
          <thead><tr><th>案件编号</th><th className="report-date-cell">报修日期</th><th>项目 / 报修位置</th><th>报修问题</th><th>当前进度</th><th>下一步</th><th>操作</th></tr></thead>
          <tbody>{data.items.map(c => <tr key={c.id} data-case-id={c.id}>
            <td><button className="case-link case-main-code" title={c.platform_report_code || ''} onClick={() => openCase(c)}>{c.platform_report_code || '报修编号待关联'}</button><small className="short-code" title={`短编号：${c.short_code || '待补'}`}>{c.short_code || '短编号待补'}</small></td>
            <td className="report-date-cell">{c.report_date || '—'}</td>
            <td>{c.project_name}<small>{place(c)}</small></td><td className="issue-cell" title={c.title}>{c.title}{c.responsible && <small>负责人：{c.responsible}</small>}</td>
            <td><span className={`workflow-status workflow-${c.workflow_stage}`}>{c.stage_label}</span>{c.workflow_stage === 'insurer_review' && <small>保司审核中</small>}</td><td>{c.next_action}</td>
            <td><button className={c.workflow_stage === 'pending_intake' ? 'board-primary-action' : 'text-button'} onClick={() => openCase(c)}>{c.action_label}</button></td>
          </tr>)}</tbody>
        </table>}
      </div>}
      <footer className="pagination"><span className="muted">{source === 'platform' ? '清单范围与官网一致；同一案件可同时出现在两个清单' : board === 'work' ? '五类照片分别计数；验收资料在验收环节核对' : '受理结果为现场查勘后进入查勘维修'}</span><div className="row"><button aria-label="上一页案件" disabled={page <= 1 || result.loading} onClick={() => setPage(page - 1)}><ChevronLeft size={16} /></button><span>{page} / {pages}</span><button aria-label="下一页案件" disabled={page >= pages || result.loading} onClick={() => setPage(page + 1)}><ChevronRight size={16} /></button></div></footer>
    </div>
  </section>;
}
