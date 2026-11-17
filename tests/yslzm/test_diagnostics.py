"""The yslzm adapter retains fixed result facts, never native response bodies."""
import json
import unittest

from autocontrol.projects.yslzm.diagnostics import extract_diagnostics, sanitize_issues


PRIVATE_MARKER = 'PRIVATE_ACCOUNT_ROLE_TOKEN_RAWRESPONSE_MARKER'


class ActivityDiagnosticsTests(unittest.TestCase):
    def test_native_home_read_failure_is_not_reported_as_a_trade_failure(self):
        for extra in ({}, {'error_type': 'TimeoutError'}):
            rows = extract_diagnostics(
                {
                    'policy_jobs': {
                        'bear': [
                            {
                                'kind': 'bear_home_brief',
                                'status': 'read_failed',
                                'reason': 'friend_home_brief_missing',
                                'action_sent_actions': 0,
                                **extra,
                            }
                        ]
                    }
                }
            )['action_issues']
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                (
                    rows[0]['status'],
                    rows[0]['failure_stage'],
                    rows[0]['sent_actions'],
                    rows[0]['sent_state'],
                ),
                ('read_failed', 'state_read', 0, 'not_sent'),
            )

    def test_bear_partial_receipts_keep_public_targets_and_exact_send_state(self):
        source = {
            'policy_jobs': {
                'bear': [
                    {
                        'kind': 'bear_buy',
                        'status': 'uncertain',
                        'operation': 264,
                        'reputation_id': 117,
                        'reason': 'trade_response_incomplete',
                        'missing_proof': ['cake', 'quota', PRIVATE_MARKER],
                        'role_id': PRIVATE_MARKER,
                        'home_owner': PRIVATE_MARKER,
                        'action_sent_actions': 1,
                    },
                    {'kind': 'bear_sell', 'status': 'confirmed', 'score_delta': 240, 'jars': 10},
                    {
                        'kind': 'bear_trade_followup',
                        'status': 'failed',
                        'operation': 27,
                        'reason': 'trade_followup_unavailable',
                        'error_type': 'ConnectionResetError',
                        'action_sent_actions': 0,
                    },
                    {
                        'kind': 'bear_store',
                        'status': 'unsupported',
                        'sku': 6350,
                        'item': 36224,
                        'reason': 'bear_store_task_grant_unsupported',
                        'raw': PRIVATE_MARKER,
                    },
                    {
                        'kind': 'bear_store',
                        'status': 'uncertain',
                        'sku': 6357,
                        'item': 36221,
                        'failure_stage': 'bear_store_ownership',
                        'reason': 'bear_store_ownership_change_missing',
                    },
                    {'kind': 'bear', 'status': 'daily_trade_complete'},
                ]
            }
        }
        rows = extract_diagnostics(source)['action_issues']
        self.assertEqual(len(rows), 4)
        self.assertEqual((rows[0]['operation'], rows[0]['reputation_id']), (264, 117))
        self.assertEqual(rows[0]['missing_proof'], ('cake', 'quota'))
        self.assertEqual((rows[0]['sent_state'], rows[0]['sent_actions']), ('sent', 1))
        self.assertEqual((rows[1]['kind'], rows[1]['sent_actions']), ('bear_trade_followup', 0))
        self.assertEqual(rows[2]['reason'], 'bear_store_task_grant_unsupported')
        self.assertEqual(rows[3]['failure_stage'], 'bear_store_ownership')
        self.assertNotIn(PRIVATE_MARKER, json.dumps(rows))

    def test_welcome_and_daily_auto_observation_do_not_hide_real_grant_errors(self):
        source = {
            'policy_jobs': {
                'bear': [
                    {'kind': 'bear_welcome', 'status': 'activated', 'coupons': 0},
                    {
                        'kind': 'bear_welcome',
                        'status': 'uncertain',
                        'failure_stage': 'award',
                        'reason': 'bear_welcome_credit_missing',
                        'action_sent_actions': 0,
                    },
                    {
                        'kind': 'bear_daily_coupons',
                        'status': 'server_automatic',
                        'eligible_coupons': 270,
                        'action_sent_actions': 0,
                    },
                    {'kind': 'bear', 'status': 'daily_sale_limit_complete'},
                ]
            }
        }
        rows = extract_diagnostics(source)['action_issues']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'bear_welcome')
        self.assertEqual(rows[0]['reason'], 'bear_welcome_credit_missing')
        self.assertEqual((rows[0]['sent_state'], rows[0]['sent_actions']), ('not_sent', 0))

    def test_astrology_attempt_read_and_sent_errors_survive_terminal_exhaustion(self):
        source = {
            'policy_jobs': {
                'astrology': [
                    {
                        'kind': 'astrology_attempt_reward',
                        'status': 'unknown',
                        'reason': 'astrology_attempt_state_unavailable',
                        'failure_stage': 'state_read',
                    },
                    {
                        'kind': 'astrology_attempt_reward',
                        'status': 'uncertain',
                        'reason': 'astrology_attempt_result_unavailable',
                        'error_type': 'TimeoutError',
                        'action_sent_actions': 1,
                    },
                    {'kind': 'astrology_attempt_reward', 'status': 'confirmed', 'gain': 1},
                    {'status': 'attempts_exhausted'},
                ]
            }
        }
        rows = extract_diagnostics(source)['action_issues']
        self.assertEqual([row['status'] for row in rows], ['read_failed', 'uncertain'])
        self.assertTrue(all(row['kind'] == 'astrology_attempt_reward' for row in rows))
        self.assertEqual((rows[0]['sent_actions'], rows[1]['sent_actions']), (0, 1))
        self.assertEqual(rows[1]['reason'], 'astrology_attempt_result_unavailable')

    def test_new_native_operation_proofs_and_public_targets_are_bounded(self):
        source = {
            'feature': 'bear',
            'kind': 'bear_buy',
            'status': 'uncertain',
            'operation': 264,
            'npc_id': 12455,
            'reputation_id': 117,
            'missing_proof': ['coupons', 'ownership', 'cake', PRIVATE_MARKER],
            'reason': 'trade_response_incomplete',
            'owner': PRIVATE_MARKER,
        }
        row = sanitize_issues([source])[0]
        self.assertEqual((row['operation'], row['npc_id'], row['reputation_id']), (264, 12455, 117))
        self.assertEqual(row['missing_proof'], ('cake', 'coupons', 'ownership'))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(row))
        for value in (True, -1, 265, 2**31, PRIVATE_MARKER):
            with self.subTest(value=value):
                bad = dict(source, operation=value, npc_id=value, reputation_id=value)
                bounded = sanitize_issues([bad])[0]
                self.assertNotIn('operation', bounded)
                if value not in (265,):
                    self.assertNotIn('npc_id', bounded)
                    self.assertNotIn('reputation_id', bounded)

    def test_bear_native_ineligible_conditions_are_not_reported_as_errors(self):
        for state in (
            'daily_trade_complete',
            'daily_sale_limit_complete',
            'outside_trade_window',
            'insufficient_resources',
        ):
            with self.subTest(state=state):
                source = {'policy_jobs': {'bear': [{'kind': 'bear', 'status': state}]}}
                self.assertNotIn('action_issues', extract_diagnostics(source))

    def test_actual_trade_proof_parts_survive_both_diagnostic_boundaries(self):
        source = {
            'policy_jobs': {
                'bear': [
                    {
                        'kind': 'bear_buy',
                        'status': 'uncertain',
                        'operation': 264,
                        'reason': 'trade_response_incomplete',
                        'missing_proof': [
                            'reputation_debit',
                            'reputation_credit',
                            'quota_advance',
                            PRIVATE_MARKER,
                        ],
                    },
                ]
            }
        }
        issues = extract_diagnostics(source)['action_issues']
        self.assertEqual(
            issues[0]['missing_proof'], ('quota_advance', 'reputation_credit', 'reputation_debit')
        )
        self.assertEqual(sanitize_issues(issues), issues)
        self.assertNotIn(PRIVATE_MARKER, json.dumps(issues))

    def test_all_store_failure_stages_survive_the_adapter_allowlist(self):
        pairs = (
            ('dispatch', 'dispatch_failed'),
            ('receipt', 'receipt_missing'),
            ('resources', 'resource_delta_mismatch'),
            ('quota', 'quota_change_missing'),
            ('ownership', 'ownership_change_missing'),
        )
        rows = [
            {
                'feature': 'bear',
                'kind': 'bear_store',
                'status': 'uncertain',
                'failure_stage': 'bear_store_' + stage,
                'reason': 'bear_store_' + reason,
            }
            for stage, reason in pairs
        ]
        bounded = sanitize_issues(rows)
        self.assertEqual(len(bounded), 5)
        self.assertEqual(
            [row['failure_stage'] for row in bounded], ['bear_store_' + stage for stage, _ in pairs]
        )
        self.assertEqual(
            [row['reason'] for row in bounded], ['bear_store_' + reason for _, reason in pairs]
        )
