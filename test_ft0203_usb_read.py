"""Regression checks for validated FT0203 packet framing and core decoding."""

import json
import unittest
from pathlib import Path

from ft0203_usb_read import CURRENT_COMMAND, decode_current_packet, validate_packet


CAPTURE_PATH = Path("ft0203_usb_read_1789836512.ndjson")


class FT0203USBReadTests(unittest.TestCase):
    def test_captured_packets_validate_and_decode(self) -> None:
        with CAPTURE_PATH.open(encoding="utf-8") as source:
            records = [json.loads(line) for line in source]
        packets = [
            bytes.fromhex(record["hex"])
            for record in records
            if record["type"] == "packet"
        ]
        self.assertEqual(len(packets), 6)
        self.assertEqual(validate_packet(packets[0]), packets[0])
        current_packets = [packet for packet in packets if packet[1] == CURRENT_COMMAND]
        self.assertEqual(len(current_packets), 5)
        readings = [decode_current_packet(validate_packet(packet)) for packet in current_packets]
        self.assertEqual({reading["packet_length"] for reading in readings}, {76})
        self.assertEqual({reading["fields"]["indoor_humidity"]["value"] for reading in readings}, {63})
        self.assertEqual({reading["fields"]["outdoor_humidity"]["value"] for reading in readings}, {81})
        self.assertEqual(readings[0]["fields"]["relative_pressure"]["status"], "validated")
        self.assertEqual(readings[0]["fields"]["absolute_pressure"]["status"], "validated")
        self.assertEqual(readings[0]["fields"]["dew_point"]["status"], "derived_validated")
        self.assertEqual(readings[0]["fields"]["feels_like"]["status"], "derived_validated")
        self.assertEqual(readings[0]["fields"]["wind_gust"]["status"], "validated")
        self.assertEqual(readings[0]["fields"]["wind_direction"]["status"], "validated")
        self.assertEqual(readings[0]["fields"]["wind_average"]["status"], "validated")
        self.assertAlmostEqual(readings[0]["fields"]["indoor_temperature"]["value"], 23.89, places=2)
        self.assertAlmostEqual(readings[0]["fields"]["outdoor_temperature"]["value"], 19.78, places=2)

    def test_invalid_checksum_is_rejected(self) -> None:
        packet = bytearray.fromhex("04800286")
        packet[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "Checksum mismatch"):
            validate_packet(bytes(packet))


if __name__ == "__main__":
    unittest.main()
