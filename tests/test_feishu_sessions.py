from idi.db import dump, now, uid
from idi.feishu_history import FeishuHistory
from tests.test_feishu_agent import bot, capture, task_for


def insert(bot, key, *, kind='chat', text='', sender='ou_member', binding=None, result='', **extra):
    binding = binding or bot.binding(bot.test_connector['id'])
    with bot.db.tx() as con:
        task_id = bot._insert_task(con, binding, kind, key,
            {'text': text, 'conversation': {'mode': 'member'}, 'photo_ids': [], **extra}, sender=sender)
        con.execute("UPDATE feishu_agent_tasks SET state='completed',result_json=? WHERE id=?", (dump({'text': result}), task_id))
    return task_id


def test_group_session_collects_members_topics_images_and_revisions_but_not_briefs(bot, project):
    capture(bot, '成员甲的问题', sender='ou_member')
    capture(bot, '成员乙的问题', sender='ou_owner', root_id='om_other_topic')
    photo_mid = capture(bot, '', images=1, sender='ou_owner')
    bot.collect_messages()
    next_revision = {**bot.binding(bot.test_connector['id']), 'revision': 2}
    insert(bot, 'new-revision', binding=next_revision, text='新配置下的提问')
    insert(bot, 'brief-day-one', kind='brief', date='2026-10-16', result='第一天的简报')
    insert(bot, 'brief-day-two', kind='brief', date='2026-10-17', result='第二天的简报')
    before = bot.db.all('SELECT * FROM feishu_agent_tasks')
    history = FeishuHistory(bot)
    listing = history.sessions(kind='all')
    assert listing['counts'] == {'group': 1, 'brief': 1}
    group = next(s for s in listing['sessions'] if s['session_kind'] == 'group')
    assert group['turn_count'] == 4 and group['member_count'] == 2 and group['image_turns'] == 1
    turns = history.transcript(group['id'])['turns']
    assert {t['binding_revision'] for t in turns} == {1, 2}
    assert any(t['conversation_mode'] == 'topic' for t in turns)
    assert all(t['kind'] != 'brief' for t in turns)
    photo_turn = next(t for t in turns if t['source_message_id'] == photo_mid)
    assert photo_turn['photos'] == [{'id': task_for(bot, photo_mid)['input']['photo_ids'][0]}]
    brief = next(s for s in listing['sessions'] if s['session_kind'] == 'brief')
    assert brief['project_id'] == project['id'] and brief['turn_count'] == 2
    assert [t['reply_text'] for t in history.transcript(brief['id'])['turns']] == ['第一天的简报', '第二天的简报']
    assert bot.db.all('SELECT * FROM feishu_agent_tasks') == before


def test_group_session_ids_isolate_connector_and_chat_and_remain_stable(bot):
    original = bot.binding(bot.test_connector['id'])
    insert(bot, 'original', text='原群消息')
    historical = {**original, 'chat_id': 'oc_previous'}
    insert(bot, 'historical', binding=historical, text='历史群消息')
    other = bot.connectors.save({'name': '第二个接入', 'kind': 'feishu', 'enabled': True,
        'config': {'app_id': 'cli_second', 'chat_id': original['chat_id'], 'chat_name': '另一接入的群', 'auto_ocr': False}})
    insert(bot, 'different-connector', binding={**original, 'connector_id': other['id']}, text='另一接入的消息')
    history = FeishuHistory(bot)
    sessions = history.sessions()['sessions']
    assert len(sessions) == 3 and len({s['id'] for s in sessions}) == 3
    assert all(len(history.transcript(s['id'])['turns']) == 1 for s in sessions)
    stable = next(s['id'] for s in sessions if s['latest']['source_excerpt'] == '原群消息')
    insert(bot, 'same-group-later', text='原群追加')
    assert history.sessions()['sessions'][0]['id'] == stable
    assert history.transcript(stable)['session']['turn_count'] == 2


