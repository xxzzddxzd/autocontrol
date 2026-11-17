"""Projection behaviour (ported from yslzm/tests/test_daily_board_model.py, plus new cases)."""

from datetime import datetime, timedelta
import json

from autocontrol.board.projection import Projection
from autocontrol.projects.yslzm.catalog import CATALOG, SCHEDULE
from autocontrol.schedule import BEIJING

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=BEIJING)
GOALS = [key for key, spec in CATALOG.tasks.items() if spec.kind == 'goal']


def new(now=NOW, previous=None):
    return Projection(CATALOG, SCHEDULE, now, previous)


def task(projection, key):
    return next(row for row in projection.snapshot()['tasks'] if row['task_id'] == key)


def test_fixed_plan_lists_every_goal_and_the_craft_jobs():
    keys = [row['task_id'] for row in new().snapshot()['tasks']]
    assert keys == [
        *GOALS,
        'gold_purchase',
        'stamina_purchase',
        'heart_crafting',
        'main_story_crafting',
    ]
    assert all(row['status'] == 'not_started' for row in new().snapshot()['tasks'])


def test_fresh_server_completion_replaces_previous_batch_time():
    projection = new()
    projection.apply_event(
        {
            'kind': 'task_progress',
            'task_id': 'cardbook',
            'next_run_at': (NOW + timedelta(minutes=5)).timestamp(),
        }
    )
    fresh = NOW + timedelta(hours=3)
    projection.apply_event(
        {
            'kind': 'plan',
            'plan': {
                'full_checklist_summary': {
                    'goals': {
                        'cardbook': {
                            'status': 'waiting',
                            'counts': {'next_completion': int(fresh.timestamp())},
                        }
                    }
                }
            },
        }
    )
    assert task(projection, 'cardbook')['next_run_at'] == fresh.isoformat(timespec='seconds')


def test_live_server_day_supersedes_initial_computer_day():
    projection = new()
    projection.apply_event(
        {
            'kind': 'plan',
            'phase': 'auto',
            'plan': {
                'game_day': '2026-11-16',
                'full_checklist_summary': {
                    'goals': {
                        'arena': {'status': 'complete', 'counts': {'obtained': 12, 'limit': 12}}
                    }
                },
            },
        }
    )
    assert projection.snapshot()['service_day'] == '2026-11-16'
    assert task(projection, 'arena')['status'] == 'complete'
    projection.apply_event({'kind': 'task_started', 'task_id': 'arena', 'game_day': '2026-11-17'})
    assert task(projection, 'arena')['status'] == 'complete'


def test_progress_and_phase_success_do_not_prove_daily_goal_success():
    projection = new()
    projection.apply_event(
        {'kind': 'task_progress', 'task_id': 'cardbook', 'current': 12, 'total': 12, 'unit': '派遣'}
    )
    projection.apply_event({'kind': 'phase_finished', 'phase': 'cardbook', 'status': 'complete'})
    assert task(projection, 'cardbook')['status'] == 'running'
    assert projection.snapshot()['summary']['completed'] == 0


def test_substep_completion_does_not_complete_parent():
    projection = new()
    projection.apply_event(
        {'kind': 'task_finished', 'task_id': 'guild_daily', 'status': 'complete'}
    )
    assert task(projection, 'guild_daily')['status'] == 'complete'
    assert task(projection, 'guild')['status'] == 'not_started'
    assert projection.snapshot()['summary']['completed'] == 0


def test_final_server_goal_facts_replace_live_progress():
    projection = new()
    projection.apply_event(
        {'kind': 'task_progress', 'task_id': 'cardbook', 'current': 7, 'total': 12}
    )
    projection.apply_result(
        {
            'phase': 'auto',
            'game_day': '2026-11-17',
            'execution': 'partial',
            'goals': {
                'cardbook': {
                    'status': 'complete',
                    'counts': {'ordinary_sent': 12, 'ordinary_limit': 12},
                }
            },
        }
    )
    assert task(projection, 'cardbook')['status'] == 'complete'
    assert task(projection, 'cardbook')['current'] == 12


