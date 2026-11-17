"""Fixed result facts only. Never copy identities, responses or exception text.

Action issues describe this invocation. Current server observations and receipt
deltas are separate facts and cannot erase an earlier failed/uncertain action.
"""
from collections import Counter

MAX_ISSUES = 128
DAILY_TASK_IDS = frozenset(
    (16, 17, 12, 13, 14, 691, 987, 15, 33, 21, 31, 35, 19, 18, 692, 20, 32, 34, 1199)
)
SOURCES = {
    'endorsement_actions': 'endorsement',
    'endorsement_maintenance_actions': 'endorsement',
    'guild_salary_actions': 'guild_salary',
    'pet_care_actions': 'pet_care',
    'arena_daily_actions': 'arena_daily',
    'arena_climb_actions': 'arena_climb',
    'cardbook_actions': 'cardbook',
    'cardbook_daily_actions': 'cardbook',
    'stage_first_clear_actions': 'stage_first_clears',
    'activity_resume_actions': 'activity_rewards',
    'daily_error_actions': 'daily_execution',
    'daily_reward_error_actions': 'daily_reward_collection',
    **{
        key + '_actions': feature
        for key, feature in (
            ('monthly_sign', 'monthly_sign'),
            ('free_reward', 'free_reward'),
            ('friend_energy', 'friend_energy'),
            ('friend_energy_resume', 'friend_energy'),
            ('home_eat_income', 'home_eat_income'),
            ('atlas_daily', 'atlas_daily'),
            ('weekly_activity', 'weekly_activity'),
            ('achievement', 'achievement'),
            ('mail', 'mail'),
            ('mail_read', 'mail_read'),
            ('month_card', 'month_card'),
            ('big_month_card', 'big_month_card'),
            ('vip_daily_gift', 'vip_daily_gift'),
            ('vip_weekly_gift', 'vip_weekly_gift'),
            ('fashion_atlas', 'fashion_atlas'),
            ('craft_suit', 'craft_suit'),
            ('chapter_reward', 'chapter_reward'),
            ('social_topic_coin', 'social_topic_coins'),
            ('discount_package_reward', 'discount_package_rewards'),
            ('guild_project_reward', 'guild_project_rewards'),
            ('vvanna_reward', 'vvanna_rewards'),
            ('share_reward', 'share_reward'),
            ('pass_reward', 'pass_reward'),
            ('ui_coin', 'ui_coin_rewards'),
            ('shop_purchase', 'shop_purchases'),
            ('palette_unlock', 'palette_unlock'),
            ('open_shop', 'open_shop'),
            ('activity_threshold', 'activity_rewards'),
            ('vvanna_quiz', 'vvanna_quiz'),
            ('vigor_exchange', 'vigor_exchange'),
            ('member_bonus', 'member_bonus'),
            ('free_lottery', 'free_lottery'),
            ('lottery_sign', 'lottery_sign'),
            ('lottery_coin', 'lottery_sign_coins'),
            ('blind_box_draw', 'blind_box_draws'),
            ('blind_box', 'blind_box_workshop'),
            ('lottery_red_envelope', 'lottery_red_envelopes'),
            ('timed_free_reward', 'free_reward'),
        )
    },
}
PHASES = frozenset(
    (
        'activity_discovery',
        'activity_resources',
        'astrology',
        'produce_activity',
        'produce_common',
        'ring_toss',
        'activity_rewards',
        'penglai',
        'bear',
        'brand_atlas',
        'brand_tower',
        'gold_purchase',
        'stamina_purchase',
        'heart_crafting',
        'main_story_crafting',
        'palette_unlock',
        'qianhuan',
    )
)
FEATURES = frozenset(SOURCES.values()) | PHASES
KINDS = FEATURES | frozenset(
    '''
    endorsement_maintenance endorsement_salary endorsement_barrier endorsement_contract
    endorsement_income endorsement_income_upgrade endorsement_income_building
    arena_upload arena_buy arena_challenge arena_skip arena_refresh arena_recommendation
    cardbook_daily cardbook_reward cardbook_dispatch cardbook_refresh
    pet_care pet_wake pet_home pet_food_supply pet_food_stock pet_feed_home pet_toilet pet_wake_home
    ui_coin_reward ui_coin daily_publicity daily_gameplay task_reward chest
    stage_first_clear plant_tree plant_tree_reward astrology_spin astrology_reward astrology_attempt_reward
    bear_buy bear_sell bear_home_entry bear_home_query bear_home_brief bear_welcome bear_daily_coupons bear_shop bear_store bear_trade_followup
    fashion_craft fashion_extension craft_material_purchase craft_material_sweep daily_resource_purchase
    activity_produce activity_produce_common activity_produce_summary activity_produce_common_summary
'''.split()
)
ERRORS = frozenset(
    '''
    OSError FileNotFoundError NotADirectoryError PermissionError TimeoutError TimeoutExpired
    ConnectionError ConnectionRefusedError ConnectionResetError BrokenPipeError ValueError TypeError
    RuntimeError KeyError AttributeError ImportError ModuleNotFoundError InvalidJournal JournalError
    AlreadyRecorded HandshakeTransportError HTTPError URLError CalledProcessError OtherError
'''.split()
)
STAGES = frozenset(
    '''
    state_read eligibility dispatch response session_recovery handler discovery home_entry food_supply
    pet_care pet_wake toilet_query salary_collect quick_start quick_finish income_upgrade income_building
    arena_state_query arena_setup arena_buy arena_challenge arena_refresh cardbook_state_query
    topic_catalog topic_discovery task_snapshot task_eligibility daily_execution daily_reward_collection
    accept award query setup claim purchase sweep craft unlock
    shop_dispatch shop_receipt shop_resources shop_quota shop_postcondition shop_pacing shop_response_tail
    bear_store_dispatch bear_store_receipt bear_store_resources bear_store_quota bear_store_ownership
'''.split()
)
FIRST_CLEAR_MISSING = {
    'first_time_story_phase_or_verified_skip_gate': '首通剧情尚未推进，且未取得可用的剧情跳过条件',
    'native_first_story_task_chain': '当前首通剧情任务链尚无可用处理路线',
    'fresh_unfinished_story_task_snapshot': '未取得当前未完成剧情任务状态',
    'native_initial_dialogue_skip_not_ready': '当前剧情尚不满足原生跳过条件',
    'native_initial_dialogue_playback': '原生剧情播放步骤未取得可用结果',
    'chapter_activity_open_state': '未取得章节活动开放状态',
    'stage_function_604': '未取得关卡功能开放条件',
    'complete_reputation_snapshot': '未取得完整进度声望状态',
    'complete_unchanged_role_stage_snapshot_702': '当前连接关卡快照不完整',
    'complete_role_stage_snapshot_702': '当前连接关卡快照不完整',
    'complete_unchanged_campaign_snapshot_5517': '当前连接章节活动快照不完整',
    'role_properties_ready_561': '角色属性尚未就绪',
    'complete_current_card_roster_715': '当前连接卡牌列表不完整',
    'current_card_restrictions': '未取得当前卡牌限制',
    'complete_current_wardrobe_576': '当前连接衣橱状态不完整',
    'same_role_body_544': '当前角色体型状态不完整',
    'native_player_and_faction_attribute_bonus': '未取得原生角色与羁绊属性加成',
    'native_required_stage_fashion_unowned': '尚未拥有该关卡要求的服饰',
    'native_required_stage_fashion_positions': '关卡要求服饰的位置配置不明确',
    'native_required_stage_fashion_combination': '未取得满足关卡要求的服饰组合',
    'native_stage_clothing_score': '未取得可用的原生搭配得分',
    'native_stage_normal_recommendation': '普通关卡原生推荐未取得可用结果',
    'native_stage_vvanna_recommendation': '玩呐关卡原生推荐未取得可用结果',
    'stage_specific_common_use_limit': '该关卡专用次数限制尚无处理流程',
    'native_photo_or_noncombat_phase_execution': '该关卡的拍照或非战斗阶段尚无处理流程',
    'native_chapter_layout_configuration': '原生章节入口配置不完整',
    'native_chapter_layout_decode': '原生章节入口配置读取失败',
    'native_story_skip_configuration': '原生剧情跳过配置不完整',
    'native_initial_story_task_template': '初始剧情任务模板不完整',
    'original_story_dialogue_configuration': '原生剧情对话配置不完整',
    'native_plain_initial_story_dialogue': '初始剧情对话尚无可用处理路线',
    'native_free_short_story_continuation': '后续短剧情尚无可用处理路线',
    'native_pure_heart_story_phase': '心意剧情阶段尚无可用处理路线',
    'native_pure_story_task_chain': '纯剧情任务链尚无可用处理路线',
    'native_free_pure_story_task': '纯剧情任务类型尚无处理流程',
    'native_pure_story_phase_continuation': '后续纯剧情阶段尚无处理流程',
    'native_free_pure_story_task_filter': '纯剧情任务条件尚无处理流程',
    'native_plain_pure_story_dialogue': '纯剧情对话尚无可用处理路线',
    'native_vvanna_objective_configuration': '玩呐目标配置不完整',
    'native_vvanna_multi_outfit_configuration': '玩呐多套搭配配置不完整',
    'native_vvanna_objective_category': '玩呐目标类型尚无处理流程',
    'native_completed_vvanna_story_phase': '玩呐前置剧情阶段尚未确认完成',
    'native_vvanna_phase_task_configuration': '玩呐阶段任务配置不完整',
    'native_one_card_correction_evidence': '未取得玩呐单卡调整所需状态',
    'vvanna_outfit_objective_verification': '当前搭配尚未满足玩呐目标条件',
    'native_vvanna_phase_correction_loadout': '未取得玩呐阶段调整搭配',
    'native_mixed_tag_count_correction_evidence': '未取得玩呐混合标签调整所需状态',
    'bounded_vvanna_outfit_progress': '玩呐搭配进度超出当前处理范围',
    'first_clear_requirement_unavailable': '该首通前置条件尚无可用诊断分类',
}
FIRST_CLEAR_READ_MISSING = frozenset(
    (
        'chapter_activity_open_state',
        'complete_reputation_snapshot',
        'complete_unchanged_role_stage_snapshot_702',
        'complete_role_stage_snapshot_702',
        'complete_unchanged_campaign_snapshot_5517',
        'complete_current_card_roster_715',
        'current_card_restrictions',
        'complete_current_wardrobe_576',
        'same_role_body_544',
        'fresh_unfinished_story_task_snapshot',
    )
)
PALETTE_SUPPLY_REASONS = {
    'exclusive_wind_vane_not_sold': '专属风向标当前不出售',
    'exclusive_color_card_not_sold': '专属色卡当前不出售',
    'palette_material_not_in_current_mall': '材料当前商城无商品',
    'reputation_only_material_offers': '当前只有声望或活动币兑换，无钻点商品',
}
REASONS = (
    frozenset(
        '''
    state_snapshot_missing operation_result_unavailable step_exception unsupported_handler server_refused
    response_result_missing request_result_missing request_result_unavailable request_already_sent
    request_already_sent_this_run action_exception preflight_failed session_recovery_failed session_unavailable
    session_interrupted session_boundary_unavailable game_day_changed session_age_limit response_timeout query_timeout
    function_state_missing play_state_unavailable observations_incomplete observation_unavailable
    full_endorsement_snapshot season_catalog_missing fresh_income_state_unavailable
    maintenance_final_endorsement_snapshot maintenance_level_changed maintenance_step_result
    gold_snapshot season_mismatch season_closed endorse_level insufficient_gold confirmed_goal_missing_from_snapshot
    locked_block locked_shop quick_outfit_unavailable guild_member_snapshot_missing weekly_quota_snapshot_missing
    weekly_income_snapshot_missing salary_state_unavailable reward_inventory_snapshot_missing
    monthly_sign_entry_missing monthly_sign_snapshot_missing monthly_sign_snapshot_changed
    monthly_sign_candidate_changed monthly_sign_goal_unconfirmed guild_snapshot_missing
    guild_membership_snapshot_changed quota_snapshot_changed project_claim_mark_unconfirmed project_quota_changed
    member_claim_timestamp_missing project_timestamp_inconsistent project_claim_time_equals_opening
    achievement_snapshot_missing campaign_snapshot_missing activity_reward_handler_missing
    achievement_award_mismatch achievement_finish_time_invalid stage_snapshot_missing stage_snapshot_changed
    chapter_activity_snapshot_missing reward_receipt_ambiguous reward_flag_unconfirmed
    configuration_checksum_mismatch configuration_ambiguous project_ui_entry_unsupported project_conditions_unsupported
    configuration_missing page_configuration_unsupported reward_prerequisites_unsupported reward_configuration_ambiguous
    guild_project_state_unavailable monthly_sign_state_unavailable discount_reward_state_unavailable vvanna_state_unavailable
    vvanna_reward_state_unavailable vvanna_reward_goal_unconfirmed
    arena_state_query_failed cardbook_state_query_failed daily_execution_exception daily_reward_collection_exception
    activity_handler_missing spin_response_unavailable plant_tree_operation_response_unavailable
    reward_collection_unavailable astrology_state_unavailable score_reward_task_state_unavailable score_reward_template_unsupported
    topic_list_not_observed topic_list_expired topic_list_context_changed topic_list_incomplete topic_query_unconfirmed
    topic_service_rejected topic_response_invalid topic_catalog_unavailable task_snapshot_unavailable
    task_eligibility_unavailable task_template_unsupported social_token_unavailable community_function_unavailable
    candidate_discovery_failed candidate_limit_exceeded invalid_candidate action_limit_reached
    session_refresh_unavailable session_boundary_invalid unmapped_due_free_pool unmapped_pool_free_state_missing
    catalog_mapping_unknown ui_mapping_unknown replenish_mapping_unknown restriction_unsupported quota_unsupported
    payment_unsupported product_unavailable switches_unknown cash_unknown balance_unknown grant_state_unknown
    quota_unknown resource_state_unknown vip_unknown role_not_ready ui_task_snapshot_unavailable
    task_template_missing task_template_not_reviewed task_template_family_unsupported
    common_source_ownership_unavailable common_source_produce_unconfirmed common_source_type_unsupported
    common_recipe_configuration_unsupported common_ownership_response_invalid
    activity_crafting_state_unavailable activity_crafting_inventory_unavailable activity_menu_missing
    shop_dispatch_failed shop_receipt_missing shop_resource_delta_mismatch shop_quota_change_missing
    shop_postcondition_missing shop_pacing_failed shop_response_tail_failed
    mail_inventory_unavailable mail_list_decode_failed mail_page_mismatch mail_inventory_incomplete
    mail_receipt_missing mail_receipt_invalid mail_attachment_bit_unconfirmed
    mail_inventory_recovered mail_attachment_dispatch_failed mail_attachment_response_unavailable
    mail_attachment_receipt_invalid mail_attachment_refresh_unavailable mail_attachment_postcondition_unavailable
    red_envelope_result_unavailable red_envelope_receipt_missing red_envelope_quota_change_missing
    red_envelope_resource_delta_mismatch red_envelope_gold_credit_missing red_envelope_dispatch_failed
    pet_wake_state_missing pet_wake_target_mismatch
    astrology_attempt_mapping_unavailable astrology_attempt_template_unsupported astrology_attempt_state_unavailable
    astrology_attempt_state_changed astrology_attempt_result_unavailable
    activity_conditions_missing activity_period_ambiguous reputation_snapshot_missing reputation_balance_invalid
    quota_snapshot_missing friend_home_list_missing reachable_bear_missing home_query_unavailable home_entry_unavailable
    trade_response_incomplete welcome_execution_unavailable shop_execution_unavailable server_day_changed
    trade_followup_refused trade_followup_invalid trade_followup_unavailable trade_dispatch_not_sent
    bear_coupon_balance_missing bear_grant_conditions_missing bear_grant_session_unavailable bear_grant_state_missing
    bear_welcome_credit_missing bear_welcome_followup_failed bear_welcome_result_missing
    bear_store_conditions_changed bear_store_followup_failed bear_store_ownership_missing bear_store_state_missing
    bear_store_receipt_missing bear_store_resource_delta_mismatch bear_store_quota_change_missing
    bear_store_ownership_change_missing bear_store_dispatch_failed bear_store_payment_unsupported
    bear_store_product_unsupported bear_store_quota_unsupported bear_store_task_grant_unsupported
    entered_home_snapshot_missing friend_home_brief_missing friend_home_directory_invalid friend_home_directory_missing
    home_location_changed home_npc_sync_missing home_npc_update_invalid native_home_inspector_missing
    owned_home_catalog_invalid owned_home_catalog_missing owned_home_selection_missing remote_home_query_unsupported
    craft_receipt_missing craft_material_debit_missing craft_output_gain_missing craft_proof_incomplete craft_target_mismatch
'''.split()
    )
    | frozenset(FIRST_CLEAR_MISSING)
    | frozenset(PALETTE_SUPPLY_REASONS)
)
CONDITIONS = frozenset(
    '''
    position_ineligible no_last_week_contribution zero_personal_wage insufficient_currency insufficient_stamina
    insufficient_items insufficient_materials insufficient_vigor insufficient_activity_money score_insufficient
    higher_opponent_score not_member not_open no_reward at_weekly_limit at_daily_limit daily_limit_complete
    closed inactive season_closed level_locked outside_draw_window waiting waiting_for_growth daily_growth_complete
    period_drawings_complete attempts_exhausted target_reached already_claimed cleared complete
    salary_collection_complete available_income_upgrades_complete available_quick_contracts_complete
    no_current_offer no_current_topic no_coin_reward quick_function_closed
    daily_trade_complete daily_sale_limit_complete outside_trade_window insufficient_resources
'''.split()
)
CARE = frozenset(('feed', 'bath', 'play', 'toilet'))
ISSUE_STATUSES = frozenset(('failed', 'uncertain', 'not_executed', 'read_failed', 'unsupported'))
SENT_STATES = frozenset(('sent', 'not_sent', 'unproven'))
PUBLIC_SHOP_NAMES = frozenset(
    ('每日福利礼盒', '每周羁绊占心组盒', '每周羁绊升级组盒', '每周潮流色卡礼盒', '每周家居小栗礼罐', '限时羁绊占心组盒', '限时羁绊升级组盒', '材料礼包')
)
PROOF_PARTS = frozenset(
    (
        'receipt',
        'quota',
        'resources',
        'gold_credit',
        'observer',
        'material_debit',
        'output_gain',
        'pet_awake_state',
        'cake',
        'coupons',
        'ownership',
        'reputation_debit',
        'reputation_credit',
        'quota_advance',
    )
)


