"""The Lark card: one compact line per unfinished task, each with its next run."""

from copy import deepcopy
from dataclasses import replace
import json
import re

import pytest

from autocontrol.board.card import CARD_LIMIT, CardRenderer
from autocontrol.board.projection import Projection
from autocontrol.lark.store import sanitize_snapshot
from autocontrol.projects.yslzm.catalog import CATALOG, SCHEDULE
from autocontrol.projects.yslzm.demo import demo_snapshot

# Card semantics are tested with server-reported wakes running every phase;
# yslzm's own policy (such wakes run only 活动) has its own tests below.
RENDER = CardRenderer(CATALOG, replace(SCHEDULE, dynamic_phases=None)).render
YSLZM_RENDER = CardRenderer(CATALOG, SCHEDULE).render
SANITIZE = dict(count_keys=CATALOG.count_keys, phases=frozenset(CATALOG.phases))
S = '　'


def snapshot(tasks, **fields):
    return {
        'project': 'yslzm',
        'service_day': '2026-11-17',
        'discovered': True,
        'tasks': tasks,
        'phases': [],
        'observed_at': '2026-11-17T10:12:00+08:00',
        'next_check_at': '2026-11-17T12:04:00+08:00',
        **fields,
    }


def row(key, status, **fields):
    return {'task_id': key, 'status': status, 'availability': 'open', **fields}


def walk(elements):
    for element in elements:
        yield element
        if element['tag'] == 'collapsible_panel':
            yield from walk(element['elements'])


def strip(content):
    return re.sub(r'<[^>]+>|\*\*|~~', '', content)


MARKS = {
    'succeed_filled': '☑',
    'ellipse_outlined': '☐',
    'record_outlined': '◉',
    'error_filled': '✗',
    'warning-hollow_filled': '⚠',
}


def rows(card_or_panel):
    """[(mark, line)] of task rows in display order: ☑ ☐ ✗ ⚠."""
    elements = (
        card_or_panel['body']['elements'] if 'body' in card_or_panel else card_or_panel['elements']
    )
    result = []
    for e in walk(elements):
        if e['tag'] == 'div' and 'icon' in e:
            mark = MARKS[e['icon']['token']]
            result.append((mark, strip(e['text']['content'])))
    return result


def open_rows(section):
    """Rows directly in a section, outside its folded 已完成 panel."""
    return rows({'elements': [e for e in section['elements'] if e['tag'] != 'collapsible_panel']})


def panel(card, ident):
    return next(e for e in walk(card['body']['elements']) if e.get('element_id') == ident)


def text(element):
    return json.dumps(element, ensure_ascii=False)


def plan(card):
    return strip(
        next(
            e for e in card['body']['elements'] if e['tag'] == 'markdown' and '执行计划' in e['content']
        )['content']
    )


def first_line(card):
    return next(line for _, line in rows(card))


# -- layout ------------------------------------------------------------------------


def test_every_task_is_a_single_line():
    card = RENDER(demo_snapshot(_project(), 'finished'))
    for e in walk(card['body']['elements']):
        if e['tag'] in ('checker', 'div'):
            assert '\n' not in e['text']['content'] and '<br>' not in e['text']['content']
    # No separate note lines below tasks: markdown is only the plan, phase errors and footer.
    for section in (
        e
        for e in card['body']['elements']
        if e['tag'] == 'collapsible_panel' and e['element_id'].startswith('sec_')
    ):
        assert all(e['tag'] != 'markdown' or '阶段' in e['content'] for e in section['elements'])


def test_unfinished_items_are_open_and_finished_ones_fold_into_their_section():
    card = RENDER(
        snapshot([row('arena', 'complete'), row('pets', 'incomplete'), row('guild', 'complete')])
    )
    section = panel(card, 'sec_auto')
    assert section['expanded'] is True
    assert open_rows(section) == [('☐', f'宠物{S}12:04执行')]
    folded = panel(card, 'done_auto')
    assert folded['expanded'] is False and '已完成 2 项' in folded['header']['title']['content']
    assert rows(folded) == [('☑', '竞技场'), ('☑', '协会')]
    # Done lines are grey and struck through, led by a green check icon.
    assert all(
        e['icon']['token'] == 'succeed_filled' and "<font color='grey'>~~" in e['text']['content']
        for e in folded['elements']
    )
    assert 'behaviors' not in text(card) and 'checker' not in text(card)  # nothing to click


