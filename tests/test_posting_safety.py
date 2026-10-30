"""Offline regression coverage for publishing blocks and restriction history."""
import asyncio
from contextlib import ExitStack, contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import health
import posting_safety
import store
import twitter_poster as tp


@contextmanager
def deny_external_calls():
    """Any accidental client construction, request or subprocess fails the test."""
    with ExitStack() as stack:
        mocks = []
        for target in (
            'twitter_poster.Client', 'health.Client',
            'httpx.AsyncClient.request', 'httpx.Client.request',
            'socket.create_connection', 'urllib.request.urlopen',
            'subprocess.run',
        ):
            mocks.append(stack.enter_context(mock.patch(
                target, side_effect=AssertionError('External operation: ' + target))))
        yield
        for denied in mocks:
            denied.assert_not_called()


class PublishingSafetyTests(unittest.TestCase):
    def test_live_single_blocks_before_config_caption_or_account_access(self):
        with deny_external_calls(), mock.patch.object(tp, 'compose_text') as compose:
            # Missing credentials and config must still produce a safety result.
            result = asyncio.run(tp.post_for_account({}, {}, {}, dry_run=False))
        self.assertEqual(result, (False, 'blocked:unsupported-publishing-backend'))
        compose.assert_not_called()

    def test_live_bulk_never_enters_individual_post_path(self):
        accounts = [{'username': 'example_one'}, {'username': 'example_two'}]
        with deny_external_calls(), mock.patch.object(tp, 'post_for_account') as post:
            result = asyncio.run(tp.post_all(accounts, {}, {}, dry_run=False))
        self.assertEqual(result, {
            'example_one': 'blocked:unsupported-publishing-backend',
            'example_two': 'blocked:unsupported-publishing-backend',
        })
        post.assert_not_called()

    def test_live_bulk_filter_preserves_block(self):
        accounts = [{'username': 'example_one'}, {'username': 'example_two'}]
        with deny_external_calls():
            result = asyncio.run(tp.post_all(
                accounts, {'live': True, 'enabled': True}, {},
                dry_run=False, only='example_two'))
        self.assertEqual(result, {'example_two': posting_safety.blocked_detail()})

    def test_private_attempt_is_blocked_for_every_legacy_mode(self):
        with deny_external_calls():
            for warming in (False, True):
                for attempt in (0, 9):
                    result = asyncio.run(tp._attempt_post(
                        {}, {}, {}, 'Draft', attempt=attempt, warming=warming))
                    self.assertEqual(result, (False, posting_safety.blocked_detail(), False))

    def test_direct_upload_and_login_helpers_have_no_side_effects(self):
        client = mock.Mock()
        with deny_external_calls(), mock.patch.object(tp, 'pick_media') as pick:
            with self.assertRaises(posting_safety.PublishingBlocked):
                asyncio.run(tp.upload_one(client, {'enable_media': True}, 'example'))
            self.assertFalse(asyncio.run(tp._relogin(client, {})))
            self.assertFalse(tp._remint_ct0(client, {}, {}, {}, attempt=3))
        pick.assert_not_called()
        self.assertEqual(client.method_calls, [])

    def test_draft_preview_works_without_credentials_or_network(self):
        with deny_external_calls(), mock.patch.object(tp, 'log') as log:
            result = asyncio.run(tp.post_for_account(
                {'username': 'example'}, {'text': 'Review this draft', 'variation': False},
                {}, dry_run=True))
        self.assertEqual(result, (True, 'dry-run'))
        log.assert_called_once_with('  [DRAFT] @example: Review this draft')

    def test_status_cannot_be_changed_by_mutating_a_returned_dict(self):
        snapshot = posting_safety.status()
        self.assertEqual(snapshot['code'], 'unsupported-publishing-backend')
        self.assertTrue(snapshot['appeal_url'].startswith('https://help.x.com/'))
        self.assertTrue(snapshot['rules_url'].startswith('https://help.x.com/'))
        snapshot['blocked'] = False
        self.assertTrue(posting_safety.status()['blocked'])

    def test_cli_live_is_rejected_before_reading_runtime_files(self):
        with deny_external_calls(), mock.patch.object(sys, 'argv', ['twitter_poster.py', '--once', '--live']), \
                mock.patch.object(tp, 'load_config') as config, \
                mock.patch.object(tp, 'load_accounts') as accounts, \
                mock.patch.object(tp, 'log'):
            self.assertEqual(tp._main(), 2)
        config.assert_not_called()
        accounts.assert_not_called()

    def test_warmup_cannot_enable_account_activity(self):
        with deny_external_calls():
            self.assertFalse(tp.is_warming('example', {'warmup_enabled': True, 'warmup_days': 14}))


class RestrictionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_patch = mock.patch.object(store, 'DATA_DIR', self.tempdir.name)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

    def test_health_never_unbenches_even_verified_readable_accounts(self):
        store.bench_account('example', 'AccountSuspended')
        original = store.read_benched()
        for status in ('OK', 'OK (@example) (readable only)', 'OK-VERIFIED', 'UNREACHABLE'):
            with deny_external_calls(), mock.patch.object(store, 'unbench_account') as unbench:
                self.assertEqual(health.sync_bench({'example': status}), ([], []))
            unbench.assert_not_called()
            self.assertEqual(store.read_benched(), original)

    def test_new_explicit_restrictions_are_persisted(self):
        with deny_external_calls():
            result = health.sync_bench({'example': 'SUSPENDED', 'other': 'UNKNOWN'})
        self.assertEqual(result, (['example'], []))
        self.assertEqual(set(store.read_benched()), {'example'})

    def test_health_preserves_definitive_notice_after_a_later_unknown_read(self):
        evidence = health.account_evidence.classify_public_response(
            '<main><h2>Account suspended</h2><p>X suspends accounts which violate the X Rules.</p></main>',
            username='example')
        health.account_evidence.persist_evidence('example', evidence)
        self.assertEqual(health.sync_bench({'example': 'SUSPENDED'}), (['example'], []))
        health.account_evidence.persist_evidence('example', {'status': 'unknown'})
        health.sync_bench({'example': 'UNKNOWN'})
        self.assertTrue(health.account_evidence.summarize_bench(store.read_benched()['example'])['confirmed'])

    def test_lifecycle_without_restriction_is_not_reported_as_verified_survival(self):
        summary = health.survival_summary({'example': {'first_seen': store.now(), 'last_status': 'UNKNOWN'}})
        self.assertEqual(summary['without_recorded_restriction'], 1)
        self.assertNotIn('alive', summary)
        self.assertNotIn('survival_pct', summary)

    def test_unknown_and_readable_statuses_preserve_restriction_history(self):
        initial = health.update_lifecycle({'example': 'SUSPENDED'}, [])['example']
        for status in ('UNKNOWN', 'UNREACHABLE', 'ERR Timeout', 'NOT-FOUND', 'OK',
                       'OK (@example) (readable only)'):
            with deny_external_calls():
                current = health.update_lifecycle({'example': status}, [])['example']
            self.assertEqual(current['died'], initial['died'])
            self.assertEqual(current['died_status'], 'SUSPENDED')
        verified = health.update_lifecycle({'example': 'OK-VERIFIED'}, [])['example']
        self.assertNotIn('died', verified)
        self.assertNotIn('died_status', verified)

    def test_public_observation_is_saved_without_private_cookie_checks(self):
        evidence = {'status': 'readable', 'source': 'public_profile', 'observed_at': 1}
        with deny_external_calls(), \
             mock.patch.object(health.account_evidence, 'fetch_public_evidence', return_value=evidence) as fetch, \
             mock.patch.object(health.account_evidence, 'persist_evidence') as persist:
            result = asyncio.run(health.check_account({'username': 'example'}, {}, {}))
        self.assertEqual(result, 'PUBLIC-READABLE (publishing unverified)')
        fetch.assert_called_once_with('example', proxy=None)
        persist.assert_called_once_with('example', evidence)

    def test_inconclusive_public_status_never_falls_back_to_healthy_session(self):
        account = {'username': 'example', 'auth_token': 'fake-for-offline-test', 'ct0': None}
        for status in ('UNKNOWN', 'UNREACHABLE', 'NOT-FOUND', 'SUSPENDED'):
            with deny_external_calls(), mock.patch.object(health, 'public_status', return_value=status):
                self.assertEqual(asyncio.run(health.check_account(account, {}, {})), status)


if __name__ == '__main__':
    unittest.main()
