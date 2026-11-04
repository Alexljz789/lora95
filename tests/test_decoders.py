import os
import tempfile
import unittest

from app import classify, lorawan, meshtastic, profiles
from app.demo import (
    cayenne_climate,
    lorawan_uplink,
    nodeinfo_packet,
    position_packet,
    telemetry_packet,
    text_packet,
)
from app.store import Store


class DecoderTests(unittest.TestCase):
    def test_meshtastic_nodeinfo(self):
        packet = nodeinfo_packet(0x0A1B2C3D, "Mesa Ridge", "MESA", 9, 2, 1234)
        result = classify.classify(packet, {"id": "test"})
        self.assertEqual(result["kind"], "meshtastic")
        self.assertEqual(result["device_name"], "Mesa Ridge")
        self.assertEqual(result["device_model"], "RAK4631")
        self.assertTrue(result["decoded"])
        self.assertEqual(result["packet"]["data"]["nodeinfo"]["role_name"], "ROUTER")

    def test_meshtastic_text_and_telemetry(self):
        text = classify.classify(
            text_packet(0x0A1B2C3D, "Ridge road is clear.", 99), {"id": "test"}
        )
        self.assertEqual(text["text"], "Ridge road is clear.")
        telemetry = classify.classify(
            telemetry_packet(0x0A1B2C3D, 84, 4.06, 12345, 100, 18.4, 38.0),
            {"id": "test"},
        )
        self.assertEqual(telemetry["kind"], "meshtastic")
        self.assertIn("telemetry", telemetry["packet"]["data"])

    def test_encrypted_direct_candidate(self):
        # Captured on the target: a direct Meshtastic-style frame whose channel
        # key is not the public default.  It should be surfaced as a candidate.
        raw = bytes.fromhex(
            "8617ECC7B826B97AB071EB12E40800F55E2D8C2B85D5790009CE2651975B04D2"
            "F46A544532224FF69A0C18693F740EE0"
        )
        packet = meshtastic.decode(raw)
        self.assertIsNotNone(packet)
        self.assertTrue(packet["candidate"])
        self.assertEqual(packet["src"], 0x7AB926B8)
        result = classify.classify(raw, {"id": "test"})
        self.assertEqual(result["kind"], "meshtastic")

    def test_lorawan_cayenne(self):
        frame = lorawan_uplink(0x260B1A2F, 42, 1, cayenne_climate(21.6, 47.0, 3.21))
        result = classify.classify(frame, {"id": "test"})
        self.assertEqual(result["kind"], "lorawan")
        self.assertEqual(result["frame"]["dev_addr"], "260B1A2F")
        self.assertEqual(result["frame"]["fcnt"], 42)
        self.assertEqual(result["frame"]["fport"], 1)
        names = {entry["type_name"] for entry in result["cayenne"]}
        self.assertIn("temperature", names)
        self.assertIn("humidity", names)

    def test_raw_text(self):
        result = classify.classify(b"\xffSENSOR-7 21.4C 48% 3.20V", {"id": "test"})
        self.assertEqual(result["kind"], "raw")
        self.assertIn("SENSOR-7", result["text"])

    def test_ascii_is_not_lorawan(self):
        raw = b"LORA95 TEST FRAME 1234567890"
        self.assertIsNone(lorawan.decode(raw))
        self.assertEqual(classify.classify(raw, {"id": "test"})["kind"], "raw")

    def test_profiles(self):
        self.assertEqual(profiles.meshtastic_frequency("US", 250.0, "LongFast"), 906.875)
        self.assertEqual(profiles.meshtastic_frequency("EU868", 250.0, "LongFast"), 869.525)
        quick = profiles.build_profiles("US", "quick")
        self.assertGreater(len(quick), 20)
        self.assertTrue(any(p.frequency_mhz == 906.875 and p.protocol == "meshtastic" for p in quick))
        custom = profiles.build_profiles("US", "custom", custom_frequency_mhz=915.0, custom_sf=9)
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0].frequency_hz, 915_000_000)


class StoreTests(unittest.TestCase):
    def test_demo_burst_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "lora95.db")
            store = Store(db)
            from app.demo import DemoFeed

            DemoFeed(store.ingest_packet, store.ingest_signal).burst()
            snapshot = store.snapshot()
            kinds = {device["kind"] for device in snapshot}
            self.assertIn("meshtastic", kinds)
            self.assertIn("lorawan", kinds)
            self.assertIn("raw", kinds)
            self.assertIn("signal", kinds)
            mesa = next(d for d in snapshot if d["name"] == "Mesa Ridge")
            detail = store.device(mesa["id"])
            self.assertGreaterEqual(len(detail["messages"]), 1)
            self.assertGreaterEqual(len(detail["packets"]), 3)
            # Re-open the same database and verify the device survives.
            store2 = Store(db)
            names = {d["name"] for d in store2.snapshot()}
            self.assertIn("Mesa Ridge", names)
            store2.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
