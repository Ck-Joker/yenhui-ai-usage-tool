#!/usr/bin/env python3
"""只讀訂閱額度；stdout 僅輸出白名單用量欄位，憑證不離開記憶體。"""
import concurrent.futures
import datetime as dt
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import ssl
import tempfile
import time
import urllib.error
import urllib.request


class UsageError(Exception):
    pass


class RateLimitError(UsageError):
    def __init__(self, retry_after=None):
        super().__init__('Claude 查詢已限流，冷卻後自動重試')
        self.retry_after = retry_after


def retry_after_seconds(value, now):
    """只解析伺服器要求的等待時間，不儲存原始 header。"""
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
            seconds = parsed.timestamp() - now if parsed.tzinfo else None
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if number(seconds) is not None else None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def epoch(value):
    if number(value) is not None:
        return float(value) if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def window(label, used, reset):
    used = number(used)
    if used is None or used < 0:
        return None
    return {'label': label, 'remaining': max(0, min(100, 100 - used)),
            'resetsAt': epoch(reset)}


def duration_label(value, fallback):
    if value == 10080:
        return '每週'
    if value == 300:
        return '5 小時'
    if number(value) is not None and value > 0:
        return ('%g 小時' % (value / 60)) if value % 60 == 0 else ('%g 分鐘' % value)
    return fallback


def parse_codex(result):
    buckets = result.get('rateLimitsByLimitId')
    if not isinstance(buckets, dict) or not buckets:
        legacy = result.get('rateLimits')
        buckets = {'codex': legacy} if isinstance(legacy, dict) else {}
    cards = []
    for key in sorted(buckets, key=lambda k: (k != 'codex', k)):
        bucket = buckets[key]
        if not isinstance(bucket, dict):
            continue
        rows = []
        for slot, fallback in [('primary', '主要額度'), ('secondary', '次要額度')]:
            raw = bucket.get(slot)
            if isinstance(raw, dict):
                row = window(duration_label(raw.get('windowDurationMins'), fallback),
                             raw.get('usedPercent'), raw.get('resetsAt'))
                if row:
                    rows.append(row)
        name = 'Codex' if key == 'codex' else bucket.get('limitName') or key
        cards.append({'id': key, 'name': str(name)[:80], 'windows': rows})
    if not cards or not any(c['windows'] for c in cards):
        raise UsageError('尚未提供訂閱額度，請確認 Codex 已登入訂閱帳號')
    return cards


def parse_claude(result):
    rows = []
    names = [('five_hour', '5 小時'), ('seven_day', '每週'),
             ('seven_day_sonnet', 'Sonnet 每週'), ('seven_day_opus', 'Opus 每週')]
    for key, label in names:
        raw = result.get(key)
        if isinstance(raw, dict):
            row = window(label, raw.get('utilization'), raw.get('resets_at'))
            if row:
                rows.append(row)
    # 新版 Claude 用具名 scope 回傳 Fable；不可猜測內部代號對應的模型。
    limits = result.get('limits')
    for raw in limits if isinstance(limits, list) else []:
        if not isinstance(raw, dict) or raw.get('kind') != 'weekly_scoped':
            continue
        scope = raw.get('scope')
        if not isinstance(scope, dict) or scope.get('surface') is not None:
            continue
        model = scope.get('model')
        if not isinstance(model, dict) or model.get('display_name') != 'Fable':
            continue
        row = window('Fable 每週', raw.get('percent'), raw.get('resets_at'))
        if row:
            rows = [r for r in rows if r['label'] != 'Fable 每週']
            rows.append(row)
    if not rows:
        raise UsageError('尚未提供訂閱額度，請確認 Claude Code 已登入訂閱帳號')
    return [{'id': 'claude', 'name': 'Claude Code', 'windows': rows}]


def codex_path():
    candidates = [os.environ.get('SUBSCRIPTION_PIN_CODEX', ''),
                  '/Applications/ChatGPT.app/Contents/Resources/codex',
                  '/Applications/Codex.app/Contents/Resources/codex',
                  str(Path.home() / 'Applications/ChatGPT.app/Contents/Resources/codex'),
                  str(Path.home() / 'Applications/Codex.app/Contents/Resources/codex'),
                  shutil.which('codex') or '', '/opt/homebrew/bin/codex',
                  '/usr/local/bin/codex', str(Path.home() / '.local/bin/codex')]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise UsageError('找不到 Codex，請先安裝並登入 Codex')


