"""The yslzm daily checklist: sections, tasks and display vocabulary.

Ported from ``yslzm/tools/daily_board/model.py`` and ``render.py``. Sections
follow the execution order cardbook → auto → craft → wardrobe → activities.
"""

from __future__ import annotations

from ...board.catalog import Catalog, Section, TaskSpec
from ...schedule import BEIJING, Schedule

# Full rounds: 06:00 starts the day, 13:15 and 21:15 cover the midday and
# evening stamina windows, 04:30 is the last pass before the 05:00 reset.
# 事件簿 dispatches 4 tasks for a few hours, three batches a day: it runs with
# the 06:00 round and then on its own every 8 hours (business/module_registry.py).
# A time the server reports (an activity opening today) wakes only 活动.
SCHEDULE = Schedule(
    times=('04:30', '06:00', '13:15', '21:15'),
    reset='05:00',
    tz=BEIJING,
    tz_name='Asia/Shanghai',
    slots=(('cardbook', ('06:00', '14:00', '22:00')),),
    dynamic_phases=('activities',),
)

PHASES = {
    'status': '计划读取',
    'cardbook': '事件簿',
    'auto': '日常',
    'daily': '日常',
    'craft': '制作',
    'wardrobe': '衣橱',
    'activities': '活动',
}
SECTIONS = (
    Section('cardbook', '事件簿'),
    Section('auto', '日常'),
    Section('craft', '制作'),
    Section('wardrobe', '衣橱', aggregate='wardrobe_palettes'),
    Section('activities', '活动', aggregate='activities'),
)

# Daily goals, in business order. All of them form the fixed plan.
GOALS = (
    TaskSpec(
        'cardbook', '事件簿', 'cardbook', seed=True, unit='派遣', next_run_count='next_completion',
        progress=(('total', 'target'), ('ordinary_sent', 'ordinary_limit'), ('confirmed_total', 'ordinary_limit')),
    ),
    TaskSpec('arena', '竞技场', 'auto', seed=True, progress=(('obtained', 'limit'),), unit='次'),
    TaskSpec('guild', '协会', 'auto', seed=True),
    TaskSpec('daily_activity', '活跃宝箱', 'auto', seed=True, progress=(('chests_claimed', 'chests_target'),), unit='宝箱'),
    TaskSpec('requested_shop', '指定商店', 'auto', seed=True),
    TaskSpec('red_dots', '红点奖励', 'auto', seed=True),
    TaskSpec('member_rewards', '会员奖励', 'auto', seed=True),
    TaskSpec('vigor_exchange', '元气兑换', 'auto', seed=True, progress=(('used', 'target'),), unit='次'),
    TaskSpec('pets', '宠物', 'auto', seed=True),
    TaskSpec('endorsement', '代言', 'auto', seed=True),
    TaskSpec('first_clears', '新关卡首通', 'auto', seed=True),
    TaskSpec('free_lottery', '免费抽取', 'auto', seed=True),
    TaskSpec('wardrobe_palettes', '衣橱色盘', 'wardrobe', seed=True),
    TaskSpec('activities', '活动', 'activities', seed=True),
)  # fmt: skip

