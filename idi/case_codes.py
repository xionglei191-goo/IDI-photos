"""Project-based display codes with permanent ownership of historical aliases."""
import re

from pypinyin import Style, lazy_pinyin

from .db import audit, now


def prefix_for(name: str) -> str:
    value = ''.join(lazy_pinyin(name, style=Style.FIRST_LETTER)).upper()
    return re.sub(r'[^A-Z0-9]', '', value)[:12] or 'XM'


def unique_prefix(con, name: str, project_id: str) -> str:
    base = prefix_for(name)
    candidate, index = base, 2
    while con.execute('SELECT id FROM projects WHERE short_prefix=? AND id<>?', (candidate, project_id)).fetchone():
        candidate, index = f'{base}{index}', index + 1
    return candidate


def issue_code(con, case: dict) -> str | None:
    project = con.execute('SELECT short_prefix FROM projects WHERE id=?', (case['project_id'],)).fetchone()
    building = re.sub(r'[^A-Za-z0-9]', '', case.get('building', '')).upper()
    unit = re.sub(r'[^A-Za-z0-9]', '', case.get('unit', '')).upper()
    room = re.sub(r'[^A-Za-z0-9]', '', case.get('room', '')).upper()
    if case.get('location_type') == 'public':
        room = room or 'GG'
    if not project or not project['short_prefix'] or not building or not room:
        return None
    location = '-'.join([building, *([f'U{unit}'] if unit else []), room])
    base = f"{project['short_prefix']}-{location}-"
    codes = con.execute('SELECT code FROM case_code_aliases WHERE code LIKE ?', (base + '%',)).fetchall()
    return next_code(base, (r[0] for r in codes))


def next_code(base, codes):
    used = [int(code[len(base):]) for code in codes if code.startswith(base) and code[len(base):].isdigit()]
    return base + f'{max(used, default=0) + 1:02d}'


def sync_project_codes(con, project_id: str, prefix: str, *, actor='local') -> int:
    """Rename within the caller's transaction, preserving each code's case owner."""
    if not prefix:
        return 0
    rows = con.execute("SELECT id,short_code,revision FROM cases WHERE project_id=? AND short_code IS NOT NULL AND short_code<>'' ORDER BY created_at,id", (project_id,)).fetchall()
    changes = [(row, prefix + '-' + row['short_code'].split('-', 1)[1]) for row in rows
               if '-' in row['short_code'] and row['short_code'].split('-', 1)[0] != prefix]
    owners = {r['code']: r['case_id'] for r in con.execute('SELECT code,case_id FROM case_code_aliases')}
    # Reserve desired codes before allocating collisions, so one collision cannot
    # take another case's otherwise available original sequence number.
    planned = {code for _, code in changes}
    for row, code in changes:
        if code in owners and owners[code] != row['id']:
            code = next_code(code.rsplit('-', 1)[0] + '-', owners.keys() | planned)
        stamp = now()
        con.execute('UPDATE cases SET short_code=?,revision=revision+1,updated_at=? WHERE id=?', (code, stamp, row['id']))
        owners[code] = row['id']
        audit(con, 'case', row['id'], 'short_code_prefix',
              {'short_code': row['short_code'], 'revision': row['revision']},
              {'short_code': code, 'revision': row['revision'] + 1, 'project_id': project_id}, actor=actor)
    return len(changes)


def normalize_code(value: str) -> str:
    return re.sub(r'\s+', '', str(value).upper()).replace('－', '-').replace('—', '-').replace('–', '-')