@pytest.mark.parametrize('done_for_today', [
    row('penglai', 'waiting', reason='daily_growth_complete', seen=True),
    row('heart_crafting', 'blocked', reason='quota_cap', seen=True),
])  # fmt: skip
def test_items_done_for_today_fold_into_finished_and_count_as_done(done_for_today):
    section_key = CATALOG.tasks[done_for_today['task_id']].section
    title = CATALOG.title(done_for_today['task_id'])
    others = {
        'activities': ('astrology', 'bear'),
        'craft': ('gold_purchase', 'main_story_crafting'),
    }
    finished, unfinished = others[section_key]
    card = RENDER(
        snapshot([row(finished, 'complete'), done_for_today, row(unfinished, 'incomplete')])
    )
    section = panel(card, f'sec_{section_key}')
    assert open_rows(section) == [('☐', f'{CATALOG.title(unfinished)}{S}12:04执行')]
    assert ('☑', title) in rows(panel(card, f'done_{section_key}'))
    assert '已完成 2 项' in panel(card, f'done_{section_key}')['header']['title']['content']
    assert '2/3' in section['header']['title']['content']
    assert card['header']['subtitle']['content'] == '每日清单 · 已完成 2/3'
    # One compact line: no reason, next run or note for a settled item anywhere.
    assert '继续' not in text(card) and text(card).count(title) == 1


def test_a_day_whose_remaining_item_is_done_for_today_is_all_done():
    card = RENDER(snapshot([row('astrology', 'complete'),
                            row('penglai', 'waiting', reason='daily_growth_complete')]))  # fmt: skip
    section = panel(card, 'sec_activities')
    assert section['expanded'] is False and '✓' in section['header']['title']['content']
    assert rows(section) == [('☑', '占星物语'), ('☑', '画中蓬莱')]
    assert card['header']['template'] == 'green'
    assert card['header']['text_tag_list'][0]['text']['content'] == '全部完成'
    assert card['config']['summary']['content'] == '以闪亮之名 每日清单 2/2'


def test_worker_growth_status_reaches_the_card_as_done_today():
    projection = Projection(CATALOG, SCHEDULE, '2026-11-17T13:35:00+08:00')
    projection.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': {}}}})
    projection.apply_result({'phase': 'activities', 'execution': 'waiting', 'updates': [
        ('astrology', {'status': 'complete'}),
        ('penglai', {'status': 'daily_growth_complete', 'counts': {'score': 1740}}),
    ]})  # fmt: skip
    row_ = next(r for r in projection.snapshot()['tasks'] if r['task_id'] == 'penglai')
    assert (row_['status'], row_['reason']) == ('waiting', 'daily_growth_complete')
    section = panel(RENDER(projection.snapshot()), 'sec_activities')
    assert '2/2' in section['header']['title']['content'] and section['expanded'] is False
    assert rows(section) == [('☑', '占星物语'), ('☑', '画中蓬莱')]


def test_an_item_harvested_this_round_is_done_on_the_card():
    projection = Projection(CATALOG, SCHEDULE, '2026-11-17T13:30:00+08:00')
    projection.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': {}}}})
    projection.apply_event(
        {'kind': 'task_finished', 'task_id': 'pet_care_actions', 'status': 'cleared'}
    )
    decayed = {'pets': {'status': 'incomplete', 'counts': {'eligible': 6, 'play': 6}}}
    projection.apply_event({'kind': 'plan', 'plan': {'goals': decayed}})
    card = RENDER(projection.snapshot())
    assert ('☑', '宠物') in rows(panel(card, 'done_auto'))
    assert '宠物' not in text([e for e in panel(card, 'sec_auto')['elements']
                              if e['tag'] != 'collapsible_panel'])  # fmt: skip


def test_a_day_done_reason_never_hides_a_failure_or_running_work():
    failed = RENDER(snapshot([row('penglai', 'failed', reason='daily_growth_complete', seen=True)]))
    assert rows(failed) == [('✗', f'画中蓬莱{S}失败 · 12:04重试')]
    running = RENDER(snapshot([row('penglai', 'running', reason='daily_growth_complete')]))
    assert rows(running) == [('◉', f'画中蓬莱{S}执行中')]


def test_a_finished_section_collapses_and_lists_its_items_directly():
    card = RENDER(snapshot([row('cardbook', 'complete'), row('arena', 'waiting')],
                           slot_runs={'cardbook': [0, 1, 2]}))  # fmt: skip
    section = panel(card, 'sec_cardbook')
    assert section['expanded'] is False and '✓' in section['header']['title']['content']
    assert rows(section) == [('☑', '事件簿')]
    assert not any(e.get('element_id') == 'done_cardbook' for e in walk(card['body']['elements']))


