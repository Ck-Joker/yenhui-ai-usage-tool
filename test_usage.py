import unittest
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
from unittest.mock import patch
import usage


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return [{'id': 'claude', 'name': 'Claude Code', 'windows': [
            {'label': '每週', 'remaining': 24, 'resetsAt': 10000}]}]

    def query(self, provider='claude', fetcher=None):
        return usage.scheduled_fetch(provider, fetcher or self.fetch,
                                     self.directory.name, lambda: self.now)

    def test_manual_wake_restart_share_deadline_and_original_timestamp(self):
        first = self.query()
        for moment in [1010, 1060, 1181, 1299]:
            self.now = moment
            cached = self.query()
            self.assertTrue(cached['cached'])
            self.assertEqual(cached['updatedAt'], first['updatedAt'])
            self.assertEqual(cached['nextAllowedAt'], 1300)
            self.assertLess(moment - cached['updatedAt'], cached['staleAfter'])
        self.assertEqual(self.calls, 1)
        self.now = 1300
        self.assertFalse(self.query()['cached'])
        self.assertEqual(self.calls, 2)

    def test_429_backoff_persists_until_success(self):
        def limited():
            self.calls += 1
            raise usage.RateLimitError()
        for delay in [600, 1200, 2400, 3600, 3600]:
            start = self.now
            result = self.query(fetcher=limited)
            self.assertTrue(result['rateLimited'])
            self.assertEqual(result['nextAllowedAt'], start + delay)
            self.now = start + delay - 1
            self.assertTrue(self.query()['cached'])
            self.now += 1
        self.assertEqual(self.calls, 5)
        self.assertEqual(self.query()['failures'], 0)
        self.now += 300
        self.assertEqual(self.query(fetcher=limited)['nextAllowedAt'], self.now + 600)

    def test_retry_after_larger_than_local_cap_is_respected(self):
        def limited():
            raise usage.RateLimitError(7200)
        self.assertEqual(self.query(fetcher=limited)['nextAllowedAt'], 8200)
        self.now = 8199
        self.query()
        self.assertEqual(self.calls, 0)

    def test_provider_schedules_are_independent(self):
        def limited():
            raise usage.RateLimitError()
        self.query(fetcher=limited)
        self.query('codex')
        self.now += 60
        self.assertFalse(self.query('codex')['cached'])
        self.assertTrue(self.query()['cached'])
        self.assertEqual(self.calls, 2)

    def test_network_failure_does_not_allow_rapid_retry(self):
        def offline():
            raise RuntimeError('FAKE_PRIVATE_VALUE')
        result = self.query(fetcher=offline)
        self.assertEqual(result['nextAllowedAt'], 1300)
        self.assertNotIn('FAKE_PRIVATE_VALUE', json.dumps(result))
        self.now += 10
        self.query()
        self.assertEqual(self.calls, 0)

    def test_interrupted_process_keeps_reservation(self):
        def interrupted():
            raise SystemExit()
        with self.assertRaises(SystemExit):
            self.query(fetcher=interrupted)
        self.now += 1
        self.assertTrue(self.query()['cached'])
        self.assertEqual(self.calls, 0)

    def test_invalid_or_unwritable_state_fails_closed(self):
        path = Path(self.directory.name) / 'claude.json'
        path.write_text('{broken')
        self.assertIsNotNone(self.query()['error'])
        self.assertEqual(self.calls, 0)
        path.write_text('null')
        self.assertIsNotNone(self.query()['error'])
        self.assertEqual(self.calls, 0)
        with patch('usage.write_state', side_effect=OSError()):
            self.assertIsNotNone(self.query('codex')['error'])
        self.assertEqual(self.calls, 0)

    def test_cache_permissions_and_no_credentials(self):
        self.query()
        root = Path(self.directory.name)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        for filename in ['claude.json', 'claude.lock']:
            self.assertEqual((root / filename).stat().st_mode & 0o777, 0o600)
        state = json.loads((root / 'claude.json').read_text())
        self.assertEqual(set(state), {'provider', 'cards', 'updatedAt', 'error', 'version',
                         'failures', 'nextAllowedAt', 'staleAfter', 'cached', 'rateLimited'})

    def test_simultaneous_processes_only_send_one_request(self):
        worker = '''import json, pathlib, sys, time, usage
directory = pathlib.Path(sys.argv[1])
def fetch():
    with (directory / 'calls').open('a') as output:
        output.write('request\\n')
    time.sleep(.3)
    return []
print(json.dumps(usage.scheduled_fetch('claude', fetch, directory)))
'''
        processes = [subprocess.Popen([sys.executable, '-c', worker, self.directory.name],
                     cwd=str(Path(usage.__file__).parent), stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE) for _ in range(4)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)['provider'], 'claude')
        self.assertEqual((Path(self.directory.name) / 'calls').read_text(), 'request\n')
        # 全新的第五個程序仍沿用前一程序的冷卻期限。
        subprocess.run([sys.executable, '-c', worker, self.directory.name],
                       cwd=str(Path(usage.__file__).parent), check=True, capture_output=True)
        self.assertEqual((Path(self.directory.name) / 'calls').read_text(), 'request\n')

    def test_retry_after_header_seconds_date_and_invalid(self):
        self.assertEqual(usage.retry_after_seconds('7200', 0), 7200)
        self.assertEqual(usage.retry_after_seconds('Thu, 01 Jan 1970 02:00:00 GMT', 0), 7200)
        self.assertEqual(usage.retry_after_seconds('Thu, 01 Jan 1970 00:00:00 GMT', 1), 0)
        for value in [None, 'bad', 'NaN', 'Infinity']:
            self.assertIsNone(usage.retry_after_seconds(value, 0))

    def test_live_fetch_429_maps_retry_header_without_exposing_body(self):
        error = urllib.error.HTTPError('https://api.anthropic.com/api/oauth/usage', 429,
                                       'FAKE_PRIVATE_VALUE', {'Retry-After': '7200'}, None)
        credentials = type('Result', (), {'returncode': 0, 'stdout': json.dumps({
            'claudeAiOauth': {'accessToken': 'FAKE_PRIVATE_VALUE'}}).encode()})()
        with patch('usage.subprocess.run', return_value=credentials), patch('usage.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = error
            result = self.query(fetcher=usage.fetch_claude)
        self.assertEqual(result['nextAllowedAt'], 8200)
        self.assertNotIn('FAKE_PRIVATE_VALUE', json.dumps(result))
        self.assertNotIn('FAKE_PRIVATE_VALUE', (Path(self.directory.name) / 'claude.json').read_text())


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.payload[:limit]


class ClaudeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 2_000_000
        self.keychain = {'accessToken': 'FAKE_PRIVATE_VALUE', 'expiresAt': (self.now - 10) * 1000,
                         'refreshToken': 'FAKE_PRIVATE_REFRESH',
                         'refreshTokenExpiresAt': (self.now + 86400) * 1000}
        self.cli_calls = []
        self.cli_effect = self.renew
        self.headers = []
        patches = [patch('usage.subprocess.run', side_effect=self.fake_run),
                   patch('usage.claude_path', return_value='/fake/bin/claude'),
                   patch('usage.urllib.request.build_opener', side_effect=self.opener),
                   patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'FAKE_PRIVATE_VALUE',
                                           'DYLD_LIBRARY_PATH': '/fake/pyinstaller'})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def renew(self):
        self.keychain.update(accessToken='FAKE_RENEWED_VALUE', expiresAt=(self.now + 28800) * 1000)

    def fake_run(self, args, **kwargs):
        if args[0] == '/usr/bin/security':
            stdout = json.dumps({'claudeAiOauth': self.keychain}).encode()
            return type('Result', (), {'returncode': 0, 'stdout': stdout})()
        self.cli_calls.append((args, kwargs))
        if self.cli_effect:
            self.cli_effect()
        return type('Result', (), {'returncode': 0})()

    def opener(self, *handlers):
        test = self
        class Opener:
            def open(self, request, timeout):
                test.headers.append(request.get_header('Authorization'))
                return FakeResponse({'five_hour': {'utilization': 7}})
        return Opener()

    def fetch(self):
        return usage.fetch_claude(self.directory.name, lambda: self.now)

    def record(self):
        return Path(self.directory.name) / 'claude-refresh.json'

    def test_expired_token_is_renewed_by_claude_code_with_clean_environment(self):
        cards = self.fetch()
        self.assertEqual(cards[0]['windows'][0]['remaining'], 93)
        self.assertEqual(len(self.cli_calls), 1)
        args, kwargs = self.cli_calls[0]
        self.assertEqual(args, ['/fake/bin/claude'] + usage.CLAUDE_REFRESH_ARGS)
        self.assertLessEqual(set(kwargs['env']), {'HOME', 'LANG', 'TERM', 'DISABLE_AUTOUPDATER',
                                                  'PATH', 'USER', 'LOGNAME', 'TMPDIR'})
        self.assertTrue(kwargs['env']['PATH'].startswith('/fake/bin:'))
        self.assertEqual(kwargs['timeout'], usage.CLAUDE_REFRESH_TIMEOUT)
        self.assertEqual(kwargs['cwd'], self.directory.name)
        for stream in ['stdin', 'stdout', 'stderr']:
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)
        self.assertEqual(self.headers, ['Bearer FAKE_RENEWED_VALUE'])
        self.assertEqual(self.record().stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.record().read_text()), {'version': 1, 'attemptedAt': self.now})
        self.assertNotIn('FAKE_', self.record().read_text())

    def test_valid_token_never_runs_claude_code(self):
        self.keychain['expiresAt'] = (self.now + 60) * 1000
        self.fetch()
        self.assertEqual(self.cli_calls, [])
        self.assertFalse(self.record().exists())
        self.assertEqual(self.headers, ['Bearer FAKE_PRIVATE_VALUE'])

    def test_failed_renewal_waits_for_cooldown(self):
        self.cli_effect = None
        for moment, calls in [(self.now, 1), (self.now + 1799, 1), (self.now + 1800, 2)]:
            self.now = moment
            with self.assertRaises(usage.UsageError) as raised:
                self.fetch()
            self.assertEqual(str(raised.exception), usage.CLAUDE_REFRESH_FAILED)
            self.assertIn('登入', str(raised.exception))
            self.assertEqual(len(self.cli_calls), calls)
        self.assertEqual(self.headers, [])

    def test_expired_refresh_token_requires_login_without_running_cli(self):
        self.keychain['refreshTokenExpiresAt'] = self.now * 1000
        with self.assertRaises(usage.UsageError) as raised:
            self.fetch()
        self.assertEqual(str(raised.exception), usage.CLAUDE_LOGIN_EXPIRED)
        self.assertEqual(self.cli_calls, [])
        self.assertFalse(self.record().exists())

    def test_missing_cli_explains_login_command(self):
        with patch('usage.claude_path', return_value=None), self.assertRaises(usage.UsageError) as raised:
            self.fetch()
        self.assertIn('claude auth login', str(raised.exception))
        self.assertEqual(self.cli_calls, [])

    def test_cli_timeout_or_launch_failure_keeps_reservation(self):
        for error in [subprocess.TimeoutExpired('claude', 20), OSError()]:
            def fail():
                raise error
            self.cli_effect = fail
            with self.assertRaises(usage.UsageError):
                self.fetch()
            self.assertEqual(json.loads(self.record().read_text())['attemptedAt'], self.now)
            self.now += usage.CLAUDE_REFRESH_COOLDOWN
        self.assertEqual(len(self.cli_calls), 2)

    def test_corrupt_record_is_replaced_before_one_attempt(self):
        self.record().write_text('{broken')
        self.fetch()
        self.assertEqual(len(self.cli_calls), 1)
        self.assertEqual(json.loads(self.record().read_text())['attemptedAt'], self.now)

    def test_scheduled_state_contract_and_secrets_unchanged(self):
        result = usage.scheduled_fetch('claude', self.fetch, self.directory.name, lambda: self.now)
        self.assertIsNone(result['error'])
        state = (Path(self.directory.name) / 'claude.json').read_text()
        self.assertEqual(set(json.loads(state)), {'provider', 'cards', 'updatedAt', 'error', 'version',
                         'failures', 'nextAllowedAt', 'staleAfter', 'cached', 'rateLimited'})
        self.assertNotIn('FAKE_', state + json.dumps(result))

    def test_claude_path_override_requires_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / 'claude'
            candidate.write_text('#!/bin/sh\n')
            patch.stopall()
            with patch.dict(os.environ, {'SUBSCRIPTION_PIN_CLAUDE': str(candidate)}):
                self.assertNotEqual(usage.claude_path(), str(candidate))
                candidate.chmod(0o700)
                self.assertEqual(usage.claude_path(), str(candidate))


