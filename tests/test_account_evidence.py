"""Offline evidence tests: error shells cannot create suspension records."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import account_evidence as ae
import autobench
import store

NOTICE = '<main><h1>Account suspended</h1><p>X suspends accounts which violate the X Rules.</p></main>'


class ClassifierTests(unittest.TestCase):
    def classify(self, html, **kw):
        return ae.classify_public_response(html, username='example', observed_at=123, **kw)

    def test_explicit_visible_heading_and_notice(self):
        for html in (NOTICE, '<h1>This account has been suspended</h1>',
                     '<div><span>Account suspended</span></div><p>X suspends accounts that violate the X Rules.</p>'):
            with self.subTest(html=html):
                result = self.classify(html)
                self.assertEqual(result['status'], 'suspended')
                self.assertEqual(result['observed_at'], 123)
                self.assertIn('suspended', result['sample'])

    def test_hidden_i18n_and_script_strings_cannot_suspend(self):
        hidden = [
            '<script>window.i18n={"suspended":"Account suspended"};</script>',
            '<script type="application/json">' + NOTICE + '</script>',
            '<style>.x::after{content:"Account suspended"}</style>',
            '<template>' + NOTICE + '</template>',
            '<div hidden>' + NOTICE + '</div>',
            '<div aria-hidden="true">' + NOTICE + '</div>',
            '<div style="display: none">' + NOTICE + '</div>',
            '<div class="hidden">' + NOTICE + '</div>',
            '<noscript>' + NOTICE + '</noscript>',
            '<div data-i18n="Account suspended">Try again later</div>',
        ]
        for html in hidden:
            with self.subTest(html=html):
                result = self.classify('<title>X / Error</title>' + html)
                self.assertNotEqual(result['status'], 'suspended')
                self.assertNotIn('Account suspended', result['sample'])

    def test_generic_error_and_title_only_are_unknown(self):
        for html in ('<title>X / Error</title><p>Something went wrong. Try again.</p>',
                     '<title>Account suspended / X</title>',
                     '<p>An account suspended yesterday was mentioned here.</p>'):
            self.assertEqual(self.classify(html)['status'], 'unknown')

    def test_quoted_timeline_notice_cannot_suspend_profile(self):
        html = '<title>Example (@example) / X</title><main><span>@example</span><article>' + NOTICE + '</article></main>'
        self.assertEqual(self.classify(html)['status'], 'readable')

    def test_profile_display_name_cannot_be_a_suspension_notice(self):
        html = '<title>Account suspended (@example) / X</title><h1>Account suspended</h1><span>@example</span>'
        self.assertEqual(self.classify(html)['status'], 'readable')

    def test_http_errors_override_even_apparent_notice(self):
        for code, status in ((404, 'not_found'), (410, 'not_found'), (429, 'rate_limited'),
                             (503, 'unavailable'), (401, 'login_required'), (403, 'unavailable'),
                             (206, 'unavailable'), (302, 'unavailable')):
            with self.subTest(code=code):
                self.assertEqual(self.classify(NOTICE, http_status=code)['status'], status)

    def test_missing_profile_is_not_suspension(self):
        self.assertEqual(self.classify('<p>This account doesn’t exist</p>')['status'], 'not_found')

    def test_age_and_login_gates_are_distinct_inconclusive_reasons(self):
        self.assertEqual(self.classify('<h1>Verify your age</h1>')['reason'], 'age_gate')
        self.assertEqual(self.classify('<h1>Sign in to view this profile</h1>')['reason'], 'login_gate')
        self.assertEqual(self.classify('<p>This content is age-restricted</p>')['status'], 'login_required')

    def test_empty_partial_network_and_truncated_never_suspend(self):
        for html, kw in (('', {}), (NOTICE, {'truncated': True}),
                         ('<main><h1>Account suspended</h1>', {}),
                         (NOTICE, {'network_error': True, 'http_status': None})):
            self.assertEqual(self.classify(html, **kw)['status'], 'unavailable')


class PersistenceAndFetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.object(store, 'DATA_DIR', self.temp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_legacy_autobench_requires_review_and_typed_suspension_is_confirmed(self):
        historical = {'reason': 'SUSPENDED (autobench)', 'source': 'autobench'}
        self.assertFalse(ae.summarize_bench(historical)['confirmed'])
        self.assertEqual(ae.summarize_bench(historical)['label'], 'Review required')
        self.assertTrue(ae.summarize_bench({'reason': 'AccountSuspended'})['confirmed'])
        self.assertEqual(ae.summarize_account(None, None)['label'], 'Not checked')

    def test_only_definitive_evidence_adds_a_restriction(self):
        unknown = ae.classify_public_response('<title>X / Error</title><p>Try again.</p>')
        self.assertFalse(ae.bench_from_evidence('example', unknown))
        self.assertEqual(store.read_benched(), {})
        explicit = ae.classify_public_response(NOTICE)
        self.assertTrue(ae.bench_from_evidence('example', explicit))
        self.assertTrue(ae.summarize_bench(store.read_benched()['example'])['confirmed'])

    def test_later_readable_observation_never_clears_confirmed_restriction(self):
        ae.bench_from_evidence('example', ae.classify_public_response(NOTICE))
        before = store.read_benched()
        readable = ae.classify_public_response('<title>Example (@example) / X</title><span>@example</span>', username='example')
        self.assertFalse(ae.bench_from_evidence('example', readable))
        self.assertEqual(store.read_benched(), before)
        self.assertTrue(ae.summarize_account(before['example'], readable)['confirmed'])

    def test_browser_evidence_supports_confirmed_notice_without_http_status(self):
        evidence = {'status': 'suspended', 'source': 'public_browser', 'classifier_version': 1,
                    'observed_at': 123, 'http_status': None, 'page_title': 'Profile / X',
                    'reason': 'Explicit Account suspended heading in rendered public profile',
                    'sample': 'Account suspended. X suspends accounts which violate the X Rules.'}
        self.assertTrue(ae.summarize_account(None, evidence)['confirmed'])
        self.assertTrue(ae.summarize_account({'reason': 'SUSPENDED (autobench)'}, evidence)['confirmed'])
        evidence['sample'] = 'Something went wrong.'
        self.assertFalse(ae.summarize_account(None, evidence)['confirmed'])

    def test_persisted_evidence_is_bounded_and_redacted(self):
        evidence = ae.classify_public_response('<p>Visible</p>')
        evidence.update(sample='auth_token=VERY_SECRET_VALUE email@example.com https://site.invalid/?password=secret ' + 'word '*200,
                        headers={'Cookie': 'secret'}, cookies='secret')
        ae.persist_evidence('example', evidence)
        stored = ae.read_evidence()['example']
        self.assertLessEqual(len(stored['sample']), 300)
        for forbidden in ('VERY_SECRET_VALUE', 'email@example.com', 'password=secret', 'headers', 'cookies'):
            self.assertNotIn(forbidden, json.dumps(stored))

    def response(self, payload, status=200, declared=None):
        response = mock.Mock()
        response.getcode.return_value = status
        response.read.return_value = payload
        response.headers = {} if declared is None else {'Content-Length': str(declared)}
        return response

    def test_fetch_is_one_bounded_request_with_no_cookie_or_range(self):
        response = self.response(NOTICE.encode())
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(ae.urllib.request, 'build_opener', return_value=opener):
            result = ae.fetch_public_evidence('example', max_bytes=1000)
        self.assertEqual(result['status'], 'suspended')
        opener.open.assert_called_once()
        response.read.assert_called_once_with(1001)
        response.close.assert_called_once()
        headers = dict(opener.open.call_args.args[0].header_items())
        self.assertNotIn('Cookie', headers)
        self.assertNotIn('Range', headers)

    def test_fetch_overflow_and_declared_short_read_are_inconclusive(self):
        for response in (self.response(NOTICE.encode() + b' '*100), self.response(NOTICE.encode(), declared=5000)):
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(ae.urllib.request, 'build_opener', return_value=opener):
                result = ae.fetch_public_evidence('example', max_bytes=100)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['reason'], 'truncated_response')

    def test_network_error_never_leaks_exception_or_retries(self):
        opener = mock.Mock()
        opener.open.side_effect = OSError('proxy http://user:SECRET@host.invalid failed')
        with mock.patch.object(ae.urllib.request, 'build_opener', return_value=opener):
            result = ae.fetch_public_evidence('example')
        opener.open.assert_called_once()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_http_error_is_classified_without_retry(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://x.com/example', 429, 'rate limited', {}, io.BytesIO(NOTICE.encode()))
        with mock.patch.object(ae.urllib.request, 'build_opener', return_value=opener):
            result = ae.fetch_public_evidence('example')
        self.assertEqual(result['status'], 'rate_limited')
        opener.open.assert_called_once()

    def test_compatibility_probe_never_rotates_and_session_sweep_is_disabled(self):
        evidence = ae.classify_public_response('<p>Try again</p>')
        with mock.patch.object(autobench.tp, 'resolve_proxy', return_value=None) as resolve, \
                mock.patch.object(ae, 'fetch_public_evidence', return_value=evidence) as fetch:
            self.assertEqual(autobench.public_status_cheap('example', {}, {}, tries=8), 'UNKNOWN')
            self.assertFalse(autobench.session_dead({'auth_token': 'not-used'}, {}, {}, tries=8))
        resolve.assert_called_once_with({'username': 'example', 'auth_token': None}, {}, {}, 0)
        fetch.assert_called_once()


if __name__ == '__main__':
    unittest.main()
