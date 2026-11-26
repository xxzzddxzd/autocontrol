"""The auto phase's final read settles claimed steps on the same round's card.

2026-11-23: 06:00 claimed 协会日常, 追光金币, 页面金币, 玩呐奖励 and 盲盒领奖,
and its final read showed nothing left, yet the card kept them 未完成 until
13:17, when the next round's empty pass reported ``cleared``. On Monday
竞技场日常 only saves the weekly schemes (``prepared``) and stayed 未完成 all
day; on other days a later round's unknown status replaced its completion.
"""

from datetime import datetime

import pytest

from autocontrol.board.projection import Projection
from autocontrol.projects.yslzm import settled_steps
from autocontrol.projects.yslzm import summary as rules
from autocontrol.projects.yslzm.catalog import CATALOG, SCHEDULE
from autocontrol.schedule import BEIJING

DAY = '2026-11-23'
MORNING = datetime(2026, 11, 23, 6, 4, tzinfo=BEIJING)
NOON = datetime(2026, 11, 23, 13, 17, tzinfo=BEIJING)

# The worker's final read (business/policy_status.py _red_dots), 2026-11-23 06:00.
RED_DOTS = {
    'status': 'partial_scope_observed',
    'ui_coin_buttons': {'status': 'cleared', 'claimable': 0, 'unknown': 0, 'unsupported': 0},
    'lottery_sign_ins': {'status': 'cleared', 'claimable': 0, 'unknown': 0},
    'blind_box_workshop': {'status': 'cleared'},
    'blind_box_draws': {'status': 'outside_draw_window'},
    'lottery_red_envelopes': {'status': 'closed'},
    'vvanna_rewards': {'status': 'cleared', 'claimable_count': 0},
    'vvanna_quiz': {'status': 'complete'},
    'discount_packages': {'status': 'cleared', 'claimable': 0, 'unknown': 0},
    'monthly_sign': {'status': 'cleared'},
    'social_topic_coins': {
        'status': 'unknown', 'claimable': 0, 'unknown': 0, 'unsupported': 1,
        'reason': 'task_template_unsupported', 'failure_stage': 'task_eligibility',
    },
    'lottery_sign_coins': {'status': 'cleared', 'claimable': 0, 'unknown': 0, 'unsupported': 0},
}  # fmt: skip
GOALS = {
    'arena': {
        'status': 'complete', 'attempted': True, 'availability': 'open',
        'counts': {'obtained': 12, 'limit': 12, 'remaining': 0, 'scheme_slots': 4},
    },
    'guild': {
        'status': 'complete', 'attempted': True, 'availability': 'open',
        'counts': {'support_used': 3, 'support_target': 3,
                   'inspiration_used': 5, 'inspiration_target': 5},
    },
}  # fmt: skip
SETTLED = [
    'lottery_sign_coins', 'lottery_sign', 'ui_coin_rewards', 'vvanna_reward', 'vvanna_quiz',
    'blind_box', 'monthly_sign', 'discount_package_reward', 'arena_daily', 'guild_daily',
]  # fmt: skip


def auto_record(red_dots=RED_DOTS, goals=GOALS):
    """The record the console gets: the worker summary through the result whitelist."""
    native = {
        'game_day': DAY,
        'execution': 'partial',
        'red_dot_sources': red_dots,
        'full_checklist_summary': {'scope': 'full_daily', 'attempted': True, 'complete': False,
                                   'goals': goals},
    }  # fmt: skip
    return {**rules.summarize('auto', DAY, 0, native), 'command': 'auto'}


def finished(projection, task, status, when, **fields):
    projection.apply_event(
        {'kind': 'task_finished', 'phase': 'auto', 'task_id': task, 'status': status,
         'game_day': DAY, 'observed_at': when.timestamp(), **fields}
    )  # fmt: skip


def rows(projection):
    return {row['task_id']: row for row in projection.snapshot()['tasks']}


def test_the_whitelisted_final_read_settles_every_source_with_nothing_left(project):
    facts = project.result_facts(auto_record())
    assert [key for key, _ in facts['updates']] == SETTLED
    assert all(source == {'status': 'cleared'} for _, source in facts['updates'])


