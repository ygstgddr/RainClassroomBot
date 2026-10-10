import json
import pytest
from core.store import Store
from core.errors import AdaptationRequired
from browser.site_adapter import completion, leaves


def sample(account='a', leaf='1'):
    return dict(key=f'{account}:10:{leaf}', classroom_id='10', leaf_id=leaf,
                course='测试课程', title='任务', kind='video', leaf_type=0,
                state='pending', progress=0, deadline=None, duration=None, note='', evidence='')


@pytest.mark.parametrize('value,expected', [(0, False), (.99, False), (1, True),
                                          ('1', None), (100, None), (True, None), (None, None),
                                          ({'success': True}, None), (float('nan'), None)])
def test_completion_never_guesses(value, expected):
    assert completion(0, value)[0] is expected


def test_nonvideo_completion_semantics():
    assert completion(3, 1)[0] is True
    assert completion(2, .6)[0] is True
    assert completion(6, {'done': 0, 'total': 0})[0] is False
    assert completion(6, {'done': 2, 'total': 3})[0] is False
    assert completion(6, {'done': 3, 'total': 3})[0] is True
    assert completion(99, 1)[0] is None


def test_account_isolation_restart_and_durable_attempts(tmp_path):
    store = Store(tmp_path)
    store.merge_course('a', '10', [sample()])
    store.merge_course('b', '10', [sample('b')])
    store.update('a:10:1', state='running', attempts=2)
    recovered = Store(tmp_path)
    recovered.recover()
    assert recovered.rows('a')[0]['state'] == 'pending'
    assert recovered.rows('a')[0]['attempts'] == 2
    assert recovered.rows('b')[0]['attempts'] == 0
    recovered.merge_course('a', '10', [sample()])
    assert recovered.rows('a')[0]['attempts'] == 2


def test_completion_requires_evidence(tmp_path):
    store = Store(tmp_path)
    store.merge_course('a', '10', [sample()])
    with pytest.raises(ValueError):
        store.update('a:10:1', state='completed')
    store.update('a:10:1', state='completed', evidence=json.dumps({'server': True}))
    assert store.rows('a')[0]['state'] == 'completed'


def test_removed_leaf_hidden_without_erasing_history(tmp_path):
    store = Store(tmp_path)
    store.merge_course('a', '10', [sample(), sample(leaf='2')])
    store.merge_course('a', '10', [sample(leaf='2')])
    assert [t['leaf_id'] for t in store.rows('a')] == ['2']
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 2


def test_unknown_chapter_shape_does_not_report_empty_success():
    with pytest.raises(AdaptationRequired):
        list(leaves([{'new_schema': []}]))
    result = list(leaves([{'section_leaf_list': [
        {'id': 1, 'leaf_type': 0}, {'leaf_list': [{'id': 2, 'leaf_type': 3}]}]}]))
    assert len(result) == 2