# Concrete phase jobs. Craft has no aggregate goal, so its fixed jobs are part
# of the plan from the start; wardrobe and activity jobs replace their goal
# once the phase reports them.
JOBS = (
    TaskSpec('gold_purchase', '金币购买', 'craft', 'job', seed=True),
    TaskSpec('stamina_purchase', '体力购买', 'craft', 'job', seed=True),
    TaskSpec('heart_crafting', '心意制作', 'craft', 'job', seed=True),
    TaskSpec('main_story_crafting', '主线制作', 'craft', 'job', seed=True),
    TaskSpec('palette_unlock', '普通色盘', 'wardrobe', 'job'),
    TaskSpec('qianhuan', '千幻色盘', 'wardrobe', 'job'),
    # Limited-time activities exist only while the server opens them today
    # (demand 'window'): closed is hidden, a window that closes undone shows
    # as missed. 活动资源 grants the water and rings those activities use.
    TaskSpec('activity_resources', '活动资源', 'activities', 'job', demand='window'),
    TaskSpec('astrology', '占星物语', 'activities', 'job', demand='window'),
    TaskSpec('bear', '寻找贪吃熊', 'activities', 'job', demand='window'),
    TaskSpec('produce_activity', '免活复刻', 'activities', 'job'),
    TaskSpec('produce_common', '复刻通用物品', 'activities', 'job'),
    TaskSpec('ring_toss', '套圈', 'activities', 'job', demand='window'),
    TaskSpec('activity_rewards', '活动奖励', 'activities', 'job'),
    TaskSpec('penglai', '画中蓬莱', 'activities', 'job', demand='window'),
    TaskSpec('brand_atlas', '品牌图鉴', 'activities', 'job'),
    TaskSpec('brand_tower', '品牌挑战', 'activities', 'job'),
    TaskSpec('activity_discovery', '开放活动', 'activities', 'job', discovery=True),
)

# Sub-steps never prove their parent goal complete.
_STEPS = {
    'arena_daily': ('arena', '竞技场日常'), 'arena_climb': ('arena', '竞技场冲榜'),
    'guild_daily': ('guild', '协会日常'), 'guild_project_rewards': ('guild', '协会企划奖励'),
    'guild_salary': ('guild', '协会工资'),
    'daily_execution': ('daily_activity', '日常执行'),
    'daily_reward_collection': ('daily_activity', '活跃领奖'),
    'daily_reward_resume': ('daily_activity', '补领活跃奖励'),
    'friend_energy': ('red_dots', '好友体力'), 'friend_energy_resume': ('red_dots', '补领好友体力'),
    'timed_free_reward': ('red_dots', '时段免费奖励'), 'timed_sign': ('red_dots', '时段签到'),
    'timed_free': ('red_dots', '时段免费体力'), 'home_eat_income': ('red_dots', '家园收入'),
    'atlas_daily': ('red_dots', '图鉴奖励'), 'ui_coin_rewards': ('red_dots', '页面金币'),
    'ui_coin': ('red_dots', '页面金币'), 'free_rewards': ('red_dots', '免费奖励'),
    'free_reward': ('red_dots', '免费奖励'), 'monthly_sign': ('red_dots', '每日签到'),
    'social_topic_coins': ('red_dots', '话题金币'), 'vvanna_rewards': ('red_dots', '玩呐奖励'),
    'vvanna_quiz': ('red_dots', '玩呐百科'), 'discount_package_rewards': ('red_dots', '降价奖励'),
    'member_bonus': ('member_rewards', '会员礼盒'), 'month_card': ('member_rewards', '月卡奖励'),
    'big_month_card': ('member_rewards', '大月卡奖励'),
    'vip_daily_gift': ('member_rewards', '会员每日礼包'),
    'vip_weekly_gift': ('member_rewards', '会员每周礼包'),
    'lottery_sign': ('free_lottery', '追光签到'), 'lottery_coin': ('free_lottery', '追光金币'),
    'lottery_sign_coins': ('free_lottery', '追光金币'),
    'lottery_red_envelopes': ('free_lottery', '追光红包'),
    'lottery_red_envelope': ('free_lottery', '追光红包'),
    'blind_box_workshop': ('free_lottery', '盲盒工坊'), 'blind_box': ('free_lottery', '盲盒领奖'),
    'blind_box_draw': ('free_lottery', '盲盒抽取'), 'blind_box_draws': ('free_lottery', '盲盒抽取'),
    'endorsement_maintenance': ('endorsement', '代言维护'),
    'cardbook_roster_resume': ('cardbook', '事件簿续派'),
    'mail': ('red_dots', '邮件奖励'), 'mail_read': ('red_dots', '邮件读取'),
    'weekly_activity': ('red_dots', '每周活跃奖励'), 'achievement': ('red_dots', '成就奖励'),
    'fashion_atlas': ('red_dots', '时装图鉴'), 'craft_suit': ('red_dots', '套装奖励'),
    'chapter_reward': ('red_dots', '章节奖励'), 'social_topic_coin': ('red_dots', '话题金币'),
    'discount_package_reward': ('red_dots', '降价奖励'),
    'guild_project_reward': ('guild', '协会企划奖励'), 'vvanna_reward': ('red_dots', '玩呐奖励'),
    'share_reward': ('red_dots', '分享奖励'), 'pass_reward': ('red_dots', '通行证奖励'),
    'open_shop': ('activities', '店长经营'), 'stage_first_clear': ('first_clears', '首通推进'),
}  # fmt: skip
STEPS = tuple(
    TaskSpec(key, title, kind='step', parent=parent) for key, (parent, title) in _STEPS.items()
)

