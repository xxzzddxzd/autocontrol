"""Fixed-field summaries of yslzm worker results.

Ported from ``yslzm/tools/run_scheduled.py``. The worker prints one JSON summary
as its last stdout line; only allowlisted fields, states and bounded counts are
kept. Identities, server replies and exception text never cross this boundary.
"""

from datetime import date, datetime, timedelta, timezone
import json

from .diagnostics import CONDITIONS, REASONS as DIAGNOSTIC_REASONS, extract_diagnostics

BEIJING = timezone(timedelta(hours=8))
MAX_SUMMARY_BYTES = 2 * 1024 * 1024
MAX_SCOPE_BYTES = 128 * 1024
MAIN_FAILURE_STAGES = frozenset(
    {
        'runtime_lock',
        'login_material',
        'connect',
        'server_clock',
        'execute',
        'summary',
    }
)
ERROR_TYPES = frozenset(
    {
        'OSError',
        'FileNotFoundError',
        'NotADirectoryError',
        'PermissionError',
        'TimeoutError',
        'ConnectionError',
        'ConnectionRefusedError',
        'ConnectionResetError',
        'BrokenPipeError',
        'ValueError',
        'TypeError',
        'RuntimeError',
        'KeyError',
        'ImportError',
        'ModuleNotFoundError',
        'InvalidJournal',
        'JournalError',
        'AlreadyRecorded',
        'HandshakeTransportError',
    }
)
AUTO_VERIFICATION = (
    'daily_full_rewards_verified',
    'daily_activity_rewards_confirmed',
    'all_actions_settled',
)
AUTO_FLAGS = AUTO_VERIFICATION + (
    'required_daily_tasks_complete',
    'all_daily_tasks_claimed',
    'red_dot_pass_complete',
    'phase_and_mail_rewards_cleared',
    'independent_recovery_failed',
)
AUTO_COUNTS = (
    'independent_session_recoveries',
    'daily_activity',
    'weekly_activity',
    'claimed_daily_chests',
    'claimed_weekly_chests',
    'unobserved_tasks',
    'unfinished_observed_tasks',
    'claimable_daily_task_rewards',
    'phase_reward_indicators',
    'mail_attachment_indicators',
    'mail_unread_reward_indicators',
)
AUTO_STATES = frozenset(
    {
        'awaiting_refresh',
        'uncertain',
        'pending_reconciliation',
        'deferred_unresolved',
        'mail_read_unconfirmed',
        'required_tasks_incomplete',
        'partial',
        'activity_complete',
        'all_observed_tasks_complete',
        'complete',
    }
)
CARDBOOK_STATES = frozenset(
    {
        'awaiting_refresh',
        'awaiting_completion',
        'awaiting_task_refresh',
        'partial',
        'daily_limit_complete',
        'ordinary_limit_complete',
        'no_dispatchable_tasks',
        'insufficient_currency',
        'currency_unknown',
        'uncertain',
        'pending_reconciliation',
        'unknown',
        'deferred_unresolved',
        'rejected',
        'deferred_reconciliation',
        'action_limit_reached',
        'session_refresh_required',
        'iteration_limit',
        'refresh_progress_mismatch',
        'progress_changed',
        'entitlement_unavailable',
        'entitlement_unknown',
        'unavailable',
    }
)
CARDBOOK_COUNTS = (
    'ordinary_sent',
    'ordinary_limit',
    'confirmed_total',
    'running_tasks',
    'claimable_rewards',
    'special_sent_today',
    'target',
)
REWARD_COUNTS = (
    'achievement',
    'mail',
    'mail_read',
    'free_reward',
    'friend_energy',
    'home_eat_income',
    'cardbook_daily',
    'monthly_sign',
    'arena_daily',
    'arena_climb',
    'guild_daily',
    'guild_project_reward',
    'activity_threshold',
    'ring_toss',
    'open_shop',
    'endorsement',
    'endorsement_maintenance',
    'free_lottery',
    'lottery_sign',
    'lottery_coin',
    'vip_daily_gift',
    'vip_weekly_gift',
    'weekly_activity',
    'chapter_reward',
    'pass_reward',
    'vvanna_reward',
    'month_card',
    'big_month_card',
    'fashion_atlas',
    'craft_suit',
    'atlas_daily',
    'social_topic_coin',
    'discount_package_reward',
    'share_reward',
    'vvanna_quiz',
    'vigor_exchange',
    'member_bonus',
    'blind_box_draw',
    'blind_box',
    'lottery_red_envelope',
    'shop_purchase',
    'palette_unlock',
    'ui_coin',
    'stage_first_clear',
    'pet_care',
    'guild_salary',
    'daily_task_reward',
    'daily_activity_chest',
    'daily_publicity',
    'daily_gameplay',
)
DAILY_REWARD_RESUME_STATES = AUTO_STATES | {'unknown'}
DAILY_REWARD_RESUME_COUNTS = (
    'daily_activity',
    'claimed_daily_chests',
    'task_rewards_confirmed',
    'chests_confirmed',
)
CARDBOOK_ACTION_KINDS = ('cardbook_reward', 'cardbook_refresh', 'cardbook_dispatch')
CARDBOOK_FAILURES = frozenset(
    {
        'uncertain',
        'pending_reconciliation',
        'unknown',
        'deferred_unresolved',
        'rejected',
        'deferred_reconciliation',
        'action_limit_reached',
        'session_refresh_required',
    }
)
ACTIVITY_SETTLED = frozenset(
    {
        'confirmed',
        'activated',
        'current_state_reconciled',
        'available_activity_rewards_complete',
        'attempts_exhausted',
        'target_reached',
        'daily_sale_complete',
        'selling',
        'highest_reward_score_reached',
        'no_remaining_sale_window',
        'closed',
    }
)
ACTIVITY_FAILURES = CARDBOOK_FAILURES | {
    'awaiting_ready',
    'owned_decoration_selection_unavailable',
    'unavailable',
    'not_ready',
    'query_unavailable',
    'awaiting_map_transition',
    'throw_limit_reached',
    'activity_switch_evidence_unknown',
    'unsupported_activity_identity',
    'old_activity_window_unknown',
    'old_sale_unsettled',
    'old_activity_not_verified_expired',
    'progress_mismatch',
    'unknown_sale_window',
    'insufficient_activity_money',
    'task_limit_reached',
    'unsupported',
    'awaiting_activity_initialization',
    'needs_claim',
    'available',
    'ready_to_open',
    'settlement_available',
    'activity_switch_required',
    'pending',
    'deferred',
    'not_recorded',
}
ACTIVITY_FEATURES = frozenset({'open_shop', 'ring_toss', 'activity_rewards'})

