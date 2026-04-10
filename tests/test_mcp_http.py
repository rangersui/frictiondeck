"""Comprehensive tests for mcp_server.py --http mode.

Runs server in a thread, exercises knock, pastebin, bearer, CGNAT, etc.
"""
import os, sys, time, threading, urllib.request, urllib.error, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = 3991

KNOCK_A = '/knock-step-01a'  # 15 chars > 12 min
KNOCK_B = '/knock-step-02b'
KNOCK_C = '/knock-step-03c'

def setup_env():
    os.environ['ELASTIK_MCP_PORT'] = str(PORT)
    os.environ['ELASTIK_MCP_BIND'] = '127.0.0.1'
    os.environ['ELASTIK_KNOCK'] = f'{KNOCK_A},{KNOCK_B},{KNOCK_C}'
    os.environ['ELASTIK_MCP_TOKEN'] = 'secret-url-key-abc'
    os.environ['ELASTIK_KNOCK_TTL'] = '600'
    # Pin Anthropic IPs to localhost for testing the URL-secret path.
    # In production this would be 160.79.104.0/21 etc.
    os.environ['ELASTIK_ANTHROPIC_IPS'] = '127.0.0.1/32'
    os.environ.pop('ELASTIK_TRUST_PROXY_HEADER', None)
    os.environ.pop('ELASTIK_TRUST_PROXY_FROM', None)

def start_server():
    sys.argv = ['mcp_server.py', '--http']
    import mcp_server
    t = threading.Thread(target=mcp_server._run_http, daemon=True)
    t.start()
    time.sleep(0.5)

def req(method, path, body=None, headers=None):
    url = f'http://127.0.0.1:{PORT}{path}'
    data = body.encode() if isinstance(body, str) else body
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        resp = urllib.request.urlopen(r, timeout=5)
        return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')

