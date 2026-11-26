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

    def test_craft_debit_diagnostics_keep_only_bounded_numbers(self):
        source = {
            'policy_jobs': {
                'heart_crafting': [
                    {
                        'kind': 'fashion_extension',
                        'status': 'uncertain',
                        'operation': 8,
                        'target_id': 35224,
                        'index': 13,
                        'reason': 'craft_material_debit_missing',
                        'missing_proof': ['material_debit'],
                        'error_type': 'TimeoutError',
                        'failure_stage': 'craft',
                        'action_sent_actions': 1,
                        'material_deltas': [
                            [1, 853, -13, 0],
                            [1, 6999, -30, None],
                            [2, 5, -1, 0],
                            [1, 8, -1, 10**9],
                            [1, 9, -1, 0],
                        ],
                        'observed_commands': [
                            [577, 1],
                            [733, 1],
                            [0, 3],
                            [70000, 1],
                            [610, 0],
                            [PRIVATE_MARKER, 1],
                            [41, True],
                        ],
                    }
                ]
            }
        }
        issues = extract_diagnostics(source)['action_issues']
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['material_deltas'], ((1, 853, -13, 0), (1, 6999, -30, None)))
        self.assertEqual(issues[0]['observed_commands'], ((577, 1), (733, 1)))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(issues))
        # A saved result read back as JSON keeps the same bounded tuples.
        self.assertEqual(sanitize_issues(json.loads(json.dumps(issues))), issues)
        # A one-copy wardrobe material is expected to change by 0.
        row = dict(issues[0], material_deltas=[[1, 10, 0, -1]])
        self.assertEqual(sanitize_issues([row])[0]['material_deltas'], ((1, 10, 0, -1),))

    def test_shop_and_first_clear_proof_numbers_survive_the_adapter_allowlist(self):
        # 2026-11-22 06:00: the daily welfare box (delta mismatch) and a first
        # clear (12 s step deadline) failed and these numbers were dropped.
        shop = {
            'kind': 'shop_purchase',
            'name': '每日福利礼盒',
            'sku': 613,
            'item': 6249,
            'limit': 15992,
            'status': 'uncertain',
            'failure_stage': 'shop_resources',
            'reason': 'shop_resource_delta_mismatch',
            'error_type': 'TimeoutError',
            'action_sent_actions': 1,
            'diagnostics': {
                'resources': [
                    {
                        'kind': 'dye',
                        'counter': 1576,
                        'before': 9999,
                        'after': 9999,
                        'delta': 0,
                        'expected_delta': 5,
                    },
                    {
                        'kind': 'gold',
                        'counter': 0,
                        'before': 120000,
                        'after': 120882,
                        'delta': 882,
                        'expected_delta': 882,
                    },
                    {
                        'kind': PRIVATE_MARKER,
                        'counter': 1,
                        'before': 0,
                        'after': 0,
                        'delta': 0,
                        'expected_delta': 1,
                    },
                ],
                'quota_before': 0,
                'quota_after': 1,
                'quota_expected': 1,
                'shopping_result': {'code': 0, 'cart_results': [{'cart_index': 0, 'code': 0}]},
                'commands': [
                    {'command': 338, 'count': 1},
                    {'command': 32, 'count': 1},
                    {'command': PRIVATE_MARKER, 'count': 1},
                ],
                'errors': [],
                'token': PRIVATE_MARKER,
            },
        }
        first = {
            'kind': 'stage_first_clear',
            'status': 'uncertain',
            'error_type': 'TimeoutError',
            'diagnostics': {
                'stage_id': 10203,
                'sent_commands': [703, 747, 748],
                'entry_error_codes': [0],
                'legacy_start_error_codes': [],
                'combat_start_observations': [{'combat_type': 1, 'error_code': 0}],
                'all_received_command_counts': {'703': 1, '747': 1, '561': 2, PRIVATE_MARKER: 4},
                'native_errors': [{'kind': 1, 'show': 1, 'code': 4012}],
                'story_skip_gates': {'token': PRIVATE_MARKER},
            },
        }
        issues = extract_diagnostics(
            {'shop_purchase_actions': [shop], 'stage_first_clear_actions': [first]}
        )['action_issues']
        saved = {row['feature']: row for row in issues}
        purchase = saved['shop_purchases']
        self.assertEqual(
            purchase['resource_deltas'], (('dye', 1576, 5, 0, 9999), ('gold', 0, 882, 882, 120000))
        )
        self.assertEqual(purchase['quota_counts'], (0, 1, 1))
        self.assertEqual(purchase['receipt_codes'], (0, 0))
        self.assertEqual(purchase['observed_commands'], ((32, 1), (338, 1)))
        clear = saved['stage_first_clears']
        self.assertEqual((clear['stage_id'], clear['sent_commands']), (10203, (703, 747, 748)))
        self.assertEqual(clear['response_codes'], ((703, 0), (747, 0)))
        self.assertEqual(clear['observed_commands'], ((561, 2), (703, 1), (747, 1)))
        self.assertEqual(clear['native_errors'], ((1, 1, 4012),))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(issues))
        # The worker's saved rows pass the adapter unchanged, also after JSON.
        self.assertEqual(sanitize_issues(json.loads(json.dumps(issues))), issues)
        # Other kinds never borrow these fields from a free-form dict.
        other = extract_diagnostics(
            {'mail_actions': [{'status': 'uncertain', 'diagnostics': shop['diagnostics']}]}
        )
        self.assertNotIn('resource_deltas', json.dumps(other))
