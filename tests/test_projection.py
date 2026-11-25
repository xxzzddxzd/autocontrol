"""Projection behaviour (ported from yslzm/tests/test_daily_board_model.py, plus new cases)."""

from datetime import datetime, timedelta
from copy import copy
import json

from autocontrol.board.projection import Projection
from autocontrol.projects.yslzm.catalog import CATALOG, SCHEDULE
from autocontrol.schedule import BEIJING

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=BEIJING)
GOALS = [key for key, spec in CATALOG.tasks.items() if spec.kind == 'goal']


def new(now=NOW, previous=None, earlier=None):
    return Projection(CATALOG, SCHEDULE, now, previous, earlier)


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


def test_new_direct_outcomes_replace_old_error_codes_without_erasing_progress():
    catalog = copy(CATALOG)
    catalog.units = catalog.units | {'错误码', '负错误码'}
    projection = Projection(catalog, SCHEDULE, NOW)

    def finish(**fields):
        projection.apply_event({'kind': 'task_finished', 'task_id': 'arena', **fields})
        return task(projection, 'arena')

    assert finish(status='failed', current=77, unit='错误码')['current'] == 77
    projection.apply_event({'kind': 'task_started', 'task_id': 'arena'})
    assert task(projection, 'arena')['current'] == 77
    row = finish(status='failed')
    assert 'current' not in row and 'unit' not in row
    assert finish(status='failed', current=1, unit='负错误码')['unit'] == '负错误码'
    row = finish(status='failed', current=42, unit='错误码')
    assert (row['current'], row['unit']) == (42, '错误码')
    row = finish(status='failed', current=8, total=10, unit='个')
    assert (row['current'], row['total'], row['unit']) == (8, 10, '个')
    row = finish(status='failed', unit='错误码')
    assert 'current' not in row and 'total' not in row


def test_final_result_updates_do_not_inherit_prior_error_code():
    catalog = copy(CATALOG)
    catalog.units = catalog.units | {'错误码'}
    projection = Projection(catalog, SCHEDULE, NOW)
    projection.apply_event(
        {
            'kind': 'task_finished',
            'task_id': 'arena',
            'status': 'failed',
            'current': 77,
            'unit': '错误码',
        }
    )
    projection.apply_result({'phase': 'auto', 'updates': [('arena', {'status': 'failed'})]})
    row = task(projection, 'arena')
    assert 'current' not in row and 'unit' not in row


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


def test_a_passed_candidate_is_dropped_and_the_clock_advances_after_a_long_run():
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


def test_a_manifest_round_task_is_done_for_the_round_its_execution_harvested(tmp_path):
    from autocontrol.projects.manifest import ManifestProject

    from .helpers import demo_copy

    manifest = demo_copy(tmp_path, tasks=[
        {'id': 'signin', 'title': '每日签到', 'section': 'daily'},
        {'id': 'income', 'title': '城镇收益', 'section': 'daily', 'demand': 'round'},
    ])  # fmt: skip
    project = ManifestProject(manifest)
    regrown = {'income': {'status': 'waiting', 'next_run_at': '2026-11-17T12:00:00+08:00'}}

    def plan(view):
        view.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': regrown}}})

    def income(view):
        return next(row for row in view.snapshot()['tasks'] if row['task_id'] == 'income')

    view = Projection(project.catalog, project.schedule, '2026-11-17T10:00:00+08:00')
    plan(view)
    view.apply_event({'kind': 'task_started', 'task_id': 'income'})
    view.apply_result({'phase': 'daily', 'updates': [('income', {'status': 'complete'})]})
    plan(view)  # it yields again later in the same round
    assert (income(view)['status'], income(view)['reason']) == ('complete', 'round_harvested')
    card = json.dumps(project.render(view.snapshot()), ensure_ascii=False)
    assert '城镇收益' in card and '1/2' in card
    # The next round decides afresh: a plan alone never harvests it.
    later = Projection(project.catalog, project.schedule, '2026-11-17T12:00:00+08:00',
                       previous=view.snapshot())  # fmt: skip
    plan(later)
    assert income(later)['status'] == 'waiting'


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