def test(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        return False
    except Exception as e:
        print(f"  ERROR {name}: {type(e).__name__}: {e}")
        return False

def main():
    setup_env()
    start_server()

    passed = 0
    failed = 0

    def t(name, fn):
        nonlocal passed, failed
        if test(name, fn):
            passed += 1
        else:
            failed += 1

    # --- pastebin behavior ---
    def pastebin_get_root():
        code, body = req('GET', '/')
        assert code == 200
        assert 'pastebin' in body
    t('GET / returns pastebin banner', pastebin_get_root)

    def pastebin_post_echo():
        code, body = req('POST', '/', 'hello world')
        assert code == 200
        key = body.strip()
        assert len(key) == 6
        code2, body2 = req('GET', f'/{key}')
        assert code2 == 200
        assert body2 == 'hello world'
    t('POST stores, GET retrieves', pastebin_post_echo)

    def pastebin_unknown_key():
        code, body = req('GET', '/nonexistent')
        assert code == 404
        assert 'not found' in body
    t('GET unknown key returns 404', pastebin_unknown_key)

    def pastebin_ring_evict():
        # fill beyond 16 slots
        keys = []
        for i in range(20):
            _, body = req('POST', '/', f'data-{i}')
            keys.append(body.strip())
        # first one should be evicted
        code, _ = req('GET', f'/{keys[0]}')
        assert code == 404, f'first key should be evicted, got {code}'
        code, _ = req('GET', f'/{keys[-1]}')
        assert code == 200, f'last key should still exist, got {code}'
    t('Ring buffer evicts oldest', pastebin_ring_evict)

    # --- unauthorized /mcp ---
    def mcp_unauthorized_is_pastebin():
        code, body = req('POST', '/mcp', '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
        assert code == 200
        # pastebin response: key, not JSON-RPC
        try:
            json.loads(body)
            assert False, 'should be pastebin key, not JSON'
        except json.JSONDecodeError:
            pass  # good
        assert len(body.strip()) == 6, f'expected 6-char key, got {body!r}'
    t('POST /mcp unauthorized = pastebin', mcp_unauthorized_is_pastebin)

    # --- URL secret path (Anthropic proxy scenario) ---
    def url_secret_unlocks_mcp():
        code, body = req('POST', '/mcp?k=secret-url-key-abc',
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
        assert code == 200
        msg = json.loads(body)
        assert msg['id'] == 1
        assert 'result' in msg
        assert msg['result']['serverInfo']['name'] == 'elastik'
    t('URL secret unlocks /mcp', url_secret_unlocks_mcp)

    def url_secret_wrong():
        code, body = req('POST', '/mcp?k=WRONG',
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
        assert code == 200
        try:
            json.loads(body)
            assert False, 'wrong URL key should fall through to pastebin'
        except json.JSONDecodeError:
            pass
    t('Wrong URL key falls through to pastebin', url_secret_wrong)

    def url_secret_missing():
        code, body = req('POST', '/mcp',
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
        assert code == 200
        try:
            json.loads(body)
            assert False, 'missing URL key should fall through'
        except json.JSONDecodeError:
            pass
    t('Missing URL key = pastebin', url_secret_missing)

    def bearer_header_ignored():
        # Claude can't send custom headers. Even if one arrives, it's ignored.
        code, body = req('POST', '/mcp',
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            headers={'Authorization': 'Bearer secret-url-key-abc'})
        assert code == 200
        try:
            json.loads(body)
            assert False, 'bearer header should be ignored (URL secret only)'
        except json.JSONDecodeError:
            pass
    t('Bearer header is ignored', bearer_header_ignored)

    # --- knock sequence ---
    def knock_then_mcp():
        req('GET', KNOCK_A)
        req('GET', KNOCK_B)
        req('GET', KNOCK_C)
        # now POST /mcp should work without any secret
        code, body = req('POST', '/mcp',
            '{"jsonrpc":"2.0","id":2,"method":"initialize","params":{}}')
        assert code == 200
        msg = json.loads(body)
        assert msg['id'] == 2
    t('Knock sequence whitelists IP', knock_then_mcp)

    # --- tools/list after knock ---
    def mcp_tools_list():
        code, body = req('POST', '/mcp',
            '{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}')
        assert code == 200, f'code {code}'
        msg = json.loads(body)
        assert 'http' in [t['name'] for t in msg['result']['tools']]
    t('MCP tools/list returns http tool', mcp_tools_list)

    print(f"\n  {passed} passed, {failed} failed (primary server)\n")

    # --- second server: Anthropic range EXCLUDES 127.0.0.1, no knock ---
    # Isolates "URL secret from non-Anthropic IP" and "no auth" cases.
    PORT2 = PORT + 1
    def start_second_server():
        os.environ['ELASTIK_MCP_PORT'] = str(PORT2)
        os.environ['ELASTIK_ANTHROPIC_IPS'] = '10.99.99.0/24'  # 127.0.0.1 NOT in this
        os.environ['ELASTIK_KNOCK'] = ''  # no knock at all
        os.environ['ELASTIK_MCP_TOKEN'] = 'secret2'
        import importlib, mcp_server
        importlib.reload(mcp_server)
        threading.Thread(target=mcp_server._run_http, daemon=True).start()
        time.sleep(0.5)
    start_second_server()

    def req2(method, path, body=None, headers=None):
        url = f'http://127.0.0.1:{PORT2}{path}'
        data = body.encode() if isinstance(body, str) else body
        r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            resp = urllib.request.urlopen(r, timeout=5)
            return resp.status, resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', 'replace')

    def url_key_from_non_anthropic_ip():
        # 127.0.0.1 is NOT in Anthropic range (10.99.99.0/24). Right key,
        # wrong IP -> pastebin.
        code, body = req2('POST', '/mcp?k=secret2',
            '{"jsonrpc":"2.0","id":10,"method":"initialize","params":{}}')
        assert code == 200
        try:
            json.loads(body)
            assert False, 'URL key from non-Anthropic IP should be pastebin, got JSON'
        except json.JSONDecodeError:
            pass
        assert len(body.strip()) == 6
    t('URL key from non-Anthropic IP = pastebin', url_key_from_non_anthropic_ip)

    def no_auth_at_all():
        code, body = req2('POST', '/mcp',
            '{"jsonrpc":"2.0","id":11,"method":"initialize","params":{}}')
        assert code == 200
        try:
            json.loads(body)
            assert False, 'no auth should be pastebin'
        except json.JSONDecodeError:
            pass
    t('No auth = pastebin', no_auth_at_all)

    def second_server_pastebin_works():
        code, body = req2('POST', '/', 'testing')
        assert code == 200
        assert len(body.strip()) == 6
    t('Second server pastebin functional', second_server_pastebin_works)

    # --- startup refusal tests (subprocess) ---
    import subprocess
    mcp_server_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'mcp_server.py')

    def run_and_expect_exit(env_overrides, expect_in_stderr):
        env = os.environ.copy()
        # Clear test env so subprocess starts clean
        for k in ['ELASTIK_MCP_PORT', 'ELASTIK_KNOCK', 'ELASTIK_MCP_TOKEN',
                  'ELASTIK_ANTHROPIC_IPS', 'ELASTIK_TRUST_PROXY_HEADER',
                  'ELASTIK_TRUST_PROXY_FROM']:
            env.pop(k, None)
        env.update(env_overrides)
        env['ELASTIK_MCP_PORT'] = '3999'  # unused port
        r = subprocess.run([sys.executable, mcp_server_path, '--http'],
                           env=env, capture_output=True, text=True, timeout=5)
        assert r.returncode != 0, f'expected nonzero exit, got {r.returncode}'
        assert expect_in_stderr in r.stderr, \
            f'expected {expect_in_stderr!r} in stderr, got: {r.stderr}'

    def refuses_short_knock():
        run_and_expect_exit(
            {'ELASTIK_KNOCK': '/a,/b,/c'},
            'knock path too short')
    t('Refuses short knock paths', refuses_short_knock)

    def refuses_root_knock():
        run_and_expect_exit(
            {'ELASTIK_KNOCK': '/,/knock-step-aaaaa,/knock-step-bbbbb'},
            'knock path too short or invalid')
    t('Refuses "/" as knock path', refuses_root_knock)

    def refuses_trust_header_without_from():
        run_and_expect_exit(
            {'ELASTIK_KNOCK': '/knock-step-aaaaa,/knock-step-bbbbb,/knock-step-ccccc',
             'ELASTIK_TRUST_PROXY_HEADER': 'x-forwarded-for'},
            'TRUST_PROXY_FROM is empty')
    t('Refuses TRUST_PROXY_HEADER without TRUST_PROXY_FROM', refuses_trust_header_without_from)

    def refuses_no_auth():
        run_and_expect_exit(
            {},  # nothing set
            'refusing to start http mode')
    t('Refuses start with no auth configured', refuses_no_auth)

    # --- methods on non-/mcp path (whitelisted) still pastebin ---
    def whitelisted_non_mcp_is_pastebin():
        code, body = req('POST', '/admin', 'whatever')
        assert code == 200
        assert len(body.strip()) == 6
    t('Whitelisted POST /admin is still pastebin', whitelisted_non_mcp_is_pastebin)

    # --- other methods ---
    def delete_method():
        code, _ = req('DELETE', '/')
        assert code == 405
    t('DELETE returns 405', delete_method)

    def put_method():
        code, _ = req('PUT', '/')
        assert code == 405
    t('PUT returns 405', put_method)

    # --- server header spoof ---
    def server_header_pastebin():
        url = f'http://127.0.0.1:{PORT}/'
        r = urllib.request.urlopen(url)
        server = r.headers.get('Server', '')
        assert 'pastebin' in server.lower(), f'server header = {server!r}'
    t('Server header says pastebin', server_header_pastebin)

    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)

if __name__ == '__main__':
    main()