def test_items_keep_business_order_and_sections_follow_execution_order():
    tasks = [row('pets', 'running'), row('arena', 'waiting'), row('cardbook', 'incomplete'),
             row('gold_purchase', 'not_started'), row('activities', 'not_started')]  # fmt: skip
    card = RENDER(snapshot(tasks))
    assert [line.split(S)[0] for _, line in rows(card)] == ['事件簿', '竞技场', '宠物', '金币购买', '活动']
    sections = [
        e['element_id'] for e in card['body']['elements'] if e['tag'] == 'collapsible_panel'
    ]
    assert sections == ['sec_cardbook', 'sec_auto', 'sec_craft', 'sec_activities']


def test_failures_have_a_red_cross_and_retry_time_with_the_reason_in_details():
    card = RENDER(snapshot([row('pets', 'failed', reason='action_exception', seen=True)]))
    section = panel(card, 'sec_auto')
    head = section['elements'][0]
    assert head['tag'] == 'div' and head['icon'] == {
        'tag': 'standard_icon',
        'token': 'error_filled',
        'color': 'red',
    }
    assert rows(section) == [('✗', f'宠物{S}失败 · 12:04重试')]
    assert '宠物：执行时发生异常，未收到成功结果' in text(panel(card, 'task_details'))
    assert card['header']['template'] == 'orange' and '1 项异常' in text(card['header'])


def test_unknown_request_results_are_flagged_and_rechecked():
    card = RENDER(snapshot([row('mail', 'result_unavailable', reason='request_result_missing', seen=True),
                            row('red_dots', 'result_unavailable', reason='request_result_missing', seen=True)]))  # fmt: skip
    assert rows(panel(card, 'sec_auto')) == [('⚠', f'红点奖励{S}结果未返回 · 12:04核对'),
                                             ('⚠', f'邮件奖励{S}结果未返回 · 12:04核对')]  # fmt: skip


def test_a_failing_sub_step_is_its_own_line_while_the_parent_stays_ticked():
    card = RENDER(snapshot([row('free_lottery', 'complete'),
                            row('lottery_red_envelope', 'result_unavailable', reason='request_result_missing', seen=True)]))  # fmt: skip
    section = panel(card, 'sec_auto')
    assert open_rows(section) == [('⚠', f'追光红包{S}结果未返回 · 12:04核对')]
    assert rows(panel(card, 'done_auto')) == [('☑', '免费抽取')]
    assert section['expanded'] is True
    assert card['header']['subtitle']['content'] == '每日清单 · 已完成 1/1'


def test_missing_sub_step_observations_go_to_details_only():
    card = RENDER(snapshot([row('member_rewards', 'complete'),
                            row('month_card', 'result_unavailable', reason='result_state_unavailable'),
                            row('vip_weekly_gift', 'result_unavailable', reason='state_unavailable')]))  # fmt: skip
    assert rows(panel(card, 'sec_auto')) == [('☑', '会员奖励')]
    assert panel(card, 'sec_auto')['expanded'] is False
    details = panel(card, 'task_details')
    assert details['expanded'] is False and '会员每周礼包 · 服务器尚未提供该项状态' in text(details)
    assert card['header']['template'] == 'green'


def test_replaced_aggregate_does_not_promote_unknown_observer():
    card = RENDER(snapshot([row('activities', 'result_unavailable'), row('astrology', 'complete'),
                            row('open_shop', 'result_unavailable', reason='state_unavailable')]))  # fmt: skip
    assert rows(card) == [('☑', '占星物语')]
    assert '店长经营' in text(panel(card, 'task_details'))
    running = RENDER(
        snapshot(
            [
                row('activities', 'incomplete'),
                row('astrology', 'complete'),
                row('open_shop', 'running'),
            ]
        )
    )
    assert ('◉', f'店长经营{S}执行中') in rows(running)


# -- schedule ------------------------------------------------------------------------