ALIASES = {
    'cardbook_daily': 'cardbook', 'pet_care': 'pets', 'shop_purchases': 'requested_shop',
    'shop_purchase': 'requested_shop', 'stage_first_clears': 'first_clears',
    'free_lottery_draws': 'free_lottery',
}  # fmt: skip

STATUS_GROUPS = {
    'complete': '''complete cleared daily_limit_complete ordinary_limit_complete
        all_unlocked_production_complete available_brand_levels_complete period_drawings_complete
        target_reached available_activity_rewards_complete already_claimed already_complete
        at_daily_limit at_weekly_limit highest_reward_score_reached no_remaining_sale_window
        daily_sale_complete all_slots_complete watering_complete weekly_quiz_complete
        activity_complete all_observed_tasks_complete no_due_free_draws
        already_confirmed current_goal_reconciled current_state_reconciled read_confirmed
        receipt_and_attachment_bit_cleared salary_collection_complete
        available_income_upgrades_complete available_quick_contracts_complete
        no_current_topic no_coin_reward no_free_draw configured_no_free no_unlocked_first_clears
        no_reward daily_trade_complete daily_sale_limit_complete outside_trade_window'''.split(),
    'closed': '''closed inactive season_closed level_locked not_member expired not_open
        outside_draw_window no_current_offer'''.split(),
    'waiting': '''waiting selling waiting_for_completion awaiting_completion daily_growth_complete
        waiting_for_growth waiting_for_stamina awaiting_daily_activity_resources awaiting_refresh
        awaiting_task_refresh waiting_open_window awaiting_map_transition not_ready
        no_dispatchable_tasks'''.split(),
    'blocked': '''blocked forbidden insufficient_currency insufficient_gold insufficient_items
        insufficient_materials insufficient_stamina insufficient_vigor insufficient_activity_money
        insufficient_recommended_score no_material_route no_sweep_route story_execution_unavailable
        no_native_shop_route quota_cap material_shop_mapping_unknown restriction_unknown
        restriction_unsupported score_insufficient entitlement_unavailable insufficient_resources'''.split(),
    'unsupported': ('unsupported', 'unsupported_activity_identity'),
    'failed': ('failed', 'rejected'),
    # The worker reports these when it cannot know the outcome; never success.
    'result_unavailable': '''timeout uncertain unknown unavailable pending_reconciliation
        session_refresh_required session_interrupted progress_mismatch progress_changed
        provider_unavailable entitlement_unknown'''.split(),
    'incomplete': '''confirmed activated progressed needs_craft available needs_claim
        settlement_available ready_to_open materials_ready partial step_finished prepared'''.split(),
    'not_started': ('not_executed', 'deferred', 'already_running'),
}


def _status_hook(raw, counts):
    if raw == 'attempts_exhausted':
        # The worker treats this as success. Only a counter that still shows
        # attempts left (remaining / remaining_attempts > 0) contradicts it.
        left = [counts[key] for key in ('remaining', 'remaining_attempts') if key in counts]
        return 'result_unavailable' if any(value > 0 for value in left) else 'complete'
    return None


