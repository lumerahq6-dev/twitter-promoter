import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import dashboard
import scheduler
import store


class DashboardSafetyTests(unittest.TestCase):
    def test_account_evidence_does_not_turn_legacy_flags_into_confirmed_bans(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(store, 'DATA_DIR', temp), \
             patch.object(scheduler, 'STATE_FILE', str(Path(temp) / 'state.json')), \
             patch.object(dashboard.tp, 'load_config', return_value={'timezone': 'UTC'}), \
             patch.object(dashboard.tp, 'load_accounts', return_value=[
                 {'username': 'legacy', 'auth_token': None, 'benched': {'reason': 'SUSPENDED (autobench)', 'source': 'autobench'}},
                 {'username': 'typed', 'auth_token': None, 'benched': {'reason': 'AccountSuspended', 'source': 'poster'}},
                 {'username': 'unseen', 'auth_token': 'fixture-only', 'benched': None}]), \
             patch.object(scheduler, 'daily_targets', return_value=[720]):
            state = dashboard.build_state()
            self.assertEqual(state['kpis']['accounts_total'], 3)
            self.assertEqual(state['kpis']['restrictions_confirmed'], 1)
            self.assertEqual(state['kpis']['restrictions_unverified'], 1)
            self.assertEqual([a['restriction']['label'] for a in state['accounts']],
                             ['Review required', 'Confirmed suspension', 'Not checked'])
            detail = dashboard.account_detail('legacy')
            self.assertFalse(detail['restriction']['confirmed'])
            self.assertTrue(detail['publishing']['blocked'])

    def test_scheduler_live_stops_before_reading_accounts_or_state(self):
        with patch.object(scheduler.tp, 'load_accounts') as accounts, \
             patch.object(scheduler, 'save_state') as save:
            self.assertEqual(scheduler.run(live=True), 2)
        accounts.assert_not_called()
        save.assert_not_called()

    def test_dry_preview_never_writes_live_state_or_polls_network(self):
        with patch.object(scheduler.tp, 'load_config', return_value={'timezone': 'UTC', 'text': 'Preview'}), \
             patch.object(scheduler.tp, 'load_accounts', return_value=[{'username': 'review_demo'}]), \
             patch.object(scheduler.tp, 'load_proxies') as proxies, \
             patch.object(scheduler.tp, 'Client') as client, \
             patch.object(scheduler.tp.store, 'record_post') as record, \
             patch.object(scheduler, 'save_state') as save, \
             patch.object(scheduler, 'single_instance') as lock:
            self.assertEqual(scheduler.run(live=False), 0)
        proxies.assert_not_called()
        client.assert_not_called()
        record.assert_not_called()
        save.assert_not_called()
        lock.assert_not_called()

    def test_post_now_cannot_start_background_publishing(self):
        with patch.object(dashboard.threading, 'Thread') as thread, \
             patch.object(dashboard.tp, 'load_accounts') as accounts:
            ok, reason = dashboard.start_post_now('example')
        self.assertFalse(ok)
        self.assertTrue(reason)
        thread.assert_not_called()
        accounts.assert_not_called()

    def test_api_blocks_publish_and_revive_and_hides_schedule(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(store, 'DATA_DIR', temp), \
             patch.object(scheduler, 'STATE_FILE', str(Path(temp) / 'state.json')), \
             patch.object(dashboard.tp, 'load_config', return_value={'timezone': 'UTC'}), \
             patch.object(dashboard.tp, 'load_accounts', return_value=[
                 {'username': 'review_demo', 'auth_token': None, 'benched': None}]), \
             patch.object(scheduler, 'daily_targets', return_value=[720]), \
             patch.object(store, 'unbench_account') as revive:
            server = dashboard.ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base + '/api/state') as response:
                    state = json.load(response)
                self.assertTrue(state['publishing']['blocked'])
                self.assertEqual(state['upcoming'], [])
                self.assertIsNone(state['accounts'][0]['next_in_min'])
                self.assertEqual(state['accounts'][0]['next_label'], 'Paused')
                for action in ('post-now', 'revive'):
                    request = Request(base + '/api/account/' + action,
                                      data=b'{"username":"review_demo"}',
                                      headers={'Content-Type': 'application/json'})
                    with self.assertRaises(HTTPError) as failure:
                        urlopen(request)
                    self.assertEqual(failure.exception.code, 400)
                    self.assertFalse(json.load(failure.exception)['ok'])
                revive.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