def fetch_codex():
    p = subprocess.Popen([codex_path(), 'app-server', '--stdio'], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         cwd=str(Path.home()))
    def send(obj):
        p.stdin.write((json.dumps(obj) + '\n').encode())
        p.stdin.flush()
    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'subscription_pin', 'version': '1.0.0'}}})
        deadline, buffer = time.monotonic() + 25, b''
        while time.monotonic() < deadline:
            if not select.select([p.stdout], [], [], .5)[0]:
                continue
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                raise UsageError('Codex 連線結束，請開啟 Codex 檢查登入狀態')
            buffer += chunk
            if len(buffer) > 2_000_000:
                raise UsageError('Codex 回應格式不符')
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get('id') in (1, 2) and 'error' in msg:
                    raise UsageError('Codex 查詢失敗，請在 Codex 檢查登入與網路')
                if msg.get('id') == 1:
                    send({'method': 'initialized'})
                    send({'id': 2, 'method': 'account/rateLimits/read'})
                elif msg.get('id') == 2:
                    return parse_codex(msg.get('result', {}))
        raise UsageError('Codex 查詢逾時，下次會自動重試')
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        p.stdin.close()
        p.stdout.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UsageError('Claude 用量端點已變更，請更新此程式')


CLAUDE_REFRESH_COOLDOWN = 1800
CLAUDE_REFRESH_TIMEOUT = 20
# 只為了讓 Claude Code 自行換發登入：停用工具、MCP、設定檔與 session，請求量約數百 token。
CLAUDE_REFRESH_ARGS = ['-p', 'Reply with exactly: ok', '--model', 'haiku',
                       '--no-session-persistence', '--tools', '', '--strict-mcp-config',
                       '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
                       '--disable-slash-commands', '--system-prompt', 'Reply with exactly: ok']
CLAUDE_LOGIN_EXPIRED = 'Claude 登入已到期，請在終端機執行 claude auth login'
CLAUDE_REFRESH_FAILED = 'Claude 登入自動更新未成功，約 30 分鐘後再試，或在終端機執行 claude auth login'


def claude_path():
    candidates = [os.environ.get('SUBSCRIPTION_PIN_CLAUDE', ''),
                  str(Path.home() / '.local/bin/claude'), shutil.which('claude') or '',
                  '/opt/homebrew/bin/claude', '/usr/local/bin/claude',
                  str(Path.home() / '.claude/local/claude')]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def read_claude_auth():
    result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s',
                             'Claude Code-credentials', '-w'], capture_output=True, timeout=15)
    if result.returncode:
        raise UsageError('無法讀取 Claude 登入，請先登入 Claude Code 並允許鑰匙圈存取')
    try:
        auth = json.loads(result.stdout).get('claudeAiOauth', {})
        token = auth.get('accessToken')
    except (ValueError, AttributeError):
        raise UsageError('Claude 登入格式不符，請重新登入 Claude Code')
    if not isinstance(token, str) or not token:
        raise UsageError('請先在 Claude Code 登入訂閱帳號')
    return auth


def claude_expired(auth, now):
    expiry = number(auth.get('expiresAt'))
    return bool(expiry) and expiry / 1000 <= now


def refresh_claude_login(auth, state_dir=None, clock=time.time):
    """access token 到期時，請官方 Claude Code 自行換發；本程式不使用也不改寫 refresh token。"""
    refresh_expiry = number(auth.get('refreshTokenExpiresAt'))
    if refresh_expiry and refresh_expiry / 1000 <= clock():
        raise UsageError(CLAUDE_LOGIN_EXPIRED)
    path = claude_path()
    if not path:
        raise UsageError('Claude 登入已到期，找不到 claude 指令，請安裝 Claude Code 後執行 claude auth login')
    directory = Path(state_dir) if state_dir is not None else STATE_DIR
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = directory / 'claude-refresh.json'
    try:
        attempted = number(json.loads(record.read_text()).get('attemptedAt'))
    except (OSError, ValueError, AttributeError):
        attempted = None
    if attempted is not None and 0 <= clock() - attempted < CLAUDE_REFRESH_COOLDOWN:
        raise UsageError(CLAUDE_REFRESH_FAILED)
    # 先保存嘗試時間；程序中斷或寫入失敗都不會連續呼叫 Claude Code。
    write_state(record, {'version': 1, 'attemptedAt': clock()})
    env = {'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8', 'TERM': 'dumb', 'DISABLE_AUTOUPDATER': '1',
           'PATH': ':'.join([os.path.dirname(path), '/opt/homebrew/bin', '/usr/local/bin',
                             '/usr/bin', '/bin', '/usr/sbin', '/sbin'])}
    for key in ('USER', 'LOGNAME', 'TMPDIR'):
        if os.environ.get(key):
            env[key] = os.environ[key]
    try:
        subprocess.run([path] + CLAUDE_REFRESH_ARGS, cwd=str(directory), env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=CLAUDE_REFRESH_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        pass
    # 以鑰匙圈實際期限判斷，不依賴 CLI 結束碼。
    refreshed = read_claude_auth()
    if claude_expired(refreshed, clock()):
        raise UsageError(CLAUDE_REFRESH_FAILED)
    return refreshed


def fetch_claude(state_dir=None, clock=time.time):
    auth = read_claude_auth()
    if claude_expired(auth, clock()):
        auth = refresh_claude_login(auth, state_dir, clock)
    token = auth['accessToken']
    request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20',
        'User-Agent': 'subscription-pin/1.0'})
    try:
        handlers = [NoRedirect()]
        if getattr(sys, 'frozen', False):
            import certifi
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
        with urllib.request.build_opener(*handlers).open(request, timeout=20) as response:
            return parse_claude(json.loads(response.read(1_000_000)))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise UsageError('Claude 登入已失效，請重新登入 Claude Code')
        if error.code == 429:
            raise RateLimitError(retry_after_seconds(error.headers.get('Retry-After'), time.time()))
        raise UsageError('Claude 服務暫時無法查詢（HTTP %d）' % error.code)