def public_issue_target(source):
    result = {}
    for key in ('sku', 'item', 'limit', 'target_id', 'npc_id', 'reputation_id'):
        value = source.get(key)
        if type(value) is int and 0 < value <= 2**31 - 1:
            result[key] = value
    index = source.get('index')
    if type(index) is int and 0 <= index <= 65535:
        result['index'] = index
    sequence = source.get('target_sequence')
    if type(sequence) is int and 1 <= sequence <= 1024:
        result['target_sequence'] = sequence
    name = source.get('name')
    if type(name) is str and name in PUBLIC_SHOP_NAMES:
        result['name'] = name
    missing = source.get('missing_proof')
    if type(missing) in (list, tuple):
        values = tuple(sorted({v for v in missing[:8] if type(v) is str and v in PROOF_PARTS}))
        if values:
            result['missing_proof'] = values
    return result


TASKS = {14804: 'official_shop_coin', 2109: 'heart_crafting_coin'}
TASK_LABELS = frozenset(TASKS.values()) | {'fashion_magazine_coin'}
TASK_STATES = frozenset(
    (
        'confirmed',
        'cleared',
        'no_reward',
        'claimable',
        'unknown',
        'not_executed',
        'uncertain',
        'rejected',
        'unsupported',
        'closed',
    )
)


