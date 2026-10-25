import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from server import App, Handler, ThreadingHTTPServer, TZ, stamp, InputError


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(self.temp.name)

    def tearDown(self):
        self.app.db.close()
        self.temp.cleanup()

    def live(self):
        self.app.update_config({'wechat': {'group_name': '测试家庭群', 'patient_sender': '爸爸', 'caregiver_sender': '家属'}})
        self.app.bridge_heartbeat({'status': 'dry_run'})
        self.app.update_config({'wechat': {'live_enabled': True}})
        self.app.bridge_heartbeat({'status': 'ready'})

    def test_new_profile_is_unknown_and_saved_profile_survives_restart(self):
        profile = self.app.state()['config']['profile']
        self.assertIsNone(profile['age'])
        self.assertIsNone(profile['stroke_months'])
        self.assertEqual(profile['swallowing'], 'unknown')
        self.assertEqual(profile['notes'], '')
        self.app.update_config({'profile': {'age': None, 'stroke_months': None}})
        self.app.update_config({'profile': {'age': 70, 'stroke_months': 1}})
        self.app.db.close()
        self.app = App(self.temp.name)
        self.assertEqual(self.app.config['profile']['age'], 70)
        self.assertEqual(self.app.config['profile']['stroke_months'], 1)

    def test_records_only_after_confirmation_and_not_twice(self):
        out = self.app.process_message('血压138/82 mmHg，空腹血糖7.2 mmol/L')
        self.assertEqual(len(self.app.state()['measurements']), 0)
        self.app.confirm(out['pending_id'])
        self.assertEqual(len(self.app.state()['measurements']), 2)
        with self.assertRaises(InputError):
            self.app.confirm(out['pending_id'])

    def test_missing_unit_confirmation_and_historical_time(self):
        result = self.app.process_message('昨天空腹血糖7.2')
        with self.assertRaises(InputError):
            self.app.confirm(result['pending_id'])
        yesterday = stamp(datetime.now(TZ) - timedelta(days=1))
        self.app.confirm(result['pending_id'], measured_at=yesterday)
        item = self.app.state()['measurements'][0]
        self.assertEqual(item['measured_at'], yesterday)
        self.assertTrue(item['unit_confirmed'])

    def test_test_chat_never_queues_live_messages(self):
        self.live()
        self.app.process_message('苹果能吃吗')
        self.assertFalse(self.app.lease_outbox()['messages'])

    def test_bridge_dedup_and_sender_guard(self):
        self.live()
        args = dict(text='空腹血糖7.2 mmol/L', sender='爸爸', source='wechat', message_id='example-1')
        self.app.process_message(**args)
        result = self.app.process_message(**args)
        self.assertFalse(result['accepted'])
        self.assertEqual(len(self.app.state()['pending']), 1)
        result = self.app.process_message('血压140/80', '外人', 'wechat', 'example-2')
        self.assertFalse(result['accepted'])

    def test_caregiver_must_mark_proxy(self):
        self.live()
        self.app.process_message('我的血压130/80', '家属', 'wechat', 'a')
        self.assertEqual(len(self.app.state()['pending']), 0)
        self.app.process_message('爸爸血压130/80', '家属', 'wechat', 'b')
        self.assertEqual(len(self.app.state()['pending']), 1)

    def test_live_confirmation_command(self):
        self.live()
        first = self.app.process_message('血压138/82', '爸爸', 'wechat', 'one')
        self.app.process_message('确认 ' + first['pending_id'], '爸爸', 'wechat', 'two')
        self.assertEqual(len(self.app.state()['measurements']), 1)

    def test_no_cross_channel_confirm(self):
        self.live()
        first = self.app.process_message('血压138/82')
        result = self.app.process_message('确认 ' + first['pending_id'], '爸爸', 'wechat', 'two')
        self.assertTrue(result['needs_correction'])
        self.assertEqual(len(self.app.state()['measurements']), 0)

    def test_invalid_confirmation_is_recoverable(self):
        self.live()
        result = self.app.process_message('确认 00000000', '爸爸', 'wechat', 'invalid')
        self.assertTrue(result['accepted'])
        self.assertTrue(result['needs_correction'])
        self.assertTrue(self.app.config['wechat']['live_enabled'])

    def test_disabled_reminder_has_no_late_followup(self):
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        entry = {'id':'bp', 'time':now.strftime('%H:%M'), 'label':'血压', 'kind':'bp', 'enabled':True, 'grace_minutes':10}
        self.app.update_config({'reminders':[entry]})
        self.app.scheduler(now)
        entry['enabled'] = False
        self.app.update_config({'reminders':[entry]})
        self.app.scheduler(now+timedelta(minutes=11))
        self.assertEqual(len(self.app.state()['alerts']), 0)

    def test_low_glucose_alert_before_confirmation(self):
        result = self.app.process_message('血糖3.0 mmol/L')
        self.assertIn('立即', result['reply'])
        self.assertEqual(self.app.state()['alerts'][0]['level'], 'attention')
        self.assertEqual(len(self.app.state()['measurements']), 0)

    def test_delivery_lease_and_no_retry_on_uncertain_send(self):
        self.live()
        self.app.process_message('水果能吃吗', '爸爸', 'wechat', 'one')
        leased = self.app.lease_outbox()['messages']
        self.assertEqual(len(leased), 1)
        self.assertFalse(self.app.lease_outbox()['messages'])
        self.app.scheduler(datetime.now(TZ) + timedelta(seconds=90))
        status = self.app.db.execute('SELECT status FROM outbox WHERE id=?', (leased[0]['id'],)).fetchone()[0]
        self.assertEqual(status, 'uncertain')

    def test_route_change_disarms_and_cancels_queue(self):
        self.live()
        self.app.process_message('苹果能吃吗', '爸爸', 'wechat', 'one')
        self.app.update_config({'wechat': {'group_name': '另一个群'}})
        self.assertFalse(self.app.config['wechat']['live_enabled'])
        self.assertFalse(self.app.lease_outbox()['messages'])

    def test_route_change_cancels_old_pending(self):
        self.live()
        result = self.app.process_message('血压138/82', '爸爸', 'wechat', 'one')
        self.app.update_config({'wechat': {'group_name': '新群'}})
        with self.assertRaises(InputError):
            self.app.confirm(result['pending_id'])

    def test_inflight_model_result_cannot_cross_routes(self):
        self.live()
        def delayed_reply(_):
            self.app.update_config({'wechat': {'group_name': '新群'}})
            self.app.update_config({'wechat': {'live_enabled': True}})
            return '旧群的健康问答', 'model'
        with patch.object(self.app, 'answer', delayed_reply):
            result = self.app.process_message('苹果能吃吗', '爸爸', 'wechat', 'one')
        self.assertTrue(result['cancelled'])
        self.assertEqual(len(self.app.state()['outbox']), 0)

    def test_model_adapter_and_failure_fallback(self):
        self.app.update_config({'model': {'api_key':'test-only', 'model':'test-model', 'enabled':True}})
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps({'choices':[{'message':{'content':'一般饮食参考'}}]}).encode()
        class Opener:
            def open(self, req, timeout):
                self.req = req
                return Response()
        opener = Opener()
        with patch('server.build_opener', return_value=opener):
            result = self.app.process_message('苹果能吃吗')
        self.assertEqual(result['answer_source'], 'model')
        request_body = json.loads(opener.req.data)
        self.assertEqual(request_body['model'], 'test-model')
        self.assertNotIn('test-only', result['reply'])
        with patch('server.build_opener', side_effect=OSError('fail')):
            result = self.app.process_message('苹果能吃吗')
        self.assertEqual(result['answer_source'], 'offline_fallback')

    def test_extreme_glucose_and_acute_chest_pain(self):
        result = self.app.process_message('血糖20 mmol/L')
        self.assertEqual(result['assessment']['level'], 'attention')
        result = self.app.process_message('爸爸现在胸痛')
        self.assertEqual(result['assessment']['level'], 'urgent')
        self.assertIn('120', result['reply'])

    def test_heartbeat_error_disarms(self):
        self.live()
        self.app.bridge_heartbeat({'status': 'error'})
        self.app.scheduler()
        self.assertFalse(self.app.config['wechat']['live_enabled'])

    def test_restart_disarms(self):
        self.live()
        self.app.process_message('苹果能吃吗', '爸爸', 'wechat', 'one')
        self.app.db.close()
        self.app = App(self.temp.name)
        self.assertFalse(self.app.config['wechat']['live_enabled'])
        self.assertFalse(self.app.lease_outbox()['messages'])

    def test_scheduler_dedup_followup_and_manual_isolation(self):
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        self.app.update_config({'reminders': [{'id':'bp', 'time':now.strftime('%H:%M'), 'label':'血压', 'kind':'bp', 'enabled':True, 'grace_minutes':10}]})
        self.app.scheduler(now, force_test=True)
        self.app.scheduler(now)
        self.app.scheduler(now)
        self.assertEqual(len(self.app.state()['outbox']), 2)
        self.app.scheduler(now + timedelta(minutes=11))
        self.app.scheduler(now + timedelta(minutes=12))
        self.assertEqual(len(self.app.state()['alerts']), 1)
        self.assertIn('不代表没有测量', self.app.state()['alerts'][0]['message'])

    def test_test_reading_does_not_satisfy_live_reminder(self):
        self.live()
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        self.app.update_config({'reminders': [{'id':'bp', 'time':now.strftime('%H:%M'), 'label':'血压', 'kind':'bp', 'enabled':True, 'grace_minutes':10}]})
        test_record = self.app.process_message('血压138/82')
        self.app.confirm(test_record['pending_id'])
        self.app.scheduler(now)
        self.app.scheduler(now + timedelta(minutes=11))
        self.assertTrue(any('尚未收到' in a['message'] and a['source']=='wechat' for a in self.app.state()['alerts']))

    def test_menu_requires_confirmed_plan(self):
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        self.app.update_config({'reminders': [{'id':'menu', 'time':now.strftime('%H:%M'), 'label':'菜单', 'kind':'menu', 'enabled':True, 'grace_minutes':10}]})
        self.app.scheduler(now)
        self.assertEqual(len(self.app.state()['outbox']), 0)
        days = ['']*7
        days[now.weekday()] = '此处为已核对的测试菜单'
        self.app.update_config({'meal_plan': {'days': days, 'confirmed': True}})
        self.app.scheduler(now, force_test=True)
        self.assertIn('已核对的测试菜单', self.app.state()['outbox'][0]['text'])

    def test_secrets_not_in_state_and_invalid_target(self):
        self.app.update_config({'model': {'api_key':'SAMPLE-SECRET'}})
        self.assertNotIn('SAMPLE-SECRET', json.dumps(self.app.state()))
        self.assertNotIn('bridge_token', self.app.state()['config'])
        with self.assertRaises(InputError):
            self.app.update_config({'doctor_targets': {'bp_systolic_high': 100, 'bp_systolic_low': 120}})

    def test_multiple_meal_contexts(self):
        result = self.app.process_message('空腹血糖7.2 mmol/L 餐后2小时血糖8.5 mmol/L')
        self.assertEqual([i['context'] for i in result['items']], ['fasting','postmeal2h'])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = App(self.temp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.app = self.app
        self.url = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.db.close()
        self.temp.cleanup()

    def test_csrf_and_origin_and_bridge_auth(self):
        for headers in ({}, {'X-CSRF-Token': self.app.csrf, 'Origin':'https://untrusted.example'}):
            with self.assertRaises(HTTPError) as caught:
                urlopen(Request(self.url + '/api/message', data=b'{"text":"hi"}', headers=headers))
            self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.url + '/api/bridge/config')
        self.assertEqual(caught.exception.code, 403)
        req = Request(self.url + '/api/message', data=json.dumps({'text':'苹果能吃吗'}).encode(), headers={'X-CSRF-Token':self.app.csrf})
        with urlopen(req) as response:
            self.assertEqual(response.status, 200)

    def test_only_one_listener_can_claim_the_port(self):
        with self.assertRaises(OSError):
            ThreadingHTTPServer(self.server.server_address, Handler)


if __name__ == '__main__':
    unittest.main()