FEATURE_KEYS = (
    'daily_execution',
    'daily_reward_collection',
    'friend_energy',
    'home_eat_income',
    'atlas_daily',
    'arena_daily',
    'arena_climb',
    'guild_daily',
    'guild_daily_state',
    'guild_project_rewards',
    'guild_project_reward_state',
    'cardbook_daily',
    'vigor_exchange',
    'free_lottery',
    'lottery_sign',
    'lottery_sign_coins',
    'lottery_red_envelopes',
    'blind_box_workshop',
    'blind_box_draws',
    'activity_rewards',
    'ring_toss',
    'open_shop',
    'vvanna_quiz',
    'discount_package_rewards',
    'discount_package_reward_state',
    'monthly_sign',
    'social_topic_coins',
    'member_bonus',
    'pet_care',
    'ui_coin_rewards',
    'endorsement',
    'endorsement_maintenance',
    'palette_unlock',
    'guild_salary',
    'shop_purchases',
    'stage_first_clears',
    'stage_first_clear_state',
    'vvanna_rewards',
    'vvanna_reward_state',
    'daily_reward_resume',
)
FEATURE_PROBLEM_STATES = CARDBOOK_FAILURES | frozenset(
    {
        'partial',
        'unsupported',
        'unavailable',
        'unconfirmed',
        'forbidden',
        'awaiting_ready',
        'awaiting_map_transition',
        'no_dispatchable_tasks',
        'entitlement_unknown',
        'entitlement_unavailable',
        'insufficient_currency',
        'insufficient_gold',
        'insufficient_items',
        'insufficient_materials',
        'insufficient_refresh_currency',
        'insufficient_stamina',
        'insufficient_vigor',
        'insufficient_activity_money',
        'insufficient_recommended_score',
        'currency_unknown',
        'resource_state_unknown',
        'query_unknown',
        'query_rejected',
        'query_unavailable',
        'query_unconfirmed',
        'progress_mismatch',
        'progress_changed',
        'refresh_progress_mismatch',
        'refresh_unavailable',
        'refresh_counter_unknown',
        'refresh_currency_unknown',
        'session_observations_unavailable',
        'material_mapping_unknown',
        'material_replenishment_unavailable',
        'material_state_changed',
        'wardrobe_changed',
        'wardrobe_unknown',
        'quota_unknown',
        'quota_unsupported',
        'unsupported_envelope',
        'unsupported_activity_identity',
        'unsupported_free_semantics',
        'owned_decoration_selection_unavailable',
        'answer_conflict',
        'answer_unavailable',
        'entry_changed',
        'entry_unconfirmed',
        'needs_food_supply',
        'food_unknown',
        'prior_batch_unresolved',
        'claim_limit_reached',
        'run_limit_reached',
        'throw_limit_reached',
        'iteration_limit',
        'batch_limit_reached',
        'closed_unconfirmed',
        'configuration_binding_unconfirmed',
        'state_unknown',
        'restriction_unknown',
        'restriction_unsupported',
        'availability_unknown',
        'balance_unknown',
        'cash_unknown',
        'grant_state_unknown',
        'level_unknown',
        'local_fields_unknown',
        'old_activity_window_unknown',
        'unknown_sale_window',
        'switches_unknown',
        'ui_mapping_unknown',
        'replenish_mapping_unknown',
    }
)

CHECKLIST_GOAL_COUNTS = {
    'arena': ('obtained', 'limit', 'remaining', 'scheme_slots', 'displayed_rank', 'target_rank'),
    'cardbook': (
        'ordinary_sent',
        'ordinary_limit',
        'special_sent',
        'total',
        'target',
        'running',
        'claimable',
        'next_completion',
    ),
    'guild': (
        'support_used',
        'support_target',
        'inspiration_used',
        'inspiration_target',
        'project_weekly_used',
        'project_weekly_limit',
        'salary_weekly_used',
        'salary_weekly_limit',
        'salary_claimable',
        'salary_unknown',
        'salary_gold',
        'salary_gifts',
    ),
    'daily_activity': (
        'activity',
        'target',
        'chests_claimed',
        'chests_target',
        'mandatory_publicity_complete',
    ),
    'requested_shop': ('products_checked', 'available', 'unavailable', 'unknown'),
    'red_dots': ('claimable', 'unknown', 'unsupported', 'sources_checked', 'sources_required'),
    'member_rewards': ('seven_day_available', 'daily_claimable', 'vip_unknown', 'vip_claimable'),
    'vigor_exchange': ('used', 'target', 'remaining'),
    'pets': (
        'total',
        'eligible',
        'blocked',
        'missing_properties',
        'feed',
        'bath',
        'play',
        'toilet',
    ),
    'endorsement': (
        'rounds_used',
        'rounds_done',
        'round_limit',
        'salary_ready',
        'contracts_ready',
        'barriers_ready',
        'income_upgrades_available',
        'actions_confirmed',
    ),
    'first_clears': ('normal', 'hard', 'vvanna', 'heart', 'missing'),
    'free_lottery': ('due_pools', 'observed_pools', 'unsupported'),
    'wardrobe_palettes': (
        'owned',
        'remaining',
        'affordable',
        'forbidden',
        'qianhuan_unlocked',
        'qianhuan_remaining',
        'qianhuan_needs_ordinary',
        'qianhuan_forbidden',
        'qianhuan_affordable',
    ),
    'activities': (
        'open',
        'unknown',
        'shop_remaining',
        'shop_score',
        'shop_next_settlement',
        'ring_remaining',
        'ring_score',
        'ring_target',
        'rewards_claimable',
        'recipes_remaining',
        'unsupported',
    ),
}
CHECKLIST_STATUSES = frozenset(
    {'complete', 'waiting', 'blocked', 'incomplete', 'unknown', 'not_started'}
)
CHECKLIST_AVAILABILITY = frozenset({'open', 'closed', 'unknown'})