def number(value, maximum=100000):
    return value if type(value) is int and 0 <= value <= maximum else None


def exception_fields(error):
    """A decorator may attach safe categories; the exception body is ignored."""
    source = getattr(error, 'reward_issue', None)
    source = source if type(source) is dict else {}
    result = {}
    for field, allowed in (('reason', REASONS), ('failure_stage', STAGES)):
        value = source.get(field, getattr(error, field, None))
        if type(value) is str and value in allowed:
            result[field] = value
    kind = type(error).__name__
    result['error_type'] = kind if kind in ERRORS else 'OtherError'
    return result


def sanitize_issues(rows):
    if type(rows) is not list:
        return []
    output = []
    for source in rows[:MAX_ISSUES]:
        if (
            type(source) is not dict
            or type(source.get('feature')) is not str
            or source['feature'] not in FEATURES
            or type(source.get('status')) is not str
            or source['status'] not in ISSUE_STATUSES
        ):
            continue
        row = {'feature': source['feature'], 'status': source['status']}
        for key, allowed in (
            ('kind', KINDS),
            ('failure_stage', STAGES),
            ('reason', REASONS),
            ('error_type', ERRORS),
            ('care', CARE),
            ('sent_state', SENT_STATES),
        ):
            value = source.get(key)
            if type(value) is str and value in allowed:
                row[key] = value
        op = source.get('operation')
        if type(op) is int and (0 <= op <= 32 or op == 264):
            row['operation'] = op
        code = source.get('code')
        if type(code) is int and -1 <= code <= 2**31 - 1:
            row['code'] = code
        for key in ('count', 'sent_actions'):
            value = number(source.get(key))
            if value is not None:
                row[key] = value
        row.update(public_issue_target(source))
        output.append(row)
    return output


