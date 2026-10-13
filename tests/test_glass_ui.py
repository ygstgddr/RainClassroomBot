"""User-facing actions for the desktop UI, using fictional data and no browser."""
import tkinter as tk

import pytest

from core.store import Store
from gui.interface import Interface
from main import demo_data


@pytest.fixture
def desktop(tmp_path):
    store = Store(tmp_path)
    demo_data(store)
    store.set('desired', 'idle')
    store.set('worker_busy', False)
    root = tk.Tk()
    root.withdraw()
    ui = Interface(root, store)
    root.update()
    try:
        yield root, ui, store
    finally:
        ui.closing = True
        root.destroy()


def test_primary_start_accepts_changed_selection_without_extra_scan_button(desktop):
    root, ui, store = desktop
    ui.select_group('none')
    ui.toggle_course('1')
    assert not store.scope_scanned()
    ui.primary_button.invoke()
    assert store.get('desired') == 'run'
    assert store.get('run_mode') == 'start'
    assert store.selected_courses() == ['1']


def test_primary_button_pauses_then_allows_reselection_and_resume(desktop):
    root, ui, store = desktop
    ui.primary_button.invoke()
    store.set('worker_busy', True)
    ui.refresh()
    ui.primary_button.invoke()
    assert store.get('desired') == 'paused'

    # Pause is cooperative: changing the scope must remain blocked until the
    # worker acknowledges it and exits its current operation.
    ui.select_group('none')
    assert store.selected_courses() == ['1', '2']
    store.set('worker_busy', False)
    ui.select_group('none')
    ui.toggle_course('2')
    ui.refresh()
    ui.primary_button.invoke()
    assert store.get('desired') == 'run'
    assert store.get('run_mode') == 'resume'
    assert store.selected_courses() == ['2']


def test_primary_never_starts_an_empty_course_selection(desktop):
    root, ui, store = desktop
    ui.select_group('none')
    ui.show_page('tasks')
    ui.primary_button.invoke()
    assert store.get('desired') not in ('run', 'scan')
    assert not store.selected_courses()


def test_primary_pauses_an_in_progress_scan_without_queueing_resume(desktop):
    root, ui, store = desktop
    store.set('desired', 'run')
    store.set('worker_busy', True)
    store.set('scan_progress', dict(status='running', total=2, completed=1,
        tasks=2, failed=0, phase='扫描课程'))
    ui.refresh()
    ui.primary_button.invoke()
    assert store.get('desired') == 'paused'
    # Periodic refresh is display-only; it must not revive a user's pause.
    ui.refresh()
    assert store.get('desired') == 'paused'


def test_primary_button_remains_visible_at_minimum_window_size(desktop):
    root, ui, store = desktop
    width, height = root.minsize()
    root.geometry(f'{width}x{height}+20+20')
    root.deiconify()
    ui.show_page('courses')
    root.update()
    button = ui.primary_button
    assert button.winfo_ismapped()
    left = button.winfo_rootx() - root.winfo_rootx()
    top = button.winfo_rooty() - root.winfo_rooty()
    assert left >= 0 and top >= 0
    assert left + button.winfo_width() <= root.winfo_width()
    assert top + button.winfo_height() <= root.winfo_height()


def test_course_name_cell_click_selects_row_and_search_preserves_selection(desktop):
    root, ui, store = desktop
    root.deiconify()
    ui.show_page('courses')
    root.update()
    x, y, width, height = ui.course_tree.bbox('1', 'name')
    # Click the name column, not the checkbox column: the whole row is an input.
    ui.course_tree.event_generate('<Button-1>', x=x + min(width // 2, 40), y=y + height // 2)
    ui.course_tree.event_generate('<ButtonRelease-1>', x=x + min(width // 2, 40), y=y + height // 2)
    root.update()
    assert store.selected_courses() == ['2']

    ui.course_search.set('英语')
    assert ui.course_tree.get_children() == ('1',)
    assert store.selected_courses() == ['2']
    ui.course_search.set('不存在的课程')
    assert not ui.course_tree.get_children()
    assert '没有匹配' in ui.course_empty.cget('text')
    ui.course_search.set('')
    assert set(ui.course_tree.get_children()) == {'1', '2'}
    assert store.selected_courses() == ['2']


def test_task_filters_search_and_completed_toggle_show_expected_rows(desktop):
    root, ui, store = desktop
    ui.show_page('tasks')
    ui.task_filter.set('PPT')
    assert ui.tree.get_children() == ('demo:2:3',)
    ui.task_filter.set('需人工处理')
    assert ui.tree.get_children() == ('demo:2:4',)
    ui.task_filter.set('图文')
    assert not ui.tree.get_children()
    assert '筛选' in ui.task_empty.cget('text')
    ui.only_unfinished.set(False)
    assert ui.tree.get_children() == ('demo:1:2',)
    ui.task_filter.set('全部类型')
    ui.task_search.set('Unit 1')
    assert ui.tree.get_children() == ('demo:1:1',)
    assert len(store.rows()) == 4


def test_empty_queue_guidance_distinguishes_no_selection_from_unscanned(desktop):
    root, ui, store = desktop
    ui.select_group('none')
    ui.show_page('tasks')
    assert not ui.tree.get_children()
    assert '先选' in ui.task_empty.cget('text')
    # A selected course with no cached tasks has not thereby been completed.
    catalog = store.catalog()
    store.set('course_catalog:demo', catalog + [dict(classroom_id='3', name='新课程')])
    ui.toggle_course('3')
    assert not ui.tree.get_children()
    assert '扫描' in ui.task_empty.cget('text')
    assert '全部完成' not in ui.task_empty.cget('text')


def test_paused_first_login_can_resume_loading_without_existing_courses(desktop):
    root, ui, store = desktop
    store.set('account', {})
    store.set('desired', 'paused')
    store.set('worker_busy', False)
    store.set('scan_progress', dict(status='cancelled', mode='catalog'))
    ui.refresh()
    ui.primary_button.invoke()
    assert store.get('desired') == 'catalog'
    assert store.get('scan_progress')['status'] == 'queued'
    assert store.get('scan_progress')['mode'] == 'catalog'
