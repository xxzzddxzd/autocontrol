"""A realistic sample day for previewing the card, built through the real projection."""

from __future__ import annotations

from datetime import datetime

DAY = '2026-11-17'


def _ts(clock: str) -> float:
    return datetime.fromisoformat(f'{DAY}T{clock}:00+08:00').timestamp()


def demo_snapshot(project, stage: str = 'running') -> dict:
    """``running``: mid-way through 日常; ``finished``: the round is over; ``done``: all ticked."""
    projection = project.new_projection(f'{DAY}T09:15:00+08:00', restore=False)
    projection.set_round(project.phases)
    event = projection.apply_event
    goals = {
        'cardbook': {'status': 'awaiting_completion', 'counts': {'ordinary_sent': 9, 'ordinary_limit': 13,
                     'next_completion': int(_ts('12:04'))}},
        'arena': {'status': 'complete', 'counts': {'obtained': 12, 'limit': 12}},
        'guild': {'status': 'incomplete'},
        'daily_activity': {'status': 'incomplete', 'counts': {'chests_claimed': 4, 'chests_target': 6}},
        'requested_shop': {'status': 'complete'},
        'red_dots': {'status': 'incomplete', 'counts': {'claimable': 3}},
        'member_rewards': {'status': 'complete'},
        'vigor_exchange': {'status': 'complete', 'counts': {'used': 20, 'target': 20}},
        'pets': {'status': 'incomplete'},
        'endorsement': {'status': 'incomplete'},
        'first_clears': {'status': 'blocked'},
        'free_lottery': {'status': 'complete'},
        'wardrobe_palettes': {'status': 'incomplete'},
        'activities': {'status': 'incomplete', 'counts': {'open': 3}},
    }  # fmt: skip
    event(
        {
            'kind': 'plan',
            'phase': 'cardbook',
            'observed_at': _ts('09:16'),
            'plan': {'game_day': DAY, 'full_checklist_summary': {'goals': goals}},
        }
    )
    event({'kind': 'phase_started', 'phase': 'cardbook'})
    event(
        {
            'kind': 'task_finished',
            'task_id': 'cardbook',
            'status': 'complete',
            'current': 13,
            'total': 13,
            'unit': '派遣',
            'observed_at': _ts('09:31'),
        }
    )
    event({'kind': 'phase_finished', 'phase': 'cardbook', 'status': 'complete'})
    projection.mark_slot('cardbook', f'{DAY}T09:16:00+08:00')  # the 06:00 window ran
    event({'kind': 'phase_started', 'phase': 'auto', 'observed_at': _ts('09:32')})
    event(
        {
            'kind': 'task_finished',
            'task_id': 'guild',
            'status': 'waiting',
            'next_run_at': _ts('13:15'),
        }
    )
    event(
        {
            'kind': 'task_progress',
            'task_id': 'daily_activity',
            'status': 'complete',
            'current': 6,
            'total': 6,
            'unit': '宝箱',
        }
    )
    event({'kind': 'task_failed', 'task_id': 'pet_care', 'reason': 'action_exception'})
    event(
        {
            'kind': 'task_finished',
            'task_id': 'stage_first_clears',
            'status': 'blocked',
            'reason': 'story_execution_unavailable',
        }
    )
    event(
        {
            'kind': 'task_finished',
            'task_id': 'lottery_red_envelope',
            'status': 'uncertain',
            'reason': 'request_result_missing',
        }
    )
    event(
        {
            'kind': 'task_finished',
            'task_id': 'vip_weekly_gift',
            'status': 'unknown',
            'reason': 'state_unavailable',
        }
    )
    event({'kind': 'task_started', 'task_id': 'red_dots'})
    event({'kind': 'task_started', 'task_id': 'mail', 'observed_at': _ts('10:12')})
    if stage == 'running':
        return projection.snapshot()

    event({'kind': 'task_finished', 'task_id': 'mail', 'status': 'complete'})
    event({'kind': 'task_finished', 'task_id': 'red_dots', 'status': 'complete'})
    event({'kind': 'task_finished', 'task_id': 'endorsement_maintenance', 'status': 'complete'})
    event({'kind': 'phase_finished', 'phase': 'auto', 'status': 'partial'})
    projection.apply_result(
        {
            'phase': 'craft',
            'game_day': DAY,
            'execution': 'partial',
            'updates': [
                ('gold_purchase', {'status': 'complete'}),
                ('stamina_purchase', {'status': 'complete'}),
                ('heart_crafting', {'status': 'complete'}),
                ('main_story_crafting', {'status': 'blocked', 'reason': 'no_material_route'}),
            ],
        }
    )
    projection.apply_result(
        {
            'phase': 'wardrobe',
            'game_day': DAY,
            'execution': 'complete',
            'updates': [
                ('palette_unlock', {'status': 'complete'}),
                ('qianhuan', {'status': 'complete'}),
            ],
        }
    )
    projection.apply_result(
        {
            'phase': 'activities',
            'game_day': DAY,
            'execution': 'partial',
            'updates': [
                ('activity_resources', {'status': 'complete'}),
                ('astrology', {'status': 'complete'}),
                ('ring_toss', {'status': 'waiting_for_completion', 'next_run_at': _ts('17:15')}),
                ('penglai', {'status': 'closed'}),
                ('activity_discovery', {'status': 'unsupported'}),
            ],
            'finished_at': f'{DAY}T10:41:00+08:00',
        }
    )
    if stage == 'done':
        for key in (
            'guild',
            'pets',
            'endorsement',
            'first_clears',
            'main_story_crafting',
            'ring_toss',
            'activity_discovery',
            'lottery_red_envelope',
            'vip_weekly_gift',
        ):
            event({'kind': 'task_finished', 'task_id': key, 'status': 'complete'})
        event({'kind': 'task_finished', 'task_id': 'cardbook', 'status': 'complete'})
        for slot in ('14:00', '22:00'):  # the later 事件簿 windows ran too
            projection.mark_slot('cardbook', f'{DAY}T{slot}:00+08:00')
    projection.finish_run(f'{DAY}T10:41:00+08:00')
    return projection.snapshot()