@pytest.mark.parametrize('task, expected', [
    (row('arena', 'not_started'), f'竞技场{S}12:04执行'),
    (row('arena', 'blocked', reason='insufficient_stamina', seen=True), f'竞技场{S}体力不足 · 12:04再试'),
    (row('pets', 'blocked', seen=True), f'宠物{S}暂不满足 · 12:04再试'),
    (row('guild', 'waiting', next_run_at='2026-11-17T13:15:00+08:00', seen=True), f'协会{S}等待 · 13:15执行'),
    (row('guild', 'waiting', current=8, total=13, next_run_at='2026-11-17T14:00:00+08:00'), f'协会{S}8/13 · 14:00到期 · 21:15执行'),
    (row('guild', 'waiting', next_run_at='2026-11-18T01:30:00+08:00'), f'协会{S}等待 · 凌晨01:30到期 · 凌晨04:30执行'),
    (row('guild', 'waiting', next_run_at='2026-11-18T06:30:00+08:00'), f'协会{S}等待 · 明日06:30到期'),
    (row('activity_discovery', 'unsupported', reason='unsupported'), f'开放活动{S}暂无流程 · 今日不执行'),
])  # fmt: skip
def test_every_unfinished_item_says_when_it_runs_next_on_one_line(task, expected):
    card = RENDER(
        snapshot([task], next_check_at=None, dynamic_candidates=['2026-11-17T12:04:00+08:00'])
    )
    assert first_line(card) == expected


@pytest.mark.parametrize('runs, status, observed, expected', [
    ([], 'waiting', '2026-11-17T10:12:00+08:00', f'事件簿{S}0/3 · 12:04执行'),  # catch up at the next wake
    ([0], 'waiting', '2026-11-17T10:12:00+08:00', f'事件簿{S}1/3 · 14:00执行'),
    ([0], 'failed', '2026-11-17T15:00:00+08:00', f'事件簿{S}失败 · 21:15重试'),
    ([0, 1], 'waiting', '2026-11-17T15:00:00+08:00', f'事件簿{S}2/3 · 22:00执行'),
    ([0, 1], 'waiting', '2026-11-18T02:00:00+08:00', f'事件簿{S}2/3 · 凌晨04:30执行'),
])  # fmt: skip
def test_a_slotted_task_counts_its_runs_and_names_its_next_slot(runs, status, observed, expected):
    card = RENDER(snapshot([row('cardbook', status, current=8, total=13, seen=True)],
                           observed_at=observed, next_check_at=None, slot_runs={'cardbook': runs},
                           dynamic_candidates=['2026-11-17T12:04:00+08:00']))  # fmt: skip
    assert first_line(card) == expected


def test_a_slotted_task_is_done_only_when_every_slot_ran():
    complete = row('cardbook', 'complete', seen=True)
    card = RENDER(snapshot([complete, row('arena', 'waiting')], slot_runs={'cardbook': [0, 1]},
                           observed_at='2026-11-17T15:00:00+08:00'))  # fmt: skip
    assert rows(panel(card, 'sec_cardbook')) == [('☐', f'事件簿{S}2/3 · 22:00执行')]
    card = RENDER(snapshot([row('cardbook', 'waiting'), row('arena', 'waiting')],
                           slot_runs={'cardbook': [0, 1, 2]}))  # fmt: skip
    assert rows(panel(card, 'sec_cardbook')) == [('☑', '事件簿')]


def test_ordinary_items_never_wait_for_a_slot_only_wake():
    card = RENDER(snapshot([row('arena', 'blocked', seen=True)],
                           observed_at='2026-11-17T21:30:00+08:00', next_check_at=None))  # fmt: skip
    assert first_line(card) == f'竞技场{S}暂不满足 · 凌晨04:30再试'  # 22:00 runs only 事件簿


def test_after_the_last_round_the_next_run_is_tomorrow():
    card = RENDER(
        snapshot(
            [row('arena', 'failed', reason='failure', seen=True)],
            observed_at='2026-11-18T04:40:00+08:00',
            next_check_at=None,
        )
    )
    assert '今日轮次已结束 · 下一轮 明日06:00' in plan(card)
    assert rows(card) == [('✗', f'竞技场{S}失败 · 明日06:00重试')]


def test_plan_is_two_compact_lines_with_server_wakes_and_the_last_round():
    card = RENDER(snapshot([row('guild', 'waiting', next_run_at='2026-11-17T12:04:00+08:00')],
                           dynamic_candidates=['2026-11-17T12:04:00+08:00']))  # fmt: skip
    lines = plan(card).split('\n')
    assert lines[0] == '执行计划' + S + '今日还剩 6 轮 · 下一轮 12:04'
    assert lines[1] == ('12:04协会 · 13:15 · 14:00事件簿 · 21:15 · 22:00事件簿 · 凌晨04:30末轮 · 05:00新一天')