COUNT_KEYS = '''
    obtained limit remaining scheme_slots displayed_rank target_rank ordinary_sent
    ordinary_limit special_sent total target running claimable next_completion
    support_used support_target inspiration_used inspiration_target project_weekly_used
    project_weekly_limit salary_weekly_used salary_weekly_limit salary_claimable salary_unknown
    salary_gold salary_gifts activity chests_claimed chests_target mandatory_publicity_complete
    products_checked available unavailable unknown unsupported sources_checked sources_required
    seven_day_available daily_claimable vip_unknown vip_claimable used eligible blocked
    missing_properties feed bath play toilet rounds_used rounds_done round_limit salary_ready
    contracts_ready barriers_ready income_upgrades_available actions_confirmed normal hard
    vvanna heart missing due_pools observed_pools owned affordable forbidden qianhuan_unlocked
    qianhuan_remaining qianhuan_needs_ordinary qianhuan_forbidden qianhuan_affordable open
    shop_remaining shop_score shop_next_settlement ring_remaining ring_score ring_target
    rewards_claimable recipes_remaining confirmed_actions sent_actions confirmed_spins
    confirmed_astrology_points confirmed_astrology_gold confirmed_score_rewards
    confirmed_attempt_rewards confirmed_attempts_gained
    confirmed_bear_buys confirmed_bear_sells confirmed_bear_score_gained
    confirmed_bear_score_spent confirmed_bear_jars
    confirmed_bear_welcome_rewards confirmed_bear_welcome_coupons
    confirmed_bear_store_purchases confirmed_bear_store_score_spent confirmed_bear_store_jars_spent
    confirmed_plantings confirmed_waterings confirmed_harvests confirmed_produce_outputs
    material_purchases material_sweeps fashion_crafts extensions suit_rewards
    confirmed_purchase_times count cost gain planned_count planned_cost planned_gain
    remaining_attempts bought_today purchase_limit stamina_remaining crafted dye_stages
    career_level ready_levels saved_attribute_outfits attempts_confirmed watered_today
    harvested slots mature_slots growing_slots seeds_remaining occupied_slots used_seeds
    remaining_water score unfinished_targets new_story_count requested_extra_attempts
    confirmed_throws remaining_fashions confirmed_fashions skipped_fashions owned_fashions
    unlocked_fashions blocked_by_ordinary_palettes forbidden_fashions
    unlockable_with_owned_materials water_tasks_completed ring_tasks_completed
    water_tasks_claimed ring_tasks_claimed running_tasks confirmed_total
'''.split()
UNITS = ('次', '项', '个', '份', '轮', '宝箱', '派遣', '积分', '件', '套', '步')

