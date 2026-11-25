from autocontrol.metrics import MetricsStore


def phase(day, phase='daily', **fields):
    row = {'project': 'p', 'round_id': 'r', 'service_day': day, 'phase': phase, 'mode': 'execute',
           'duration_ms': 1000, 'exit_code': 0, 'execution': 'complete', 'timed_out': False, 'busy': False,
           'actions_sent': 10, 'actions_confirmed': 10, 'actions_unconfirmed': 0, 'events': 3,
           'events_rejected': 0, 'protocol_violations': 0, 'result_valid': True}  # fmt: skip
    row.update(fields)
    return row


def test_indicators_and_thresholds(tmp_path):
    store = MetricsStore(tmp_path / 'm.sqlite3')
    for index in range(18):
        store.record_phase(phase('2026-11-17', duration_ms=1000 + index))
    store.record_phase(
        phase('2026-11-17', execution='timeout', exit_code=None, timed_out=True, duration_ms=60000)
    )
    store.record_phase(
        phase(
            '2026-11-17',
            execution='failed',
            exit_code=1,
            failure_stage='connect',
            actions_unconfirmed=1,
        )
    )
    store.record_phase(phase('2026-11-17', busy=True, execution='busy', exit_code=2))  # not counted
    store.record_phase(phase('2026-10-01'))  # outside the window
    for code in (0, 1, 1, 1):
        store.record_round({'project': 'p', 'service_day': '2026-11-17', 'mode': 'execute', 'code': code,
                            'status': 'finished', 'delivery_status': 'sent', 'finished_at': f'2026-11-17T0{code}:00'})  # fmt: skip
    store.record_task_days('p', '2026-11-16', {'a': 'complete', 'b': 'failed', 'c': 'closed'}, 'x')
    store.record_task_days('p', '2026-11-17', {'a': 'complete', 'b': 'complete'}, 'x')
    store.record_task_days('p', '2026-11-17', {'b': 'failed'}, 'y')  # last state of the day wins
    report = store.report('p', days=7, today='2026-11-17', phase_timeout=60)
    daily = report['phases']['daily']
    assert daily['runs'] == 20 and daily['success_rate'] == 0.9 and daily['timeout_rate'] == 0.05
    assert daily['failure_stages'] == {'timeout': 1, 'connect': 1} or daily['failure_stages'] == {
        'connect': 1
    }
    assert daily['p95_ms'] == 1017 or daily['p95_ms'] >= 1017
    # Today's unfinished state is not a missed day yet.
    assert report['tasks'] == {'a': {'days': 2, 'complete': 2, 'completion_rate': 1.0},
                               'b': {'days': 1, 'complete': 0, 'completion_rate': 0.0,
                                     'open_today': True}}  # fmt: skip
    checks = {check['indicator']: check['ok'] for check in report['checks']}
    assert checks['phase_success_rate'] is False and checks['timeout_rate'] is False
    assert checks['protocol_violations'] is True and checks['delivery_success_rate'] is True
    assert (
        report['indicators']['consecutive_failed_rounds'] == 3
        and checks['consecutive_failed_rounds'] is False
    )
    assert report['healthy'] is False


def test_empty_store_reports_no_data(tmp_path):
    report = MetricsStore(tmp_path / 'm.sqlite3').report('p', today='2026-11-17')
    assert report['rounds'] == 0 and report['healthy'] is True
    assert all(
        check['ok'] is None or check['indicator'] == 'consecutive_failed_rounds'
        for check in report['checks']
    )


def test_the_unconfirmed_share_needs_receipt_counts(tmp_path):
    store = MetricsStore(tmp_path / 'm.sqlite3')
    # A worker that counts only what it sent: the share is unknown, not 0.
    store.record_phase(phase('2026-11-17', actions_confirmed=None, actions_unconfirmed=None))
    report = store.report('p', days=7, today='2026-11-17', phase_timeout=60)
    daily = report['phases']['daily']
    assert daily['unconfirmed_action_rate'] is None and daily['actions_unconfirmed'] is None
    assert daily['actions_confirmed'] is None
    checks = {check['indicator']: check['ok'] for check in report['checks']}
    assert checks['unconfirmed_action_rate'] is None
    # Confirmations without an unconfirmed count: the rest of what was sent.
    store.record_phase(phase('2026-11-17', actions_confirmed=8, actions_unconfirmed=None))
    daily = store.report('p', days=7, today='2026-11-17', phase_timeout=60)['phases']['daily']
    assert daily['actions_unconfirmed'] == 2 and daily['unconfirmed_action_rate'] == 0.2


def day(status, done, streak=None):
    return {'status': status, 'done': done, 'fail_streak': streak}


def test_completion_follows_the_card_and_streaks_name_the_failing_tasks(tmp_path):
    store = MetricsStore(tmp_path / 'm.sqlite3')
    store.record_task_days('p', '2026-11-16', {
        'dispatch': day('waiting', 1),  # done for today on the card
        'shop': day('unsupported', -1),  # not counted
        'arena': day('failed', 0, 1),
        'boss': day('failed', 0, 4),
    }, 'x')  # fmt: skip
    store.record_task_days('p', '2026-11-17', {
        'dispatch': day('waiting', 0), 'arena': day('failed', 0, 2), 'boss': day('complete', 1),
    }, 'y')  # fmt: skip
    report = store.report('p', days=7, today='2026-11-17')
    assert report['tasks']['dispatch'] == {
        'days': 1, 'complete': 1, 'completion_rate': 1.0, 'open_today': True,
    }  # fmt: skip
    assert 'shop' not in report['tasks']
    assert report['tasks']['boss'] == {'days': 2, 'complete': 1, 'completion_rate': 0.5}
    # Only a task failing now counts; boss recovered today.
    assert report['task_streaks'] == {'arena': 2}
    checks = {check['indicator']: check['ok'] for check in report['checks']}
    assert report['indicators']['task_failure_streak'] == 2 and checks['task_failure_streak']
    store.record_task_days('p', '2026-11-17', {'arena': day('failed', 0, 3)}, 'z')
    report = store.report('p', days=7, today='2026-11-17')
    assert report['task_streaks'] == {'arena': 3}
    checks = {check['indicator']: check['ok'] for check in report['checks']}
    assert checks['task_failure_streak'] is False and report['healthy'] is False


def test_an_older_store_gains_the_new_task_columns(tmp_path):
    import sqlite3

    path = tmp_path / 'm.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute(
            'CREATE TABLE task_days (project TEXT NOT NULL, service_day TEXT NOT NULL, '
            'task_id TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL, '
            'PRIMARY KEY (project, service_day, task_id))'
        )
        db.execute("INSERT INTO task_days VALUES ('p', '2026-11-16', 'a', 'unsupported', 'x')")
        db.execute("INSERT INTO task_days VALUES ('p', '2026-11-16', 'b', 'waiting', 'x')")
    store = MetricsStore(path)
    store.record_task_days('p', '2026-11-17', {'b': day('waiting', 1)}, 'y')
    report = store.report('p', days=7, today='2026-11-17')
    # Old rows keep the old reading (only complete counts, no flow is skipped).
    assert report['tasks'] == {'b': {'days': 2, 'complete': 1, 'completion_rate': 0.5}}