def test_later_deferred_aggregate_does_not_erase_completed_job():
    projection = new()
    projection.apply_result(
        {
            'phase': 'wardrobe',
            'execution': 'complete',
            'updates': [
                ('palette_unlock', {'status': 'complete', 'confirmed_fashions': 9}),
                ('qianhuan', {'status': 'complete'}),
            ],
        }
    )
    projection.apply_result(
        {
            'phase': 'auto',
            'execution': 'partial',
            'goals': {'wardrobe_palettes': {'status': 'not_started'}},
        }
    )
    assert task(projection, 'palette_unlock')['status'] == 'complete'
    assert task(projection, 'qianhuan')['status'] == 'complete'


def test_new_session_missing_data_preserves_confirmed_same_day_goal():
    projection = new()
    projection.apply_result(
        {
            'phase': 'auto',
            'execution': 'partial',
            'goals': {'arena': {'status': 'complete', 'counts': {'obtained': 12, 'limit': 12}}},
        }
    )
    projection.apply_event(
        {
            'kind': 'plan',
            'phase': 'activities',
            'plan': {
                'full_checklist_summary': {
                    'goals': {'arena': {'status': 'unknown', 'counts': {'unknown': 1}}}
                }
            },
        }
    )
    row = task(projection, 'arena')
    assert (row['status'], row['counts'], row['reason']) == (
        'complete',
        {'limit': 12, 'obtained': 12},
        'completed_observation_retained',
    )
    # A real later observation that work is due again may revise the board.
    projection.apply_event(
        {
            'kind': 'plan',
            'plan': {
                'goals': {'arena': {'status': 'incomplete', 'counts': {'obtained': 0, 'limit': 12}}}
            },
        }
    )
    assert task(projection, 'arena')['status'] == 'incomplete'


def test_confirmed_action_is_an_increment_not_a_finished_job():
    projection = new()
    projection.apply_result(
        {
            'phase': 'activities',
            'execution': 'partial',
            'updates': [('ring_toss', {'status': 'confirmed', 'confirmed_throws': 1})],
        }
    )
    assert task(projection, 'ring_toss')['status'] == 'incomplete'


def test_failure_and_result_not_returned_are_distinct_and_keep_success():
    projection = new()
    for key, status in (('arena', 'complete'), ('pets', 'failed'), ('cardbook', 'uncertain')):
        projection.apply_event(
            {
                'kind': 'task_finished',
                'task_id': key,
                'status': status,
                'reason': 'request_result_missing' if status == 'uncertain' else None,
            }
        )
    projection.finish_run(NOW + timedelta(hours=1))
    assert [task(projection, key)['status'] for key in ('arena', 'pets', 'cardbook')] == [
        'complete',
        'failed',
        'result_unavailable',
    ]


def test_unreturned_running_item_is_not_left_running_at_end():
    projection = new()
    projection.apply_event({'kind': 'task_started', 'task_id': 'pets'})
    projection.finish_run(NOW)
    assert not projection.snapshot()['running']
    assert task(projection, 'pets')['reason'] == 'ended_without_result'


def test_candidates_are_live_only_and_clock_advances_after_long_run():
    projection = new()
    candidate = NOW + timedelta(minutes=5)
    projection.apply_event(
        {'kind': 'plan', 'plan': {'dynamic_candidates': [candidate.timestamp(), None]}}
    )
    assert projection.dynamic_candidates == (candidate.timestamp(),)
    projection.apply_result(
        {
            'phase': 'auto',
            'execution': 'partial',
            'finished_at': (NOW + timedelta(minutes=40)).isoformat(),
        }
    )
    assert projection.snapshot()['next_check_at'] == NOW.replace(hour=13, minute=15).isoformat()
    assert new(previous=projection.snapshot()).dynamic_candidates == ()


def test_duplicate_events_cannot_regress_progress():
    projection = new()
    projection.apply_event(
        {
            'kind': 'task_finished',
            'task_id': 'arena',
            'status': 'complete',
            'generation': 'worker1',
            'sequence': 5,
        }
    )
    projection.apply_event(
        {'kind': 'task_started', 'task_id': 'arena', 'generation': 'worker1', 'sequence': 4}
    )
    assert task(projection, 'arena')['status'] == 'complete'