UNKNOWN = {'status': 'result_unavailable', 'reason': 'action_exception'}


def test_a_phase_that_sent_no_request_keeps_what_was_known_today():
    earlier = new()
    for key, status in (('arena', 'complete'), ('guild', 'waiting'), ('first_clears', 'blocked')):
        earlier.apply_event({'kind': 'task_finished', 'task_id': key, 'status': status})
    projection = new(NOW + timedelta(hours=1), previous=earlier.snapshot())
    projection.apply_event({'kind': 'phase_started', 'phase': 'auto'})
    keys = ('arena', 'guild', 'first_clears', 'pets')
    # A login that failed before any request: every task is reported unknown.
    projection.apply_event({'kind': 'plan', 'plan': {'goals': {key: UNKNOWN for key in keys}}})
    projection.apply_result({'phase': 'auto', 'execution': 'failed', 'reason': 'login',
                             'updates': [(key, UNKNOWN) for key in keys]})  # fmt: skip
    projection.keep_known()
    assert [task(projection, key)['status'] for key in keys] == [
        'complete',
        'waiting',
        'blocked',
        'not_started',  # nothing was done: not a result to check
    ]
    assert not task(projection, 'arena')['seen']
    phase = projection.snapshot()['phases'][-1]
    assert (phase['phase'], phase['status']) == ('auto', 'failed')


def test_keeping_what_was_known_keeps_new_facts_and_ends_unfinished_work():
    projection = new()
    projection.apply_event({'kind': 'task_finished', 'task_id': 'arena', 'status': 'complete'})
    projection.apply_event({'kind': 'phase_started', 'phase': 'auto'})
    projection.apply_event({'kind': 'task_started', 'task_id': 'arena'})
    projection.apply_event({'kind': 'task_finished', 'task_id': 'guild', 'status': 'blocked'})
    projection.keep_known()
    assert task(projection, 'arena')['status'] == 'complete'
    assert task(projection, 'guild')['status'] == 'blocked'


def test_only_a_phase_without_requests_keeps_what_was_known():
    projection = new()
    projection.apply_event({'kind': 'task_finished', 'task_id': 'guild', 'status': 'waiting'})
    projection.apply_event({'kind': 'phase_started', 'phase': 'auto'})
    projection.apply_result(
        {'phase': 'auto', 'execution': 'failed', 'updates': [('guild', UNKNOWN)]}
    )
    # Without keep_known (requests were sent), the unknown result stays visible.
    assert task(projection, 'guild')['status'] == 'result_unavailable'


def test_keeping_what_was_known_also_undoes_a_spurious_request_problem():
    projection = new()
    projection.apply_event({'kind': 'task_finished', 'task_id': 'red_dots', 'status': 'complete',
                            'counts': {'unsupported': 1}})  # fmt: skip
    projection.apply_event({'kind': 'task_finished', 'task_id': 'pets', 'status': 'unknown'})
    before = {key: task(projection, key) for key in ('red_dots', 'pets')}
    projection.apply_event({'kind': 'phase_started', 'phase': 'auto'})
    projection.apply_result({'phase': 'auto', 'execution': 'failed',
                             'updates': [('red_dots', UNKNOWN), ('pets', UNKNOWN)]})  # fmt: skip
    assert task(projection, 'red_dots')['reason'] == 'action_exception'
    projection.keep_known()
    assert {key: task(projection, key) for key in before} == before


def done_row(projection, key):
    found = task(projection, key)
    return found['status'], found.get('done_at'), found.get('done_how'), found.get('trouble')


