import { useEffect, useState } from 'react';
import { FileImage, FolderInput, MessageSquare, Plus, RefreshCw } from 'lucide-react';
import { message, post, put } from './api';
import { dateTime, Empty, ErrorBox, Modal, place, State, useData } from './common';
import type { Case, Connector, Notify, Project } from './types';
import { PhotoEditor } from './editors';
import './connections.css';

function ConnectorEditor({ value, projects, cases, close, saved, notify }: { value?: Connector; projects: Project[]; cases: Case[]; close: () => void; saved: () => void; notify: Notify }) {
  const [kind] = useState(value?.kind || 'folder'), [name, setName] = useState(value?.name || ''), [path, setPath] = useState(value?.config.path || ''), [groups] = useState(value?.config.groups.join('\n') || ''), [project, setProject] = useState(value?.config.project_id || ''), [caseId, setCaseId] = useState(value?.config.case_id || ''), [ocr, setOcr] = useState(value?.config.auto_ocr ?? true), [enabled, setEnabled] = useState(value?.enabled || false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  async function save() { setBusy(true); setError(''); try { const data = { name, kind, enabled, config: { path, groups: groups.split(/\n/).map(s => s.trim()).filter(Boolean), project_id: project || null, case_id: caseId || null, auto_ocr: ocr } }; if (value) await put(`/api/connectors/${value.id}`, data); else await post('/api/connectors', data); saved(); notify('采集收图文件夹已保存'); close(); } catch (e) { setError(message(e)); } finally { setBusy(false); } }
  return <Modal title={value ? '编辑收图文件夹' : '新增收图文件夹'} close={close} footer={<><button onClick={close}>取消</button><button className="primary" disabled={busy || !name.trim()} onClick={() => void save()}>{busy ? '保存中…' : '保存配置'}</button></>}><ErrorBox error={error} /><div className="form-grid"><label>名称<input value={name} onChange={e => setName(e.target.value)} placeholder="例如：铂悦府资料收件" /></label><label>对应项目<select value={project} onChange={e => { setProject(e.target.value); setCaseId(''); }}><option value="">待确定</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label><label>固定案件<select value={caseId} onChange={e => setCaseId(e.target.value)}><option value="">按照片及上下文匹配</option>{cases.filter(c => !project || c.project_id === project).map(c => <option key={c.id} value={c.id}>{place(c)} · {c.title}</option>)}</select></label><label className="full">照片文件夹路径<input value={path} onChange={e => setPath(e.target.value)} placeholder="D:\项目资料\待归档" /><small>递归接收照片和 PDF；文件保存稳定后归档，相同原件按内容去重。</small></label><label className="check full"><input type="checkbox" checked={ocr} onChange={e => setOcr(e.target.checked)} />收到图片后进行本地OCR</label><label className="check full"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用此入口</label></div></Modal>;
}

interface Probe { bot_name: string; groups: { chat_id: string; name: string }[] }

function FeishuEditor({ value, projects, close, saved, notify }: { value?: Connector; projects: Project[]; close: () => void; saved: () => void; notify: Notify }) {
  const [name, setName] = useState(value?.name || '飞书群收图'), [appId, setAppId] = useState(value?.config.app_id || ''), [secret, setSecret] = useState('');
  const [chatId, setChatId] = useState(value?.config.chat_id || ''), [chatName, setChatName] = useState(value?.config.chat_name || ''), [project, setProject] = useState(value?.config.project_id || '');
  const [ocr, setOcr] = useState(value?.config.auto_ocr ?? true), [enabled, setEnabled] = useState(value?.enabled ?? true), [busy, setBusy] = useState(''), [error, setError] = useState(''), [probe, setProbe] = useState<Probe | null>(null);
  async function check() {
    setBusy('probe'); setError('');
    try { const result = await post<Probe>('/api/connectors/feishu/probe', { app_id: appId, app_secret: secret, connector_id: value?.id }); setProbe(result); if (!value && result.groups.length === 1) { setChatId(result.groups[0].chat_id); setChatName(result.groups[0].name); } }
    catch (e) { setError(message(e)); } finally { setBusy(''); }
  }
  async function save() {
    setBusy('save'); setError('');
    try { const data = { name, kind: 'feishu', enabled, app_secret: secret || undefined, config: { app_id: appId, chat_id: chatId, chat_name: chatName, project_id: project || null, auto_ocr: ocr } }; if (value) await put(`/api/connectors/${value.id}`, data); else await post('/api/connectors', data); setSecret(''); saved(); notify(enabled ? '飞书入口已保存，正在连接接收' : '飞书入口已保存'); close(); }
    catch (e) { setError(message(e)); } finally { setBusy(''); }
  }
  return <Modal title={value ? '配置飞书群' : '接入飞书群'} close={close} footer={<><button disabled={!!busy} onClick={close}>取消</button><button className="primary" disabled={!!busy || !name.trim() || !chatId || (!secret && !value?.secret_configured)} onClick={() => void save()}>{busy === 'save' ? '保存中…' : '保存配置'}</button></>}>
    <ErrorBox error={error} /><div className="form-grid feishu-connector-form">
      <label>入口名称<input value={name} onChange={e => setName(e.target.value)} /></label>
      <label>对应项目<select value={project} onChange={e => setProject(e.target.value)}><option value="">按照片信息匹配</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      <label className="full">App ID<input value={appId} disabled={!!value} onChange={e => { setAppId(e.target.value); setProbe(null); setChatId(''); }} placeholder="cli_…" autoComplete="off" /></label>
      <label className="full">App Secret<input type="password" value={secret} onChange={e => setSecret(e.target.value)} autoComplete="new-password" placeholder={value?.secret_configured ? '已加密保存；留空继续使用' : '填写飞书应用密钥'} /><small>只用于本机连接；以当前 Windows 用户加密保存。</small></label>
      <div className="full"><button disabled={!!busy || !appId || (!secret && !value?.secret_configured)} onClick={() => void check()}>{busy === 'probe' ? '正在验证…' : '验证连接并读取群列表'}</button>{probe && <p role="status">已验证应用{probe.bot_name ? `「${probe.bot_name}」` : ''}，可访问 {probe.groups.length} 个群。</p>}</div>
      <label className="full">接收群{value ? <input value={chatName} readOnly /> : <select value={chatId} disabled={!probe} onChange={e => { setChatId(e.target.value); setChatName(probe?.groups.find(g => g.chat_id === e.target.value)?.name || ''); }}><option value="">先验证连接，再选择群</option>{probe?.groups.map(g => <option value={g.chat_id} key={g.chat_id}>{g.name}</option>)}</select>}<small>需要企业内部群，且应用机器人已入群。首次启用后开始收集；服务重启后补查离线期间消息。</small></label>
      <label className="check full"><input type="checkbox" checked={ocr} onChange={e => setOcr(e.target.checked)} />收到图片后进行本地 OCR</label>
      <label className="check full"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用此入口</label>
    </div>
  </Modal>;
}

interface MessageRecord { id: string; connector_id: string; source_key: string; group_name: string; sender: string; message_type: string; content: string; sent_at: string | null; received_at: string; media_state: string; photo_id: string | null; raw?: { original_name?: string }; error: string | null }

function ReceiptContent({ value, open }: { value: MessageRecord; open: (id: string) => void }) {
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  useEffect(() => setThumbnailFailed(false), [value.photo_id]);
  const image = value.message_type === 'image';
  const saved = value.media_state === 'saved' && value.photo_id;
  const label = image ? '查看照片' : '查看附件';
  const type = ({ image: '图片', file: '文件', video: '视频', text: '文字' } as Record<string, string>)[value.message_type] || value.message_type;
  return <div className="receipt-content">
    {saved && <button className="receipt-preview" aria-label={image ? '查看照片缩略图' : '查看附件详情'} title={label} onClick={() => open(saved)}>
      {image && !thumbnailFailed ? <img src={`/api/photos/${encodeURIComponent(saved)}/thumbnail`} alt="群内收到的照片" loading="lazy" decoding="async" onError={() => setThumbnailFailed(true)} /> : <FileImage size={26} aria-hidden="true" />}
    </button>}
    <div>{value.content || (image ? '图片' : value.raw?.original_name || `[${type}]`)}{saved && <small>{image ? '点击缩略图查看大图' : '点击查看归档附件'}</small>}{value.error && <small className="danger-text">{value.error}</small>}</div>
  </div>;
}

export default function Connections({ projects, cases, revision, refresh, notify, embedded = false }: { projects: Project[]; cases: Case[]; revision: number; refresh: () => void; notify: Notify; embedded?: boolean }) {
  const [editor, setEditor] = useState<Connector | 'new' | 'feishu' | null>(null), [tab, setTab] = useState('connectors'), [failed, setFailed] = useState(false), [busy, setBusy] = useState(''), [tick, setTick] = useState(0);
  const [photoId, setPhotoId] = useState<string | null>(null);
  useEffect(() => { const timer = window.setInterval(() => setTick(t => t + 1), 5000); return () => window.clearInterval(timer); }, []);
  const list = useData<Connector[]>('/api/connectors', revision + tick);
  const messages = useData<MessageRecord[]>(tab === 'messages' ? '/api/messages?failed_only=' + failed : null, revision + tick);
  const visible = list.data?.filter(c => ['folder', 'feishu'].includes(c.kind)) || [];
  const receipts = messages.data?.filter(m => visible.some(c => c.id === m.connector_id)) || [];
  async function toggle(value: Connector) { setBusy(value.id); try { await put(`/api/connectors/${value.id}`, { ...value, enabled: !value.enabled }); refresh(); notify('入口状态已更新'); } catch (e) { notify(message(e), true); } finally { setBusy(''); } }
  async function retry(id: string, connector = false) { setBusy(id); try { const result = await post<{ media_state: string; error?: string }>(connector ? `/api/connectors/${id}/retry` : `/api/messages/${id}/retry`); notify(result.media_state === 'saved' ? '附件已重新归档' : result.media_state === 'pending' ? '已加入重试队列，将重新下载并归档' : result.error || '附件仍未保存', !['saved', 'pending'].includes(result.media_state)); refresh(); } catch (e) { notify(message(e), true); } finally { setBusy(''); } }
  return <section className="workspace">
    <div className="page-heading"><div>{embedded ? <h2>收图入口</h2> : <h1>收图入口</h1>}<p>从飞书群或照片文件夹自动收集，识别水印并匹配案件</p></div><div className="row"><button onClick={refresh}><RefreshCw size={15} />刷新</button><button onClick={() => setEditor('new')}><FolderInput size={15} />新增文件夹</button><button className="primary" onClick={() => setEditor('feishu')}><Plus size={15} />接入飞书群</button></div></div>
    <div className="tabs"><button className={tab === 'connectors' ? 'active' : ''} onClick={() => setTab('connectors')}><FolderInput size={15} />收图入口</button><button className={tab === 'messages' ? 'active' : ''} onClick={() => setTab('messages')}><MessageSquare size={15} />接收记录</button></div><ErrorBox error={list.error || messages.error} />
    {tab === 'connectors' ? !visible.length ? <Empty>接入一个飞书群或照片文件夹，服务运行期间持续接收新增资料。</Empty> : <div className="table-wrap grow"><table style={{ minWidth: 1050 }}><thead><tr><th>入口 / 项目</th><th>连接状态</th><th>消息 / 附件</th><th>接收与补查</th><th>说明</th><th>操作</th></tr></thead><tbody>{visible.map(c => <tr key={c.id}>
      <td><strong>{c.name}</strong><small>{c.kind === 'feishu' ? '飞书群' : '文件夹'} · {projects.find(p => p.id === c.config.project_id)?.name || '按照片信息匹配'}</small></td>
      <td><State value={c.status} />{c.kind === 'feishu' && <small>{c.enabled ? '每 30 秒补查一次' : '启用后继续补查'}</small>}</td>
      <td>消息 {c.queue?.total ?? c.counts.messages ?? 0}<small>附件已入库 {c.counts.saved || 0} · 失败 {c.counts.failed || 0}</small>{c.queue && <small>等待处理 {c.queue.pending || 0} · 消息处理失败 {c.queue.failed || 0}</small>}</td>
      <td>{dateTime(c.last_seen_at)}{c.capture && <><small>实时事件：{dateTime(c.capture.last_event_at)}</small><small>补查成功：{dateTime(c.capture.last_history_at)}</small></>}</td>
      <td className="error-cell">{c.config.chat_name || c.config.path}<small className="danger-text">{c.error || c.queue?.last_error}</small>{c.kind === 'feishu' && !c.queue?.total && <small>等待群内新消息；照片入库后显示接收记录。</small>}</td>
      <td><div className="row wrap"><button className="compact-button" onClick={() => setEditor(c)}>配置</button><button className="compact-button" disabled={busy === c.id} onClick={() => void toggle(c)}>{c.enabled ? '停用' : '启用'}</button>{c.kind === 'feishu' && !!c.queue?.failed && <button disabled={busy === c.id || !c.enabled} onClick={() => void retry(c.id, true)}>重试失败消息</button>}</div></td>
    </tr>)}</tbody></table></div> : <><div className="toolbar"><label className="check"><input type="checkbox" checked={failed} onChange={e => setFailed(e.target.checked)} />仅看附件失败或待保存</label><span className="muted">最近 200 条记录 · 自动刷新</span></div>{!receipts.length ? <Empty>当前没有接收记录</Empty> : <div className="table-wrap grow"><table className="receipt-table" style={{ minWidth: 950 }}><thead><tr><th>来源 / 发送人</th><th>内容 / 图片</th><th>原消息 / 本机接收时间</th><th>附件情况</th><th>操作</th></tr></thead><tbody>{receipts.map(m => <tr key={m.id} data-message-id={m.id}>
      <td>{m.group_name || visible.find(c => c.id === m.connector_id)?.name || '—'}{m.sender && <small title={m.sender}>{m.sender}</small>}</td>
      <td><ReceiptContent value={m} open={setPhotoId} /></td>
      <td>{dateTime(m.sent_at)}<small>接收：{dateTime(m.received_at)}</small></td>
      <td><State value={m.media_state} /></td>
      <td className="receipt-actions">{m.media_state === 'saved' && m.photo_id && <button onClick={() => setPhotoId(m.photo_id)}>{m.message_type === 'image' ? '查看照片' : '查看附件'}</button>}{m.media_state === 'failed' && <button disabled={busy === m.id} onClick={() => void retry(m.id)}>重试保存</button>}</td>
    </tr>)}</tbody></table></div>}</>}
    {editor && (editor === 'feishu' || (typeof editor === 'object' && editor.kind === 'feishu') ? <FeishuEditor value={typeof editor === 'object' ? editor : undefined} projects={projects} close={() => setEditor(null)} saved={refresh} notify={notify} /> : <ConnectorEditor value={editor === 'new' ? undefined : editor} projects={projects} cases={cases} close={() => setEditor(null)} saved={refresh} notify={notify} />)}
    {photoId && <PhotoEditor key={photoId} photoId={photoId} cases={cases} projects={projects} close={() => setPhotoId(null)} saved={refresh} notify={notify} />}
  </section>;
}