def test_only_allowed_business_fields_reach_the_snapshot():
    projection = new()
    projection.apply_event(
        {
            'kind': 'plan',
            'plan': {
                'full_checklist_summary': {
                    'goals': {
                        'arena': {
                            'status': 'complete',
                            'counts': {'obtained': 12, 'token': 99},
                            'session_key': 'private-session',
                        },
                        'private-task': {'status': 'complete', 'secret': 'private-secret'},
                    }
                }
            },
        }
    )
    projection.apply_event(
        {
            'kind': 'task_failed',
            'task_id': 'pets',
            'reason': 'private-secret',
            'title': 'private-session',
        }
    )
    document = json.dumps(projection.snapshot(), ensure_ascii=False)
    for secret in ('private-session', 'private-secret', 'private-task', '"token"'):
        assert secret not in document


def test_missing_handler_is_explicit():
    projection = new()
    projection.apply_event(
        {
            'kind': 'plan',
            'plan': {'goals': {'activities': {'status': 'unknown', 'counts': {'unsupported': 1}}}},
        }
    )
    assert task(projection, 'activities')['status'] == 'unsupported'


def test_bad_event_fields_are_discarded():
    projection = new()
    before = projection.snapshot()['tasks']
    for value in (
        None,
        [],
        {},
        {'kind': 'phase_finished', 'phase': [], 'status': {}},
        {'kind': 'task_finished', 'task_id': 'arena', 'status': {}, 'reason': [], 'unit': []},
        {'kind': 'task_finished', 'task_id': 'not-a-task', 'status': 'complete'},
    ):
        projection.apply_event(value)
    assert [row['task_id'] for row in projection.snapshot()['tasks']] == [
        row['task_id'] for row in before
    ]


def test_saved_display_restores_same_day_only_and_never_running():
    projection = new()
    projection.apply_event({'kind': 'task_finished', 'task_id': 'arena', 'status': 'complete'})
    projection.apply_event({'kind': 'task_started', 'task_id': 'pets'})
    saved = projection.snapshot()
    restored = new(NOW + timedelta(hours=1), previous=saved)
    assert task(restored, 'arena')['status'] == 'complete'
    assert (task(restored, 'pets')['status'], task(restored, 'pets')['reason']) == (
        'result_unavailable',
        'worker_unavailable',
    )
    tomorrow = new(NOW + timedelta(days=1), previous=saved)
    assert task(tomorrow, 'arena')['status'] == 'not_started'


def test_failure_seen_this_round_survives_a_later_aggregate_until_completed():
    projection = new()
    projection.apply_event(
        {'kind': 'task_failed', 'task_id': 'pet_care', 'reason': 'action_exception'}
    )
    projection.apply_result(
        {'phase': 'auto', 'execution': 'partial', 'goals': {'pets': {'status': 'incomplete'}}}
    )
    assert (task(projection, 'pets')['status'], task(projection, 'pets')['reason']) == (
        'failed',
        'action_exception',
    )
    projection.apply_event({'kind': 'plan', 'plan': {'goals': {'pets': {'status': 'complete'}}}})
    assert task(projection, 'pets')['status'] == 'complete'
    # A restored failure is history, not this round's observation.
    failed = new()
    failed.apply_event({'kind': 'task_failed', 'task_id': 'pets', 'reason': 'action_exception'})
    restored = new(NOW + timedelta(hours=1), previous=failed.snapshot())
    restored.apply_event({'kind': 'plan', 'plan': {'goals': {'pets': {'status': 'incomplete'}}}})
    assert task(restored, 'pets')['status'] == 'incomplete'