def test_a_completion_keeps_its_time_and_how_it_came_after_trouble():
    projection = new()
    at = lambda minutes: (NOW + timedelta(minutes=minutes)).timestamp()  # noqa: E731
    projection.apply_event({'kind': 'task_finished', 'task_id': 'arena', 'status': 'complete',
                            'observed_at': at(5)})  # fmt: skip
    projection.apply_event({'kind': 'task_failed', 'task_id': 'guild', 'reason': 'action_exception',
                            'observed_at': at(6)})  # fmt: skip
    projection.apply_event({'kind': 'task_failed', 'task_id': 'pets', 'status': 'uncertain',
                            'reason': 'request_result_missing', 'observed_at': at(7)})  # fmt: skip
    assert done_row(projection, 'guild')[3] == 'failed'
    assert done_row(projection, 'pets')[3] == 'unknown'
    projection.apply_result({'phase': 'auto', 'execution': 'complete',
                             'finished_at': (NOW + timedelta(minutes=30)).isoformat(),
                             'updates': [('guild', {'status': 'complete'}), ('pets', {'status': 'complete'})]})  # fmt: skip
    first = (NOW + timedelta(minutes=5)).isoformat(timespec='seconds')
    later = (NOW + timedelta(minutes=30)).isoformat(timespec='seconds')
    assert done_row(projection, 'arena') == ('complete', first, None, None)
    assert done_row(projection, 'guild') == ('complete', later, 'retry', None)
    assert done_row(projection, 'pets') == ('complete', later, 'confirm', None)
    # A later report of the same completion keeps the first time.
    projection.apply_event({'kind': 'task_finished', 'task_id': 'arena', 'status': 'complete',
                            'observed_at': at(50)})  # fmt: skip
    assert done_row(projection, 'arena')[1] == first
    # The next round restores it from the saved card. New work reopens it, and
    # finishing again keeps the day's first completion time.
    restored = new(NOW + timedelta(hours=1), previous=projection.snapshot())
    assert done_row(restored, 'guild') == ('complete', later, 'retry', None)
    restored.apply_event({'kind': 'plan', 'plan': {'goals': {'arena': {'status': 'incomplete'}}}})
    assert done_row(restored, 'arena')[0] == 'incomplete'
    restored.apply_event({'kind': 'task_finished', 'task_id': 'arena', 'status': 'complete'})
    assert done_row(restored, 'arena') == ('complete', first, None, None)


def test_a_completion_in_a_round_started_by_hand_is_marked():
    projection = new()
    projection.set_manual(True)
    projection.apply_event({'kind': 'task_finished', 'task_id': 'arena', 'status': 'complete'})
    assert done_row(projection, 'arena')[2] == 'manual'


def test_the_saved_card_keeps_completion_history():
    from autocontrol.lark.store import sanitize_snapshot

    projection = new()
    projection.apply_event(
        {'kind': 'task_failed', 'task_id': 'guild', 'reason': 'action_exception'}
    )
    projection.apply_event({'kind': 'task_finished', 'task_id': 'guild', 'status': 'complete'})
    saved = sanitize_snapshot(projection.snapshot(), count_keys=CATALOG.count_keys)
    row = next(r for r in saved['tasks'] if r['task_id'] == 'guild')
    assert (row['done_how'], row['done_at']) == ('retry', NOW.isoformat(timespec='seconds'))