def sanitize_task_results(source):
    if type(source) is not dict:
        return {}
    out = {}
    for label in sorted(TASK_LABELS):
        row = source.get(label)
        if (
            type(row) is not dict
            or type(row.get('status')) is not str
            or row['status'] not in TASK_STATES
        ):
            continue
        value = {'status': row['status']}
        for key in ('confirmed_actions',):
            count = number(row.get(key))
            if count is not None:
                value[key] = count
        reason = row.get('reason')
        if type(reason) is str and reason in REASONS:
            value['reason'] = reason
        out[label] = value
    return out


def sanitize_daily_tasks(source):
    """Public daily IDs and bounded progress only; never retain server replies."""
    if type(source) not in (list, tuple):
        return []
    result = {}
    for row in source[:64]:
        if type(row) is not dict:
            continue
        ident = row.get('id', row.get('task_id'))
        state = row.get('state')
        if (
            type(ident) is not int
            or ident not in DAILY_TASK_IDS
            or type(state) is not str
            or state not in ('claimed', 'claimable', 'active', 'unobserved')
        ):
            continue
        value = {'task_id': ident, 'state': state}
        counts = row.get('event_counts')
        if (
            type(counts) in (list, tuple)
            and len(counts) <= 8
            and all(type(n) is int and 0 <= n <= 2**31 - 1 for n in counts)
        ):
            value['event_counts'] = list(counts)
        if type(row.get('disabled_gameplay')) is bool:
            value['disabled_gameplay'] = row['disabled_gameplay']
        result[ident] = value
    return list(result.values())