def test_block_seen_this_round_survives_a_later_bare_aggregate():
    projection = new()
    projection.apply_event(
        {
            'kind': 'task_finished',
            'task_id': 'vigor_exchange_actions',
            'status': 'insufficient_vigor',
        }
    )
    goal = {'status': 'incomplete', 'counts': {'used': 12, 'target': 20}}
    projection.apply_result(
        {'phase': 'auto', 'execution': 'partial', 'goals': {'vigor_exchange': goal}}
    )
    projection.apply_event({'kind': 'plan', 'plan': {'goals': {'vigor_exchange': goal}}})
    row = task(projection, 'vigor_exchange')
    assert (row['status'], row['reason']) == ('blocked', 'insufficient_vigor')
    projection.apply_event(
        {'kind': 'plan', 'plan': {'goals': {'vigor_exchange': {'status': 'complete'}}}}
    )
    assert task(projection, 'vigor_exchange')['status'] == 'complete'
    # A restored block is history, not this round's observation.
    earlier = new()
    earlier.apply_event(
        {
            'kind': 'task_finished',
            'task_id': 'vigor_exchange_actions',
            'status': 'insufficient_vigor',
        }
    )
    restored = new(NOW + timedelta(hours=1), previous=earlier.snapshot())
    restored.apply_event({'kind': 'plan', 'plan': {'goals': {'vigor_exchange': goal}}})
    assert task(restored, 'vigor_exchange')['status'] == 'incomplete'


def test_a_block_its_own_phase_reported_survives_later_sessions_this_round():
    blocked = {'status': 'blocked', 'counts': {'normal': 1, 'missing': 1}}
    bare = {'status': 'incomplete', 'counts': {'normal': 1, 'missing': 0}}
    projection = new()
    projection.apply_result(
        {'phase': 'auto', 'execution': 'partial', 'goals': {'first_clears': blocked}}
    )
    # Later phases log in afresh and may not hold the wardrobe evidence.
    projection.apply_result(
        {'phase': 'craft', 'execution': 'waiting', 'goals': {'first_clears': bare}}
    )
    projection.apply_event({'kind': 'plan', 'plan': {'goals': {'first_clears': bare}}})
    assert task(projection, 'first_clears')['status'] == 'blocked'
    # Its own phase may still clear it, and a new round decides afresh.
    restored = new(NOW + timedelta(hours=1), previous=projection.snapshot())
    restored.apply_event({'kind': 'plan', 'plan': {'goals': {'first_clears': bare}}})
    assert task(restored, 'first_clears')['status'] == 'incomplete'
    projection.apply_result(
        {'phase': 'auto', 'execution': 'partial', 'goals': {'first_clears': bare}}
    )
    assert task(projection, 'first_clears')['status'] == 'incomplete'
    # A bystander phase reporting a block does not pin it.
    other = new()
    other.apply_result(
        {'phase': 'craft', 'execution': 'waiting', 'goals': {'first_clears': blocked}}
    )
    other.apply_event({'kind': 'plan', 'plan': {'goals': {'first_clears': bare}}})
    assert task(other, 'first_clears')['status'] == 'incomplete'


def test_a_round_task_harvested_this_round_stays_done_for_the_round():
    projection = new()
    projection.apply_event({'kind': 'task_started', 'task_id': 'pet_care_actions'})
    projection.apply_event(
        {'kind': 'task_finished', 'task_id': 'pet_care_actions', 'status': 'cleared'}
    )
    decayed = {'status': 'incomplete', 'counts': {'eligible': 6, 'feed': 6, 'play': 6}}
    projection.apply_result({'phase': 'auto', 'execution': 'partial', 'goals': {'pets': decayed}})
    projection.apply_event({'kind': 'plan', 'plan': {'goals': {'pets': decayed}}})
    row = task(projection, 'pets')
    assert (row['status'], row['reason']) == ('complete', 'round_harvested')
    # The next round decides afresh: regenerated needs reopen it until harvested.
    later = new(NOW + timedelta(hours=1), previous=projection.snapshot())
    assert task(later, 'pets')['status'] == 'complete'
    later.apply_event({'kind': 'plan', 'plan': {'goals': {'pets': decayed}}})
    assert task(later, 'pets')['status'] == 'incomplete'


def test_pets_left_only_with_sleeping_or_sick_pets_are_harvested():
    projection = new()
    projection.apply_event(
        {'kind': 'task_finished', 'task_id': 'pet_care_actions', 'status': 'blocked'}
    )
    assert task(projection, 'pets')['status'] == 'complete'