def test_an_unknown_window_state_is_no_evidence_and_settled_rows_survive_the_close(tmp_path):
    from autocontrol.projects.manifest import ManifestProject

    from .helpers import demo_copy

    window = {'section': 'daily', 'demand': 'window'}
    keys = ('dim', 'blind', 'seen', 'capped', 'odd')
    manifest = demo_copy(tmp_path, tasks=[
        {'id': 'signin', 'title': '每日签到', 'section': 'daily'},
        *({'id': key, 'title': key, **window} for key in keys),
    ])  # fmt: skip
    project = ManifestProject(manifest)
    view = Projection(project.catalog, project.schedule, '2026-11-17T10:00:00+08:00')

    def plan(goals):
        view.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': goals}}})

    plan({
        'dim': {'status': 'result_unavailable'},  # a read-only plan that could not tell
        'blind': {'status': 'not_started', 'availability': 'unknown'},
        'seen': {'status': 'result_unavailable', 'availability': 'open'},
        'capped': {'status': 'waiting', 'reason': 'done_for_today'},
        'odd': {'status': 'unsupported'},
    })  # fmt: skip
    plan({key: {'status': 'closed'} for key in keys})
    rows = {row['task_id']: row for row in view.snapshot()['tasks']}
    assert rows['dim']['status'] == 'closed' and 'opened' not in rows['dim']
    assert rows['blind']['status'] == 'closed' and 'opened' not in rows['blind']
    # Open but unknown: the day's outcome stays unknown rather than "missed".
    assert rows['seen']['status'] == 'result_unavailable' and rows['seen']['opened'] is True
    assert (rows['capped']['status'], rows['capped']['reason']) == ('waiting', 'done_for_today')
    assert rows['odd']['status'] == 'unsupported'


def test_a_window_activity_done_today_is_not_undone_by_a_later_read(tmp_path):
    from autocontrol.projects.manifest import ManifestProject

    from .helpers import demo_copy

    manifest = demo_copy(tmp_path, tasks=[
        {'id': 'signin', 'title': '每日签到', 'section': 'daily'},
        {'id': 'boss', 'title': '限时首领', 'section': 'daily', 'demand': 'window'},
    ])  # fmt: skip
    project = ManifestProject(manifest)
    view = Projection(project.catalog, project.schedule, '2026-11-17T10:00:00+08:00')

    def plan(goals):
        view.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': goals}}})

    def boss():
        return next(row for row in view.snapshot()['tasks'] if row['task_id'] == 'boss')

    plan({'boss': {'status': 'incomplete'}})
    view.apply_event({'kind': 'task_finished', 'task_id': 'boss', 'status': 'complete'})
    plan({'boss': {'status': 'incomplete'}})  # a read that cannot see today's fights
    assert boss()['status'] == 'complete'
    plan({'boss': {'status': 'closed'}})
    assert boss()['status'] == 'complete'  # never "missed"
    later = datetime(2026, 11, 17, 19, tzinfo=BEIJING).timestamp()
    plan({'boss': {'status': 'waiting', 'reason': 'not_open', 'next_run_at': later}})
    assert boss()['status'] == 'waiting'  # a second window today is still shown


def test_a_window_task_that_only_started_and_then_found_it_closed_is_not_missed(tmp_path):
    from autocontrol.projects.manifest import ManifestProject

    from .helpers import demo_copy

    manifest = demo_copy(tmp_path, tasks=[
        {'id': 'signin', 'title': '每日签到', 'section': 'daily'},
        {'id': 'fair', 'title': '庙会', 'section': 'daily', 'demand': 'window'},
    ])  # fmt: skip
    project = ManifestProject(manifest)
    view = Projection(project.catalog, project.schedule, '2026-11-17T06:09:00+08:00')
    view.apply_event({'kind': 'task_started', 'task_id': 'fair'})
    view.apply_event({'kind': 'task_finished', 'task_id': 'fair', 'status': 'closed'})
    fair = next(row for row in view.snapshot()['tasks'] if row['task_id'] == 'fair')
    assert fair['status'] == 'closed' and 'opened' not in fair  # hidden, never "missed"