def checklist_summary(value):
    if type(value) is not dict or value.get('scope') != 'full_daily':
        return None
    result = {'scope': 'full_daily'}
    for key in ('attempted', 'complete'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    source_goals = value.get('goals')
    if type(source_goals) is not dict:
        result.update(attempted=False, complete=False, goals={})
        return result
    goals = {}
    for goal, keys in CHECKLIST_GOAL_COUNTS.items():
        source = source_goals.get(goal)
        if type(source) is not dict:
            continue
        row = {}
        status = allowed(source.get('status'), CHECKLIST_STATUSES)
        if status is not None:
            row['status'] = status
        availability = allowed(source.get('availability'), CHECKLIST_AVAILABILITY)
        if availability is not None:
            row['availability'] = availability
        if type(source.get('attempted')) is bool:
            row['attempted'] = source['attempted']
        counts = source.get('counts')
        if type(counts) is dict:
            row['counts'] = {
                key: counts[key]
                for key in keys
                if type(counts.get(key)) is int and 0 <= counts[key] <= 2**63 - 1
            }
        goals[goal] = row
    result['goals'] = goals
    all_attempted = len(goals) == len(CHECKLIST_GOAL_COUNTS) and all(
        row.get('attempted') is True for row in goals.values()
    )
    result['attempted'] = result.get('attempted') is True and all_attempted
    result['complete'] = (
        result.get('complete') is True
        and result['attempted']
        and all(row.get('status') == 'complete' for row in goals.values())
    )
    return result


def feature_problems(summary):
    problems = {}
    for feature in FEATURE_KEYS:
        value = summary.get(feature)
        if type(value) is list:
            # Later observations supersede earlier rows from this feature.
            value = next(
                (
                    row
                    for row in reversed(value)
                    if type(row) is dict and type(row.get('status')) is str
                ),
                None,
            )
        if type(value) is not dict:
            continue
        # A known current eligibility condition is not an action failure.
        # Keep explicit exceptions/refusals even if a condition also appears.
        if (
            value.get('reason') in CONDITIONS
            and not value.get('error_type')
            and value.get('code', value.get('error_code')) is None
        ):
            continue
        status = allowed(value.get('status'), FEATURE_PROBLEM_STATES)
        if status is not None:
            problems[feature] = status
    return problems


FEATURE_REASONS = frozenset(
    (
        'topic_discovery_unavailable',
        'session_boundary_unavailable',
        'daily_execution_exception',
        'daily_reward_collection_exception',
        'community_function_unavailable',
        'social_token_unavailable',
        'topic_response_invalid',
        'topic_service_rejected',
        'topic_query_unconfirmed',
        'topic_list_not_observed',
        'topic_list_context_changed',
        'topic_list_expired',
        'topic_list_incomplete',
        'topic_cursor_repeated',
        'topic_page_limit',
        'topic_count_limit',
        'topic_catalog_unavailable',
        'task_snapshot_unavailable',
        'task_eligibility_unavailable',
        'task_template_unsupported',
        'all_mapped_topics_completed',
        'reachable_topics_completed',
        'reachable_topics_claimable',
    )
)


def server_observation_details(summary):
    """Retain fixed completion evidence and diagnostic categories, never replies."""
    result = {}
    states = FEATURE_PROBLEM_STATES | frozenset(
        (
            'claimable',
            'cleared',
            'closed',
            'complete',
            'needs_claim',
            'no_current_topic',
            'no_coin_reward',
            'topic_list_incomplete',
        )
    )
    for key in (
        'atlas_daily',
        'social_topic_coins',
        'member_rewards',
        'red_dot_sources',
        'daily_execution',
        'daily_reward_collection',
    ):
        source = summary.get('member_reward_status' if key == 'member_rewards' else key)
        if key in ('daily_execution', 'daily_reward_collection') and type(source) is list:
            source = next((row for row in reversed(source) if type(row) is dict), None)
        if type(source) is not dict:
            continue
        if key == 'red_dot_sources':
            value = {'status': 'partial_scope_observed', 'sources': {}}
            for name in (
                'ui_coin_buttons',
                'lottery_sign_ins',
                'blind_box_workshop',
                'blind_box_draws',
                'lottery_red_envelopes',
                'vvanna_rewards',
                'vvanna_quiz',
                'discount_packages',
                'monthly_sign',
                'social_topic_coins',
                'lottery_sign_coins',
            ):
                row = source.get(name)
                if type(row) is not dict:
                    continue
                item = {
                    'status': allowed(
                        row.get('status'),
                        states
                        | {
                            'outside_draw_window',
                            'weekly_quiz_complete',
                            'available_activity_rewards_complete',
                            'at_daily_limit',
                        },
                        'unknown',
                    )
                }
                add_counts(item, row, ('claimable', 'claimable_count', 'unknown', 'unsupported'))
                for field in ('reason', 'failure_stage'):
                    options = (
                        FEATURE_REASONS
                        if field == 'reason'
                        else frozenset(
                            (
                                'topic_catalog',
                                'topic_discovery',
                                'task_snapshot',
                                'task_eligibility',
                            )
                        )
                    )
                    reason = allowed(row.get(field), options)
                    if reason is not None:
                        item[field] = reason
                value['sources'][name] = item
            for name in ('all_home_badges', 'local_unread_markers'):
                state = allowed(source.get(name), frozenset(('cleared', 'unknown', 'unverified')))
                if state is not None:
                    value[name] = state
            result[key] = value
            continue
        value = {'status': allowed(source.get('status'), states, 'unknown')}
        if key == 'social_topic_coins' and type(source.get('missing_template_targets')) is list:
            targets = []
            for row in source['missing_template_targets'][:16]:
                if type(row) is dict and all(
                    type(row.get(field)) is int and 0 < row[field] <= 2**31 - 1
                    for field in ('topic_id', 'task_id')
                ):
                    target = {field: row[field] for field in ('topic_id', 'task_id')}
                    if target not in targets:
                        targets.append(target)
            if targets:
                value['missing_template_targets'] = targets
        add_counts(
            value,
            source,
            (
                'finished_today',
                'claimable',
                'cleared',
                'unknown',
                'unsupported',
                'ui_task_count',
                'catalog_topics',
                'known_completed_topics',
                'mapped_topics_known_completed',
                'template_missing_completed',
                'reachable_topics',
                'mapped_topics',
                'unmapped_topics',
                'no_reward_topics',
            ),
        )
        for field, options in (
            (
                'completion_evidence_status',
                frozenset(('complete', 'partial', 'none_known', 'unavailable')),
            ),
            (
                'topic_list_status',
                frozenset(
                    (
                        'complete',
                        'incomplete',
                        'not_observed',
                        'expired',
                        'context_changed',
                        'query_failed',
                    )
                ),
            ),
            ('reason', FEATURE_REASONS),
            ('topic_list_reason', FEATURE_REASONS),
            (
                'failure_stage',
                frozenset(
                    ('topic_catalog', 'topic_discovery', 'task_snapshot', 'task_eligibility')
                ),
            ),
        ):
            item = allowed(source.get(field), options)
            if item is not None:
                value[field] = item
        if source.get('error_type') is not None:
            value['error_type'] = allowed(
                source['error_type'],
                ERROR_TYPES | {'AttributeError', 'HTTPError', 'URLError', 'CalledProcessError'},
                'OtherError',
            )
        if type(source.get('list_complete')) is bool:
            value['list_complete'] = source['list_complete']
        if type(source.get('claimable')) is bool:
            value['claimable'] = source['claimable']
        if key == 'member_rewards':
            member_states = frozenset(
                (
                    'complete',
                    'claimable',
                    'available',
                    'unavailable',
                    'closed',
                    'inactive',
                    'unknown',
                    'entitlement_unknown',
                    'last_service_day_ineligible',
                    'no_current_offer',
                )
            )
            for field in (
                'ordinary_30_daily',
                'big_68_daily',
                'seven_day_status',
                'vip_daily_status',
                'vip_weekly_status',
            ):
                item = allowed(source.get(field), member_states)
                if item is not None:
                    value[field] = item
            for field in ('vip_daily_reason', 'vip_weekly_reason'):
                reason = allowed(
                    source.get(field),
                    frozenset(
                        (
                            'vip_session_or_function_state_missing',
                            'vip_reward_observer_unavailable',
                            'vip_function_state_missing',
                            'vip_entitlement_state_missing',
                            'vip_weekly_offer_absent',
                            'auto_reward_list_missing',
                            'vip_reward_state_missing',
                        )
                    ),
                )
                if reason is not None:
                    value[field] = reason
        result[key] = value
    return result


def report_snapshot_summary(value):
    if type(value) is not dict or value.get('report_only') is not True:
        return None
    source = allowed(value.get('source'), frozenset(('login', 'before_close', 'manual')))
    day = iso_day(value.get('game_day'))
    stamp = value.get('observed_server_time')
    if source is None or day is None or type(stamp) is not int or not 0 < stamp < 2**32:
        return None
    if (datetime.fromtimestamp(stamp, BEIJING) - timedelta(hours=5)).date().isoformat() != day:
        return None
    result = {
        'source': source,
        'game_day': day,
        'observed_server_time': stamp,
        'report_only': True,
        'observed_at': datetime.fromtimestamp(stamp, BEIJING).isoformat(timespec='seconds'),
    }
    if value.get('capture_error_type') is not None:
        result['capture_error_type'] = allowed(
            value['capture_error_type'], ERROR_TYPES, 'OtherError'
        )
    return result


def iso_day(value):
    if type(value) is not str or len(value) != 10:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return value if day.isoformat() == value else None


def allowed(value, options, fallback=None):
    return value if type(value) is str and value in options else fallback


def latest_summary(output):
    if type(output) is str:
        output = output.encode('utf-8')
    if type(output) is not bytes or not output:
        return None
    # Ignore preceding output and only accept the final record.
    tail = output[-(MAX_SUMMARY_BYTES + 1) :].rstrip()
    line = tail.rsplit(b'\n', 1)[-1]
    if len(line) > MAX_SUMMARY_BYTES or (len(output) > MAX_SUMMARY_BYTES and line == tail):
        return None
    try:
        summary = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return summary if type(summary) is dict else None


def add_counts(result, source, keys):
    for key in keys:
        value = source.get(key)
        if type(value) is int and 0 <= value <= 100000:
            result[key] = value


def cardbook_row(summary):
    rows = summary.get('cardbook_actions')
    if type(rows) is list:
        for row in reversed(rows):
            if type(row) is not dict:
                continue
            kind = row.get('kind')
            if kind == 'cardbook_daily' or (
                allowed(kind, CARDBOOK_ACTION_KINDS) is not None
                and allowed(row.get('status'), CARDBOOK_FAILURES) is not None
            ):
                return row
    if summary.get('kind') == 'cardbook_daily':
        return summary
    return {}


def missing_summary(result):
    failed = result['exit_code'] != 0
    return {
        **result,
        'execution': 'failed' if failed else 'unverified',
        'failure_stage': 'process' if failed else 'summary',
        'error_type': 'MissingSummary',
    }


def cardbook_diagnostics(result, summary, exit_code):
    rows = summary.get('cardbook_actions')
    if type(rows) is list:
        result['confirmed_cardbook_counts'] = {
            kind: sum(
                type(row) is dict and row.get('kind') == kind and row.get('status') == 'confirmed'
                for row in rows
            )
            for kind in CARDBOOK_ACTION_KINDS
        }
    activity_rows = summary.get('activity_resume_actions')
    if type(activity_rows) is list:
        result['confirmed_activity_counts'] = {
            feature: sum(
                type(row) is dict
                and row.get('feature') == feature
                and row.get('status') == 'confirmed'
                for row in activity_rows
            )
            for feature in sorted(ACTIVITY_FEATURES)
        }
        states = {}
        for row in activity_rows:
            if type(row) is not dict:
                continue
            feature = allowed(row.get('feature'), ACTIVITY_FEATURES)
            if feature is None:
                continue
            state = {
                'status': allowed(
                    row.get('status'), ACTIVITY_SETTLED | ACTIVITY_FAILURES, 'unrecognized'
                )
            }
            for key in ('remaining', 'score', 'highest_reward_threshold'):
                value = row.get(key)
                if type(value) is int and 0 <= value <= 2**63 - 1:
                    state[key] = value
            states[feature] = state
        if states:
            result['activity_resume_state'] = states
    if exit_code == 0:
        return
    failures = {}
    activity_problems = {}
    for group in (
        'activity_resume',
        'timed_free_reward',
        'endorsement_maintenance',
        'friend_energy_resume',
    ):
        rows = summary.get(group + '_actions')
        if type(rows) is not list:
            continue
        for row in rows:
            if type(row) is not dict:
                continue
            status = row.get('status')
            if group == 'activity_resume' and allowed(status, ACTIVITY_SETTLED) is None:
                problem = allowed(status, ACTIVITY_FAILURES, 'unrecognized')
                failures[group] = problem
                feature = allowed(row.get('feature'), ACTIVITY_FEATURES)
                if feature is not None:
                    activity_problems[feature] = problem
            elif allowed(status, CARDBOOK_FAILURES) is not None:
                failures[group] = status
    if failures:
        result['cardbook_group_failures'] = failures
    if activity_problems:
        result['activity_resume_problems'] = activity_problems


PHASE_JOBS = {
    'craft': ('gold_purchase', 'stamina_purchase', 'heart_crafting', 'main_story_crafting'),
    'activities': (
        'activity_resources',
        'astrology',
        'bear',
        'produce_activity',
        'produce_common',
        'ring_toss',
        'activity_rewards',
        'penglai',
        'brand_atlas',
        'brand_tower',
        'activity_discovery',
    ),
    'wardrobe': ('palette_unlock', 'qianhuan'),
}
PHASE_SUCCESS = frozenset(
    (
        'complete',
        'cleared',
        'daily_limit_complete',
        'all_unlocked_production_complete',
        'available_brand_levels_complete',
        'closed',
        'inactive',
        'season_closed',
        'level_locked',
        'period_drawings_complete',
        'attempts_exhausted',
        'target_reached',
        'available_activity_rewards_complete',
        'already_claimed',
        'daily_trade_complete',
        'daily_sale_limit_complete',
        'outside_trade_window',
    )
)
PHASE_WAITING = frozenset(
    (
        'selling',
        'waiting',
        'waiting_for_completion',
        'daily_growth_complete',
        'waiting_for_growth',
        'waiting_for_stamina',
        'awaiting_daily_activity_resources',
    )
)
PHASE_STATES = (
    FEATURE_PROBLEM_STATES
    | PHASE_SUCCESS
    | PHASE_WAITING
    | frozenset(
        (
            'blocked',
            'no_material_route',
            'story_execution_unavailable',
            'all_unlocked_production_complete',
            'available_brand_levels_complete',
            'materials_ready',
            'no_sweep_route',
            'recommendation_unavailable',
            'vip_unknown',
            'expired_unconfirmed',
            'all_slots_complete',
            'watering_complete',
            'waiting_for_growth',
            'unrecognized',
            'action_limit',
            'not_started',
            'quota_cap',
            'material_shop_mapping_unknown',
            'no_native_shop_route',
            'insufficient_materials',
            'no_server_progress',
            'not_executed',
            'outside_draw_window',
            'needs_craft',
        )
    )
)
PHASE_COUNTS = (
    'used',
    'target',
    'remaining',
    'count',
    'cost',
    'gain',
    'planned_count',
    'planned_cost',
    'planned_gain',
    'remaining_attempts',
    'bought_today',
    'purchase_limit',
    'stamina_remaining',
    'crafted',
    'dye_stages',
    'claimable',
    'unknown',
    'unsupported',
    'career_level',
    'ready_levels',
    'saved_attribute_outfits',
    'attempts_confirmed',
    'watered_today',
    'harvested',
    'slots',
    'mature_slots',
    'growing_slots',
    'seeds_remaining',
    'occupied_slots',
    'used_seeds',
    'remaining_water',
    'score',
    'unfinished_targets',
    'new_story_count',
    'requested_extra_attempts',
    'confirmed_throws',
    'remaining_fashions',
    'confirmed_fashions',
    'skipped_fashions',
    'owned_fashions',
    'unlocked_fashions',
    'blocked_by_ordinary_palettes',
    'forbidden_fashions',
    'unlockable_with_owned_materials',
    'water_tasks_completed',
    'ring_tasks_completed',
    'water_tasks_claimed',
    'ring_tasks_claimed',
    'water_remaining',
    'ring_remaining',
    'grant_tasks_unknown',
    'grant_tasks_available',
    'grant_tasks_claimed',
    'unsupported_activity_count',
    'minimum_sweep_stamina',
    'current_outputs',
    'already_owned',
    'unowned_outputs',
    'craftable_outputs',
    'unknown_outputs',
)
PHASE_COUNTS += (
    'attempt_tasks_claimed',
    'attempt_tasks_claimable',
    'attempt_tasks_unknown',
    'attempt_tasks_below_threshold',
    'buy_used',
    'sell_used',
    'buy_target',
    'sell_target',
    'buy_limit',
    'sell_limit',
    'buy_remaining',
    'sell_remaining',
    'jars',
    'cakes',
)
PHASE_REASONS = frozenset(
    (
        'request_result_unavailable',
        'request_already_sent',
        'request_already_sent_this_run',
        'session_interrupted',
        'session_recovery_failed',
        'reward_collection_unavailable',
        'astrology_state_unavailable',
        'score_reward_task_state_unavailable',
        'score_reward_template_unsupported',
        'spin_response_unavailable',
        'plant_tree_operation_response_unavailable',
    )
)


def summarize_policy_phase(command, target_day, exit_code, summary):
    jobs = summary.get('policy_jobs')
    result = {'exit_code': exit_code, 'policy_jobs': {}}
    day = iso_day(summary.get('game_day'))
    if day:
        result['game_day'] = day
    if type(jobs) is not dict:
        return missing_summary(result)
    problems = {}
    for name in PHASE_JOBS[command]:
        rows = jobs.get(name)
        if type(rows) is not list:
            continue
        rows = [row for row in rows if type(row) is dict]
        last = rows[-1] if rows else {'status': 'cleared'}
        states = summary.get('policy_job_states')
        actual = (
            states.get(name, last.get('status')) if type(states) is dict else last.get('status')
        )
        status = allowed(actual, PHASE_STATES, 'unrecognized')
        value = {
            'status': status,
            'confirmed_actions': sum(row.get('status') == 'confirmed' for row in rows),
        }
        sent = next(
            (
                row['sent_actions']
                for row in reversed(rows)
                if type(row.get('sent_actions')) is int and 0 <= row['sent_actions'] <= 100000
            ),
            None,
        )
        if sent is not None:
            value['sent_actions'] = sent
        if name == 'astrology':
            spins = [
                row
                for row in rows
                if row.get('kind') == 'astrology_spin' and row.get('status') == 'confirmed'
            ]
            value['confirmed_spins'] = len(spins)
            for source, target in (
                ('points', 'confirmed_astrology_points'),
                ('gold', 'confirmed_astrology_gold'),
            ):
                value[target] = sum(
                    row[source]
                    for row in spins
                    if type(row.get(source)) is int and 0 < row[source] <= 100000
                )
            value['confirmed_score_rewards'] = sum(
                row.get('kind') == 'astrology_reward'
                and row.get('status') == 'confirmed'
                and row.get('reward_claimed') is True
                for row in rows
            )
            attempt_rewards = [
                row
                for row in rows
                if row.get('kind') == 'astrology_attempt_reward'
                and row.get('status') == 'confirmed'
            ]
            value['confirmed_attempt_rewards'] = len(attempt_rewards)
            value['confirmed_attempts_gained'] = sum(
                row.get('gain', 0)
                for row in attempt_rewards
                if type(row.get('gain')) is int and 0 < row['gain'] <= 100000
            )
        if name == 'bear':
            buys = [
                row
                for row in rows
                if row.get('kind') == 'bear_buy' and row.get('status') == 'confirmed'
            ]
            sales = [
                row
                for row in rows
                if row.get('kind') == 'bear_sell' and row.get('status') == 'confirmed'
            ]
            welcome = [
                row
                for row in rows
                if row.get('kind') == 'bear_welcome'
                and row.get('status') == 'confirmed'
                and row.get('reward_claimed') is True
            ]
            store = [
                row
                for row in rows
                if row.get('kind') == 'bear_store' and row.get('status') == 'confirmed'
            ]
            value['confirmed_bear_welcome_rewards'] = len(welcome)
            value['confirmed_bear_welcome_coupons'] = sum(
                row['coupons']
                for row in welcome
                if type(row.get('coupons')) is int and 0 < row['coupons'] <= 2**31 - 1
            )
            value['confirmed_bear_store_purchases'] = sum(
                row['quantity']
                for row in store
                if type(row.get('quantity')) is int and 0 < row['quantity'] <= 100000
            )
            for currency, key in (
                (116, 'confirmed_bear_store_score_spent'),
                (120, 'confirmed_bear_store_jars_spent'),
            ):
                value[key] = sum(
                    row['spent']
                    for row in store
                    if row.get('currency') == currency
                    and type(row.get('spent')) is int
                    and 0 < row['spent'] <= 2**31 - 1
                )
            value['confirmed_bear_buys'] = sum(
                row.get('count', 1)
                for row in buys
                if type(row.get('count', 1)) is int and 0 < row.get('count', 1) <= 100000
            )
            value['confirmed_bear_sells'] = sum(
                row.get('count', 1)
                for row in sales
                if type(row.get('count', 1)) is int and 0 < row.get('count', 1) <= 100000
            )
            trades = buys + sales
            value['confirmed_bear_score_gained'] = sum(
                row['score_delta']
                for row in trades
                if type(row.get('score_delta')) is int and 0 < row['score_delta'] <= 2**31 - 1
            )
            value['confirmed_bear_score_spent'] = sum(
                -row['score_delta']
                for row in trades
                if type(row.get('score_delta')) is int and -(2**31) < row['score_delta'] < 0
            )
            value['confirmed_bear_jars'] = sum(
                row['jars']
                for row in sales
                if type(row.get('jars')) is int and 0 < row['jars'] <= 100000
            )
        if name == 'penglai':
            for operation, target in (
                (1, 'confirmed_plantings'),
                (2, 'confirmed_waterings'),
                (3, 'confirmed_harvests'),
            ):
                value[target] = sum(
                    row.get('kind') == 'plant_tree'
                    and row.get('status') == 'confirmed'
                    and row.get('operation') == operation
                    for row in rows
                )
        if name in ('produce_activity', 'produce_common'):
            value['confirmed_produce_outputs'] = sum(
                row.get('produced', 0)
                for row in rows
                if row.get('kind')
                == ('activity_produce' if name == 'produce_activity' else 'activity_produce_common')
                and row.get('status') == 'confirmed'
                and type(row.get('produced')) is int
                and 0 < row['produced'] <= 100000
            )
        for kind, field in (
            ('craft_material_purchase', 'material_purchases'),
            ('craft_material_sweep', 'material_sweeps'),
            ('fashion_craft', 'fashion_crafts'),
            ('fashion_extension', 'extensions'),
            ('suit_card_reward', 'suit_rewards'),
        ):
            count = sum(
                row.get('kind') == kind and row.get('status') == 'confirmed' for row in rows
            )
            if count:
                value[field] = count
        add_counts(value, last, PHASE_COUNTS)
        if name in ('gold_purchase', 'stamina_purchase'):
            resource = 'gold' if name == 'gold_purchase' else 'stamina'
            value['confirmed_purchase_times'] = sum(
                row['count']
                for row in rows
                if row.get('kind') == 'daily_resource_purchase'
                and row.get('resource') == resource
                and row.get('status') == 'confirmed'
                and type(row.get('count')) is int
                and 0 < row['count'] <= 65535
            )
        result['policy_jobs'][name] = value
        reason = allowed(last.get('reason'), PHASE_REASONS | DIAGNOSTIC_REASONS | CONDITIONS)
        if reason is not None:
            value['reason'] = reason
        if last.get('error_type') is not None:
            value['error_type'] = allowed(last.get('error_type'), ERROR_TYPES, 'OtherError')
        if (
            status in ('uncertain', 'pending_reconciliation', 'unknown')
            and 'error_type' not in value
        ):
            diagnostic = next(
                (
                    row
                    for row in reversed(rows)
                    if row.get('status') in ('uncertain', 'failed', 'unknown')
                    and row.get('error_type') is not None
                ),
                None,
            )
            if diagnostic is not None:
                value['error_type'] = allowed(
                    diagnostic.get('error_type'), ERROR_TYPES, 'OtherError'
                )
                reason = allowed(diagnostic.get('reason'), PHASE_REASONS)
                if reason is not None:
                    value['reason'] = reason
                add_counts(value, diagnostic, ('operation', 'slot'))
        category = allowed(
            last.get('category'), frozenset(('fashion', 'dye', 'makeup', 'photo', 'card'))
        )
        if category:
            value['category'] = category
        source = last.get('unfinished_by_category')
        if type(source) is dict:
            value['unfinished_by_category'] = {}
            add_counts(
                value['unfinished_by_category'],
                source,
                ('fashion', 'dye', 'makeup', 'photo', 'card'),
            )
        blocked = last.get('blocked_reason_counts', last.get('blocked_counts'))
        if type(blocked) is dict:
            value['blocked_reason_counts'] = {
                key: count
                for key, count in blocked.items()
                if type(key) is str
                and key in PHASE_REASONS | DIAGNOSTIC_REASONS | CONDITIONS
                and type(count) is int
                and 0 < count <= 100000
            }
        if status not in PHASE_SUCCESS | PHASE_WAITING:
            problems[name] = status
    if problems:
        result['feature_problems'] = problems
    state = allowed(summary.get('execution'), AUTO_STATES | {'waiting', 'session_refresh_required'})
    result['execution'] = (
        state
        if state in ('pending_reconciliation', 'uncertain', 'session_refresh_required')
        else 'failed'
        if exit_code != 0
        else state
        if state in ('awaiting_refresh', 'partial')
        else (
            'waiting'
            if any(row['status'] in PHASE_WAITING for row in result['policy_jobs'].values())
            else 'complete'
        )
        if day is not None
        and day >= target_day
        and len(result['policy_jobs']) == len(PHASE_JOBS[command])
        and not problems
        else 'partial'
    )
    return result


def summarize(command, target_day, exit_code, summary):
    """Keep confirmed deltas when a later failure terminates the same call."""
    failed = type(summary) is dict and summary.get('execution') == 'failed'
    source = {**summary, 'execution': 'partial'} if failed else summary
    result = _summarize_fields(command, target_day, exit_code, source)
    if type(summary) is dict:
        diagnostics = extract_diagnostics(summary)
        result.update(
            {
                key: value
                for key, value in diagnostics.items()
                if key != 'additional_confirmed_reward_counts'
            }
        )
        rewards = {}
        for source in (
            result.get('confirmed_reward_counts'),
            summary.get('confirmed_reward_counts'),
            diagnostics.get('additional_confirmed_reward_counts'),
        ):
            if type(source) is dict:
                for key in REWARD_COUNTS:
                    value = source.get(key)
                    if type(value) is int and 0 <= value <= 100000:
                        rewards[key] = max(rewards.get(key, 0), value)
        if rewards:
            result['confirmed_reward_counts'] = rewards
        add_counts(result, summary, ('sent_actions',))
        if summary.get('status_only') is True:
            result['status_only'] = True
        resume = summary.get('daily_reward_resume')
        if type(resume) is dict:
            state = allowed(
                resume.get('status'), DAILY_REWARD_RESUME_STATES | {'failed', 'not_executed'}
            )
            if state is not None:
                result['daily_reward_resume'] = {'status': state}
                add_counts(result['daily_reward_resume'], resume, DAILY_REWARD_RESUME_COUNTS)
        add_counts(result, summary, ('daily_activity', 'claimed_daily_chests'))
        details = server_observation_details(summary)
        if details:
            result['feature_details'] = details
        snapshot = report_snapshot_summary(summary.get('report_snapshot'))
        if snapshot is not None:
            result['report_snapshot'] = snapshot
        if failed:
            result.update(
                execution='failed',
                failure_stage=allowed(summary.get('failure_stage'), MAIN_FAILURE_STAGES, 'execute'),
                error_type=allowed(summary.get('error_type'), ERROR_TYPES, 'OtherError'),
            )
    return result


def _summarize_fields(command, target_day, exit_code, summary):
    result = {'exit_code': exit_code}
    if exit_code == 2 and (summary or {}).get('execution') == 'already_running':
        return {**result, 'execution': 'already_running', 'failure_stage': 'runtime_lock'}
    if summary is None:
        return missing_summary(result)

    row = cardbook_row(summary) if command == 'cardbook' else {}
    day = iso_day(summary.get('game_day')) or iso_day(row.get('game_day'))
    if day is not None:
        result['game_day'] = day
    details = server_observation_details(summary)
    if details:
        result['feature_details'] = details
    snapshot = report_snapshot_summary(summary.get('report_snapshot'))
    if snapshot is not None:
        result['report_snapshot'] = snapshot
    if summary.get('status_only') is True:
        result['status_only'] = True
    if command in PHASE_JOBS:
        return summarize_policy_phase(command, target_day, exit_code, summary)

    if summary.get('daily_activity_games_deferred') is True:
        result['deferred_to_activities'] = ['ring_toss', 'penglai']
    if summary.get('wardrobe_palettes_deferred') is True:
        result['deferred_to_wardrobe'] = ['palette_unlock', 'qianhuan']

    if command == 'cardbook':
        state = allowed(row.get('status'), CARDBOOK_STATES)
        cardbook_diagnostics(result, summary, exit_code)
        resume = summary.get('daily_reward_resume')
        if type(resume) is dict:
            resume_status = allowed(resume.get('status'), DAILY_REWARD_RESUME_STATES)
            if resume_status is not None:
                result['daily_reward_resume'] = {'status': resume_status}
                add_counts(result['daily_reward_resume'], resume, DAILY_REWARD_RESUME_COUNTS)
                problems = feature_problems({'daily_reward_resume': resume})
                if problems:
                    result['feature_problems'] = problems
        if state is None:
            return missing_summary(result)
        result['cardbook_status'] = state
        action_kind = allowed(row.get('kind'), CARDBOOK_ACTION_KINDS)
        if action_kind is not None:
            result['cardbook_action_kind'] = action_kind
            result['failure_stage'] = 'execute'
            if row.get('error_type') is not None:
                result['error_type'] = allowed(row.get('error_type'), ERROR_TYPES, 'OtherError')
        add_counts(result, row, CARDBOOK_COUNTS)
        if exit_code != 0:
            result['execution'] = 'failed'
        elif state == 'daily_limit_complete' and day is not None and day >= target_day:
            result['execution'] = (
                'awaiting_completion' if result.get('running_tasks', 0) > 0 else 'complete'
            )
        elif state in ('awaiting_refresh', 'awaiting_completion', 'awaiting_task_refresh'):
            result['execution'] = state
        else:
            result['execution'] = 'partial'
        return result

    state = allowed(summary.get('execution'), AUTO_STATES)
    if state is None:
        return missing_summary(result)
    result['daily_status'] = state
    checklist = (
        checklist_summary(summary.get('full_checklist_summary')) if command == 'auto' else None
    )
    if checklist is not None:
        result['full_checklist_summary'] = checklist
    problems = feature_problems(summary)
    if problems:
        result['feature_problems'] = problems
    add_counts(result, summary, AUTO_COUNTS)
    for group, keys in (
        ('confirmed_reward_counts', REWARD_COUNTS),
        ('confirmed_arena_counts', ('arena_upload', 'arena_buy', 'arena_challenge', 'arena_skip')),
        ('deferred_counts', ('current', 'historical', 'unknown')),
    ):
        source = summary.get(group)
        if type(source) is dict:
            counts = {}
            add_counts(counts, source, keys)
            result[group] = counts
    for key in AUTO_FLAGS:
        if type(summary.get(key)) is bool:
            result[key] = summary[key]
    cardbook = summary.get('cardbook_daily')
    if type(cardbook) is dict:
        cardbook_status = allowed(cardbook.get('status'), CARDBOOK_STATES)
        if cardbook_status is not None:
            result['cardbook_status'] = cardbook_status
    if exit_code != 0:
        result['execution'] = 'failed'
    elif state in {
        'pending_reconciliation',
        'uncertain',
        'deferred_unresolved',
        'mail_read_unconfirmed',
        'required_tasks_incomplete',
        'awaiting_refresh',
    }:
        result['execution'] = state
    elif command == 'auto' and 'full_checklist_summary' in summary:
        goals = (checklist or {}).get('goals', {})
        statuses = [row.get('status') for row in goals.values()]
        if (checklist or {}).get('complete') is True:
            result['execution'] = 'complete'
        elif (
            len(goals) == len(CHECKLIST_GOAL_COUNTS)
            and 'waiting' in statuses
            and all(status in ('complete', 'waiting') for status in statuses)
        ):
            result['execution'] = 'waiting'
        else:
            result['execution'] = 'partial'
    elif state in {'activity_complete', 'all_observed_tasks_complete', 'complete'}:
        verified = (
            day is not None
            and day >= target_day
            and all(summary.get(key) is True for key in AUTO_VERIFICATION)
        )
        result['execution'] = 'complete' if verified else 'partial'
    else:
        result['execution'] = state
    return result


def needs_fresh_session(record):
    problems = record.get('feature_problems')
    refresh_states = frozenset(('uncertain', 'pending_reconciliation', 'session_refresh_required'))
    group_failures = record.get('cardbook_group_failures')
    resume = record.get('daily_reward_resume')
    grouped_refresh = (
        type(group_failures) is dict
        and any(state in refresh_states for state in group_failures.values())
        or record.get('cardbook_status') in refresh_states
        or type(resume) is dict
        and resume.get('status') in refresh_states
    )
    failed_session = (
        record.get('execution') == 'failed'
        and record.get('failure_stage') in ('connect', 'execute', 'summary')
        and record.get('error_type')
        in (
            'TimeoutError',
            'ConnectionError',
            'ConnectionResetError',
            'BrokenPipeError',
            'JournalError',
            'AlreadyRecorded',
        )
    )
    return (
        failed_session
        or grouped_refresh
        or record['execution'] in refresh_states
        or type(problems) is dict
        and any(
            status in ('uncertain', 'pending_reconciliation', 'session_refresh_required')
            for status in problems.values()
        )
    )


def result_exit_code(record):
    if record.get('result_storage') == 'failed':
        return 1
    code = record['exit_code']
    if code is None:
        return 1
    if code != 0:
        return code if code > 0 else 128 - code
    if record['execution'] in ('failed', 'unverified', 'timeout') or needs_fresh_session(record):
        return 1
    return 0