def test_endorsement_collected_this_round_is_done_despite_new_income():
    accrued = {'status': 'incomplete', 'counts': {'salary_ready': 4}}
    # The worker reports the endorsement job as its maintenance step.
    for task_id in ('endorsement_maintenance', 'endorsement_actions'):
        for status, expected in (('complete', 'complete'), ('partial', 'incomplete')):
            projection = new()
            projection.apply_event({'kind': 'task_started', 'task_id': task_id})
            projection.apply_event({'kind': 'task_finished', 'task_id': task_id, 'status': status})
            projection.apply_result(
                {'phase': 'auto', 'execution': 'partial', 'goals': {'endorsement': accrued}}
            )
            projection.apply_event({'kind': 'plan', 'plan': {'goals': {'endorsement': accrued}}})
            assert task(projection, 'endorsement')['status'] == expected, (task_id, status)


def test_stamina_consumer_is_done_once_this_round_used_the_stamina():
    used = {'status': 'waiting_for_stamina', 'reason': 'insufficient_stamina'}
    projection = new()
    projection.apply_result(
        {'phase': 'craft', 'execution': 'waiting', 'updates': [('main_story_crafting', used)]}
    )
    row = task(projection, 'main_story_crafting')
    assert (row['status'], row['reason']) == ('complete', 'round_harvested')
    blocked = new()
    blocked.apply_result(
        {
            'phase': 'craft',
            'execution': 'partial',
            'updates': [('main_story_crafting', {'status': 'blocked'})],
        }
    )
    assert task(blocked, 'main_story_crafting')['status'] == 'blocked'
    # Short of stamina is a real block for a target that is not a consumer.
    target = new()
    target.apply_event(
        {'kind': 'task_finished', 'task_id': 'stage_first_clears', 'status': 'insufficient_stamina'}
    )
    assert task(target, 'first_clears')['status'] == 'blocked'


def test_a_later_failure_in_the_round_reopens_a_harvest():
    projection = new()
    projection.apply_event(
        {'kind': 'task_finished', 'task_id': 'pet_care_actions', 'status': 'cleared'}
    )
    projection.apply_event(
        {'kind': 'task_failed', 'task_id': 'pet_care_actions', 'reason': 'action_exception'}
    )
    projection.apply_event({'kind': 'plan', 'plan': {'goals': {'pets': {'status': 'incomplete'}}}})
    assert task(projection, 'pets')['status'] == 'failed'


def test_restored_rows_do_not_invent_a_reason_from_their_status():
    saved = {
        'service_day': '2026-11-17',
        'tasks': [
            {'task_id': 'penglai', 'status': 'waiting', 'availability': 'open'},
            {
                'task_id': 'pets',
                'status': 'blocked',
                'reason': 'insufficient_items',
                'availability': 'open',
            },
        ],
    }
    restored = new(NOW, previous=saved)
    assert 'reason' not in task(restored, 'penglai')
    assert task(restored, 'pets')['reason'] == 'insufficient_items'


def test_worker_success_vocabulary_ticks_sub_steps():
    projection = new()
    for key, status in (
        ('mail_read', 'read_confirmed'),
        ('guild_salary', 'salary_collection_complete'),
        ('social_topic_coin', 'no_current_topic'),
        ('vip_weekly_gift', 'no_current_offer'),
    ):
        projection.apply_event({'kind': 'task_finished', 'task_id': key, 'status': status})
    states = {
        key: task(projection, key)['status']
        for key in ('mail_read', 'guild_salary', 'social_topic_coin')
    }
    assert set(states.values()) == {'complete'}
    assert task(projection, 'vip_weekly_gift')['status'] == 'closed'