def safe_fetch(provider, fetcher):
    try:
        return {'provider': provider, 'cards': fetcher(), 'updatedAt': time.time(), 'error': None}
    except RateLimitError as error:
        return {'provider': provider, 'cards': [], 'updatedAt': None, 'error': str(error),
                'rateLimited': True, 'retryAfter': error.retry_after}
    except UsageError as error:
        return {'provider': provider, 'cards': [], 'updatedAt': None, 'error': str(error)}
    except Exception:
        # 不回傳 exception／HTTP body，避免上游資料帶入登入資訊。
        return {'provider': provider, 'cards': [], 'updatedAt': None,
                'error': '連線或資料解析失敗，請檢查網路與登入狀態'}


INTERVALS = {'codex': 60, 'claude': 300}
STATE_DIR = Path.home() / 'Library/Application Support/Subscription Pin'


def write_state(path, state):
    # 同目錄原子替換；即使程序中途結束，也不留下半份冷卻紀錄。
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
        os.chmod(output.name, 0o600)
        json.dump(state, output, ensure_ascii=False, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(output.name, path)


def scheduled_fetch(provider, fetcher, state_dir=None, clock=time.time):
    """所有程序及更新入口共用節流；狀態不能可靠保存時不送出查詢。"""
    interval = INTERVALS[provider]
    directory = Path(state_dir) if state_dir is not None else STATE_DIR
    def unavailable(message):
        return {'provider': provider, 'cards': [], 'updatedAt': None, 'error': message,
                'nextAllowedAt': clock() + interval, 'staleAfter': interval + 120,
                'rateLimited': False, 'cached': True}
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        path = directory / (provider + '.json')
        with open(directory / (provider + '.lock'), 'a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return unavailable('另一個視窗正在查詢，稍後自動讀取結果')
            if path.exists():
                state = json.loads(path.read_text())
                if (state.get('version') != 1 or state.get('provider') != provider or
                        number(state.get('nextAllowedAt')) is None or
                        not isinstance(state.get('failures'), int) or
                        not isinstance(state.get('cards'), list)):
                    raise ValueError('invalid state')
                if clock() < state['nextAllowedAt']:
                    return dict(state, cached=True)
            else:
                state = {'version': 1, 'provider': provider, 'cards': [], 'updatedAt': None,
                         'error': '正在等候查詢結果', 'failures': 0, 'rateLimited': False}
            # 查詢前持久化預約；強制關閉／重開也不能立刻重送。
            state.update(nextAllowedAt=clock() + interval, staleAfter=interval + 120)
            write_state(path, state)
            result = safe_fetch(provider, fetcher)
            delay = interval
            failures = state['failures']
            if result.get('rateLimited'):
                failures = min(failures + 1, 4)
                delay = max(min(600 * 2 ** (failures - 1), 3600),
                            result.pop('retryAfter', None) or 0)
            elif result['error'] is None:
                failures = 0
                result['updatedAt'] = clock()
            else:
                result.pop('retryAfter', None)
            result.update(version=1, failures=failures, nextAllowedAt=clock() + delay,
                          staleAfter=interval + 120, cached=False,
                          rateLimited=bool(result.get('rateLimited')))
            write_state(path, result)
            return result
    except Exception:
        # 不因快取損壞、磁碟或權限錯誤繞過節流，也不輸出例外內文。
        return unavailable('無法保存查詢冷卻狀態，已暫停連線')


def main():
    if '--self-check' in sys.argv:
        # 離線封裝驗證，不讀鑰匙圈、快取或帳號，不送出網路請求。
        if getattr(sys, 'frozen', False):
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        else:
            context = ssl.create_default_context()
        print(json.dumps({'ok': bool(context.get_ca_certs()), 'frozen': bool(getattr(sys, 'frozen', False)),
                          'python': sys.version.split()[0], 'credentialsRead': False, 'networkRequests': 0}))
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(scheduled_fetch, 'codex', fetch_codex),
                pool.submit(scheduled_fetch, 'claude', fetch_claude)]
        data = {'providers': [job.result() for job in jobs]}
    print(json.dumps(data, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