def test_a_window_task_started_again_is_judged_by_how_its_day_stood_when_it_finds_it_closed(
    tmp_path,
):
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
    view = Projection(project.catalog, project.schedule, '2026-11-17T10:00:00+08:00')
    goals = {key: {'status': 'incomplete'} for key in ('event', 'fair', 'expo')}
    view.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': goals}}})
    view.apply_event({'kind': 'task_finished', 'task_id': 'event', 'status': 'complete'})
    view.apply_event({'kind': 'task_finished', 'task_id': 'fair', 'status': 'timeout'})
    done_at = next(row for row in view.snapshot()['tasks'] if row['task_id'] == 'event')['done_at']
    # A later round starts every activity again and each finds its window closed.
    for key in ('event', 'fair', 'expo'):
        view.apply_event({'kind': 'task_started', 'task_id': key})
        view.apply_event({'kind': 'task_finished', 'task_id': key, 'status': 'closed'})
    view.finish_run()
    rows = {row['task_id']: row for row in view.snapshot()['tasks']}
    assert rows['event']['status'] == 'complete' and rows['event']['done_at'] == done_at
    assert (rows['fair']['status'], rows['fair']['reason']) == ('result_unavailable', 'timeout')
    assert (rows['expo']['status'], rows['expo']['reason']) == ('incomplete', 'window_ended')
    card = json.dumps(project.render(view.snapshot()), ensure_ascii=False)
    assert "限时活动　<font color='grey'>已错过" not in card and "庙会　<font color='grey'>已错过" not in card


def test_a_read_only_plan_does_not_undo_a_window_done_today_while_it_runs_again(tmp_path):
    from autocontrol.projects.manifest import ManifestProject

    from .helpers import demo_copy

    manifest = demo_copy(tmp_path, tasks=[
        {'id': 'signin', 'title': '每日签到', 'section': 'daily'},
        {'id': 'fair', 'title': '庙会', 'section': 'daily', 'demand': 'window'},
    ])  # fmt: skip
    project = ManifestProject(manifest)
    view = Projection(project.catalog, project.schedule, '2026-11-17T10:00:00+08:00')

    def plan(status):
        goals = {'fair': {'status': status}}
        view.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': goals}}})

    def fair():
        return next(row for row in view.snapshot()['tasks'] if row['task_id'] == 'fair')

    plan('incomplete')
    view.apply_result({'phase': 'daily', 'updates': [('fair', {'status': 'complete'})]})
    view.apply_event({'kind': 'phase_started', 'phase': 'daily'})
    view.apply_event({'kind': 'task_started', 'task_id': 'fair'})
    plan('incomplete')  # a read-only plan cannot see today's participation
    assert fair()['status'] == 'running'
    plan('closed')
    assert fair()['status'] == 'complete' and fair()['done_at']


def test_wakes_reported_earlier_survive_a_failed_or_partial_round(tmp_path):
    from autocontrol.projects.manifest import ManifestProject

    from .helpers import demo_copy

    data = json.loads((demo_copy(tmp_path)).read_text())
    data['schedule']['phases'] = {'shop': {'times': ['07:00', '15:00']}}
    path = tmp_path / 'demo' / 'autocontrol.json'
    path.write_text(json.dumps(data, ensure_ascii=False))
    project = ManifestProject(path)
    start = datetime.fromisoformat('2026-11-17T09:00:00+08:00')
    first = project.new_projection(start, restore=False)
    shop, daily = start + timedelta(hours=1), start + timedelta(hours=2)
    first.apply_result({'phase': 'shop', 'execution': 'waiting', 'finished_at': start.isoformat(),
                        'updates': [], 'dynamic_candidates': [shop.timestamp()]})  # fmt: skip
    first.apply_result({'phase': 'daily', 'execution': 'partial', 'finished_at': start.isoformat(),
                        'updates': [], 'dynamic_candidates': [daily.timestamp()]})  # fmt: skip
    saved = project.sanitize(first.snapshot())

    later = start + timedelta(minutes=20)
    failed = Projection(project.catalog, project.schedule, later, saved)
    failed.apply_result({'phase': 'daily', 'execution': 'failed', 'finished_at': later.isoformat(),
                         'updates': [], 'reason': 'connect'})  # fmt: skip
    assert failed.dynamic_candidates == (shop.timestamp(), daily.timestamp())  # nothing lost
    assert failed.dynamic_owners[shop.timestamp()] == {'shop'}

    again = Projection(
        project.catalog, project.schedule, later, project.sanitize(failed.snapshot())
    )
    newer = start + timedelta(minutes=90)
    again.apply_result({'phase': 'shop', 'execution': 'waiting', 'finished_at': later.isoformat(),
                        'updates': [], 'dynamic_candidates': [newer.timestamp()]})  # fmt: skip
    # The shop phase ran: its new time replaces its old one; the daily phase's stays.
    assert again.dynamic_candidates == (newer.timestamp(), daily.timestamp())
    gone = Projection(project.catalog, project.schedule, daily + timedelta(minutes=1),
                      project.sanitize(again.snapshot()))  # fmt: skip
    assert gone.dynamic_candidates == ()  # passed times are not carried


