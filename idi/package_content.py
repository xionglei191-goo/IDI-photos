"""Compare exported material independently of export time, ordering and runtime refreshes."""
import hashlib
import json

from .domain import CASE_FIELDS


def content_fingerprint(manifest):
    def pick(value, fields):
        return {key: value.get(key) for key in sorted(fields)}

    def ordered(values):
        return sorted(values, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))

    # The case preference can change while a project-default or explicitly chosen
    # export still uses the same template. The actual template is compared below.
    case = pick(manifest['case'], (CASE_FIELDS - {'insurer_template_id'}) | {'id', 'project_name', 'workflow_stage'})
    photos = []
    for source in manifest['photos']:
        photo = {key: value for key, value in source.items() if key not in ('revision', 'package_path', 'export')}
        photo['sources'] = ordered(photo.get('sources', []))
        photo['confirmed_fields'] = sorted(photo.get('confirmed_fields', []))
        photos.append(photo)
    visits = []
    for source in manifest.get('visits', []):
        visit = {key: value for key, value in source.items() if key not in ('created_at', 'updated_at', 'revision', 'counts')}
        for field in ('photo_ids', 'shared_checkin_ids', 'copied_checkin_ids'):
            visit[field] = sorted(visit.get(field, []))
        visits.append(visit)
    quality = {**manifest['completeness']}
    quality['items'] = ordered([{**item, 'photo_ids': sorted(item.get('photo_ids', []))} for item in quality['items']])
    content = {'case': case, 'photos': ordered(photos), 'visits': ordered(visits), 'completeness': quality,
               'template': pick(manifest['template'], ('id', 'name', 'insurer', 'requirements')),
               'parent_id': manifest.get('parent_id'), 'format': manifest.get('format', 'report-bundle-v1'),
               'root_directory': manifest.get('root_directory'), 'record': manifest.get('record')}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
