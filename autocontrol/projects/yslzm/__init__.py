"""yslzm (以闪亮之名) daily automation: worker phases, results and checklist."""

from __future__ import annotations

from datetime import datetime

from ..base import Project
from ... import storage
from ...worker import run_streamed
from .catalog import CATALOG, SCHEDULE
from .phases import (
    BEIJING,
    DEFAULT_PRIVATE,
    DEFAULT_PROJECT,
    PROGRESS_ENV,
    command_line,
    latest_summary,
    result_exit_code,
    run_round,
    run_scheduled,
)

# Red-dot sources in the auto phase's final status read, and the step that
# claims each one.
SETTLED_SOURCES = {
    'lottery_sign_coins': 'lottery_sign_coins',  # 追光金币
    'lottery_sign_ins': 'lottery_sign',  # 追光签到
    'ui_coin_buttons': 'ui_coin_rewards',  # 页面金币
    'vvanna_rewards': 'vvanna_reward',  # 玩呐奖励
    'vvanna_quiz': 'vvanna_quiz',  # 玩呐百科
    'blind_box_workshop': 'blind_box',  # 盲盒领奖
    'monthly_sign': 'monthly_sign',  # 每日签到
    'discount_packages': 'discount_package_reward',  # 降价奖励
}
_NOTHING_LEFT = ('claimable', 'claimable_count', 'unknown', 'unsupported')


def settled_steps(record) -> list:
    """Steps the auto phase's final read shows done for now.

    A claim reports ``confirmed`` (未完成 on the card) because the job does
    not re-read its source afterwards, so only the next round's empty pass
    (``cleared``) finished the step, hours later. The phase's final status
    read, taken after both reward passes, already shows it: a reward source
    with nothing left to claim, the arena goal complete (on Monday the daily
    job only saves the four schemes and reports ``prepared``; the climb takes
    the attempts), or both association quotas used. Anything partial,
    unknown or unsupported stays as the worker reported it.
    """
    if type(record) is not dict or record.get('command') != 'auto':
        return []
    updates = []
    details = record.get('feature_details')
    red = details.get('red_dot_sources') if type(details) is dict else None
    sources = red.get('sources') if type(red) is dict else None
    if type(sources) is dict:
        for name, step in SETTLED_SOURCES.items():
            row = sources.get(name)
            if (
                type(row) is dict
                and row.get('status') in ('cleared', 'complete')
                and all(_zero(row, field) for field in _NOTHING_LEFT)
            ):
                updates.append((step, {'status': 'cleared'}))
    checklist = record.get('full_checklist_summary')
    goals = checklist.get('goals') if type(checklist) is dict else None
    if type(goals) is dict:
        arena = goals.get('arena')
        if type(arena) is dict and arena.get('status') == 'complete':
            updates.append(('arena_daily', {'status': 'cleared'}))
        guild = goals.get('guild')
        counts = guild.get('counts') if type(guild) is dict else None
        if (
            type(counts) is dict
            and _reached(counts, 'support', 3)
            and _reached(counts, 'inspiration', 5)
        ):
            updates.append(('guild_daily', {'status': 'cleared'}))
    return updates


def _zero(row, field) -> bool:
    return field not in row or (type(row[field]) is int and row[field] == 0)


def _reached(counts, name, least) -> bool:
    used, target = counts.get(name + '_used'), counts.get(name + '_target')
    return type(used) is int and type(target) is int and used >= target >= least