REASONS = {
    'insufficient_currency': '货币不足', 'insufficient_gold': '金币不足',
    'insufficient_resources': '可用资源不足',
    'insufficient_items': '道具不足', 'insufficient_materials': '材料不足',
    'insufficient_stamina': '体力不足', 'insufficient_vigor': '元气不足',
    'insufficient_activity_money': '活动货币不足',
    'insufficient_recommended_score': '推荐评分不足',
    'request_result_missing': '请求已发出，但未收到结果',
    'request_result_unavailable': '请求已发出，但未收到结果',
    'request_already_sent': '本轮已发出请求，尚未收到结果',
    'request_already_sent_this_run': '本轮已发出请求，尚未收到结果',
    'session_interrupted': '会话已中断', 'session_recovery_failed': '会话恢复失败，尚未执行',
    'session_unavailable': '当前会话不可用，尚未执行',
    'action_exception': '执行时发生异常，未收到成功结果',
    'unsupported': '发现活动，但尚无处理流程',
    'unsupported_activity_identity': '发现活动，但尚无匹配的处理流程',
    'state_unavailable': '服务器尚未提供该项状态',
    'ended_without_result': '执行已结束，但该项未返回结果',
    'worker_unavailable': '执行器未返回状态',
    'forbidden': '当前授权或服务器规则不允许执行',
    'no_material_route': '当前没有可用材料获取路径',
    'no_sweep_route': '当前没有可用扫荡路径',
    'story_execution_unavailable': '当前剧情步骤无法自动推进',
    'waiting_for_stamina': '等待体力恢复',
    'awaiting_daily_activity_resources': '等待日常活动资源',
    'waiting_for_completion': '等待服务器任务到期',
    'waiting_for_growth': '等待服务器生长完成',
    'daily_growth_complete': '今日成长已完成，明日继续',
    'waiting': '等待服务器计时完成',
    'selling': '等待营业结束', 'not_executed': '本轮尚未执行',
    'failure': '执行失败，未收到该项成功结果',
    'timeout': '执行超时，尚未收到结果',
    'runtime_lock': '账号正在由其他程序使用',
    'login_material': '读取登录材料失败',
    'connect': '连接服务器失败',
    'server_clock': '未取得服务器时间',
    'execute': '业务执行环节失败',
    'summary': '执行结果摘要未返回',
    'process': '执行进程异常退出',
    'process_start': '执行进程启动失败',
    'project_cwd': '无法访问项目目录',
    'invocation_scope': '本轮请求排除集合无效，未启动执行',
    'session_refresh_unavailable': '会话需要刷新，当前无法恢复',
    'result_state_unavailable': '服务器未返回该步骤的最终状态',
    'provider_unavailable': '该步骤所需的服务器数据不可用',
    'session_observations_unavailable': '当前连接未收到该步骤所需的数据',
    'activity_handler_catalog_missing': '发现活动，但尚无对应处理目录',
    'unsupported_activity': '发现活动，但尚无处理流程',
    'wardrobe_snapshot_missing': '服务器衣橱数据尚未完整返回',
    'social_directory_missing': '服务器社交目录尚未返回',
    'session_age_limit': '会话已到刷新时间',
    'cardbook_state_query_failed': '读取事件簿状态失败',
    'completed_observation_retained': '本日已确认完成；本次未返回新状态',
    'quota_cap': '今日次数已达上限',
}  # fmt: skip
SHORT_REASONS = {
    'insufficient_currency': '货币不足', 'insufficient_gold': '金币不足',
    'insufficient_resources': '资源不足',
    'insufficient_items': '道具不足', 'insufficient_materials': '材料不足',
    'insufficient_stamina': '体力不足', 'waiting_for_stamina': '等待体力',
    'insufficient_vigor': '元气不足', 'insufficient_activity_money': '活动币不足',
    'insufficient_recommended_score': '评分不足', 'forbidden': '未获授权',
    'no_material_route': '缺少材料路线', 'no_sweep_route': '缺少扫荡路线',
    'story_execution_unavailable': '剧情待推进',
    'daily_growth_complete': '今日已成长',
}  # fmt: skip
REQUEST_PROBLEMS = (
    'request_result_missing', 'request_result_unavailable', 'request_already_sent',
    'request_already_sent_this_run', 'ended_without_result', 'timeout', 'action_exception',
    'session_interrupted',
)  # fmt: skip

CATALOG = Catalog(
    project='yslzm',
    display_name='以闪亮之名',
    sections=SECTIONS,
    tasks=(*GOALS, *JOBS, *STEPS),
    phases=PHASES,
    phase_sections={'daily': 'auto'},
    aliases=ALIASES,
    status_groups=STATUS_GROUPS,
    status_hook=_status_hook,
    reasons=REASONS,
    short_reasons=SHORT_REASONS,
    request_problems=REQUEST_PROBLEMS,
    count_keys=COUNT_KEYS,
    units=UNITS,
    # Nothing more can happen before the next service day; the card counts
    # these as done today. daily_growth_complete: today's watering is done and
    # the plants grow on tomorrow. quota_cap: every attempt the game sells today
    # is bought (its cap is below the usual twelve).
    day_done_reasons=('daily_growth_complete', 'quota_cap'),
    # Harvested every round (收菜): done once this round's own job finished it.
    # pets: every eligible pet cared ('blocked' = only sleeping/sick pets left);
    # endorsement: income collected (its job reports as 代言维护, the worker
    # aliases endorsement to endorsement_maintenance); main story: available
    # stamina used up.
    round_tasks={
        'pets': ('blocked',),
        'endorsement': (),
        'endorsement_maintenance': (),
        'main_story_crafting': ('waiting_for_stamina', 'insufficient_stamina'),
    },
)