def test_a_new_state_drops_the_time_of_the_state_it_replaced():
    projection = new()
    soon = (NOW + timedelta(hours=2)).isoformat(timespec='seconds')
    wait = {'status': 'waiting', 'reason': 'waiting_for_completion'}
    projection.apply_event(
        {'kind': 'task_finished', 'task_id': 'arena', **wait, 'next_run_at': soon}
    )
    # The same wait read again without a time keeps it: absence of evidence,
    # also when a plan's bare read lacks the reason.
    projection.apply_event({'kind': 'plan', 'plan': {'goals': {'arena': dict(wait)}}})
    projection.apply_event({'kind': 'plan', 'plan': {'goals': {'arena': {'status': 'waiting'}}}})
    assert task(projection, 'arena')['next_run_at'] == soon
    # A failure replaces the wait; the wait's time no longer applies.
    projection.apply_event(
        {'kind': 'task_failed', 'task_id': 'arena', 'reason': 'action_exception'}
    )
    assert 'next_run_at' not in task(projection, 'arena')
    # Done for today replaces a timed wait the same way.
    projection.apply_event(
        {'kind': 'task_finished', 'task_id': 'guild', **wait, 'next_run_at': soon}
    )
    projection.apply_event({'kind': 'task_finished', 'task_id': 'guild', 'status': 'waiting',
                            'reason': 'quota_cap'})  # fmt: skip
    assert 'next_run_at' not in task(projection, 'guild')
    # A start alone is no new state: the time stays until the task reports.
    projection.apply_event(
        {'kind': 'task_finished', 'task_id': 'bear', **wait, 'next_run_at': soon}
    )
    projection.apply_event({'kind': 'task_started', 'task_id': 'bear'})
    assert task(projection, 'bear')['next_run_at'] == soon


def streak(view, key='guild'):
    return task(view, key).get('fail_streak')


def test_failure_streaks_count_failed_rounds_and_continue_into_the_next_day():
    from autocontrol.lark.store import sanitize_snapshot

    failed = {'kind': 'task_failed', 'task_id': 'guild', 'reason': 'action_exception'}
    first = new()
    first.apply_event(failed)
    first.finish_run(NOW + timedelta(minutes=5))
    assert streak(first) == 1
    saved = sanitize_snapshot(first.snapshot(), count_keys=CATALOG.count_keys)
    second = new(NOW + timedelta(hours=2), previous=saved)
    second.apply_event(failed)
    second.finish_run(NOW + timedelta(hours=2, minutes=5))
    assert streak(second) == 2
    # A round that does not report the task neither extends nor ends it.
    third = new(NOW + timedelta(hours=4), previous=second.snapshot())
    third.apply_event({'kind': 'task_finished', 'task_id': 'arena', 'status': 'complete'})
    third.finish_run(NOW + timedelta(hours=4, minutes=5))
    assert streak(third) == 2 and streak(third, 'arena') is None
    # The next service day starts from the last card of the day before.
    tomorrow = NOW + timedelta(days=1)
    fourth = new(tomorrow, earlier=third.snapshot())
    assert fourth.day == '2026-11-18' and streak(fourth) == 2
    fourth.apply_event(failed)
    fourth.finish_run(tomorrow + timedelta(minutes=5))
    assert streak(fourth) == 3
    # Any other outcome ends it.
    fourth.apply_event({'kind': 'task_finished', 'task_id': 'guild', 'status': 'complete'})
    fourth.finish_run(tomorrow + timedelta(minutes=10))
    assert streak(fourth) is None
    # A day that ended without the failure carries nothing over.
    assert streak(new(tomorrow + timedelta(days=1), earlier=fourth.snapshot())) is None