class UsageTests(unittest.TestCase):
    def test_missing_recipient_credentials_never_falls_back_to_developer(self):
        result = type('Result', (), {'returncode': 44, 'stdout': b''})()
        with patch('usage.subprocess.run', return_value=result), patch('usage.urllib.request.build_opener') as network:
            data = usage.safe_fetch('claude', usage.fetch_claude)
        self.assertIn('先登入', data['error'])
        self.assertEqual(data['cards'], [])
        network.assert_not_called()

    def test_offline_self_check_does_not_read_accounts(self):
        with patch('usage.sys.argv', ['usage', '--self-check']), patch('usage.subprocess.run') as keychain, \
             patch('usage.scheduled_fetch') as network, patch('builtins.print') as output:
            usage.main()
        keychain.assert_not_called()
        network.assert_not_called()
        self.assertFalse(json.loads(output.call_args.args[0])['credentialsRead'])

    def test_remaining_and_bounds(self):
        self.assertEqual(usage.window('每週', 76, None)['remaining'], 24)
        self.assertEqual(usage.window('每週', 101, None)['remaining'], 0)
        self.assertEqual(usage.window('每週', 0, None)['remaining'], 100)

    def test_unknown_is_not_full(self):
        for value in [None, True, '12', float('nan'), float('inf'), -1]:
            self.assertIsNone(usage.window('每週', value, None))
        with self.assertRaises(usage.UsageError):
            usage.parse_claude({'five_hour': {'utilization': None}})

    def test_weekly_primary_and_bucket_precedence(self):
        data = {'rateLimits': {'primary': {'usedPercent': 99}},
                'rateLimitsByLimitId': {'codex': {'primary': {
                    'usedPercent': 8, 'windowDurationMins': 10080, 'resetsAt': 1790000792}}}}
        cards = usage.parse_codex(data)
        self.assertEqual(len(cards[0]['windows']), 1)
        self.assertEqual(cards[0]['windows'][0]['label'], '每週')
        self.assertEqual(cards[0]['windows'][0]['remaining'], 92)

    def test_legacy_fallback(self):
        cards = usage.parse_codex({'rateLimits': {'primary': {
            'usedPercent': 20, 'windowDurationMins': 300}}})
        self.assertEqual(cards[0]['windows'][0]['label'], '5 小時')

    def test_multiple_buckets(self):
        slot = {'primary': {'usedPercent': 10, 'windowDurationMins': 300}}
        cards = usage.parse_codex({'rateLimitsByLimitId': {'spark': slot, 'codex': slot}})
        self.assertEqual([c['id'] for c in cards], ['codex', 'spark'])

    def test_dates_and_missing(self):
        self.assertEqual(usage.epoch('2026-09-15T00:00:00+00:00'), 1789430400)
        self.assertEqual(usage.epoch('2026-09-15T08:00:00+08:00'), 1789430400)
        for value in [None, 'bad', '2026-09-15T00:00:00', 0, True]:
            self.assertIsNone(usage.epoch(value))

    def test_no_sensitive_exception(self):
        def fail():
            raise RuntimeError('FAKE_PRIVATE_VALUE')
        result = usage.safe_fetch('test', fail)
        self.assertNotIn('FAKE_PRIVATE_VALUE', str(result))
        self.assertEqual(result['cards'], [])
        self.assertIsNone(result['updatedAt'])

    def test_redirect_refused(self):
        with self.assertRaises(usage.UsageError):
            usage.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')

    def test_claude_nullable_model_windows(self):
        cards = usage.parse_claude({'five_hour': {'utilization': 17, 'resets_at': None},
                                   'seven_day': {'utilization': 76}, 'seven_day_sonnet': None})
        self.assertEqual([r['remaining'] for r in cards[0]['windows']], [83, 24])

    def test_missing_codex_rejected(self):
        for data in [{}, {'rateLimits': None}, {'rateLimitsByLimitId': {'codex': None}}]:
            with self.assertRaises(usage.UsageError):
                usage.parse_codex(data)

    def test_fable_uses_named_scope_not_internal_code(self):
        result = {'five_hour': {'utilization': 20}, 'seven_day': {'utilization': 76},
                  'nimbus_quill': {'utilization': 0}, 'limits': [{
                      'kind': 'weekly_scoped', 'percent': 100,
                      'resets_at': '2026-09-15T06:00:00+00:00',
                      'scope': {'model': {'id': None, 'display_name': 'Fable'}, 'surface': None}}]}
        rows = usage.parse_claude(result)[0]['windows']
        self.assertEqual([r['remaining'] for r in rows], [80, 24, 0])
        self.assertEqual(rows[-1]['label'], 'Fable 每週')
        self.assertEqual(rows[-1]['resetsAt'], 1789452000)

    def test_fable_missing_is_not_invented(self):
        rows = usage.parse_claude({'five_hour': {'utilization': 20}, 'limits': None})[0]['windows']
        self.assertEqual(len(rows), 1)

    def test_fable_wrong_scope_and_null_rejected(self):
        result = {'five_hour': {'utilization': 20}, 'limits': [None,
                  {'kind': 'weekly_scoped', 'percent': 5, 'scope': None},
                  {'kind': 'weekly_scoped', 'percent': 5, 'scope': {
                      'model': {'display_name': 'Fable'}, 'surface': 'other'}}]}
        self.assertEqual(len(usage.parse_claude(result)[0]['windows']), 1)


if __name__ == '__main__':
    unittest.main()