class YslzmProject(Project):
    name = 'yslzm'
    catalog = CATALOG
    schedule = SCHEDULE
    # cardbook → auto → craft → wardrobe → activities: claim and redispatch the
    # event book, finish dailies, buy and craft, unlock palettes, and finally
    # use the day's activity resources in a fresh session.
    phases = ('cardbook', 'auto', 'craft', 'wardrobe', 'activities')
    commands = ('auto', 'daily', 'cardbook', 'craft', 'wardrobe', 'activities')
    default_workdir = DEFAULT_PROJECT
    default_private = DEFAULT_PRIVATE
    # Same card identity as the earlier tools/daily_board publisher, so today's
    # existing card keeps being updated instead of a second one being created.
    uuid_namespace = 'yslzm-daily-board'
    # Bounded execution (admission B4): historical phases take up to ~14 min.
    phase_timeout = 1800.0

    def new_run_scope(self):
        # Requests sent this round without a result; RAM only, never persisted.
        return {'uncertain_targets': []}

    def observe(self, day, *, on_event, timeout, round_id=None):
        started = datetime.now(BEIJING)
        result = run_streamed(
            command_line('status', day, python=self.python),
            cwd=self.workdir,
            on_event=on_event,
            progress_env=PROGRESS_ENV,
            timeout=timeout,
            on_stderr=lambda data: storage.keep_stderr(
                self.private, started, 'status-observe', data
            ),
        )
        return result.returncode, latest_summary(result.stdout) or {}

    def run_phase(self, phase, day, *, run_scope, on_event, round_id=None):
        record, _ = run_scheduled(
            phase,
            day,
            project=self.workdir,
            private=self.private,
            run_scope=run_scope,
            on_event=on_event,
            python=self.python,
            timeout=self.phase_timeout,
        )
        return record

    def shares_session(self):
        # Opt-in until proven: ``{"shared_session": true}`` in autocontrol.json.
        return self.settings().get('shared_session') is True

    def run_round(self, phases, day, *, run_scope, on_event, on_phase, round_id=None):
        return run_round(
            phases,
            day,
            project=self.workdir,
            private=self.private,
            run_scope=run_scope,
            on_event=on_event,
            on_phase=on_phase,
            python=self.python,
            timeout=self.phase_timeout,
        )

    def result_facts(self, record):
        phase = record.get('command')
        updates = []
        jobs = record.get('policy_jobs')
        if isinstance(jobs, dict):
            for key, source in jobs.items():
                spec = self.catalog.tasks.get(key)
                if (
                    spec is not None
                    and spec.kind == 'job'
                    and spec.section == phase
                    and isinstance(source, dict)
                ):
                    updates.append((key, source))
        if phase == 'cardbook':
            if isinstance(record.get('cardbook_status'), str):
                updates.append(('cardbook', {**record, 'status': record['cardbook_status']}))
            if record.get('failure_stage') in ('process', 'summary'):
                updates.append(
                    ('cardbook', {'status': 'result_unavailable', 'reason': 'ended_without_result'})
                )
        updates.extend(settled_steps(record))
        checklist = record.get('full_checklist_summary')
        return {
            'phase': phase,
            'game_day': record.get('game_day'),
            'goals': checklist.get('goals') if isinstance(checklist, dict) else None,
            'updates': updates,
            'execution': record.get('execution'),
            'reason': record.get('failure_stage'),
            'finished_at': record.get('finished_at'),
        }

    def exit_code(self, record):
        return result_exit_code(record)

    def conformance(self):
        """Static admission checks against the yslzm checkout (adapter mode)."""
        from .conformance import check

        return check(self.workdir, phase_timeout=self.phase_timeout)

    def phase_metrics(self, record):
        """Approximate standard metrics from the native yslzm record (adapter mode)."""
        confirmed = 0
        for group in ('confirmed_reward_counts', 'confirmed_cardbook_counts', 'confirmed_arena_counts',
                      'confirmed_activity_counts', 'confirmed_pet_counts'):  # fmt: skip
            values = record.get(group)
            if isinstance(values, dict):
                confirmed += sum(v for v in values.values() if type(v) is int and v >= 0)
        jobs = record.get('policy_jobs')
        if isinstance(jobs, dict):
            confirmed += sum(
                job['confirmed_actions']
                for job in jobs.values()
                if isinstance(job, dict) and type(job.get('confirmed_actions')) is int
            )
        issues = [row for row in record.get('action_issues') or () if isinstance(row, dict)]
        metrics = {
            'actions_confirmed': confirmed,
            'actions_failed': sum(
                row.get('count', 1) for row in issues if row.get('status') == 'failed'
            ),
            'actions_unconfirmed': sum(
                row.get('count', 1) for row in issues if row.get('status') == 'uncertain'
            ),
        }
        if type(record.get('sent_actions')) is int:
            metrics['actions_sent'] = record['sent_actions']
        if type(record.get('session_logins')) is int:
            metrics['sessions'] = record['session_logins']  # known only for shared rounds
        return metrics
