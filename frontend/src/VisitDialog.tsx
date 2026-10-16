import { useEffect, useState, type FormEvent } from 'react';
import { api, message, post, put } from './api';
import { dateTime, ErrorBox, Loading, Modal, PHOTO_GROUPS, place, ROLES, STAGES, useData } from './common';
import type { Case, Notify, Photo, PhotoPage } from './types';
import './visits.css';

export interface Visit {
  id: string; stage: string; visit_date: string; visit_key: string; notes: string; revision: number;
  started_at: string | null; finished_at: string | null; crew_number: number | null; same_day_repair: number;
  photo_ids: string[]; shared_checkin_ids: string[]; counts: Record<string, number>;
}
const photoDay = (p: Photo) => (p.watermark_time || p.exif_time || '').slice(0, 10);
const timeInput = (value: string | null) => (value || '').replace(' ', 'T').slice(0, 16);

export default function VisitDialog({ value, close, notify, refresh }: { value: Case; close: () => void; notify: Notify; refresh: () => void }) {
  const [revision, setRevision] = useState(0), [editing, setEditing] = useState<Visit | null>(null);
  const [stage, setStage] = useState('work'), [date, setDate] = useState(''), [key, setKey] = useState(''), [notes, setNotes] = useState('');
  const [start, setStart] = useState(''), [end, setEnd] = useState(''), [crew, setCrew] = useState('1'), [sameDay, setSameDay] = useState(false);
  const [ids, setIds] = useState<string[]>([]), [shared, setShared] = useState<string[]>([]), [photos, setPhotos] = useState<Photo[]>([]);
  const [busy, setBusy] = useState(false), [loading, setLoading] = useState(true), [error, setError] = useState(''), [matchingDate, setMatchingDate] = useState(true);
  const path = `/api/cases/${value.id}/visits`;
  const list = useData<Visit[]>(path, revision);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    void (async () => {
      const all: Photo[] = [];
      let page = 1;
      while (true) {
        const result = await api<PhotoPage>(`/api/photos?case_id=${value.id}&page_size=500&page=${page}`);
        all.push(...result.items);
        if (all.length >= result.total) break;
        if (!result.items.length) throw new Error('本案照片尚未完整读取，请刷新');
        page += 1;
      }
      if (alive) setPhotos(all);
    })().catch(e => { if (alive) setError(message(e)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [value.id, revision]);
  function edit(visit: Visit | null) {
    setEditing(visit); setError(''); setStage(visit?.stage || 'work'); setDate(visit?.visit_date || '');
    setKey(visit?.visit_key || (visit ? '' : `第 ${(list.data?.filter(v => v.stage === 'work').length || 0) + 1} 次进场`));
    setNotes(visit?.notes || ''); setStart(timeInput(visit?.started_at || null)); setEnd(timeInput(visit?.finished_at || null));
    setCrew(String(visit?.crew_number || 1)); setSameDay(!!visit?.same_day_repair); setIds(visit?.photo_ids || []); setShared(visit?.shared_checkin_ids || []);
  }
  async function save(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError('');
    try {
      const data = { stage, visit_date: date, visit_key: key, notes, started_at: start || null, finished_at: end || null,
        crew_number: stage === 'work' ? crew : null, same_day_repair: stage === 'work' && sameDay,
        photo_ids: ids, shared_checkin_ids: stage === 'work' && sameDay ? shared : [], revision: editing?.revision };
      const result = editing ? await put<Visit>(`${path}/${editing.id}`, data) : await post<Visit>(path, data);
      edit(result); setRevision(v => v + 1); refresh(); notify('本次进场及照片关联已保存，原件保留');
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  const matching = (p: Photo) => !matchingDate || !date || !photoDay(p) || (photoDay(p) >= date && photoDay(p) <= (end?.slice(0, 10) || date));
  const ownPhotos = photos.filter(p => (p.stage === stage || (stage === 'work' && p.photo_group === 'completion')) && (matching(p) || ids.includes(p.id)));
  const sharedPhotos = photos.filter(p => p.photo_group === 'survey_checkin' && ['building_checkin', 'door_checkin'].includes(p.role) && p.media_type === 'image' && (matching(p) || shared.includes(p.id)));
  function choices(items: Photo[], reuse: boolean) {
    const selected = reuse ? shared : ids;
    const change = reuse ? setShared : setIds;
    return <div className="visit-photo-options">{items.map(p => {
      const assignedElsewhere = reuse ? p.shared_checkin_visit_id && p.shared_checkin_visit_id !== editing?.id : p.visit_id && p.visit_id !== editing?.id;
      const wrongDate = reuse && (!date || photoDay(p) !== date);
      const reason = p.recognition_state === 'conflict' ? '识别冲突，需先核对' : assignedElsewhere ? '已关联另一次进场' : wrongDate ? '日期与本次进场不同或未核实' : '';
      return <label className={`visit-photo-option ${reason ? 'unavailable' : ''}`} key={p.id}>
        <input type="checkbox" aria-label={`${p.original_name}（${reuse ? '共用查勘打卡' : '本次资料'}）`} disabled={busy || (!!reason && !selected.includes(p.id))} checked={selected.includes(p.id)} onChange={e => change(v => e.target.checked ? [...v, p.id] : v.filter(id => id !== p.id))} />
        {p.thumbnail_url && <img src={p.thumbnail_url} alt="" loading="lazy" />}
        <span><strong>{p.original_name}</strong><small>{ROLES[p.role] || PHOTO_GROUPS[p.photo_group]} · {dateTime(p.watermark_time || p.exif_time)}</small>
          <small>{reason || (reuse ? '原件仍归查勘；同时用于本次施工打卡' : p.visit_key || p.process || '未分进场')}</small></span>
      </label>;
    })}{!items.length && <p className="muted">当前筛选下没有可选照片。</p>}</div>;
  }
  return <Modal title="进场与维修记录" wide close={() => { if (!busy) close(); }} footer={<><span className="grow muted">照片只保存一份；不同次进场分别核对</span><button disabled={busy} onClick={close}>关闭</button><button className="primary" type="submit" form="visit-record" disabled={busy || loading || !date}>{busy ? '正在保存…' : editing ? '保存本次记录' : '建立进场记录'}</button></>}>
    <p className="muted">{place(value)} · {value.title}</p><ErrorBox error={error || list.error} />
    <div className="visit-record-heading"><h3>已记录的进场</h3><button disabled={busy} onClick={() => edit(null)}>新增一次进场</button></div>
    <div className="visit-record-list">{list.data?.map(v => <article key={v.id} className={editing?.id === v.id ? 'active' : ''}>
      <div className="row between wrap"><strong>{v.visit_date} · {v.visit_key || STAGES[v.stage]}</strong><button disabled={busy} aria-label={`编辑${v.visit_date} ${v.visit_key || STAGES[v.stage]}`} onClick={() => edit(v)}>编辑本次</button></div>
      <p>{STAGES[v.stage]}{v.crew_number ? ` · ${v.crew_number} 人` : ''}{v.started_at ? ` · ${timeInput(v.started_at).slice(11)}` : ''}{v.finished_at ? ` 至 ${timeInput(v.finished_at).slice(0, 10) === v.visit_date ? timeInput(v.finished_at).slice(11) : dateTime(v.finished_at)}` : ''}{v.same_day_repair ? ' · 当天维修、共用查勘打卡' : ''}</p>
      {v.notes && <p className="visit-notes">{v.notes}</p>}
      {v.stage === 'work' && <div className="visit-counts"><span className={v.counts.building_checkin ? '' : 'material-missing'}>楼栋打卡 {v.counts.building_checkin}</span><span className={v.counts.door_checkin ? '' : 'material-missing'}>入户打卡 {v.counts.door_checkin}</span><span className={v.counts.construction ? '' : 'material-missing'}>施工 {v.counts.construction}</span><span>完工 {v.counts.completion}</span>{!!v.counts.shared_checkin && <span>其中共用打卡 {v.counts.shared_checkin}</span>}</div>}
    </article>)}{!list.loading && !list.data?.length && <p className="muted">尚未区分进场。按实际记录建立；同一天多次上门也分别记录。</p>}</div>
    <form id="visit-record" onSubmit={save} className="visit-record-form">
      <h3>{editing ? '编辑本次进场' : '新增进场'}</h3><fieldset disabled={busy}><div className="form-grid">
        <label>进场阶段<select value={stage} onChange={e => { setStage(e.target.value); setIds([]); setShared([]); setSameDay(false); }}>{Object.entries(STAGES).filter(([id]) => id !== 'unknown').map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
        <label>进场日期<input required type="date" value={date} onChange={e => setDate(e.target.value)} /></label>
        <label className="full">本次标记<input aria-label="本次标记" aria-describedby="visit-key-hint" value={key} onChange={e => setKey(e.target.value)} placeholder="如：第一次进场、下午返场" maxLength={120} /><small id="visit-key-hint">同一天多次进场，用不同标记区分。</small></label>
        <label>进场开始时间<input type="datetime-local" value={start} onChange={e => setStart(e.target.value)} /></label>
        <label>本次结束时间<input type="datetime-local" value={end} onChange={e => setEnd(e.target.value)} /></label>
        {stage === 'work' && <label>本次进场人数<input type="number" min={1} max={10000} value={crew} onChange={e => setCrew(e.target.value)} /></label>}
        <label className="full">本次维修记录<textarea rows={3} value={notes} onChange={e => setNotes(e.target.value)} placeholder="记录本次施工内容、结果和后续安排" /></label>
        {stage === 'work' && <label className="check full"><input type="checkbox" checked={sameDay} onChange={e => { setSameDay(e.target.checked); if (!e.target.checked) setShared([]); }} />查勘后当天维修，与查勘是同一次进场</label>}
      </div></fieldset>
      <label className="check space-top"><input type="checkbox" checked={matchingDate} onChange={e => setMatchingDate(e.target.checked)} />按本次日期筛选照片</label>
      <p className="muted">日期筛选用于找照片，不会自动把同一天的多次施工合并。已属于其他进场的照片需先从原记录移除。</p>
      {loading ? <Loading /> : <><h4>本次资料 · 已选 {ids.length}</h4>{choices(ownPhotos, false)}
        {stage === 'work' && sameDay && <><h4>共用本次查勘打卡 · 已选 {shared.length}</h4><p className="muted">选择楼栋及入户照片。保留查勘分类，同时计入这一次施工；后续进场需使用新的打卡。</p>{choices(sharedPhotos, true)}</>}</>}
    </form>
  </Modal>;
}