def test_yslzm_server_wakes_run_only_activities_and_skip_the_reset():
    wakes = ['2026-11-18T00:00:00+08:00', '2026-11-18T04:59:59+08:00', '2026-11-18T05:00:12+08:00']
    card = YSLZM_RENDER(snapshot([row('pets', 'blocked', seen=True), row('ring_toss', 'waiting')],
                                 observed_at='2026-11-17T21:30:00+08:00', next_check_at=None,
                                 dynamic_candidates=wakes))  # fmt: skip
    lines = plan(card).split('\n')
    assert lines[1] == '22:00事件簿 · 凌晨00:00活动 · 凌晨04:30末轮 · 05:00新一天'  # no 04:59
    assert rows(panel(card, 'sec_auto')) == [('☐', f'宠物{S}暂不满足 · 凌晨04:30再试')]
    assert rows(panel(card, 'sec_activities')) == [('☐', f'套圈{S}等待 · 凌晨00:00再试')]


def test_a_running_round_says_which_items_it_will_still_reach():
    phases = [{'phase': 'cardbook', 'status': 'complete'}, {'phase': 'auto', 'status': 'running'}]
    tasks = [row('arena', 'not_started'), row('pets', 'failed', reason='failure', seen=True),
             row('gold_purchase', 'not_started'), row('cardbook', 'incomplete', seen=True)]  # fmt: skip
    live = snapshot(tasks, phases=phases, running=True, round_phases=['cardbook', 'auto', 'craft'],
                    slot_runs={'cardbook': [0]})  # fmt: skip
    card = RENDER(live)
    assert rows(panel(card, 'sec_auto')) == [('☐', f'竞技场{S}本轮稍后'), ('✗', f'宠物{S}失败 · 12:04重试')]
    assert rows(panel(card, 'sec_craft')) == [('☐', f'金币购买{S}本轮稍后')]
    assert rows(panel(card, 'sec_cardbook')) == [('☐', f'事件簿{S}1/3 · 14:00执行')]
    assert '正在执行：日常阶段' in plan(card)
    finished = RENDER({**live, 'running': False, 'finished_at': '2026-11-17T10:30:00+08:00',
                       'phases': [{**p, 'status': 'complete'} for p in phases]})  # fmt: skip
    assert rows(panel(finished, 'sec_craft')) == [('☐', f'金币购买{S}12:04执行')]


def test_done_items_do_not_repeat_progress_and_percentages_are_never_shown():
    card = RENDER(snapshot([row('arena', 'complete', current=12, total=12),
                            row('cardbook', 'waiting', current=8, total=13, unit='派遣'),
                            row('pets', 'running', current=7, unit='个')]))  # fmt: skip
    assert ('☑', '竞技场') in rows(card)
    assert ('☐', f'事件簿{S}0/3 · 12:04执行') in rows(card)  # runs, not dispatch counts
    assert ('◉', f'宠物{S}执行中 · 7 个') in rows(card)
    assert '%' not in text(card) and '12/12' not in text(card)


def test_closed_tasks_are_hidden_and_not_counted():
    card = RENDER(snapshot([row('pets', 'closed'), row('arena', 'complete'),
                            row('endorsement', 'not_started', availability='closed')]))  # fmt: skip
    assert card['header']['subtitle']['content'] == '每日清单 · 已完成 1/1'
    assert '宠物' not in text(card) and '代言' not in text(card)


# -- header, phases and robustness ---------------------------------------------------------


def test_discovery_failure_names_the_failed_stage():
    projection = Projection(CATALOG, SCHEDULE, '2026-11-17T09:00:00+08:00')
    projection.apply_event(
        {'kind': 'phase_finished', 'phase': 'status', 'status': 'failed', 'reason': 'connect'}
    )
    projection.finish_run('2026-11-17T09:01:00+08:00')
    card = RENDER(projection.snapshot())
    assert '本轮未能读取完整计划 · 连接服务器失败' in text(card)
    assert '正在读取今日任务' not in text(card)
    assert '正在读取今日任务' in text(
        RENDER(Projection(CATALOG, SCHEDULE, '2026-11-17T09:00:00+08:00').snapshot())
    )


def test_failed_phase_is_reported_inside_its_section():
    card = RENDER(
        snapshot(
            [row('arena', 'not_started')],
            phases=[{'phase': 'auto', 'status': 'failed', 'reason': 'connect'}],
        )
    )
    section = panel(card, 'sec_auto')
    assert '阶段异常' in section['header']['title']['content'] and section['expanded']
    assert '日常阶段 · 连接服务器失败' in text(section)
    assert '1 项异常' in text(card['header'])