def test_a_bare_unknown_never_replaces_a_known_state():
    projection = new()
    projection.apply_result(
        {
            'phase': 'cardbook',
            'execution': 'awaiting_completion',
            'updates': [
                (
                    'cardbook',
                    {'status': 'awaiting_completion', 'ordinary_sent': 8, 'ordinary_limit': 13},
                )
            ],
        }
    )
    later = int((NOW + timedelta(hours=6)).timestamp())
    projection.apply_event(
        {
            'kind': 'plan',
            'plan': {
                'goals': {'cardbook': {'status': 'unknown', 'counts': {'next_completion': later}}}
            },
        }
    )
    row = task(projection, 'cardbook')
    assert row['status'] == 'waiting' and row['next_run_at'] == (
        NOW + timedelta(hours=6)
    ).isoformat(timespec='seconds')
    projection.apply_result(
        {
            'phase': 'activities',
            'execution': 'partial',
            'updates': [('brand_tower', {'status': 'complete'})],
        }
    )
    projection.apply_result(
        {
            'phase': 'activities',
            'execution': 'partial',
            'updates': [('brand_tower', {'status': 'unknown'})],
        }
    )
    assert (
        task(projection, 'brand_tower')['status'],
        task(projection, 'brand_tower')['reason'],
    ) == ('complete', 'completed_observation_retained')
    # A request that was sent without a result is a real risk and is shown.
    projection.apply_event(
        {
            'kind': 'task_failed',
            'task_id': 'brand_tower',
            'status': 'uncertain',
            'reason': 'request_result_missing',
        }
    )
    assert task(projection, 'brand_tower')['status'] == 'result_unavailable'
    # An unknown may still fill a task nobody has observed yet.
    projection.apply_event({'kind': 'task_finished', 'task_id': 'pets', 'status': 'unknown'})
    assert task(projection, 'pets')['status'] == 'result_unavailable'


def test_round_tasks_must_be_known_goals_or_jobs():
    import pytest

    from autocontrol.board.catalog import Catalog, Section, TaskSpec

    def build(round_tasks):
        return Catalog(
            project='x', display_name='x', sections=[Section('a', 'A')],
            tasks=[TaskSpec('g', 'G', 'a'), TaskSpec('s', 'S', kind='step', parent='g')],
            phases={'a': 'A'}, round_tasks=round_tasks,
        )  # fmt: skip

    assert build({'g': ('waiting_for_stamina',)}).round_tasks == {
        'g': frozenset({'waiting_for_stamina'})
    }
    assert build({'s': ()}).round_tasks == {'s': frozenset()}  # a step stands for its parent
    with pytest.raises(ValueError):
        build({'missing': ()})


def test_a_window_activity_keeps_todays_outcome_after_it_closes(tmp_path):
    from autocontrol.projects.manifest import ManifestProject

    from .helpers import demo_copy

    window = {'section': 'daily', 'demand': 'window'}
    manifest = demo_copy(tmp_path, tasks=[
        {'id': 'signin', 'title': '每日签到', 'section': 'daily'},
        {'id': 'event', 'title': '限时活动', **window},
        {'id': 'fair', 'title': '庙会', **window},
        {'id': 'expo', 'title': '展会', **window},
    ])  # fmt: skip
    project = ManifestProject(manifest)
    opens = datetime(2026, 11, 17, 14, tzinfo=BEIJING).timestamp()
    view = Projection(project.catalog, project.schedule, '2026-11-17T10:00:00+08:00')

    def plan(target, goals):
        target.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': goals}}})

    plan(view, {'event': {'status': 'incomplete'}, 'expo': {'status': 'closed'},
                'fair': {'status': 'waiting', 'reason': 'not_open', 'next_run_at': opens}})  # fmt: skip
    view.apply_event({'kind': 'task_finished', 'task_id': 'event', 'status': 'complete'})
    plan(view, {key: {'status': 'closed'} for key in ('event', 'fair', 'expo')})
    rows = {row['task_id']: row for row in view.snapshot()['tasks']}
    assert rows['event']['status'] == 'complete' and rows['event']['availability'] != 'closed'
    assert (rows['fair']['status'], rows['fair']['reason']) == ('incomplete', 'window_ended')
    assert 'next_run_at' not in rows['fair']
    assert rows['expo']['status'] == 'closed' and 'opened' not in rows['expo']  # never opened today
    card = json.dumps(project.render(view.snapshot()), ensure_ascii=False)
    assert "庙会　<font color='grey'>已错过" in card and '今日不执行' in card and '展会' not in card
    # The opening survives a restart (restored from the saved display).
    later = Projection(project.catalog, project.schedule, '2026-11-17T21:00:00+08:00',
                       previous=project.sanitize(view.snapshot()))  # fmt: skip
    plan(later, {'fair': {'status': 'closed'}, 'event': {'status': 'closed'}})
    again = {row['task_id']: row for row in later.snapshot()['tasks']}
    assert again['fair']['reason'] == 'window_ended' and again['event']['status'] == 'complete'
