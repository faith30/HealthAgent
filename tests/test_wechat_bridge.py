"""Bridge contract tests with fake UI/API objects; never imports wxautox4.

Run from the project directory: python -m unittest discover -s tests -v
Passing these tests does not establish real WeChat compatibility or delivery.
"""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

from wechat_bridge import ApiClient, Bridge, BridgeError, FixedWindow, GroupConfig, SessionTracker, MAX_IMAGE_BYTES


GROUP = "测试家庭群"
CONFIG = {"group_name": GROUP, "patient_sender": "爸爸", "caregiver_sender": "家属", "live_enabled": True}


class FakeMessage:
    def __init__(self, ui_id, text="血压 135/82 mmHg", sender="爸爸", attr="friend", kind="text", group=GROUP):
        self.id = ui_id
        self.content = text
        self.sender = sender
        self.attr = attr
        self.type = kind
        self.hash = text  # Equal content deliberately produces a repeated hash.
        self.group = group

    def chat_info(self):
        return {"chat_type": "group", "chat_name": self.group}


class FakeWeChat:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.group = GROUP
        self.online = True
        self.chat_calls = []
        self.send_calls = []
        self.send_error = None

    def ChatWith(self, *, who, exact):
        self.chat_calls.append((who, exact))
        self.group = who

    def ChatInfo(self):
        return {"chat_type": "group", "chat_name": self.group}

    def IsOnline(self):
        return self.online

    def GetAllMessage(self):
        return list(self.messages)

    def SendMsg(self, *, msg, clear):
        self.send_calls.append((msg, clear))
        if self.send_error:
            raise self.send_error
        return True


class FakeImageMessage(FakeMessage):
    def __init__(self, ui_id, *, sender="爸爸", attr="friend", group=GROUP, behavior=None):
        super().__init__(ui_id, text="[图片]", sender=sender, attr=attr, kind="image", group=group)
        self.download_calls = []
        self.behavior = behavior
        self.image_bytes = b"FAKE_IMAGE_CONTENT_FOR_TRANSFER_TEST_ONLY"

    def download(self, *, dir_path, original):
        self.download_calls.append((Path(dir_path), original))
        if self.behavior:
            return self.behavior(Path(dir_path))
        target = Path(dir_path) / "image.png"
        target.write_bytes(self.image_bytes)
        return target


class FakeApi:
    def __init__(self, config=None):
        self.config = copy.deepcopy(config or CONFIG)
        self.calls = []
        self.outbox = []
        self.repeat_lease = False
        self.fail_sent_ack = False

    def request(self, path, body=None):
        self.calls.append((path, copy.deepcopy(body)))
        if path == "/api/bridge/config":
            return copy.deepcopy(self.config)
        if path in ("/api/bridge/message", "/api/bridge/image", "/api/bridge/image-error"):
            # A bridge must not send this reply directly: the server owns queuing.
            return {"accepted": True, "reply": "SERVER_REPLY_NOT_TO_SEND_DIRECTLY"}
        if path == "/api/bridge/outbox":
            result = copy.deepcopy(self.outbox[:1])
            if result and not self.repeat_lease:
                self.outbox.pop(0)
            return {"messages": result}
        if path == "/api/bridge/ack":
            if self.fail_sent_ack and body["status"] == "sent":
                raise BridgeError("Simulated ACK timeout; send result is uncertain.")
            return {"ok": True}
        if path == "/api/bridge/heartbeat":
            return {"ok": True}
        raise AssertionError("Unexpected API route: " + path)

    def heartbeat(self, status, detail):
        self.request("/api/bridge/heartbeat", {"status": status, "detail": detail})

    def bodies(self, route):
        return [body for path, body in self.calls if path == route]


