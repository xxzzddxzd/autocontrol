"""Ported from yslzm/tests/test_run_scheduled.py (summaries, storage, child protocol).

Tests of the removed legacy entry points (run_full_checklist, the run_scheduled CLI and the
webhook digest) were dropped; the round runner owns phase sequencing now.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from autocontrol import storage
from autocontrol.projects.yslzm import phases as scheduled, summary as rules


DAY = '2026-11-11'
PRIVATE_MARKER = 'private response, token or account id'


class ScheduledRunTests(unittest.TestCase):
    def test_daily_entry_saves_a_sanitized_result(self):
        summary = {
            'game_day': DAY,
            'execution': 'activity_complete',
            'daily_full_rewards_verified': True,
            'daily_activity_rewards_confirmed': True,
            'all_actions_settled': True,
            'daily_activity': 120,
            'claimed_daily_chests': 6,
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)) as run:
            record, path = self.run_result('daily')
        self.assertEqual(record['command'], 'daily')
        self.assertEqual(record['execution'], 'complete')
        self.assertEqual(record['claimed_daily_chests'], 6)
        self.assertIn('daily', run.call_args.args[0])
        self.assertEqual(json.loads(path.read_text()), record)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'workspace'
        self.project.mkdir()
        self.private = self.root / 'private'
        self.private.mkdir(mode=0o700)

    def run_result(self, command='auto'):
        return scheduled.run_scheduled(command, DAY, project=self.project, private=self.private)

    def completed(self, summary, code=0):
        output = PRIVATE_MARKER.encode() + b'\n' + json.dumps(summary).encode() + b'\n'
        return subprocess.CompletedProcess([], code, stdout=output)

    def test_auto_result_persists_only_allowlisted_fields(self):
        summary = {
            'game_day': DAY,
            'execution': 'activity_complete',
            'daily_full_rewards_verified': True,
            'daily_activity_rewards_confirmed': True,
            'all_actions_settled': True,
            'daily_activity': 120,
            'claimed_daily_chests': 6,
            'cardbook_daily': {'status': 'awaiting_completion', 'detail': PRIVATE_MARKER},
            'login': PRIVATE_MARKER,
            'mail_unread_reward_indicators': PRIVATE_MARKER,
            'confirmed_reward_counts': {PRIVATE_MARKER: 1, 'mail': 3},
            'deferred_counts': {'current': 2, PRIVATE_MARKER: 1},
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)) as run:
            record, path = self.run_result()
        self.assertEqual(record['execution'], 'complete')
        self.assertEqual(record['daily_activity'], 120)
        self.assertEqual(record['cardbook_status'], 'awaiting_completion')
        self.assertEqual(path.parent.name, record['civil_date'])
        self.assertTrue(record['started_at'].endswith('+08:00'))
        self.assertTrue(record['finished_at'].endswith('+08:00'))
        self.assertEqual(json.loads(path.read_text()), record)
        self.assertNotIn(PRIVATE_MARKER, path.read_text())
        self.assertNotIn('mail_unread_reward_indicators', record)
        self.assertEqual(record['confirmed_reward_counts'], {'mail': 3})
        self.assertEqual(record['deferred_counts'], {'current': 2})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertIsNone(run.call_args.kwargs['timeout'])
        self.assertEqual(run.call_args.args[0][-1], '--scheduled-result')

    def test_feature_problems_locate_pending_without_retaining_private_rows(self):
        summary = {
            'game_day': DAY,
            'execution': 'pending_reconciliation',
            'lottery_red_envelopes': [
                {'status': 'confirmed'},
                {'status': 'uncertain', 'diagnostics': PRIVATE_MARKER, 'id': PRIVATE_MARKER},
            ],
            'cardbook_daily': {'status': 'unknown', 'missing': PRIVATE_MARKER},
            'palette_unlock': {'status': 'insufficient_materials', 'recipe': PRIVATE_MARKER},
            'pet_care': {'status': 'needs_food_supply'},
            'monthly_sign': {'status': 'unknown', 'raw': PRIVATE_MARKER},
            'open_shop': [{'status': 'partial'}, {'status': 'selling'}],
            'ring_toss': [{'status': 'query_unavailable'}],
            'vvanna_quiz': {'status': 'answer_unavailable'},
            'endorsement_maintenance': {'status': PRIVATE_MARKER},
            PRIVATE_MARKER: {'status': 'pending_reconciliation'},
            'confirmed_reward_counts': {'lottery_red_envelope': 1, 'pet_care': 0},
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result()
        self.assertEqual(
            record['feature_problems'],
            {
                'lottery_red_envelopes': 'uncertain',
                'cardbook_daily': 'unknown',
                'palette_unlock': 'insufficient_materials',
                'pet_care': 'needs_food_supply',
                'monthly_sign': 'unknown',
                'ring_toss': 'query_unavailable',
                'vvanna_quiz': 'answer_unavailable',
            },
        )
        self.assertEqual(
            record['confirmed_reward_counts'], {'lottery_red_envelope': 1, 'pet_care': 0}
        )
        self.assertNotIn(PRIVATE_MARKER, path.read_text())
        self.assertNotIn('diagnostics', path.read_text())

    def test_feature_problems_ignore_unexpected_shapes_and_superseded_problems(self):
        summary = {
            'game_day': DAY,
            'execution': 'activity_complete',
            'open_shop': [{'status': 'unknown'}, {'status': 'daily_sale_complete'}],
            'pet_care': {'status': ['unknown']},
            'monthly_sign': PRIVATE_MARKER,
            'ring_toss': [None, PRIVATE_MARKER, {'other': PRIVATE_MARKER}],
            'social_topic_coins': {'status': {'private': PRIVATE_MARKER}},
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result()
        self.assertNotIn('feature_problems', record)
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_full_checklist_summary_keeps_only_fixed_goal_schema(self):
        summary = {
            'game_day': DAY,
            'execution': 'activity_complete',
            'full_checklist_summary': {
                'scope': 'full_daily',
                'attempted': True,
                'complete': False,
                'diagnostics': PRIVATE_MARKER,
                'goals': {
                    'endorsement': {
                        'status': 'waiting',
                        'availability': 'open',
                        'attempted': True,
                        'counts': {'rounds_done': 1, 'round_limit': 3, PRIVATE_MARKER: 123},
                        'details': PRIVATE_MARKER,
                    },
                    'cardbook': {
                        'status': 'waiting',
                        'availability': 'open',
                        'counts': {'next_completion': 1794371400, 'running': 4},
                    },
                    'pets': {
                        'status': PRIVATE_MARKER,
                        'availability': PRIVATE_MARKER,
                        'attempted': PRIVATE_MARKER,
                        'counts': {'feed': True, 'bath': -1, 'play': PRIVATE_MARKER},
                    },
                    PRIVATE_MARKER: {'status': 'unknown'},
                },
            },
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result()
        self.assertEqual(
            record['full_checklist_summary'],
            {
                'scope': 'full_daily',
                'attempted': False,
                'complete': False,
                'goals': {
                    'endorsement': {
                        'status': 'waiting',
                        'availability': 'open',
                        'attempted': True,
                        'counts': {'rounds_done': 1, 'round_limit': 3},
                    },
                    'cardbook': {
                        'status': 'waiting',
                        'availability': 'open',
                        'counts': {'next_completion': 1794371400, 'running': 4},
                    },
                    'pets': {'counts': {}},
                },
            },
        )
        self.assertNotIn(PRIVATE_MARKER, path.read_text())
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, _ = scheduled.run_scheduled(
                'daily', DAY, project=self.project, private=self.private
            )
        self.assertNotIn('full_checklist_summary', record)

    def test_full_checklist_completion_overrides_activity_only_flags(self):
        flags = dict.fromkeys(rules.AUTO_VERIFICATION, True)
        goals = {
            key: {'status': 'complete', 'attempted': True} for key in rules.CHECKLIST_GOAL_COUNTS
        }
        summary = {
            'game_day': DAY,
            'execution': 'activity_complete',
            **flags,
            'full_checklist_summary': {
                'scope': 'full_daily',
                'attempted': True,
                'complete': False,
                'goals': goals,
            },
        }
        cases = [('waiting', 'waiting'), ('unknown', 'partial'), ('blocked', 'partial')]
        for goal_status, expected in cases:
            with self.subTest(goal_status=goal_status):
                goals['endorsement']['status'] = goal_status
                with patch.object(
                    scheduled.subprocess, 'run', return_value=self.completed(summary)
                ):
                    record, _ = self.run_result()
                self.assertEqual(record['execution'], expected)
                self.assertEqual(record['daily_status'], 'activity_complete')
        summary['execution'] = 'pending_reconciliation'
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, _ = self.run_result()
        self.assertEqual(record['execution'], 'pending_reconciliation')
        summary['execution'] = 'activity_complete'
        summary['full_checklist_summary']['complete'] = True
        goals['endorsement']['status'] = 'complete'
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, _ = self.run_result()
        self.assertEqual(record['execution'], 'complete')

    def test_full_completion_requires_all_goals_and_their_attempt_evidence(self):
        flags = dict.fromkeys(rules.AUTO_VERIFICATION, True)
        for goals in (
            None,
            {},
            {'arena': {'status': 'complete', 'attempted': True}},
            {
                key: {'status': 'complete', 'attempted': False}
                for key in rules.CHECKLIST_GOAL_COUNTS
            },
        ):
            with self.subTest(goals=goals):
                summary = {
                    'game_day': DAY,
                    'execution': 'activity_complete',
                    **flags,
                    'full_checklist_summary': {
                        'scope': 'full_daily',
                        'attempted': True,
                        'complete': True,
                        'goals': goals,
                    },
                }
                with patch.object(
                    scheduled.subprocess, 'run', return_value=self.completed(summary)
                ):
                    record, _ = self.run_result()
                self.assertEqual(record['execution'], 'partial')
                self.assertFalse(record['full_checklist_summary']['complete'])

    def test_auto_zero_exit_without_verified_rewards_is_partial(self):
        summary = {
            'game_day': DAY,
            'execution': 'activity_complete',
            'daily_full_rewards_verified': True,
            'daily_activity_rewards_confirmed': True,
            'all_actions_settled': False,
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, _ = self.run_result()
        self.assertEqual((record['exit_code'], record['execution']), (0, 'partial'))

    def test_cardbook_results_use_final_daily_row_and_exit_code(self):
        summary = {
            'game_day': DAY,
            'cardbook_actions': [
                {'kind': 'cardbook_daily', 'status': 'awaiting_completion'},
                {
                    'kind': 'cardbook_daily',
                    'status': 'daily_limit_complete',
                    'confirmed_total': 13,
                    'detail': PRIVATE_MARKER,
                },
                {'kind': 'cardbook_reward', 'status': 'confirmed'},
                {'kind': 'cardbook_refresh', 'status': 'confirmed'},
                {'kind': 'cardbook_dispatch', 'status': 'confirmed'},
            ],
            'activity_resume_actions': [{'status': 'awaiting_ready'}],
            'timed_free_reward_actions': [{'status': 'unknown'}],
            'endorsement_maintenance_actions': [{'status': 'rejected'}],
        }
        for code, expected in ((0, 'complete'), (1, 'failed')):
            with self.subTest(code=code), patch.object(
                scheduled.subprocess, 'run', return_value=self.completed(summary, code)
            ):
                record, path = self.run_result('cardbook')
                self.assertEqual(record['execution'], expected)
                self.assertEqual(record['confirmed_total'], 13)
                self.assertEqual(
                    record['confirmed_cardbook_counts'],
                    {'cardbook_reward': 1, 'cardbook_refresh': 1, 'cardbook_dispatch': 1},
                )
                if code:
                    self.assertEqual(
                        record['cardbook_group_failures'],
                        {
                            'activity_resume': 'awaiting_ready',
                            'timed_free_reward': 'unknown',
                            'endorsement_maintenance': 'rejected',
                        },
                    )
                self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_cardbook_retains_activity_confirmation_counts_and_last_fixed_state(self):
        summary = {
            'game_day': DAY,
            'cardbook_actions': [{'kind': 'cardbook_daily', 'status': 'awaiting_completion'}],
            'activity_resume_actions': [
                *[
                    {'feature': 'ring_toss', 'status': 'confirmed', 'target_id': PRIVATE_MARKER}
                    for _ in range(6)
                ],
                {
                    'feature': 'ring_toss',
                    'status': 'attempts_exhausted',
                    'remaining': 0,
                    'score': 1500,
                    'highest_reward_threshold': 4000,
                    'diagnostics': PRIVATE_MARKER,
                },
                {'feature': 'open_shop', 'status': 'confirmed'},
                {'feature': 'open_shop', 'status': 'selling', 'score': 150000},
                {'feature': 'activity_rewards', 'status': 'activated'},
                {'feature': 'activity_rewards', 'status': 'confirmed'},
                {'feature': 'activity_rewards', 'status': 'available_activity_rewards_complete'},
                {'feature': PRIVATE_MARKER, 'status': 'confirmed'},
            ],
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result('cardbook')
        self.assertEqual(
            record['confirmed_activity_counts'],
            {'open_shop': 1, 'ring_toss': 6, 'activity_rewards': 1},
        )
        self.assertEqual(
            record['activity_resume_state'],
            {
                'ring_toss': {
                    'status': 'attempts_exhausted',
                    'remaining': 0,
                    'score': 1500,
                    'highest_reward_threshold': 4000,
                },
                'open_shop': {'status': 'selling', 'score': 150000},
                'activity_rewards': {'status': 'available_activity_rewards_complete'},
            },
        )
        self.assertEqual(record['execution'], 'awaiting_completion')
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_activity_last_state_rejects_arbitrary_strings_and_bad_numbers(self):
        summary = {
            'game_day': DAY,
            'cardbook_actions': [{'kind': 'cardbook_daily', 'status': 'awaiting_completion'}],
            'activity_resume_actions': [
                {
                    'feature': 'ring_toss',
                    'status': PRIVATE_MARKER,
                    'remaining': True,
                    'score': -1,
                    'highest_reward_threshold': PRIVATE_MARKER,
                },
            ],
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary, 1)):
            record, path = self.run_result('cardbook')
        self.assertEqual(record['activity_resume_state'], {'ring_toss': {'status': 'unrecognized'}})
        self.assertNotIn(PRIVATE_MARKER, path.read_text())
        self.assertEqual(record['execution'], 'failed')

    def test_activity_resume_failure_states_retain_feature_without_widening_success(self):
        summary = {
            'game_day': DAY,
            'cardbook_actions': [{'kind': 'cardbook_daily', 'status': 'awaiting_task_refresh'}],
            'activity_resume_actions': [
                {
                    'feature': 'open_shop',
                    'status': 'insufficient_activity_money',
                    'diagnostics': PRIVATE_MARKER,
                },
                {'feature': 'ring_toss', 'status': 'query_unavailable', 'id': PRIVATE_MARKER},
                {'feature': 'activity_rewards', 'status': 'task_limit_reached'},
                {'feature': PRIVATE_MARKER, 'status': 'unknown'},
            ],
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary, 1)):
            record, path = self.run_result('cardbook')
        self.assertEqual(record['execution'], 'failed')
        self.assertEqual(
            record['activity_resume_problems'],
            {
                'open_shop': 'insufficient_activity_money',
                'ring_toss': 'query_unavailable',
                'activity_rewards': 'task_limit_reached',
            },
        )
        self.assertNotIn(PRIVATE_MARKER, path.read_text())
        for status in (
            'awaiting_map_transition',
            'old_sale_unsettled',
            'unsupported',
            'awaiting_activity_initialization',
            'unknown_sale_window',
        ):
            with self.subTest(status=status):
                self.assertNotIn(status, rules.ACTIVITY_SETTLED)
                summary['activity_resume_actions'] = [{'feature': 'open_shop', 'status': status}]
                with patch.object(
                    scheduled.subprocess, 'run', return_value=self.completed(summary, 1)
                ):
                    record, _ = self.run_result('cardbook')
                self.assertEqual(record['cardbook_group_failures']['activity_resume'], status)
                self.assertEqual(record['activity_resume_problems']['open_shop'], status)
                self.assertEqual(record['execution'], 'failed')

    def test_activity_resume_diagnostics_ignore_settled_and_unrecognized_strings(self):
        summary = {
            'game_day': DAY,
            'cardbook_actions': [{'kind': 'cardbook_daily', 'status': 'awaiting_completion'}],
            'activity_resume_actions': [
                {'feature': 'open_shop', 'status': 'selling'},
                {'feature': 'ring_toss', 'status': 'target_reached'},
                {'feature': 'activity_rewards', 'status': PRIVATE_MARKER},
            ],
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary, 1)):
            record, path = self.run_result('cardbook')
        self.assertEqual(record['activity_resume_problems'], {'activity_rewards': 'unrecognized'})
        self.assertNotIn(PRIVATE_MARKER, path.read_text())
        summary['activity_resume_actions'].pop()
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, _ = self.run_result('cardbook')
        self.assertNotIn('activity_resume_problems', record)

    def test_cardbook_daily_reward_resume_persists_only_fixed_status_and_counts(self):
        for status, code in (
            ('activity_complete', 0),
            ('partial', 0),
            ('unknown', 1),
            ('uncertain', 1),
            ('pending_reconciliation', 1),
        ):
            with self.subTest(status=status):
                summary = {
                    'game_day': DAY,
                    'cardbook_actions': [
                        {'kind': 'cardbook_daily', 'status': 'awaiting_completion'}
                    ],
                    'daily_reward_resume': {
                        'status': status,
                        'daily_activity': 120,
                        'claimed_daily_chests': 6,
                        'task_rewards_confirmed': 3,
                        'chests_confirmed': 2,
                        'diagnostics': PRIVATE_MARKER,
                        'role_id': PRIVATE_MARKER,
                    },
                }
                with patch.object(
                    scheduled.subprocess, 'run', return_value=self.completed(summary, code)
                ):
                    record, path = self.run_result('cardbook')
                self.assertEqual(
                    record['daily_reward_resume'],
                    {
                        'status': status,
                        'daily_activity': 120,
                        'claimed_daily_chests': 6,
                        'task_rewards_confirmed': 3,
                        'chests_confirmed': 2,
                    },
                )
                self.assertEqual(record['execution'], 'failed' if code else 'awaiting_completion')
                if status == 'activity_complete':
                    self.assertNotIn('feature_problems', record)
                else:
                    self.assertEqual(record['feature_problems'], {'daily_reward_resume': status})
                self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_cardbook_daily_reward_resume_rejects_unexpected_values(self):
        summary = {
            'game_day': DAY,
            'cardbook_actions': [{'kind': 'cardbook_daily', 'status': 'awaiting_completion'}],
            'daily_reward_resume': {
                'status': 'partial',
                'daily_activity': PRIVATE_MARKER,
                'claimed_daily_chests': True,
                'task_rewards_confirmed': -1,
                'chests_confirmed': 100001,
            },
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, _ = self.run_result('cardbook')
        self.assertEqual(record['daily_reward_resume'], {'status': 'partial'})
        summary['daily_reward_resume']['status'] = PRIVATE_MARKER
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result('cardbook')
        self.assertNotIn('daily_reward_resume', record)
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_cardbook_action_failure_has_business_result_without_daily_row(self):
        summary = {
            'game_day': DAY,
            'cardbook_actions': [
                {'kind': 'cardbook_reward', 'status': 'confirmed'},
                {
                    'kind': 'cardbook_dispatch',
                    'status': 'uncertain',
                    'error_type': 'TimeoutError',
                    'target': 13,
                    'detail': PRIVATE_MARKER,
                },
            ],
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary, 1)):
            record, path = self.run_result('cardbook')
        self.assertEqual(record['execution'], 'failed')
        self.assertEqual(record['cardbook_action_kind'], 'cardbook_dispatch')
        self.assertEqual(record['cardbook_status'], 'uncertain')
        self.assertEqual(record['error_type'], 'TimeoutError')
        self.assertEqual(record['target'], 13)
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_cardbook_dispatch_target_with_active_tasks_still_awaits_completion(self):
        summary = {
            'game_day': DAY,
            'cardbook_actions': [
                {'kind': 'cardbook_daily', 'status': 'daily_limit_complete', 'running_tasks': 2},
            ],
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, _ = self.run_result('cardbook')
        self.assertEqual(record['execution'], 'awaiting_completion')
        self.assertEqual(record['cardbook_status'], 'daily_limit_complete')

    def test_early_cwd_and_child_start_failures_are_saved(self):
        with patch.object(scheduled.subprocess, 'run') as run:
            self.project.rmdir()
            record, path = self.run_result()
            run.assert_not_called()
        self.assertEqual(record['failure_stage'], 'project_cwd')
        self.assertEqual(record['error_type'], 'FileNotFoundError')
        self.assertTrue(path.is_file())
        self.project.mkdir()
        with patch.object(scheduled.subprocess, 'run', side_effect=PermissionError(PRIVATE_MARKER)):
            record, path = self.run_result()
        self.assertEqual(record['failure_stage'], 'process_start')
        self.assertEqual(record['error_type'], 'PermissionError')
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_missing_summary_timeout_and_main_failures_are_never_success(self):
        cases = [
            (
                self.completed({'irrelevant': PRIVATE_MARKER}),
                'unverified',
                'summary',
                'MissingSummary',
            ),
            (
                self.completed({'irrelevant': PRIVATE_MARKER}, 1),
                'failed',
                'process',
                'MissingSummary',
            ),
            (
                subprocess.TimeoutExpired([], 1, output=PRIVATE_MARKER),
                'timeout',
                'execute',
                'TimeoutExpired',
            ),
            (
                self.completed(
                    {
                        'execution': 'failed',
                        'failure_stage': 'connect',
                        'error_type': PRIVATE_MARKER,
                    },
                    1,
                ),
                'failed',
                'connect',
                'OtherError',
            ),
            (
                self.completed({'execution': 'already_running'}, 2),
                'already_running',
                'runtime_lock',
                None,
            ),
        ]
        for value, execution, stage, error_type in cases:
            options = {'side_effect' if isinstance(value, Exception) else 'return_value': value}
            with self.subTest(execution=execution), patch.object(
                scheduled.subprocess, 'run', **options
            ):
                record, path = self.run_result()
            self.assertEqual((record['execution'], record['failure_stage']), (execution, stage))
            self.assertEqual(record.get('error_type'), error_type)
            self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_real_child_protocol_preserves_cardbook_waiting(self):
        script = self.project / 'main.py'
        script.write_text(
            "import json, sys\n"
            "assert sys.argv[1:] == ['cardbook', '--not-before-game-day', "
            f"'{DAY}', '--scheduled-result']\n"
            f"print({PRIVATE_MARKER!r}, file=sys.stderr)\n"
            "print(json.dumps({'kind': 'cardbook_daily', 'status': 'awaiting_refresh', "
            "'game_day': '2026-11-10'}))\n"
        )
        record, path = self.run_result('cardbook')
        self.assertEqual((record['exit_code'], record['execution']), (0, 'awaiting_refresh'))
        self.assertEqual(record['game_day'], '2026-11-10')
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_deferred_games_are_fixed_result_metadata_not_an_exhaustion_claim(self):
        summary = {
            'game_day': DAY,
            'execution': 'partial',
            'daily_activity_games_deferred': True,
            'wardrobe_palettes_deferred': True,
            'deferred_to_activities': [PRIVATE_MARKER],
            'deferred_to_wardrobe': [PRIVATE_MARKER],
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result('auto')
        self.assertEqual(record['deferred_to_activities'], ['ring_toss', 'penglai'])
        self.assertEqual(record['deferred_to_wardrobe'], ['palette_unlock', 'qianhuan'])
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_new_phase_keeps_only_fixed_results_and_respects_waiting(self):
        summary = {
            'game_day': DAY,
            'execution': 'waiting',
            'policy_jobs': {
                'activity_resources': [
                    {
                        'status': 'daily_limit_complete',
                        'water_tasks_claimed': 3,
                        'ring_tasks_claimed': 3,
                        'water_remaining': 0,
                        'ring_remaining': 0,
                        'response': PRIVATE_MARKER,
                    }
                ],
                'ring_toss': [{'status': 'attempts_exhausted', 'confirmed_throws': 6}],
                'activity_rewards': [{'status': 'available_activity_rewards_complete'}],
                'brand_atlas': [{'status': 'cleared', 'unknown': 0}],
                'activity_discovery': [{'status': 'complete'}],
                'astrology': [{'status': 'closed'}],
                'bear': [{'status': 'closed'}],
                'produce_activity': [{'status': 'closed'}],
                'produce_common': [{'status': 'closed'}],
                'brand_tower': [{'status': 'level_locked', 'private': PRIVATE_MARKER}],
                'penglai': [
                    {'status': 'confirmed', 'response': PRIVATE_MARKER},
                    {
                        'status': 'daily_growth_complete',
                        'score': 120,
                        'used_seeds': 3,
                        'role': PRIVATE_MARKER,
                    },
                ],
            },
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result('activities')
        self.assertEqual(record['execution'], 'waiting')
        self.assertEqual(
            record['policy_jobs']['activity_resources'],
            {
                'status': 'daily_limit_complete',
                'confirmed_actions': 0,
                'water_tasks_claimed': 3,
                'ring_tasks_claimed': 3,
                'water_remaining': 0,
                'ring_remaining': 0,
            },
        )
        self.assertEqual(
            record['policy_jobs']['penglai'],
            {
                'status': 'daily_growth_complete',
                'score': 120,
                'used_seeds': 3,
                'confirmed_actions': 1,
                'confirmed_plantings': 0,
                'confirmed_waterings': 0,
                'confirmed_harvests': 0,
            },
        )
        self.assertNotIn(PRIVATE_MARKER, path.read_text())
        summary.update(execution='partial', policy_job_states={'penglai': 'unknown'})
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, _ = self.run_result('activities')
        self.assertEqual(record['execution'], 'partial')
        self.assertEqual(record['policy_jobs']['penglai']['status'], 'unknown')

    def test_missing_activity_grants_do_not_report_full_phase_complete(self):
        summary = {
            'game_day': DAY,
            'execution': 'complete',
            'policy_jobs': {
                name: [{'status': 'closed'}]
                for name in rules.PHASE_JOBS['activities']
                if name != 'activity_resources'
            },
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, _ = self.run_result('activities')
        self.assertEqual(record['execution'], 'partial')

    def test_bear_and_astrology_counts_keep_current_confirmed_deltas_separate_from_totals(self):
        jobs = {name: [{'status': 'closed'}] for name in rules.PHASE_JOBS['activities']}
        jobs['astrology'] = [
            {'kind': 'astrology_attempt_reward', 'status': 'confirmed', 'gain': 1},
            {'kind': 'astrology_attempt_reward', 'status': 'uncertain', 'gain': 7},
            *[
                {'kind': 'astrology_spin', 'status': 'confirmed', 'points': 12, 'gold': 15000}
                for _ in range(7)
            ],
            {'kind': 'astrology_spin', 'status': 'uncertain', 'error_type': 'TimeoutError'},
            {
                'status': 'attempts_exhausted',
                'remaining_attempts': 0,
                'score': 180,
                'attempt_tasks_claimed': 3,
                'attempt_tasks_claimable': 0,
                'attempt_tasks_unknown': 0,
                'attempt_tasks_below_threshold': 0,
            },
        ]
        jobs['bear'] = [
            {'kind': 'bear_buy', 'status': 'confirmed', 'score_delta': -150, 'count': 1},
            {
                'kind': 'bear_sell',
                'status': 'confirmed',
                'score_delta': 240,
                'count': 1,
                'jars': 10,
            },
            {'kind': 'bear_sell', 'status': 'uncertain', 'score_delta': 240, 'jars': 10},
            {'kind': 'bear_welcome', 'status': 'confirmed', 'coupons': 450, 'reward_claimed': True},
            {'kind': 'bear_store', 'status': 'confirmed', 'quantity': 1, 'spent': 90},
            {
                'kind': 'bear_daily_coupons',
                'status': 'server_automatic',
                'eligible_coupons': 270,
                'action_sent_actions': 0,
            },
            {
                'status': 'daily_trade_complete',
                'score': 1110,
                'jars': 100,
                'buy_used': 3,
                'sell_used': 6,
                'buy_limit': 3,
                'sell_limit': 6,
                'buy_target': 3,
                'sell_target': 6,
                'cakes': 0,
                'buy_remaining': 0,
                'sell_remaining': 0,
                'raw': PRIVATE_MARKER,
            },
        ]
        summary = {
            'game_day': DAY,
            'policy_jobs': jobs,
            'policy_job_states': {'astrology': 'uncertain', 'bear': 'uncertain'},
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result('activities')
        astro = record['policy_jobs']['astrology']
        bear = record['policy_jobs']['bear']
        self.assertEqual(
            (
                astro['confirmed_spins'],
                astro['confirmed_astrology_gold'],
                astro['confirmed_attempt_rewards'],
                astro['confirmed_attempts_gained'],
            ),
            (7, 105000, 1, 1),
        )
        self.assertEqual(
            (
                bear['confirmed_bear_buys'],
                bear['confirmed_bear_sells'],
                bear['confirmed_bear_score_spent'],
                bear['confirmed_bear_score_gained'],
                bear['confirmed_bear_jars'],
            ),
            (1, 1, 150, 240, 10),
        )
        self.assertEqual(
            (bear['score'], bear['jars'], bear['buy_used'], bear['sell_used']), (1110, 100, 3, 6)
        )
        self.assertEqual((bear['buy_target'], bear['sell_target'], bear['cakes']), (3, 6, 0))
        self.assertEqual(bear['confirmed_actions'], 4)
        self.assertEqual(record['execution'], 'partial')
        self.assertEqual(json.loads(path.read_text()), record)
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_bear_native_terminal_conditions_settle_without_fake_increments(self):
        for status in ('daily_trade_complete', 'daily_sale_limit_complete', 'outside_trade_window'):
            with self.subTest(status=status):
                jobs = {name: [{'status': 'closed'}] for name in rules.PHASE_JOBS['activities']}
                jobs['bear'] = [
                    {
                        'kind': 'bear_daily_coupons',
                        'status': 'server_automatic',
                        'eligible_coupons': 270,
                        'action_sent_actions': 0,
                    },
                    {'kind': 'bear', 'status': status, 'score': 720},
                ]
                summary = {'game_day': DAY, 'policy_jobs': jobs}
                record = rules.summarize('activities', DAY, 0, summary)
                self.assertEqual(record['execution'], 'complete')
                bear = record['policy_jobs']['bear']
                self.assertEqual(bear['status'], status)
                self.assertEqual(bear['confirmed_actions'], 0)
                self.assertEqual(bear['confirmed_bear_score_gained'], 0)
                self.assertNotIn('action_issues', record)

    def test_bear_welcome_and_store_metrics_count_only_current_confirmed_rewards(self):
        rows = [
            {'kind': 'bear_welcome', 'status': 'confirmed', 'reward_claimed': True, 'coupons': 450},
            {
                'kind': 'bear_welcome',
                'status': 'confirmed',
                'reward_claimed': False,
                'coupons': 999,
            },
            {'kind': 'bear_welcome', 'status': 'activated', 'reward_claimed': True, 'coupons': 999},
            {'kind': 'bear_welcome', 'status': 'uncertain', 'reward_claimed': True, 'coupons': 999},
            {
                'kind': 'bear_daily_coupons',
                'status': 'server_automatic',
                'coupons': 270,
                'eligible_coupons': 270,
                'action_sent_actions': 0,
            },
            {
                'kind': 'bear_store',
                'status': 'confirmed',
                'quantity': 2,
                'currency': 116,
                'spent': 600,
            },
            {
                'kind': 'bear_store',
                'status': 'confirmed',
                'quantity': 1,
                'currency': 120,
                'spent': 90,
            },
            {
                'kind': 'bear_store',
                'status': 'uncertain',
                'quantity': 1,
                'currency': 120,
                'spent': 150,
            },
            {
                'kind': 'bear_store',
                'status': 'rejected',
                'quantity': 1,
                'currency': 116,
                'spent': 300,
            },
            {
                'kind': 'bear_buy',
                'status': 'confirmed',
                'count': 1,
                'score_delta': -150,
                'coupons': 999,
                'spent': 999,
                'quantity': 999,
            },
            {
                'kind': 'bear_sell',
                'status': 'confirmed',
                'count': 1,
                'score_delta': 240,
                'jars': 10,
                'coupons': 999,
            },
            {'kind': 'bear', 'status': 'daily_trade_complete', 'score': 9999, 'jars': 9999},
        ]
        record = rules.summarize(
            'activities', DAY, 0, {'game_day': DAY, 'policy_jobs': {'bear': rows}}
        )
        bear = record['policy_jobs']['bear']
        self.assertEqual(
            (bear['confirmed_bear_welcome_rewards'], bear['confirmed_bear_welcome_coupons']),
            (1, 450),
        )
        self.assertEqual(
            (
                bear['confirmed_bear_store_purchases'],
                bear['confirmed_bear_store_score_spent'],
                bear['confirmed_bear_store_jars_spent'],
            ),
            (3, 600, 90),
        )
        self.assertEqual(
            (
                bear['confirmed_bear_score_spent'],
                bear['confirmed_bear_score_gained'],
                bear['confirmed_bear_jars'],
            ),
            (150, 240, 10),
        )

    def test_bear_welcome_and_store_metrics_reject_invalid_numeric_values(self):
        for invalid in (True, False, -1, 2**31, PRIVATE_MARKER):
            with self.subTest(invalid=invalid):
                rows = [
                    {
                        'kind': 'bear_welcome',
                        'status': 'confirmed',
                        'reward_claimed': True,
                        'coupons': invalid,
                    },
                    {
                        'kind': 'bear_store',
                        'status': 'confirmed',
                        'quantity': invalid,
                        'currency': 116,
                        'spent': invalid,
                    },
                    {
                        'kind': 'bear_store',
                        'status': 'confirmed',
                        'quantity': invalid,
                        'currency': 120,
                        'spent': invalid,
                    },
                ]
                record = rules.summarize(
                    'activities', DAY, 0, {'game_day': DAY, 'policy_jobs': {'bear': rows}}
                )
                bear = record['policy_jobs']['bear']
                for field in (
                    'confirmed_bear_welcome_coupons',
                    'confirmed_bear_store_purchases',
                    'confirmed_bear_store_score_spent',
                    'confirmed_bear_store_jars_spent',
                ):
                    self.assertEqual(bear[field], 0)

    def test_bear_absence_cannot_satisfy_the_complete_activity_phase(self):
        jobs = {
            name: [{'status': 'closed'}]
            for name in rules.PHASE_JOBS['activities']
            if name != 'bear'
        }
        record = rules.summarize('activities', DAY, 0, {'game_day': DAY, 'policy_jobs': jobs})
        self.assertEqual(record['execution'], 'partial')

    def test_new_typed_increments_reject_boolean_and_out_of_range_resource_values(self):
        jobs = {name: [{'status': 'closed'}] for name in rules.PHASE_JOBS['activities']}
        jobs['astrology'] = [
            {'kind': 'astrology_attempt_reward', 'status': 'confirmed', 'gain': True},
            {'kind': 'astrology_attempt_reward', 'status': 'confirmed', 'gain': 100001},
            {'kind': 'astrology_attempt_reward', 'status': 'uncertain', 'gain': 100},
            {'status': 'attempts_exhausted'},
        ]
        jobs['bear'] = [
            {'kind': 'bear_buy', 'status': 'confirmed', 'count': True, 'score_delta': False},
            {
                'kind': 'bear_sell',
                'status': 'confirmed',
                'count': 100001,
                'score_delta': 2**31,
                'jars': True,
            },
            {
                'kind': 'bear_sell',
                'status': 'uncertain',
                'count': 1,
                'score_delta': 240,
                'jars': 10,
            },
            {'status': 'daily_trade_complete', 'buy_used': True, 'jars': -1},
        ]
        record = rules.summarize('activities', DAY, 0, {'game_day': DAY, 'policy_jobs': jobs})
        self.assertEqual(record['policy_jobs']['astrology']['confirmed_attempts_gained'], 0)
        for key in (
            'confirmed_bear_buys',
            'confirmed_bear_sells',
            'confirmed_bear_score_gained',
            'confirmed_bear_score_spent',
            'confirmed_bear_jars',
        ):
            self.assertEqual(record['policy_jobs']['bear'][key], 0)
        self.assertNotIn('buy_used', record['policy_jobs']['bear'])
        self.assertNotIn('jars', record['policy_jobs']['bear'])

    def test_wardrobe_results_keep_qianhuan_counts_without_responses(self):
        summary = {
            'game_day': DAY,
            'execution': 'partial',
            'policy_jobs': {
                'palette_unlock': [{'status': 'partial', 'remaining_fashions': 3}],
                'qianhuan': [
                    {'status': 'confirmed', 'kind': 'qianhuan_unlock'},
                    {
                        'status': 'partial',
                        'confirmed_fashions': 1,
                        'remaining_fashions': 5,
                        'unlocked_fashions': 100,
                        'blocked_by_ordinary_palettes': 3,
                        'response': PRIVATE_MARKER,
                    },
                ],
            },
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result('wardrobe')
        self.assertEqual(record['policy_jobs']['qianhuan']['confirmed_fashions'], 1)
        self.assertEqual(record['policy_jobs']['qianhuan']['blocked_by_ordinary_palettes'], 3)
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_real_large_wardrobe_ipc_is_accepted_but_only_small_fixed_results_are_saved(self):
        row = {
            'kind': 'palette_unlock',
            'status': 'insufficient_materials',
            'reason': 'exclusive_wind_vane_not_sold',
            'diagnostics': {
                'material_planning': [{'item_name': PRIVATE_MARKER, 'detail': PRIVATE_MARKER * 32}]
            },
        }
        summary = {
            'game_day': DAY,
            'execution': 'partial',
            'sent_actions': 0,
            'policy_jobs': {
                'palette_unlock': [dict(row, fashion=index) for index in range(374)]
                + [
                    {
                        'status': 'partial',
                        'remaining_fashions': 374,
                        'blocked_reason_counts': {'exclusive_wind_vane_not_sold': 374},
                    }
                ],
                'qianhuan': [
                    {
                        'status': 'partial',
                        'remaining_fashions': 374,
                        'blocked_by_ordinary_palettes': 374,
                    }
                ],
            },
        }
        raw = json.dumps(summary).encode()
        self.assertGreater(len(raw), 256 * 1024)
        self.assertLess(len(raw), scheduled.MAX_SUMMARY_BYTES)
        script = self.project / 'main.py'
        script.write_text(
            'import json\n'
            + f'row={row!r}\n'
            + f"summary={dict(game_day=DAY,execution='partial',sent_actions=0)!r}\n"
            + "summary['policy_jobs']={'palette_unlock':[dict(row,fashion=index)for index in range(374)]+"
            "[{'status':'partial','remaining_fashions':374,'blocked_reason_counts':{'exclusive_wind_vane_not_sold':374}}],"
            "'qianhuan':[{'status':'partial','remaining_fashions':374,'blocked_by_ordinary_palettes':374}]}\n"
            'print(json.dumps(summary))\n'
        )
        record, path = self.run_result('wardrobe')
        self.assertEqual(record['exit_code'], 0)
        self.assertEqual(record['execution'], 'partial')
        self.assertEqual(record['policy_jobs']['palette_unlock']['remaining_fashions'], 374)
        self.assertEqual(
            record['policy_jobs']['palette_unlock']['blocked_reason_counts'],
            {'exclusive_wind_vane_not_sold': 374},
        )
        self.assertEqual(record['action_issues'][0]['count'], 374)
        saved = path.read_bytes()
        self.assertLess(len(saved), 256 * 1024)
        self.assertNotIn(PRIVATE_MARKER.encode(), saved)
        self.assertNotIn(b'diagnostics', saved)
        self.assertNotIn(b'item_name', saved)
        self.assertNotIn(b'rawresponse', saved)

    def test_ipc_json_above_two_mib_is_still_rejected(self):
        self.assertEqual(scheduled.MAX_SUMMARY_BYTES, 2 * 1024 * 1024)
        self.assertIsNone(
            scheduled.latest_summary(b'{"detail":"' + b'x' * scheduled.MAX_SUMMARY_BYTES + b'"}\n')
        )
        self.assertEqual(
            scheduled.latest_summary(
                b'x' * (scheduled.MAX_SUMMARY_BYTES + 100) + b'\n{"execution":"partial"}\n'
            ),
            {'execution': 'partial'},
        )

    def test_server_completion_details_and_snapshot_are_fixed_fields_only(self):
        summary = {
            'game_day': DAY,
            'execution': 'uncertain',
            'daily_activity': 120,
            'claimed_daily_chests': 6,
            'social_topic_coins': {
                'status': 'unknown',
                'known_completed_topics': 200,
                'catalog_topics': 399,
                'completion_evidence_status': 'partial',
                'topic_list_status': 'not_observed',
                'reason': 'topic_list_not_observed',
                'failure_stage': 'topic_discovery',
                'token': PRIVATE_MARKER,
            },
            'atlas_daily': {'status': 'cleared', 'finished_today': 1, 'response': PRIVATE_MARKER},
            'report_snapshot': {
                'source': 'before_close',
                'observed_server_time': int(
                    scheduled.datetime.fromisoformat(DAY + 'T12:00:00+08:00').timestamp()
                ),
                'game_day': DAY,
                'report_only': True,
                'response': PRIVATE_MARKER,
                'observed_at': PRIVATE_MARKER,
            },
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result('auto')
        self.assertEqual(
            record['feature_details']['social_topic_coins']['known_completed_topics'], 200
        )
        self.assertEqual(record['feature_details']['atlas_daily']['finished_today'], 1)
        self.assertEqual(record['report_snapshot']['source'], 'before_close')
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_craft_result_distinguishes_material_progress_from_finished_garments(self):
        summary = {
            'game_day': DAY,
            'execution': 'partial',
            'policy_jobs': {
                'main_story_crafting': [
                    {
                        'kind': 'craft_material_purchase',
                        'status': 'confirmed',
                        'response': PRIVATE_MARKER,
                    },
                    {'kind': 'craft_material_sweep', 'status': 'confirmed'},
                    {'kind': 'fashion_craft', 'status': 'confirmed'},
                    {'kind': 'fashion_extension', 'status': 'uncertain'},
                    {'status': 'session_refresh_required', 'stamina_remaining': 2},
                ]
            },
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result('craft')
        job = record['policy_jobs']['main_story_crafting']
        self.assertEqual(
            (
                job['confirmed_actions'],
                job['material_purchases'],
                job['material_sweeps'],
                job['fashion_crafts'],
            ),
            (3, 1, 1, 1),
        )
        self.assertNotIn('extensions', job)
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_red_sources_and_absent_weekly_offer_preserve_only_fixed_facts(self):
        summary = {
            'game_day': DAY,
            'execution': 'partial',
            'red_dot_sources': {
                'ui_coin_buttons': {
                    'status': 'needs_claim',
                    'claimable': 10,
                    'unknown': 0,
                    'response': PRIVATE_MARKER,
                },
                'social_topic_coins': {
                    'status': 'unknown',
                    'unsupported': 1,
                    'reason': 'task_template_unsupported',
                },
                'local_unread_markers': 'unknown',
                'all_home_badges': 'unverified',
                PRIVATE_MARKER: {'status': 'unknown'},
            },
            'member_reward_status': {
                'status': 'cleared',
                'vip_weekly_status': 'no_current_offer',
                'vip_weekly_reason': 'vip_weekly_offer_absent',
                'response': PRIVATE_MARKER,
            },
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result()
        details = record['feature_details']
        self.assertEqual(details['red_dot_sources']['sources']['ui_coin_buttons']['claimable'], 10)
        self.assertEqual(
            details['red_dot_sources']['sources']['social_topic_coins']['unsupported'], 1
        )
        self.assertEqual(details['red_dot_sources']['local_unread_markers'], 'unknown')
        self.assertEqual(details['member_rewards']['vip_weekly_status'], 'no_current_offer')
        self.assertEqual(details['member_rewards']['vip_weekly_reason'], 'vip_weekly_offer_absent')
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_purchase_delta_is_summed_from_confirmed_rows_not_server_used_quota(self):
        jobs = {
            'gold_purchase': [
                {
                    'kind': 'daily_resource_purchase',
                    'resource': 'gold',
                    'status': 'confirmed',
                    'count': 4,
                },
                {
                    'kind': 'daily_resource_purchase',
                    'resource': 'gold',
                    'status': 'confirmed',
                    'count': 6,
                },
                {
                    'kind': 'daily_resource_purchase',
                    'resource': 'gold',
                    'status': 'daily_limit_complete',
                    'used': 10,
                    'remaining': 0,
                },
            ],
            'stamina_purchase': [
                {
                    'kind': 'daily_resource_purchase',
                    'resource': 'stamina',
                    'status': 'daily_limit_complete',
                    'used': 8,
                    'remaining': 0,
                }
            ],
        }
        with patch.object(
            scheduled.subprocess,
            'run',
            return_value=self.completed(
                {'game_day': DAY, 'execution': 'partial', 'policy_jobs': jobs}
            ),
        ):
            record, _ = self.run_result('craft')
        self.assertEqual(record['policy_jobs']['gold_purchase']['confirmed_purchase_times'], 10)
        self.assertEqual(record['policy_jobs']['gold_purchase']['confirmed_actions'], 2)
        self.assertEqual(record['policy_jobs']['stamina_purchase']['confirmed_purchase_times'], 0)
        self.assertEqual(record['policy_jobs']['stamina_purchase']['confirmed_actions'], 0)

    def test_separate_wardrobe_phase_retains_only_bounded_goal_results(self):
        summary = {
            'game_day': DAY,
            'execution': 'partial',
            'policy_jobs': {
                'palette_unlock': [
                    {'status': 'confirmed'},
                    {
                        'status': 'partial',
                        'remaining_fashions': 20,
                        'confirmed_fashions': 1,
                        'skipped_fashions': 20,
                        'response': PRIVATE_MARKER,
                    },
                ],
                'qianhuan': [{'status': 'complete', 'remaining': 0, 'private': PRIVATE_MARKER}],
            },
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary)):
            record, path = self.run_result('wardrobe')
        self.assertEqual(record['execution'], 'partial')
        self.assertEqual(
            record['policy_jobs']['palette_unlock'],
            {
                'status': 'partial',
                'confirmed_actions': 1,
                'remaining_fashions': 20,
                'confirmed_fashions': 1,
                'skipped_fashions': 20,
            },
        )
        self.assertEqual(record['policy_jobs']['qianhuan']['remaining'], 0)
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_concurrent_runs_keep_separate_atomic_results(self):
        child = self.completed({'execution': 'already_running'}, 2)
        with patch.object(scheduled.subprocess, 'run', return_value=child):
            with ThreadPoolExecutor(max_workers=4) as pool:
                runs = list(pool.map(lambda _: self.run_result(), range(12)))
        paths = {path for _, path in runs}
        self.assertEqual(len(paths), 12)
        self.assertEqual(set(next(iter(paths)).parent.iterdir()), paths)
        self.assertTrue(all(json.loads(path.read_text())['exit_code'] == 2 for path in paths))

    def test_unsafe_results_directory_blocks_writes_after_child_runs(self):
        unsafe = self.private / 'results'
        unsafe.symlink_to(self.project, target_is_directory=True)
        with patch.object(
            scheduled.subprocess,
            'run',
            return_value=self.completed({'execution': 'already_running'}, 2),
        ) as run:
            record, path = self.run_result()
            self.assertIsNone(path)
            self.assertEqual(record['execution'], 'already_running')
            self.assertEqual(record['result_storage'], 'failed')
            run.assert_called_once()
        self.assertEqual(list(self.project.iterdir()), [])

    def test_exception_summary_keeps_successful_deltas_and_fixed_failure(self):
        summary = {
            'execution': 'failed',
            'game_day': DAY,
            'failure_stage': 'execute',
            'error_type': 'TimeoutError',
            'sent_actions': 7,
            'confirmed_reward_counts': {'mail': 2, 'guild_salary': 1, PRIVATE_MARKER: 999},
            'full_checklist_summary': {
                'scope': 'full_daily',
                'goals': {'pets': {'status': 'incomplete', 'counts': {'feed': 4}}},
            },
            'response': PRIVATE_MARKER,
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary, 1)):
            record, path = self.run_result()
        self.assertEqual(record['sent_actions'], 7)
        self.assertEqual(record['confirmed_reward_counts'], {'mail': 2, 'guild_salary': 1})
        self.assertEqual(record['execution'], 'failed')
        self.assertEqual(record['error_type'], 'TimeoutError')
        self.assertEqual(record['full_checklist_summary']['goals']['pets']['counts'], {'feed': 4})
        self.assertNotIn(PRIVATE_MARKER, path.read_text())

    def test_failed_policy_keeps_prior_receipts_and_last_job_send_total(self):
        summary = {
            'execution': 'failed',
            'game_day': DAY,
            'sent_actions': 4,
            'failure_stage': 'execute',
            'error_type': 'ConnectionResetError',
            'policy_jobs': {
                'gold_purchase': [
                    {
                        'kind': 'daily_resource_purchase',
                        'resource': 'gold',
                        'status': 'confirmed',
                        'count': 10,
                        'sent_actions': 1,
                    },
                    {
                        'status': 'uncertain',
                        'sent_actions': 4,
                        'error_type': 'ConnectionResetError',
                    },
                ]
            },
            'run_scope': {'uncertain_targets': [['shop_purchase', DAY, 'precise-sku']]},
        }
        with patch.object(scheduled.subprocess, 'run', return_value=self.completed(summary, 1)):
            record, path = self.run_result('craft')
        self.assertEqual(record['sent_actions'], 4)
        self.assertEqual(record['policy_jobs']['gold_purchase']['sent_actions'], 4)
        self.assertEqual(record['policy_jobs']['gold_purchase']['confirmed_purchase_times'], 10)
        self.assertEqual(record['execution'], 'failed')
        self.assertNotIn('run_scope', path.read_text())
        self.assertNotIn('precise-sku', path.read_text())

    def test_timeout_keeps_bounded_safe_partial_success_and_scope_only_in_ram(self):
        scope = {'uncertain_targets': []}
        summary = {
            'execution': 'partial',
            'game_day': DAY,
            'sent_actions': 2,
            'confirmed_reward_counts': {'mail': 1},
            'run_scope': {'uncertain_targets': [['free_reward', DAY, 'mail-unknown']]},
            'token': PRIVATE_MARKER,
        }
        failure = subprocess.TimeoutExpired([], 5, output=self.completed(summary).stdout)
        with patch.object(scheduled.subprocess, 'run', side_effect=failure):
            record, path = scheduled.run_scheduled(
                'auto', DAY, project=self.project, private=self.private, run_scope=scope
            )
        self.assertEqual(record['execution'], 'timeout')
        self.assertEqual(record['confirmed_reward_counts'], {'mail': 1})
        self.assertEqual(scope, summary['run_scope'])
        self.assertNotIn(PRIVATE_MARKER, path.read_text())
        self.assertNotIn('mail-unknown', path.read_text())
        self.assertNotIn('run_scope', record)

    def test_storage_failure_keeps_actual_actions_in_memory_without_replaying(self):
        summary = {
            'execution': 'partial',
            'game_day': DAY,
            'sent_actions': 2,
            'confirmed_reward_counts': {'mail': 1},
        }
        with patch.object(
            scheduled.subprocess, 'run', return_value=self.completed(summary)
        ) as run, patch.object(
            storage, 'open_private_directory', side_effect=PermissionError(PRIVATE_MARKER)
        ):
            record, path = self.run_result()
        run.assert_called_once()
        self.assertIsNone(path)
        self.assertEqual(record['confirmed_reward_counts'], {'mail': 1})
        self.assertEqual(record['sent_actions'], 2)
        self.assertEqual(record['result_storage'], 'failed')
        self.assertEqual(scheduled.result_exit_code(record), 1)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(record))

    def test_invalid_or_oversized_invocation_scope_never_reaches_a_child(self):
        for scope in (
            {'token': PRIVATE_MARKER},
            {'uncertain_targets': [[True]]},
            {'uncertain_targets': [['x' * 4096] * 8] * 8},
        ):
            with self.subTest(scope_type=tuple(scope)), patch.object(
                scheduled.subprocess, 'run'
            ) as run:
                record = scheduled.run_child(self.project, 'auto', DAY, None, run_scope=scope)
            run.assert_not_called()
            self.assertEqual(record['failure_stage'], 'invocation_scope')
            self.assertNotIn(PRIVATE_MARKER, json.dumps(record))