def sanitize_public_activities(rows):
    if type(rows) not in (list, tuple):
        return []
    output = []
    for row in rows[:64]:
        if type(row) is not dict:
            continue
        tab = number(row.get('tab'), 2**31 - 1)
        ids = row.get('activity_ids')
        if type(ids) not in (list, tuple):
            continue
        ids = sorted(
            {value for value in ids[:64] if type(value) is int and 0 < value <= 2**31 - 1}
        )
        if ids:
            output.append({'activity_ids': ids, **({'tab': tab} if tab is not None else {})})
    return output


def sanitize_first_clear_requirements(source):
    if type(source) is not dict:
        return None
    result = {}
    state = source.get('status')
    if type(state) is str and state in ('observed', 'unavailable', 'unknown'):
        result['status'] = state
    for field, keys in (
        ('candidate_counts', ('normal', 'hard', 'vvanna', 'heart')),
        ('missing_counts', FIRST_CLEAR_MISSING),
    ):
        rows = source.get(field)
        if type(rows) is dict:
            result[field] = {key: rows[key] for key in keys if number(rows.get(key)) is not None}
    gates = source.get('story_skip')
    if type(gates) is dict:
        value = {
            key: gates[key]
            for key in (
                'task_switch_snapshot',
                'chapter_skip_task_switch',
                'chapter_skip_functions_open',
                'chapter_skip_progress_met',
            )
            if type(gates.get(key)) is bool
        }
        count = number(gates.get('required_dialogue_count'), 65535)
        if count is not None:
            value['required_dialogue_count'] = count
        if value:
            result['story_skip'] = value
    return result or None


