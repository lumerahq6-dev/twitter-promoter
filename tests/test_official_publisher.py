"""Official publisher tests: fake HTTP responses only, no live credentials."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import official_publisher as op


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError('Unexpected request')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def me(name='example'):
    return Response({'data': {'id': '1', 'username': name}})


def initialized():
    return Response({'data': {'id': '123'}})


def metadata():
    return Response({'data': {'id': '123', 'associated_metadata': {
        'sensitive_media_warning': {'adult_content': True}}}})


class OfficialPublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.video = self.root / 'video.mp4'
        self.video.write_bytes(b'local-test-video')
        self.no_network = mock.patch('socket.create_connection', side_effect=AssertionError('Network denied'))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def publisher(self, session, **kwargs):
        return op.OfficialPublisher(session, kwargs.pop('token', 'secret-test-token'),
            kwargs.pop('expected_username', 'example'), data_dir=self.data, **kwargs)

    def error(self, publisher, category, **kwargs):
        with self.assertRaises(op.PublishError) as caught:
            publisher.publish('Reviewed text', **kwargs)
        self.assertEqual(caught.exception.category, category)
        return caught.exception

    def test_default_cli_preview_is_offline_and_no_secret_or_content_output(self):
        output = io.StringIO()
        with mock.patch.dict('os.environ', {'X_USER_ACCESS_TOKEN': 'topsecret'}), \
                mock.patch.object(op, 'OfficialPublisher') as publisher, redirect_stdout(output):
            self.assertEqual(op.main(['--text', 'topsecret', '--video', str(self.video)]), 0)
        publisher.assert_not_called()
        self.assertNotIn('topsecret', output.getvalue())
        self.assertFalse(self.data.exists())
        self.assertFalse(json.loads(output.getvalue())['will_publish'])

    def test_missing_configuration_stops_before_http(self):
        for kwargs in ({'token': ''}, {'expected_username': ''}, {'token': 'Bearer secret'}):
            session = Session()
            self.error(self.publisher(session, **kwargs), 'missing_or_invalid_user_configuration')
            self.assertEqual(session.calls, [])

    def test_benched_username_case_insensitive_and_malformed_bench_block(self):
        self.data.mkdir()
        bench = self.data / 'benched.json'
        for content, category in (('{"@EXAMPLE": {}}', 'account_locally_benched'),
                                  ('broken', 'bench_unreadable'), ('[]', 'bench_unreadable')):
            bench.write_text(content)
            session = Session()
            self.error(self.publisher(session), category)
            self.assertEqual(session.calls, [])

    def test_identity_mismatch_makes_no_write(self):
        session = Session(me('someone_else'))
        self.error(self.publisher(session), 'username_mismatch', video=self.video)
        self.assertEqual([c[:2] for c in session.calls], [('GET', op.API + '/2/users/me')])

    def test_text_create_uses_user_token_json_and_no_redirects(self):
        session = Session(me('EXAMPLE'), Response({'data': {'id': '456'}}, 201))
        result = self.publisher(session).publish('Reviewed text')
        self.assertEqual(result['id'], '456')
        method, url, request = session.calls[-1]
        self.assertEqual((method, url), ('POST', op.API + '/2/tweets'))
        self.assertEqual(request['json'], {'text': 'Reviewed text'})
        self.assertEqual(request['headers'], {'Authorization': 'Bearer secret-test-token'})
        self.assertFalse(request['allow_redirects'])
        self.assertEqual(len(session.calls), 2)
        receipt = json.loads((self.data / 'publishing_receipts.jsonl').read_text())
        self.assertEqual((receipt['tweet_id'], receipt['user_id']), ('456', '1'))
        self.assertNotIn('Reviewed text', json.dumps(receipt))
        self.assertFalse((self.data / 'official_publisher_holds' / 'example.json').exists())

    def test_video_chunked_multipart_metadata_and_create_order(self):
        session = Session(me(), initialized(), Response({'data': {}}), Response({'data': {}}),
            Response({'data': {'processing_info': {'state': 'pending', 'check_after_secs': 1}}}),
            Response({'data': {'processing_info': {'state': 'succeeded'}}}), metadata(),
            Response({'data': {'id': '456'}}, 201))
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        with mock.patch.object(op, 'CHUNK_BYTES', 8):
            result = self.publisher(session, clock=lambda: now[0], sleep=sleep).publish(
                'Reviewed clip', self.video)
        self.assertEqual(result['id'], '456')
        self.assertEqual(session.calls[1][2]['json'], {'media_type': 'video/mp4',
            'total_bytes': self.video.stat().st_size, 'media_category': 'tweet_video'})
        chunks = [c for c in session.calls if c[1].endswith('/append')]
        self.assertEqual([c[2]['data'] for c in chunks], [{'segment_index': '0'}, {'segment_index': '1'}])
        self.assertEqual(b''.join(c[2]['files']['media'][1] for c in chunks), self.video.read_bytes())
        self.assertTrue(all('json' not in c[2] for c in chunks))
        self.assertEqual(session.calls[5][2]['params'], {'media_id': '123', 'command': 'STATUS'})
        self.assertEqual(session.calls[6][2]['json'], {'id': '123', 'metadata': {
            'sensitive_media_warning': {'adult_content': True}}})
        self.assertEqual(session.calls[-1][2]['json']['media'], {'media_ids': ['123']})

    def test_failed_processing_prevents_metadata_and_create(self):
        session = Session(me(), initialized(), Response({'data': {}}),
            Response({'data': {'processing_info': {'state': 'failed'}}}))
        self.error(self.publisher(session), 'media_processing_failed', video=self.video)
        self.assertEqual(len(session.calls), 4)

    def test_processing_deadline_and_invalid_delay_prevent_post(self):
        for delay, category in ((600, 'processing_deadline_exceeded'),
                                ('1', 'invalid_processing_delay'), (-1, 'invalid_processing_delay')):
            session = Session(me(), initialized(), Response({'data': {}}), Response({'data': {
                'processing_info': {'state': 'pending', 'check_after_secs': delay}}}))
            self.error(self.publisher(session), category, video=self.video)
            self.assertEqual(len(session.calls), 4)

    def test_processing_status_disappearing_does_not_mean_ready(self):
        session = Session(me(), initialized(), Response({'data': {}}), Response({'data': {
            'processing_info': {'state': 'pending', 'check_after_secs': 0}}}), Response({'data': {}}))
        self.error(self.publisher(session, sleep=lambda _: None),
                   'processing_status_missing', video=self.video)
        self.assertEqual(len(session.calls), 5)

    def test_missing_or_false_adult_metadata_confirmation_blocks(self):
        for body in ({'data': {'id': '123'}}, {'data': {'id': '123', 'associated_metadata': {
                'sensitive_media_warning': {'adult_content': False}}}}, {'data': {'id': '999',
                'associated_metadata': {'sensitive_media_warning': {'adult_content': True}}}}):
            session = Session(me(), initialized(), Response({'data': {}}),
                Response({'data': {}}), Response(body))
            self.error(self.publisher(session), 'adult_warning_unconfirmed', video=self.video)
            self.assertEqual(len(session.calls), 5)

    def test_metadata_http_error_stops_without_post(self):
        session = Session(me(), initialized(), Response({'data': {}}), Response({'data': {}}),
                          Response({'title': 'Forbidden'}, 403))
        self.error(self.publisher(session), 'forbidden', video=self.video)
        self.assertEqual(len(session.calls), 5)

    def test_non_sensitive_explicit_input_skips_metadata(self):
        session = Session(me(), initialized(), Response({'data': {}}), Response({'data': {}}),
                          Response({'data': {'id': '456'}}, 201))
        self.publisher(session).publish('', self.video, adult=False)
        self.assertFalse(any(c[1].endswith('/metadata') for c in session.calls))

    def test_error_classification_and_secret_redaction(self):
        token = 'secret/test-token'
        for code, category in ((64, 'account_suspended'), (326, 'account_locked'),
                               (226, 'automation_flagged'), (344, 'unknown_code_344')):
            self.data = self.root / ('data-' + str(code))
            session = Session(Response({'type': 'about:blank', 'title': 'Rejected',
                'detail': 'Bearer ' + token, 'errors': [{'code': code, 'message': token,
                    'access_token': 'other-secret', 'extra': 'x' * 5000}]}, 403))
            error = self.error(self.publisher(session, token=token), category)
            self.assertEqual(error.stage, 'identity')
            self.assertEqual(len(session.calls), 1)
            retry = Session()
            self.error(self.publisher(retry), 'account_review_hold')
            self.assertEqual(retry.calls, [])
        lines = (self.data / 'publishing_incidents.jsonl').read_text()
        self.assertNotIn(token, lines)
        self.assertNotIn('other-secret', lines)
        self.assertLess(len(lines), 12000)
        self.assertEqual(json.loads(lines.splitlines()[0])['codes'], [344])

    def test_generic_403_is_not_suspension_and_200_errors_are_checked(self):
        for status in (403, 200):
            session = Session(Response({'errors': [{'code': 999, 'message': 'Rejected'}]}, status))
            self.error(self.publisher(session), 'forbidden' if status == 403 else 'api_error')
            self.assertEqual(len(session.calls), 1)

    def test_create_timeout_and_malformed_success_never_retry(self):
        for index, response in enumerate((TimeoutError('Bearer secret-test-token'),
                         Response(ValueError('invalid'), 201), Response({'data': {}}, 201),
                         Response([], 201), Response({'title': 'Unavailable'}, 503))):
            self.data = self.root / ('data-' + str(index))
            session = Session(me(), response)
            self.error(self.publisher(session), 'create_outcome_unknown')
            self.assertEqual(len(session.calls), 2)
            retry = Session()
            self.error(self.publisher(retry), 'account_review_hold')
            self.assertEqual(retry.calls, [])

    def test_existing_hold_and_expected_user_id_block(self):
        self.data.mkdir()
        holds = self.data / 'official_publisher_holds'
        holds.mkdir()
        (holds / 'example.json').write_text('{}')
        session = Session()
        self.error(self.publisher(session, expected_username='EXAMPLE'), 'account_review_hold')
        self.assertEqual(session.calls, [])
        self.data = self.root / 'other-data'
        session = Session(me())
        self.error(self.publisher(session, expected_user_id='999'), 'username_mismatch')
        self.assertEqual(len(session.calls), 1)

    def test_receipt_failure_keeps_review_hold_and_never_reposts(self):
        session = Session(me(), Response({'data': {'id': '456'}}, 201))
        self.data.mkdir()
        (self.data / 'publishing_receipts.jsonl').mkdir()  # Deliberately unwritable as a file.
        self.error(self.publisher(session), 'accepted_post_receipt_unavailable')
        self.assertEqual(len(session.calls), 2)
        self.assertTrue((self.data / 'official_publisher_holds' / 'example.json').exists())

    def test_bench_rechecked_before_create(self):
        publisher = self.publisher(Session(me()))
        with mock.patch.object(publisher, '_bench_check',
                side_effect=[None, op.PublishError('preflight', 'account_locally_benched')]):
            self.error(publisher, 'account_locally_benched')
        self.assertEqual(len(publisher.session.calls), 1)


if __name__ == '__main__':
    unittest.main()
