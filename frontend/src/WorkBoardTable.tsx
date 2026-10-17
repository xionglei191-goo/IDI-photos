import { ChevronRight } from 'lucide-react';
import { dateTime, PHOTO_GROUPS, place } from './common';
import type { Case } from './types';
import type { BoardCase, OnlinePhotoCount } from './workflowTypes';

function OnlineCount({ value, label }: { value?: OnlinePhotoCount; label: string }) {
  const state = value?.state || 'unread';
  const text = state === 'unsupported' ? '—' : state === 'unlinked' ? '未关联' : state === 'unread' ? '未读取'
    : state === 'partial' ? (value?.count ? `≥${value.count}` : '待核实') : String(value?.count ?? 0);
  const extras = [value?.videos ? `${value.videos} 视频` : '', value?.other ? `${value.other} 其他附件` : ''].filter(Boolean).join(' · ');
  return <div className={`online-photo-count online-count-${state}`} title={value?.note || '官网附件尚未读取'}>
    <span aria-label={`官网 ${label} ${text}${state === 'known' || (state === 'partial' && value?.count) ? '张' : ''}`}>{text}</span>
    {extras && <small>{extras}</small>}
  </div>;
}

export default function WorkBoardTable({ items, openCase, showPhotos }: {
  items: BoardCase[]; openCase: (value: Case) => void; showPhotos: (caseId: string, group?: string) => void;
}) {
  return <table className="board-table work-board-table" aria-label="查勘维修官网与本地照片对照">
    <thead>
      <tr>
        <th rowSpan={2} scope="col">案件编号 / 项目</th><th rowSpan={2} scope="col" className="report-date-cell">报修日期</th><th rowSpan={2} scope="col">报修问题</th><th rowSpan={2} scope="col">当前进度</th>
        <th rowSpan={2} scope="col" className="photo-source-heading">来源</th><th colSpan={5} scope="colgroup" className="photo-count-heading">照片数量</th>
        <th rowSpan={2} scope="col" title="本地草稿与官网已有资料的准备核对">平台准备</th><th rowSpan={2} scope="col">保司资料</th><th rowSpan={2} scope="col">下一步</th>
      </tr>
      <tr>{Object.values(PHOTO_GROUPS).map(label => <th scope="col" className="photo-count-heading" key={label}>{label}</th>)}</tr>
    </thead>
    {items.map(c => <tbody className="case-photo-group" key={c.id} data-case-id={c.id}>
      <tr data-photo-source="official">
        <td rowSpan={2}><button className="case-link case-main-code" title={c.platform_report_code || ''} onClick={() => openCase(c)}>{c.platform_report_code || '报修编号待关联'}</button><small className="short-code" title={`短编号：${c.short_code || '待补'}`}>{c.short_code || '短编号待补'}</small><small>{c.project_name} · {place(c)}</small></td>
        <td rowSpan={2} className="report-date-cell">{c.report_date || '—'}</td>
        <td rowSpan={2} className="issue-cell" title={c.title}>{c.title}{c.responsible && <small>负责人：{c.responsible}</small>}</td>
        <td rowSpan={2}><span className={`workflow-status workflow-${c.workflow_stage}`}>{c.stage_label}</span>{!!c.work_visit_count && <small>{c.work_visit_count} 次施工进场</small>}</td>
        <th scope="row" className="photo-source-cell official-source" title={c.online_photos?.read_at ? `官网照片实际核验：${dateTime(c.online_photos.read_at)}；检查列表不会更新此时间` : '尚无已核实的官网快照'}>官网<small>{c.online_photos?.read_at ? new Date(c.online_photos.read_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '未关联 / 未读取'}</small></th>
        {Object.keys(PHOTO_GROUPS).map(group => <td className="photo-count-cell official-photo-cell" data-photo-group={group} key={group}><OnlineCount value={c.online_photos?.groups[group]} label={PHOTO_GROUPS[group]} /></td>)}
        <td rowSpan={2}><span className={c.platform_missing ? 'material-missing' : 'material-ready'}>{c.platform_status}</span></td>
        <td rowSpan={2}><button className={`text-button ${c.insurer_missing ? 'material-missing' : 'material-ready'}`} onClick={() => openCase(c)}>{c.insurer_status}</button></td>
        <td rowSpan={2}><button className="board-next" onClick={() => openCase(c)}>{c.action_label}<ChevronRight size={14} /></button></td>
      </tr>
      <tr data-photo-source="local">
        <th scope="row" className="photo-source-cell local-source">本地<small>已归档原件</small></th>
        {Object.keys(PHOTO_GROUPS).map(group => <td className="photo-count-cell" data-photo-group={group} key={group}>
          <button aria-label={`${c.short_code || c.title} ${PHOTO_GROUPS[group]} ${c.photo_counts[group]}张`} title={`查看本地${PHOTO_GROUPS[group]}`} className={c.photo_counts[group] ? 'count-present' : ['survey_checkin', 'survey_problem'].includes(group) || c.workflow_stage !== 'survey' ? 'count-missing' : 'count-future'} onClick={() => showPhotos(c.id, group)}>{c.photo_counts[group]}</button>{group === 'work_checkin' && !!c.shared_checkin_count && <small title="同一次进场共用查勘原件，不增加原件总数">共用 {c.shared_checkin_count}</small>}
        </td>)}
      </tr>
    </tbody>)}
  </table>;
}