def test_per_round_work_shows_the_time_of_this_rounds_harvest(tmp_path):
    from autocontrol.projects.manifest import ManifestProject

    from .helpers import demo_copy

    manifest = demo_copy(tmp_path, tasks=[
        {'id': 'signin', 'title': '每日签到', 'section': 'daily'},
        {'id': 'income', 'title': '城镇收益', 'section': 'daily', 'demand': 'round'},
    ])  # fmt: skip
    project = ManifestProject(manifest)

    def income(view):
        return next(row for row in view.snapshot()['tasks'] if row['task_id'] == 'income')

    def harvest(view, when):
        view.apply_result({'phase': 'daily', 'finished_at': when,
                           'updates': [('income', {'status': 'complete'})]})  # fmt: skip

    view = Projection(project.catalog, project.schedule, '2026-11-17T10:00:00+08:00')
    harvest(view, '2026-11-17T10:05:00+08:00')
    assert income(view)['done_at'] == '2026-11-17T10:05:00+08:00'
    later = Projection(project.catalog, project.schedule, '2026-11-17T12:00:00+08:00',
                       previous=view.snapshot())  # fmt: skip
    regrown = {'income': {'status': 'waiting', 'next_run_at': '2026-11-17T14:00:00+08:00'}}
    later.apply_event({'kind': 'plan', 'plan': {'full_checklist_summary': {'goals': regrown}}})
    harvest(later, '2026-11-17T12:05:00+08:00')
    assert income(later)['done_at'] == '2026-11-17T12:05:00+08:00'


def restarted(key, earlier, outcome, *, via_result=False):
    """``key`` finished as ``earlier`` at 09:00, then a 13:00 round started it again."""
    first = new()
    first.apply_event({'kind': 'task_finished', 'task_id': key, **earlier})
    first.finish_run(NOW + timedelta(minutes=10))
    later = NOW + timedelta(hours=4)
    projection = new(later, first.snapshot())
    projection.apply_event({'kind': 'task_started', 'task_id': key})
    assert task(projection, key)['status'] == 'running'
    if via_result:
        projection.apply_result(
            {'phase': 'auto', 'execution': 'complete', 'updates': [(key, outcome)]}
        )
    else:
        projection.apply_event({'kind': 'task_finished', 'task_id': key, **outcome})
    projection.finish_run(later + timedelta(minutes=5))
    return task(first, key), task(projection, key)


def test_work_done_earlier_today_stays_done_when_its_restart_returns_only_unknown():
    for via_result in (False, True):
        before, row = restarted(
            'arena', {'status': 'complete'}, {'status': 'unknown'}, via_result=via_result
        )
        assert (row['status'], row['reason']) == ('complete', 'completed_observation_retained')
        assert row['done_at'] == before['done_at']


def test_a_restart_returning_only_unknown_leaves_other_work_unknown():
    for key, earlier, outcome in (
        # not done before it started: the round may have changed it
        ('arena', {'status': 'prepared'}, {'status': 'unknown'}),
        # a request went out and its answer is missing
        (
            'arena',
            {'status': 'complete'},
            {'status': 'uncertain', 'reason': 'request_result_missing'},
        ),
        # per-round work: this round's own outcome decides
        ('pets', {'status': 'complete'}, {'status': 'unknown'}),
    ):
        _, row = restarted(key, earlier, outcome)
        assert row['status'] == 'result_unavailable', (key, earlier, outcome)
