#!/usr/bin/env python3
"""A protocol v1 demo worker: a template for onboarding a script to autocontrol.

It fakes a "server" in ``state.json`` (next to this file) so it runs offline.
Real scripts read their live service instead; the protocol usage is the same:

* every run re-reads the current state (never the console's display);
* ``observe`` mode (phase ``status``) only reads and reports a full plan;
* ``execute`` mode reports progress and a final state for each task;
* the last stdout line is the result; the exit code follows the protocol.
"""

import json
from pathlib import Path
import sys
import time

# Real scripts vendor sdk/python/autocontrol_worker.py next to themselves.
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / 'sdk' / 'python')]
from autocontrol_worker import EXIT_CONFIG, Worker  # noqa: E402

STATE = Path(__file__).with_name('state.json')


def load(day):
    try:
        data = json.loads(STATE.read_text())
    except (OSError, ValueError):
        data = {}
    if data.get('day') != day:  # the fake server resets daily
        data = {'day': day, 'signin': False, 'mail': 2, 'energy': 0, 'coins': 10}
    return data


def states(server, now):
    return {
        'signin': {'status': 'complete' if server['signin'] else 'not_started'},
        'mail': {
            'status': 'complete' if not server['mail'] else 'incomplete',
            'counts': {'claimed': 2 - server['mail']},
        },
        'energy': (
            {'status': 'complete', 'current': 3, 'total': 3}
            if server['energy'] >= 3
            else {
                'status': 'waiting',
                'current': server['energy'],
                'total': 3,
                'next_run_at': now + 3600,
            }
            if server['energy']
            else {'status': 'not_started', 'current': 0, 'total': 3}
        ),
        # A window activity: not open today (it would report waiting + not_open
        # + next_run_at when it opens later today).
        'limited_event': {'status': 'closed', 'reason': 'not_open'},
        # Open activities no module handles today (none in this fake server).
        'open_activities': {'status': 'complete', 'counts': {'unsupported': 0}},
        'weekly_pack': {'status': 'blocked', 'reason': 'insufficient_currency'}
        if server['coins'] < 50
        else {'status': 'not_started'},
    }


def main():
    worker = Worker()
    if worker.phase not in ('status', 'daily', 'shop'):
        worker.finish('failed', {}, failure_stage='config')
        return EXIT_CONFIG
    server = load(worker.service_day)
    worker.count('reads')
    now = time.time()
    current = states(server, now)
    plan = {key: value for key, value in current.items()
            if worker.phase == 'status' or (key == 'weekly_pack') == (worker.phase == 'shop')}  # fmt: skip
    worker.plan(plan)
    if worker.observe:
        worker.count('actions_sent', 0)
        worker.finish('complete', current, next_wake=[now + 3600])
        return 0

    if worker.phase == 'daily':
        if not server['signin']:
            with worker.task('signin') as run:
                server['signin'] = True
                worker.count('actions_sent').count('actions_confirmed')
                # Report the state read back from the server, never assumed.
                run.finish('complete' if server['signin'] else 'incomplete')
        if server['mail']:
            worker.started('mail')
            worker.finished('mail_read', 'complete')
            server['mail'] = 0
            worker.count('actions_sent').count('actions_confirmed')
            worker.finished('mail', counts={'claimed': 2})
        if server['energy'] < 1:
            worker.started('energy')
            server['energy'] = 1  # one of three time windows claimed
            worker.count('actions_sent').count('actions_confirmed')
            worker.progress('energy', current=1, total=3)
    STATE.write_text(json.dumps(server))
    final = states(server, now)
    tasks = {
        key: value
        for key, value in final.items()
        if (key == 'weekly_pack') == (worker.phase == 'shop')
    }
    outcome = (
        'complete'
        if all(t['status'] in ('complete', 'closed') for t in tasks.values())
        else 'partial'
    )
    if (
        all(t['status'] in ('complete', 'closed', 'waiting') for t in tasks.values())
        and outcome != 'complete'
    ):
        outcome = 'waiting'
    worker.finish(outcome, tasks, next_wake=[now + 3600] if worker.phase == 'daily' else ())
    return worker.exit_code(outcome)


if __name__ == '__main__':
    raise SystemExit(main())