def _issue(feature, source):
    status = source.get('status', source.get('outcome'))
    reason = source.get('reason')
    error = source.get('error_type')
    if type(status) is not str:
        return None
    reason = reason if type(reason) is str else None
    if reason in PALETTE_SUPPLY_REASONS and status not in (
        'failed',
        'rejected',
        'uncertain',
        'unconfirmed',
    ):
        status = 'not_executed'
    if (
        (status in CONDITIONS or reason in CONDITIONS)
        and error is None
        and source.get('code', source.get('error_code')) is None
    ):
        if status not in ('failed', 'rejected', 'uncertain', 'unconfirmed'):
            return None
    if status in (
        'confirmed',
        'activated',
        'initialized',
        'already_confirmed',
        'current_state_reconciled',
        'current_goal_reconciled',
        'prepared',
        'available_activity_rewards_complete',
        'all_unlocked_production_complete',
        'available_brand_levels_complete',
        'ordinary_limit_complete',
        'already_configured',
    ):
        return None
    if status in ('not_executed', 'excluded') and reason in (
        'request_result_unavailable',
        'request_already_sent',
        'request_already_sent_this_run',
    ):
        return None  # The original issue is retained; this marker is not a new failure.
    if (
        status in ('partial', 'waiting', 'blocked', 'not_started')
        and error is None
        and reason not in REASONS
    ):
        return None
    stage = source.get('failure_stage')
    stage = stage if type(stage) is str and stage in STAGES else None
    if status in ('rejected', 'failed'):
        category = 'failed'
    elif status in (
        'uncertain',
        'pending_reconciliation',
        'deferred_unresolved',
        'deferred_reconciliation',
        'unconfirmed',
        'mail_read_unconfirmed',
        'closed_unconfirmed',
        'expired_unconfirmed',
        'session_refresh_required',
    ):
        category = 'uncertain'
    elif status in ('not_executed', 'not_started', 'excluded'):
        category = 'not_executed'
    elif status == 'unsupported' or reason in (
        'season_catalog_missing',
        'activity_handler_missing',
        'task_template_unsupported',
        'payment_unsupported',
        'project_ui_entry_unsupported',
        'project_conditions_unsupported',
    ):
        category = 'unsupported'
    elif status == 'read_failed':
        category = 'read_failed'
    elif (
        status
        in (
            'unknown',
            'unrecognized',
            'unverified',
            'query_unknown',
            'query_unavailable',
            'query_rejected',
            'entry_unconfirmed',
            'counter_unknown',
            'resource_state_unknown',
            'quota_unknown',
            'availability_unknown',
            'state_unknown',
        )
        or error is not None
    ):
        category = (
            'read_failed'
            if stage
            in ('state_read', 'arena_state_query', 'cardbook_state_query', 'eligibility', 'query')
            or error is None
            else 'failed'
        )
    else:
        return None
    kind = source.get('kind', source.get('feature', feature))
    kind = kind if type(kind) is str and kind in KINDS else feature
    if stage is None:
        if feature.startswith('arena'):
            stage = 'arena_state_query' if category == 'read_failed' else 'handler'
        elif kind == 'endorsement_salary':
            stage = 'salary_collect'
        elif kind == 'endorsement_income_upgrade':
            stage = 'income_upgrade'
        elif kind == 'endorsement_income_building':
            stage = 'income_building'
        elif kind in ('pet_food_stock', 'pet_feed_home', 'pet_wake_home', 'pet_toilet'):
            stage = 'state_read'
            category = 'read_failed'
        elif kind == 'pet_home':
            stage = 'home_entry'
        elif type(source.get('care')) is str and source['care'] in CARE:
            stage = 'pet_care'
        else:
            stage = (
                'state_read'
                if category == 'read_failed'
                else 'response'
                if category in ('uncertain', 'failed')
                else 'handler'
            )
    canonical_reason = (
        reason
        if type(reason) is str and reason in REASONS
        else (
            'state_snapshot_missing'
            if category == 'read_failed'
            else 'unsupported_handler'
            if category == 'unsupported'
            else 'operation_result_unavailable'
            if category == 'uncertain'
            else 'step_exception'
        )
    )
    row = {
        'feature': feature,
        'status': category,
        'kind': kind,
        'failure_stage': stage,
        'reason': canonical_reason,
        'sent_state': 'unproven',
        'count': 1,
    }
    if type(error) is str:
        row['error_type'] = error if error in ERRORS else 'OtherError'
    if type(source.get('care')) is str and source['care'] in CARE:
        row['care'] = source['care']
    operation = source.get('operation')
    if type(operation) is int and (0 <= operation <= 32 or operation == 264):
        row['operation'] = operation
    code = source.get('code', source.get('error_code'))
    if type(code) is int and -1 <= code <= 2**31 - 1:
        row['code'] = code
        if code < 0:
            row.update(status='uncertain', reason='response_result_missing')
        elif status == 'rejected':
            row.update(sent_state='sent', reason='server_refused')
    if category in ('read_failed', 'not_executed', 'unsupported'):
        row.update(sent_state='not_sent', sent_actions=0)
    elif feature == 'guild_salary' and status == 'uncertain':
        row['sent_state'] = 'sent'
    # A wrapper's terminal sent_actions is a whole-job total, not this row's
    # send count. Never distribute it among issues.
    exact = number(source.get('action_sent_actions'))
    if exact is not None:
        row.update(sent_actions=exact, sent_state='sent' if exact else 'not_sent')
    row.update(public_issue_target(source))
    return row


