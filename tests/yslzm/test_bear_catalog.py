"""The actual adapter exposes bear results without completing its parent goal."""

from datetime import datetime
import json

import pytest

from autocontrol.board.projection import Projection
from autocontrol.projects.yslzm import summary as rules
from autocontrol.projects.yslzm.catalog import CATALOG, SCHEDULE
from autocontrol.schedule import BEIJING


NOW = datetime(2026, 11, 17, 13, 0, tzinfo=BEIJING)


@pytest.mark.parametrize(
    ('native', 'expected'),
    (
        ('confirmed', 'incomplete'),
        ('daily_trade_complete', 'complete'),
        ('daily_sale_limit_complete', 'complete'),
        ('outside_trade_window', 'complete'),
        ('insufficient_resources', 'blocked'),
        ('uncertain', 'result_unavailable'),
    ),
)
def test_actual_bear_job_survives_adapter_translation_and_has_the_right_card_state(
    project, native, expected
):
    facts = project.result_facts(
        {
            'command': 'activities',
            'game_day': '2026-11-17',
            'execution': 'complete',
            'policy_jobs': {'bear': {'status': native}},
        }
    )
    assert facts['updates'] == [('bear', {'status': native})]
    projection = Projection(CATALOG, SCHEDULE, NOW)
    projection.apply_result(facts)
    rows = {row['task_id']: row for row in projection.snapshot()['tasks']}
    assert rows['bear']['title'] == '寻找贪吃熊'
    assert rows['bear']['phase'] == 'activities'
    assert rows['bear']['level'] == 'job'
    assert rows['bear']['status'] == expected
    if native == 'insufficient_resources':
        assert rows['bear']['reason'] == 'insufficient_resources'
    assert rows['activities']['status'] == 'not_started'
    assert '寻找贪吃熊' in json.dumps(project.render(projection.snapshot()), ensure_ascii=False)


@pytest.mark.parametrize(
    ('native', 'expected'), (('insufficient_resources', 'blocked'), ('available', 'incomplete'))
)
def test_native_bear_state_survives_the_result_whitelist_into_the_card(project, native, expected):
    # 2026-11-21: score 140 < cake price 150 reached the card as 状态未返回,
    # because the result whitelist turned insufficient_resources into unrecognized.
    jobs = {name: [{'status': 'closed'}] for name in rules.PHASE_JOBS['activities']}
    jobs['bear'] = [
        {'kind': 'bear', 'status': native, 'score': 140, 'buy_used': 0, 'buy_target': 3}
    ]
    summary = {'game_day': '2026-11-17', 'policy_jobs': jobs}
    record = rules.summarize('activities', '2026-11-17', 0, summary)
    assert record['policy_jobs']['bear']['status'] == native
    projection = Projection(CATALOG, SCHEDULE, NOW)
    projection.apply_result(project.result_facts({**record, 'command': 'activities'}))
    bear = next(row for row in projection.snapshot()['tasks'] if row['task_id'] == 'bear')
    assert bear['status'] == expected
    text = json.dumps(project.render(projection.snapshot()), ensure_ascii=False)
    assert '服务器尚未提供该项状态' not in text
    if native == 'insufficient_resources':
        assert '资源不足' in text


def test_bear_typed_increments_are_retained_without_counting_currency_as_actions(project):
    increments = {
        'confirmed_bear_buys': 3,
        'confirmed_bear_sells': 3,
        'confirmed_bear_score_gained': 720,
        'confirmed_bear_score_spent': 450,
        'confirmed_bear_jars': 30,
    }
    record = {
        'command': 'activities',
        'game_day': '2026-11-17',
        'execution': 'complete',
        'sent_actions': 6,
        'policy_jobs': {
            'bear': {
                'status': 'daily_trade_complete',
                'confirmed_actions': 6,
                **increments,
                'owner': 123456789,
            }
        },
    }
    projection = Projection(CATALOG, SCHEDULE, NOW)
    projection.apply_result(project.result_facts(record))
    bear = next(row for row in projection.snapshot()['tasks'] if row['task_id'] == 'bear')
    assert bear['counts'] == {'confirmed_actions': 6, **increments}
    assert project.phase_metrics(record)['actions_confirmed'] == 6
    assert project.phase_metrics(record)['actions_sent'] == 6


def test_bear_welcome_and_store_have_separate_typed_increments_without_extra_action_counts(project):
    increments = {
        'confirmed_bear_welcome_rewards': 1,
        'confirmed_bear_welcome_coupons': 450,
        'confirmed_bear_store_purchases': 2,
        'confirmed_bear_store_score_spent': 300,
        'confirmed_bear_store_jars_spent': 90,
    }
    record = {
        'command': 'activities',
        'game_day': '2026-11-17',
        'execution': 'complete',
        'policy_jobs': {
            'bear': {'status': 'daily_trade_complete', 'confirmed_actions': 3, **increments}
        },
    }
    projection = Projection(CATALOG, SCHEDULE, NOW)
    projection.apply_result(project.result_facts(record))
    row = next(row for row in projection.snapshot()['tasks'] if row['task_id'] == 'bear')
    assert row['counts'] == {'confirmed_actions': 3, **increments}
    assert project.phase_metrics(record)['actions_confirmed'] == 3


def test_astrology_grant_increments_share_the_existing_astrology_job(project):
    projection = Projection(CATALOG, SCHEDULE, NOW)
    projection.apply_result(
        project.result_facts(
            {
                'command': 'activities',
                'game_day': '2026-11-17',
                'execution': 'complete',
                'policy_jobs': {
                    'astrology': {
                        'status': 'daily_limit_complete',
                        'confirmed_attempt_rewards': 3,
                        'confirmed_attempts_gained': 12,
                    }
                },
            }
        )
    )
    row = next(row for row in projection.snapshot()['tasks'] if row['task_id'] == 'astrology')
    assert row['counts'] == {'confirmed_attempt_rewards': 3, 'confirmed_attempts_gained': 12}
