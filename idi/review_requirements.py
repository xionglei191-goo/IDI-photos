"""Classify incomplete evidence without relaxing official submission rules."""


def pending_checkins(plan):
    """Only missing check-ins qualify, after the stage's main evidence is present.

    `existing` comes from the official stage detail, independently of whether a
    remote attachment has been downloaded into the local photo archive.
    """
    main_key = {'survey': 'fileInfoList', 'repair': 'recoveredFileInfo'}.get(plan.get('stage'))
    if not main_key:
        return []
    fields = {field['key']: field for field in plan['file_fields']}
    main = fields.get(main_key, {})
    if not (main.get('existing') or main.get('selected')):
        return []
    errors = [issue for issue in plan['issues'] if issue['severity'] == 'error']
    if not errors or any(issue.get('code') != 'required'
                         or not fields.get(issue['field'], {}).get('checkin') for issue in errors):
        return []
    return [{'field': issue['field'], 'label': fields[issue['field']]['label'], 'message': issue['message']}
            for issue in errors]