def extract_diagnostics(report):
    if type(report) is not dict:
        return {}
    issues = []
    pets = Counter()
    tasks = sanitize_task_results(report.get('task_results'))
    public = []
    seen_sources = set()
    extra_counts = Counter()
    climb = {'battles': 0, 'purchases': 0}

    def roster_read_recovered(rows, index, source):
        if not (
            type(report.get('fresh_sessions')) is int
            and report['fresh_sessions'] > 1
            and source.get('kind') == 'cardbook_daily'
            and source.get('status') == 'unknown'
            and source.get('missing') in ('current_card_roster_715', 'current_card_restrictions')
            and source.get('error_type') is None
            and source.get('code', source.get('error_code')) is None
        ):
            return False
        later = [row for row in rows[index + 1 :] if type(row) is dict]
        return bool(
            later
            and later[-1].get('kind') == 'cardbook_daily'
            and later[-1].get('status')
            in (
                'awaiting_completion',
                'daily_limit_complete',
                'ordinary_limit_complete',
                'no_dispatchable_tasks',
            )
            and any(
                row.get('kind') == 'cardbook_dispatch' and row.get('status') == 'confirmed'
                for row in later
            )
        )

    def consume(feature, rows):
        if type(rows) not in (list, tuple):
            return
        if id(rows) in seen_sources:
            return
        seen_sources.add(id(rows))
        specific = False
        for index, source in enumerate(rows):
            if type(source) is not dict:
                continue
            if feature == 'cardbook' and roster_read_recovered(rows, index, source):
                continue
            missing = source.get('missing')
            if (
                feature == 'stage_first_clears'
                and source.get('status') == 'unavailable'
                and missing is not None
            ):
                values = missing if type(missing) in (list, tuple) else [missing]
                known = {
                    value
                    for value in values[:64]
                    if type(value) is str and value in FIRST_CLEAR_MISSING
                }
                if not known:
                    known = {'first_clear_requirement_unavailable'}
                for value in sorted(known):
                    row = _issue(
                        feature,
                        dict(
                            source,
                            status='not_executed',
                            reason=value,
                            failure_stage='state_read'
                            if value in FIRST_CLEAR_READ_MISSING
                            else 'eligibility',
                        ),
                    )
                    if row is not None:
                        issues.append(row)
                        specific = True
                continue
            if source.get('status') == 'confirmed' and feature == 'pet_care':
                care = source.get('care')
                if type(care) is str and care in CARE:
                    pets[care] += 1
                elif type(source.get('feature')) is str and source['feature'] in (
                    'pet_wake',
                    'pet_food_supply',
                ):
                    pets[source['feature']] += 1
            if feature == 'ui_coin_rewards':
                task = source.get('task', source.get('task_id'))
                label = TASKS.get(task) if type(task) is int else None
                explicit = source.get('result_label')
                if type(explicit) is str and explicit in TASK_LABELS:
                    label = explicit
                if label and type(source.get('status')) is str and source['status'] in TASK_STATES:
                    value = tasks.setdefault(
                        label, {'status': source['status'], 'confirmed_actions': 0}
                    )
                    value['status'] = source['status']
                    if source['status'] == 'confirmed':
                        value['confirmed_actions'] = value.get('confirmed_actions', 0) + 1
            public.extend(sanitize_public_activities(source.get('unsupported_activities')))
            if (
                source.get('kind') in ('endorsement_maintenance', 'activity_produce_common_summary')
                and specific
            ):
                continue
            value = _issue(feature, source)
            if value is not None:
                issues.append(value)
                specific = True

    for source, feature in SOURCES.items():
        rows = report.get(source)
        consume(feature, rows)
        if source in (
            'endorsement_maintenance_actions',
            'timed_free_reward_actions',
            'friend_energy_resume_actions',
        ) and type(rows) in (list, tuple):
            key = {
                'endorsement_maintenance_actions': 'endorsement_maintenance',
                'timed_free_reward_actions': 'free_reward',
                'friend_energy_resume_actions': 'friend_energy',
            }[source]
            extra_counts[key] += sum(
                type(row) is dict and row.get('status') == 'confirmed' for row in rows
            )
        if source == 'arena_climb_actions' and type(rows) in (list, tuple):
            climb['battles'] = sum(
                type(row) is dict and row.get('status') == 'battle_confirmed' for row in rows
            )
            climb['purchases'] = max(
                (number(row.get('purchased_attempts')) or 0 for row in rows if type(row) is dict),
                default=0,
            )
    jobs = report.get('policy_jobs')
    if type(jobs) is dict:
        for feature in PHASES:
            consume(feature, jobs.get(feature))
    daily = report.get('daily_run')
    if type(daily) is dict:
        consume('daily_execution', daily.get('actions'))
    for issue in sanitize_issues(report.get('action_issues')):
        if issue not in issues:
            issues.append(issue)
    grouped = {}
    for issue in issues:
        identity = tuple(sorted((k, v) for k, v in issue.items() if k != 'count'))
        if identity in grouped:
            grouped[identity]['count'] += issue.get('count', 1)
        else:
            grouped[identity] = dict(issue)
    result = {}
    daily_tasks = sanitize_daily_tasks(report.get('daily_tasks', report.get('tasks')))
    if daily_tasks:
        result['daily_tasks'] = daily_tasks
    requirements = sanitize_first_clear_requirements(
        report.get('stage_first_clear_state', report.get('first_clear_requirements'))
    )
    if requirements is not None:
        result['first_clear_requirements'] = requirements
    if grouped:
        result['action_issues'] = list(grouped.values())[:MAX_ISSUES]
    if any(extra_counts.values()):
        result['additional_confirmed_reward_counts'] = {k: v for k, v in extra_counts.items() if v}
    elif type(report.get('additional_confirmed_reward_counts')) is dict:
        result['additional_confirmed_reward_counts'] = {
            k: v
            for k, v in report['additional_confirmed_reward_counts'].items()
            if k in ('endorsement_maintenance', 'free_reward', 'friend_energy')
            and number(v) is not None
        }
    if any(climb.values()):
        result['confirmed_arena_climb_counts'] = climb
    elif type(report.get('confirmed_arena_climb_counts')) is dict:
        result['confirmed_arena_climb_counts'] = {
            k: v
            for k, v in report['confirmed_arena_climb_counts'].items()
            if k in ('battles', 'purchases') and number(v) is not None
        }
    if pets:
        result['confirmed_pet_counts'] = dict(pets)
    elif type(report.get('confirmed_pet_counts')) is dict:
        result['confirmed_pet_counts'] = {
            k: v
            for k, v in report['confirmed_pet_counts'].items()
            if k in CARE | {'pet_wake', 'pet_food_supply'} and number(v) is not None
        }
    if tasks:
        result['task_results'] = tasks
    if public:
        result['unsupported_activities'] = public
    elif report.get('unsupported_activities'):
        result['unsupported_activities'] = sanitize_public_activities(
            report['unsupported_activities']
        )
    return result