class BridgeTests(unittest.TestCase):
    def make_bridge(self, *, send=True, live=True, initial=None):
        api = FakeApi(dict(CONFIG, live_enabled=live))
        wx = FakeWeChat(initial)
        bridge = Bridge(api, FixedWindow(wx, GROUP), GroupConfig.from_dict(api.config), send)
        bridge.initialize()
        return bridge, api, wx

    def enqueue(self, api, *, group=GROUP):
        api.outbox.append({"id": "outbound-1", "group_name": group, "text": "测试提示"})

    def test_each_disabled_gate_prevents_plaintext_and_outbox_access(self):
        for send, live in ((False, True), (True, False), (False, False)):
            with self.subTest(send=send, live=live):
                bridge, api, wx = self.make_bridge(send=send, live=live)
                wx.messages.append(FakeMessage("new", text="PRIVATE_PATIENT_CONTENT"))
                self.enqueue(api)
                bridge.tick()
                self.assertEqual(api.bodies("/api/bridge/message"), [])
                self.assertFalse(any(path == "/api/bridge/outbox" for path, _ in api.calls))
                self.assertNotIn("PRIVATE_PATIENT_CONTENT", json.dumps(api.calls))
                self.assertEqual(wx.send_calls, [])
                self.assertEqual(api.bodies("/api/bridge/heartbeat")[-1]["status"], "dry_run")

    def test_visible_history_seeded_and_new_message_processed_once(self):
        bridge, api, wx = self.make_bridge(initial=[FakeMessage("old")])
        bridge.tick()
        self.assertEqual(api.bodies("/api/bridge/message"), [])
        wx.messages.append(FakeMessage("new"))
        bridge.tick()
        bridge.tick()
        posts = api.bodies("/api/bridge/message")
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0]["message_id"].startswith("wx-session:"))

    def test_only_exact_allowed_senders_incoming_text(self):
        bridge, api, wx = self.make_bridge()
        wx.messages = [FakeMessage("father"), FakeMessage("carer", sender="家属"),
                       FakeMessage("outsider", sender="其他人"), FakeMessage("near", sender="爸爸 "),
                       FakeMessage("self", sender="爸爸", attr="self"),
                       FakeMessage("image", kind="image"), FakeMessage("voice", kind="voice")]
        bridge.tick()
        self.assertEqual([p["sender"] for p in api.bodies("/api/bridge/message")], ["爸爸", "家属"])

    def test_equal_readings_with_distinct_ui_ids_are_not_collapsed(self):
        bridge, api, wx = self.make_bridge()
        wx.messages.append(FakeMessage("reading-1"))
        bridge.tick()
        wx.messages.append(FakeMessage("reading-2"))
        bridge.tick()
        posts = api.bodies("/api/bridge/message")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["text"], posts[1]["text"])
        self.assertNotEqual(posts[0]["message_id"], posts[1]["message_id"])

    def test_server_reply_is_not_sent_directly(self):
        bridge, api, wx = self.make_bridge()
        wx.messages.append(FakeMessage("new"))
        bridge.tick()
        self.assertEqual(len(api.bodies("/api/bridge/message")), 1)
        self.assertEqual(wx.send_calls, [])

    def test_several_slow_answers_do_not_expire_heartbeat_cumulatively(self):
        bridge, api, wx = self.make_bridge()
        wx.messages = [FakeMessage("first"), FakeMessage("second"), FakeMessage("third")]
        original = api.request
        clock = {"now": 0, "heartbeat": 0}

        def request(path, body=None):
            if path == "/api/bridge/heartbeat":
                clock["heartbeat"] = clock["now"]
            elif path == "/api/bridge/message":
                clock["now"] += 25  # Model timeout boundary, no actual waiting.
                if clock["now"] - clock["heartbeat"] >= 45:
                    raise BridgeError("Simulated backend heartbeat expiry")
            return original(path, body)

        api.request = request
        bridge.tick()
        self.assertEqual(len(api.bodies("/api/bridge/message")), 3)
        self.assertEqual(clock["now"], 75)
        self.assertLess(clock["now"] - clock["heartbeat"], 45)

    def test_queue_send_and_ack_once_without_switching_chat(self):
        bridge, api, wx = self.make_bridge()
        self.enqueue(api)
        bridge.tick()
        bridge.tick()
        self.assertEqual(wx.chat_calls, [(GROUP, True)])
        self.assertEqual(wx.send_calls, [("测试提示", True)])
        self.assertEqual(api.bodies("/api/bridge/ack"), [{"id": "outbound-1", "status": "sent"}])

    def test_ack_timeout_does_not_allow_uncertain_send_retry(self):
        bridge, api, wx = self.make_bridge()
        self.enqueue(api)
        api.fail_sent_ack = True
        api.repeat_lease = True  # Even a faulty server replay must not resend.
        with self.assertRaises(BridgeError):
            bridge.tick()
        with self.assertRaises(BridgeError):
            bridge.tick()
        self.assertEqual(len(wx.send_calls), 1)
        self.assertEqual(len(api.bodies("/api/bridge/ack")), 1)

    def test_send_exception_stops_and_never_repeats_attempt(self):
        bridge, api, wx = self.make_bridge()
        self.enqueue(api)
        api.repeat_lease = True
        wx.send_error = RuntimeError("Simulated uncertain UI result")
        with self.assertRaises(BridgeError):
            bridge.tick()
        with self.assertRaises(BridgeError):
            bridge.tick()
        self.assertEqual(len(wx.send_calls), 1)
        self.assertEqual(api.bodies("/api/bridge/ack")[0]["status"], "failed")

    def test_current_group_mismatch_stops_before_processing_or_send(self):
        bridge, api, wx = self.make_bridge()
        wx.group = "其他群"
        wx.messages.append(FakeMessage("new"))
        self.enqueue(api)
        with self.assertRaises(BridgeError):
            bridge.tick()
        self.assertEqual(wx.send_calls, [])
        self.assertEqual(api.bodies("/api/bridge/message"), [])
        self.assertEqual(wx.chat_calls, [(GROUP, True)])

    def test_outbound_wrong_group_is_failed_without_sending(self):
        bridge, api, wx = self.make_bridge()
        self.enqueue(api, group="其他群")
        with self.assertRaises(BridgeError):
            bridge.tick()
        self.assertEqual(wx.send_calls, [])
        self.assertEqual(api.bodies("/api/bridge/ack")[0]["status"], "failed")

    def test_route_config_change_requires_manual_restart(self):
        bridge, api, wx = self.make_bridge()
        api.config["caregiver_sender"] = "新家属"
        with self.assertRaises(BridgeError):
            bridge.tick()
        self.assertEqual(wx.chat_calls, [(GROUP, True)])
        self.assertEqual(wx.send_calls, [])

    def test_entire_viewport_id_rebuild_stops_without_history_replay(self):
        bridge, api, wx = self.make_bridge(initial=[FakeMessage("old-id")])
        wx.messages = [FakeMessage("rebuilt-id")]
        with self.assertRaises(BridgeError):
            bridge.tick()
        self.assertEqual(api.bodies("/api/bridge/message"), [])

    def test_missing_duplicate_or_reused_ui_id_stops(self):
        for messages in ([FakeMessage("")], [FakeMessage("a"), FakeMessage("a")],
                         [FakeMessage("a", text="changed content")]):
            with self.subTest(messages=[m.id for m in messages]):
                tracker = SessionTracker()
                tracker.observe([FakeMessage("a")], seed=True)
                with self.assertRaises(BridgeError):
                    tracker.observe(messages)

    def test_message_source_group_is_independently_checked(self):
        bridge, api, wx = self.make_bridge()
        wx.messages.append(FakeMessage("foreign", group="其他群"))
        with self.assertRaises(BridgeError):
            bridge.tick()
        self.assertEqual(api.bodies("/api/bridge/message"), [])

    def test_bridge_api_url_must_be_literal_loopback_without_credentials(self):
        for url in ("http://example.com:8765", "http://127.0.0.1.evil:8765", "http://localhost:8765",
                    "http://user:pass@127.0.0.1:8765", "http://127.0.0.1:8765/path", "https://127.0.0.1:8765"):
            with self.subTest(url=url), self.assertRaises(BridgeError):
                ApiClient(url, "test-token-at-least-sixteen-characters")
        # Constructing the client does not connect or send a request.
        ApiClient("http://127.0.0.1:8765", "test-token-at-least-sixteen-characters")

    def test_image_upload_base64_then_private_temp_cleanup(self):
        bridge, api, wx = self.make_bridge()
        message = FakeImageMessage("photo")
        wx.messages.append(message)
        bridge.tick()
        uploads = api.bodies("/api/bridge/image")
        self.assertEqual(len(uploads), 1)
        self.assertEqual(base64.b64decode(uploads[0]["image_base64"]), message.image_bytes)
        self.assertEqual(uploads[0]["text"], "")
        self.assertEqual(uploads[0]["sender"], "爸爸")
        self.assertEqual(uploads[0]["group_name"], GROUP)
        directory, original = message.download_calls[0]
        self.assertFalse(original)
        self.assertFalse(directory.exists())
        self.assertEqual(wx.send_calls, [])  # Returned reply is not sent directly.

    def test_image_dry_run_never_downloads_or_forwards_content(self):
        for send, live in ((False, True), (True, False), (False, False)):
            with self.subTest(send=send, live=live):
                bridge, api, wx = self.make_bridge(send=send, live=live)
                message = FakeImageMessage("photo")
                wx.messages.append(message)
                bridge.tick()
                self.assertEqual(message.download_calls, [])
                self.assertEqual(api.bodies("/api/bridge/image"), [])
                self.assertEqual(api.bodies("/api/bridge/image-error"), [])
                self.assertNotIn("FAKE_IMAGE_CONTENT", json.dumps(api.calls))

    def test_images_from_self_other_senders_and_initial_history_are_ignored(self):
        history = FakeImageMessage("old")
        bridge, api, wx = self.make_bridge(initial=[history])
        self_image = FakeImageMessage("self-image", attr="self")
        other_image = FakeImageMessage("other-image", sender="其他人")
        wx.messages.extend([self_image, other_image])
        bridge.tick()
        self.assertEqual(api.bodies("/api/bridge/image"), [])
        self.assertTrue(all(not m.download_calls for m in (history, self_image, other_image)))

    def test_image_path_escape_is_rejected_without_deleting_external_file(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "unrelated.png"
            external.write_bytes(b"EXTERNAL_FILE_MUST_REMAIN")
            bridge, api, wx = self.make_bridge()
            message = FakeImageMessage("photo", behavior=lambda directory: external)
            wx.messages.append(message)
            bridge.tick()
            self.assertEqual(api.bodies("/api/bridge/image"), [])
            self.assertEqual(api.bodies("/api/bridge/image-error")[0]["error"], "unsafe_path")
            self.assertEqual(external.read_bytes(), b"EXTERNAL_FILE_MUST_REMAIN")
            self.assertFalse(message.download_calls[0][0].exists())

    def test_image_too_large_reports_once_and_cleans_temp(self):
        def oversized(directory):
            target = directory / "large.png"
            with target.open("wb") as stream:
                stream.truncate(MAX_IMAGE_BYTES + 1)
            return target

        bridge, api, wx = self.make_bridge()
        message = FakeImageMessage("photo", behavior=oversized)
        wx.messages.append(message)
        bridge.tick()
        bridge.tick()
        self.assertEqual(api.bodies("/api/bridge/image"), [])
        self.assertEqual(len(api.bodies("/api/bridge/image-error")), 1)
        self.assertEqual(api.bodies("/api/bridge/image-error")[0]["error"], "too_large")
        self.assertEqual(len(message.download_calls), 1)
        self.assertFalse(message.download_calls[0][0].exists())

    def test_image_download_failure_is_recoverable_and_not_retried(self):
        def raises(directory):
            (directory / "partial.tmp").write_bytes(b"partial")
            raise RuntimeError("simulated library failure")

        for behavior in (raises, lambda directory: {"success": False}, lambda directory: directory):
            with self.subTest(behavior=behavior):
                bridge, api, wx = self.make_bridge()
                message = FakeImageMessage("photo", behavior=behavior)
                wx.messages.append(message)
                bridge.tick()
                wx.messages.append(FakeMessage("later-text"))
                bridge.tick()
                self.assertEqual(len(message.download_calls), 1)
                self.assertEqual(len(api.bodies("/api/bridge/image-error")), 1)
                self.assertEqual(len(api.bodies("/api/bridge/message")), 1)
                self.assertFalse(message.download_calls[0][0].exists())

    def test_image_api_failure_cleans_temp_and_never_reuploads_same_message(self):
        bridge, api, wx = self.make_bridge()
        message = FakeImageMessage("photo")
        wx.messages.append(message)
        original = api.request

        def fails_upload(path, body=None):
            result = original(path, body)
            if path == "/api/bridge/image":
                raise BridgeError("Simulated API timeout after upload")
            return result

        api.request = fails_upload
        with self.assertRaises(BridgeError):
            bridge.tick()
        self.assertFalse(message.download_calls[0][0].exists())
        bridge.tick()  # A caller accidentally continuing still cannot upload again.
        self.assertEqual(len(api.bodies("/api/bridge/image")), 1)
        self.assertEqual(len(message.download_calls), 1)
        self.assertEqual(api.bodies("/api/bridge/image-error"), [])

    def test_image_group_change_during_download_stops_without_upload(self):
        bridge, api, wx = self.make_bridge()

        def switch_group(directory):
            target = directory / "image.png"
            target.write_bytes(b"image")
            wx.group = "其他群"
            return target

        message = FakeImageMessage("photo", behavior=switch_group)
        wx.messages.append(message)
        with self.assertRaises(BridgeError):
            bridge.tick()
        self.assertEqual(api.bodies("/api/bridge/image"), [])
        self.assertEqual(api.bodies("/api/bridge/image-error"), [])
        self.assertFalse(message.download_calls[0][0].exists())

    def test_image_live_switch_off_during_download_prevents_upload(self):
        bridge, api, wx = self.make_bridge()

        def disable_live(directory):
            target = directory / "image.png"
            target.write_bytes(b"image")
            api.config["live_enabled"] = False
            return target

        message = FakeImageMessage("photo", behavior=disable_live)
        wx.messages.append(message)
        bridge.tick()
        self.assertEqual(api.bodies("/api/bridge/image"), [])
        self.assertEqual(api.bodies("/api/bridge/image-error"), [])
        self.assertFalse(message.download_calls[0][0].exists())


if __name__ == "__main__":
    unittest.main()