def test_header_states():
    done = RENDER(snapshot([row('arena', 'complete')]))
    assert (done['header']['template'], done['header']['text_tag_list'][0]['text']['content']) == (
        'green',
        '全部完成',
    )
    idle = RENDER(snapshot([row('arena', 'waiting')]))
    assert (idle['header']['template'], idle['header']['text_tag_list'][0]['text']['content']) == (
        'wathet',
        '下次 12:04',
    )
    assert done['header']['title']['content'] == '以闪亮之名 · 11月17日 周二'
    running = RENDER(snapshot([row('arena', 'running'), row('pets', 'failed')], running=True))
    assert [t['text']['content'] for t in running['header']['text_tag_list']] == ['执行中', '1 项异常']
    assert running['config']['summary']['content'] == '以闪亮之名 每日清单 0/2 · 执行中 · 1 项异常'


def test_rendering_is_pure_deterministic_and_survives_sanitizing():
    source = demo_snapshot(_project(), 'running')
    before = deepcopy(source)
    assert RENDER(source) == RENDER(source)
    assert source == before
    clean = sanitize_snapshot(source, **SANITIZE)
    assert clean['round_phases'] == source['round_phases']
    assert RENDER(clean) == RENDER(source)


def test_large_failing_plan_keeps_every_error_within_the_message_limit():
    tasks = [row(key, 'failed', reason='action_exception', seen=True) for key in CATALOG.tasks]
    card = RENDER(snapshot(tasks))
    content = json.dumps(card, ensure_ascii=False, separators=(',', ':'))
    assert len(content.encode()) <= CARD_LIMIT
    assert '失败' in content
    primary = [
        key
        for key, spec in CATALOG.tasks.items()
        if spec.kind != 'step' and key not in ('activities', 'wardrobe_palettes')
    ]
    for key in primary:
        assert CATALOG.title(key) in content


def test_element_ids_are_valid_and_unique():
    card = RENDER(demo_snapshot(_project(), 'finished'))
    ids = [e['element_id'] for e in walk(card['body']['elements']) if 'element_id' in e]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,19}', ident) for ident in ids)
    tags = {e['tag'] for e in walk(card['body']['elements'])}
    assert tags <= {'markdown', 'collapsible_panel', 'div'}  # no clickable checkboxes


def test_spacing_uses_only_forms_lark_accepts():
    """Lark rejected a two-value panel header padding with error 230099."""
    allowed = re.compile(r'-?\d+px( -?\d+px){3}|-?\d+px')
    found = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('padding', 'margin'):
                    found.append(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for stage in ('running', 'finished', 'done'):
        visit(RENDER(demo_snapshot(_project(), stage)))
    assert found and all(allowed.fullmatch(value) for value in found), sorted(set(found))


def test_invalid_day_is_rejected():
    with pytest.raises(ValueError):
        RENDER({'service_day': 'tomorrow'})


def _project():
    from autocontrol.projects.yslzm import YslzmProject

    return YslzmProject(workdir='/nonexistent', private='/nonexistent')


def test_demo_round_trip_shows_running_finished_and_done_states():
    assert RENDER(demo_snapshot(_project(), 'running'))['header']['template'] == 'blue'
    finished = RENDER(demo_snapshot(_project(), 'finished'))
    assert finished['header']['template'] == 'orange'
    assert '画中蓬莱' not in text(finished)  # closed today
    done = RENDER(demo_snapshot(_project(), 'done'))
    assert done['header']['template'] == 'green'
    assert all(
        not e['expanded'] for e in done['body']['elements'] if e['tag'] == 'collapsible_panel'
    )


def test_a_late_reset_day_shows_plain_times_for_its_own_date():
    from autocontrol.schedule import Schedule

    late = Schedule(times=('00:05', '06:05', '12:05', '18:05'), reset='23:00')
    render = CardRenderer(CATALOG, late).render
    card = render(snapshot([row('arena', 'blocked', seen=True)], service_day='2026-11-18',
                           observed_at='2026-11-18T10:00:00+08:00', next_check_at=None))  # fmt: skip
    lines = plan(card).split('\n')
    assert lines[1] == '12:05 · 18:05末轮 · 23:00新一天'  # never 「凌晨12:05」
    assert first_line(card) == f'竞技场{S}暂不满足 · 12:05再试'
    assert '11月18日' in card['header']['title']['content']