def test_full_session_history_paginates_past_sixty_and_searches_older_complete_text(bot, client, monkeypatch):
    for i in range(95):
        insert(bot, f'long-history-{i}', text=f'第{i}个问题', result=('内容' * 200 + '历史深处的检索词') if not i else f'第{i}个答复')
    assert len(bot.tasks()) == 60
    monkeypatch.setattr(bot.followup, 'client', lambda *_: (_ for _ in ()).throw(AssertionError('read must not contact Feishu')))
    listing = client.get('/api/feishu-agent/sessions', params={'q': '历史深处的检索词'}).json()
    assert listing['total'] == 1 and listing['sessions'][0]['turn_count'] == 95
    session_id = listing['sessions'][0]['id']
    first = client.get(f'/api/feishu-agent/sessions/{session_id}').json()
    assert len(first['turns']) == 30 and first['has_more']
    sequences = [t['sequence'] for t in first['turns']]
    assert sequences == sorted(sequences)
    # New arrivals cannot shift the earlier-page cursor.
    insert(bot, 'after-first-page', text='分页期间的新消息')
    before = first['before']; all_ids = {t['id'] for t in first['turns']}
    while True:
        page = client.get(f'/api/feishu-agent/sessions/{session_id}', params={'before': before}).json()
        assert not all_ids.intersection(t['id'] for t in page['turns'])
        assert all(t['sequence'] < before for t in page['turns'])
        all_ids.update(t['id'] for t in page['turns']); before = page['before']
        if not page['has_more']: break
    assert len(all_ids) == 95
    assert '历史深处的检索词' in page['turns'][0]['reply_text']
    assert client.get(f'/api/feishu-agent/sessions/{session_id}').json()['session']['turn_count'] == 96
    assert not bot.test_client.sent


def test_session_attention_keeps_processing_and_delivery_independent_and_validates_requests(bot, client):
    task_id = insert(bot, 'unknown-delivery', text='问题', result='已生成答复')
    with bot.db.tx() as con:
        con.execute('INSERT INTO feishu_agent_outbox(id,task_id,recipient_id,content,state,error,created_at) VALUES(?,?,?,?,?,?,?)',
            (uid(), task_id, 'ou_member', dump({'text': '已生成答复'}), 'unknown', '送达未知', now()))
    old = bot.db.all('SELECT * FROM feishu_agent_outbox')
    listing = client.get('/api/feishu-agent/sessions?attention=true').json()
    assert listing['total'] == 1 and listing['sessions'][0]['attention_count'] == 1
    session_id = listing['sessions'][0]['id']
    turn = client.get(f'/api/feishu-agent/sessions/{session_id}').json()['turns'][0]
    assert turn['state'] == 'completed' and turn['delivery_state'] == 'unknown'
    assert bot.db.all('SELECT * FROM feishu_agent_outbox') == old
    for path in ['/sessions?limit=100', '/sessions?offset=-1', '/sessions?kind=invalid',
                 '/sessions?q=' + 'a' * 161, f'/sessions/{session_id}?before=-1', f'/sessions/{session_id}?limit=100']:
        assert client.get('/api/feishu-agent' + path).status_code == 422
    assert client.get('/api/feishu-agent/sessions/fs_nonexistent').status_code == 404
    assert client.get('/api/feishu-agent/sessions?kind=brief').json()['sessions'] == []


def test_session_list_pagination_does_not_split_turns_and_search_is_literal(bot):
    binding = bot.binding(bot.test_connector['id'])
    for i in range(23):
        insert(bot, f'group-{i}', binding={**binding, 'chat_id': f'oc_group{i}'}, text=f'群消息{i}')
    history = FeishuHistory(bot)
    first = history.sessions(limit=20); second = history.sessions(offset=20, limit=20)
    assert first['total'] == 23 and first['has_more'] and len(first['sessions']) == 20
    assert len(second['sessions']) == 3 and not second['has_more']
    assert not {s['id'] for s in first['sessions']} & {s['id'] for s in second['sessions']}
    assert history.sessions(query='%')['total'] == 0
