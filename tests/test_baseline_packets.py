"""Packet construction only: synthetic inputs and no transport calls."""
import unittest
from unittest.mock import patch

from core import proto


class BaselinePacketTests(unittest.TestCase):
    def setUp(self):
        signer = patch.object(proto, "sign_ws", return_value="offline-signature")
        signer.start()
        self.addCleanup(signer.stop)

    def assert_context(self, packet, platform="windows", device_platform="web_pc"):
        # Both outer headers and nested request context must carry the profile.
        for key, value in (("os", platform), ("device_platform", device_platform)):
            self.assertIn(proto.f_header(key, value), packet)
            self.assertIn(proto.f_ctx(key, value), packet)
        self.assertIn(proto.f_ctx("tt-ticket-guard-public-key", "fake-public"), packet)
        self.assertIn(proto.f_ctx("tt-ticket-guard-client-data", "fake-client-data"), packet)

    def test_text_default_and_explicit_platform_context(self):
        for settings in ({}, {"platform": "android", "device_platform": "web"}):
            with self.subTest(settings=settings):
                packet, short_id, client_id = proto.build_ws_packet(
                    "fake-conversation", 123, "offline text", "fake-device", "fake-token",
                    tt_public_key="fake-public", tt_client_data="fake-client-data",
                    client_id="fake-request", **settings,
                )
                self.assert_context(packet, **settings)
                self.assertEqual((short_id, client_id), (123, "fake-request"))
                self.assertIn(b"offline text", packet)
                self.assertIn(b"fake-conversation", packet)

    def test_legacy_builders_still_construct_offline_packets(self):
        common = dict(conv_id="fake-conversation", device_id="fake-device",
                      sdk_ms_token="fake-token", tt_public_key="fake-public",
                      tt_client_data="fake-client-data")
        cases = [
            (proto.build_video_share_packet, {"item_detail": {"id": "123"}}),
            (proto.build_reaction_packet, {"msg_type": 123, "emoji": "x", "sender_id": "456"}),
            (proto.build_reaction_packet, {"msg_type": 123, "emoji": "x", "sender_id": "456", "remove": True}),
            (proto.build_delete_packet, {"msg_type": 123}),
            (proto.build_delete_everyone_packet, {"msg_type": 123, "msg_id": 456}),
        ]
        for builder, arguments in cases:
            with self.subTest(builder=builder.__name__, arguments=arguments):
                result = builder(**common, **arguments)
                packet = result[0] if isinstance(result, tuple) else result
                self.assert_context(packet)
                self.assertIn(b"fake-conversation", packet)
