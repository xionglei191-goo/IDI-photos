import type { PlatformContract, PlatformField, PlatformPlan } from './platformTypes';

export const visibleReviewField = (field: PlatformField, values: Record<string, unknown>) =>
  Object.entries(field.when || {}).every(([key, allowed]) => allowed.includes(String(values[key] ?? '')));

export default function ReviewBusinessFields({ plan, values, dictionaries, editable, disabled, change }: {
  plan: PlatformPlan; values: Record<string, string | number>; dictionaries: PlatformContract['dictionaries'];
  editable: boolean; disabled: boolean; change: (key: string, value: string) => void;
}) {
  return <section className="review-business" aria-label="核对业务信息"><h3>业务信息</h3>
    <fieldset className="platform-fieldset" disabled={disabled}><div className="form-grid">
      {plan.fields.filter(f => f.kind !== 'files' && visibleReviewField(f, values)).map(field => {
        const value = String(values[field.key] ?? '');
        const options = field.options || dictionaries[field.dictionary || ''] || [];
        const issues = editable ? plan.issues.filter(i => i.severity === 'error' && i.field === field.key) : [];
        const hintId = `review-field-${field.key}`;
        let display = options.find(o => o.code === value)?.name || value || '—';
        if (field.kind === 'personnel') display = plan.personnel.filter(p => value.split(',').includes(p.userId)).map(p => p.name).join('、') || '—';
        if (field.name_key && plan.values[field.name_key]) display = String(plan.values[field.name_key]);
        const common = { 'aria-label': field.label, 'aria-invalid': !!issues.length, 'aria-describedby': issues.length ? hintId : undefined };
        return <label key={field.key} className={field.kind === 'textarea' || field.kind === 'personnel' ? 'full' : ''}>
          <span>{field.label}{editable && field.required && <b className="required-star"> *</b>}</span>
          {!editable || field.kind === 'readonly' ? <output aria-label={field.label} className="platform-output">{display}</output>
            : field.kind === 'textarea' ? <textarea {...common} rows={2} value={value} onChange={e => change(field.key, e.target.value)} />
            : ['select', 'dictionary'].includes(field.kind) ? <select {...common} value={value} onChange={e => change(field.key, e.target.value)}>
              <option value="">请选择</option>{value && !options.some(o => o.code === value) && <option value={value}>待核对：{value}</option>}
              {options.map(o => <option key={o.code} value={o.code}>{o.name}</option>)}</select>
            : field.kind === 'personnel' ? <select {...common} multiple value={value.split(',').filter(Boolean)} onChange={e => change(field.key, Array.from(e.target.selectedOptions, o => o.value).join(','))}>
              {plan.personnel.map(p => <option value={p.userId} key={p.userId}>{p.name}{p.phone ? ` · ${p.phone}` : ''}</option>)}</select>
            : <input {...common} type={field.kind === 'datetime' ? 'datetime-local' : field.kind === 'date' ? 'date' : field.kind === 'integer' ? 'number' : 'text'}
              min={field.kind === 'integer' ? 1 : undefined} step={field.kind === 'integer' ? 1 : undefined}
              value={field.kind === 'datetime' ? value.replace(' ', 'T').slice(0, 16) : value} onChange={e => change(field.key, e.target.value)} />}
          {!!issues.length && <small id={hintId} className="review-field-error">{issues.map(i => i.message).join('；')}</small>}
        </label>;
      })}
    </div></fieldset>
    {Object.values(plan.field_sources || {}).some(s => s.origin === 'automatic') && <details className="review-sources"><summary>查看自动填写依据</summary>
      <dl>{plan.fields.filter(f => plan.field_sources?.[f.key]?.origin === 'automatic' && visibleReviewField(f, values)).map(f =>
        <div key={f.key}><dt>{f.label}</dt><dd>{plan.field_sources![f.key].source}</dd></div>)}</dl>
    </details>}
  </section>;
}