def test_claimed_steps_are_complete_on_the_same_rounds_card(project):
    claimed = ('guild_daily', 'lottery_sign_coins', 'ui_coin_rewards', 'vvanna_reward', 'blind_box')
    projection = Projection(CATALOG, SCHEDULE, MORNING)
    for task in claimed:
        finished(projection, task, 'confirmed', MORNING)
    finished(projection, 'arena_daily', 'prepared', MORNING)  # Monday: schemes only
    before = rows(projection)
    assert {before[task]['status'] for task in (*claimed, 'arena_daily')} == {'incomplete'}
    projection.apply_result(project.result_facts(auto_record()))
    after = rows(projection)
    for task in (*claimed, 'arena_daily'):
        assert after[task]['status'] == 'complete', task
        assert after[task]['done_at'] == MORNING.isoformat(timespec='seconds')
        assert after[task]['level'] == 'step'
    # Sub-steps still never prove their parent goal complete.
    assert after['free_lottery']['status'] == 'not_started'


def test_a_later_unknown_arena_status_no_longer_overwrites_the_days_completion(project):
    projection = Projection(CATALOG, SCHEDULE, MORNING)
    finished(projection, 'arena_daily', 'daily_limit_complete', MORNING)
    projection.finish_run(MORNING)
    # A later round: the job starts again and ends with a status the progress
    # channel does not know (sent as unknown, result_state_unavailable).
    projection.apply_event(
        {'kind': 'task_started', 'phase': 'auto', 'task_id': 'arena_daily', 'game_day': DAY,
         'observed_at': NOON.timestamp()}
    )  # fmt: skip
    finished(projection, 'arena_daily', 'unknown', NOON, reason='result_state_unavailable')
    # The console keeps the morning's completion through a restart that only
    # returns an unknown; the auto result then confirms it.
    kept = rows(projection)['arena_daily']
    assert (kept['status'], kept['reason']) == ('complete', 'completed_observation_retained')
    projection.apply_result(project.result_facts(auto_record()))
    arena = rows(projection)['arena_daily']
    assert arena['status'] == 'complete'
    assert 'reason' not in arena
    assert arena['done_at'] == MORNING.isoformat(timespec='seconds')


@pytest.mark.parametrize(
    'row',
    (
        {'status': 'cleared', 'claimable': 1, 'unknown': 0, 'unsupported': 0},
        {'status': 'cleared', 'claimable': 0, 'unknown': 1, 'unsupported': 0},
        {'status': 'cleared', 'claimable': 0, 'unknown': 0, 'unsupported': 1},
        {'status': 'cleared', 'claimable': True},
        {'status': 'cleared', 'claimable': '0'},
        {'status': 'needs_claim', 'claimable': 2},
        {'status': 'unknown', 'reason': 'provider_unavailable'},
        {'status': 'partial'},
        'cleared',
    ),
)
def test_a_source_with_anything_left_or_unknown_settles_nothing(row):
    record = {
        'command': 'auto',
        'feature_details': {'red_dot_sources': {'sources': {'lottery_sign_coins': row}}},
    }
    assert settled_steps(record) == []


def test_vvanna_rewards_left_to_claim_keep_the_step_open(project):
    red = {**RED_DOTS, 'vvanna_rewards': {'status': 'needs_claim', 'claimable_count': 3}}
    keys = [key for key, _ in project.result_facts(auto_record(red_dots=red))['updates']]
    assert 'vvanna_reward' not in keys
    assert 'lottery_sign_coins' in keys


@pytest.mark.parametrize(
    'goals',
    (
        {'arena': {**GOALS['arena'], 'status': 'incomplete'}},
        {'arena': {**GOALS['arena'], 'status': 'unknown'}},
        {'arena': {**GOALS['arena'], 'status': 'not_started', 'attempted': False}},
        {'guild': {'status': 'incomplete', 'counts': {'support_used': 3, 'support_target': 3,
                                                      'inspiration_used': 4, 'inspiration_target': 5}}},
        {'guild': {'status': 'incomplete', 'counts': {'support_used': 2, 'support_target': 3,
                                                      'inspiration_used': 5, 'inspiration_target': 5}}},
        {'guild': {'status': 'unknown', 'counts': {'support_used': 3, 'support_target': 3}}},
        {'guild': {'status': 'unknown', 'counts': {'support_used': 0, 'support_target': 0,
                                                   'inspiration_used': 0, 'inspiration_target': 0}}},
    ),
)  # fmt: skip
def test_goals_short_of_their_target_settle_no_step(goals):
    record = {'command': 'auto', 'full_checklist_summary': {'goals': goals}}
    assert settled_steps(record) == []


@pytest.mark.parametrize('command', ('cardbook', 'craft', 'wardrobe', 'activities', 'daily', None))
def test_only_the_auto_phase_settles_steps(command):
    record = {**auto_record(), 'command': command}
    assert settled_steps(record) == []


def test_a_round_without_a_final_read_settles_nothing(project):
    record = {**rules.summarize('auto', DAY, 1, None), 'command': 'auto'}
    assert settled_steps(record) == []
    assert project.result_facts(record)['updates'] == []
