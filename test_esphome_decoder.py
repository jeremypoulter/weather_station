"""Compare the C++ decoder used by the ESPHome component with the Python reference.

Compiles tests/decoder_cli.cpp (which includes components/ft0203/ft0203_protocol.h)
and decodes real captured packets with both implementations.
"""

import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ft0203_usb_read import decode_current_packet, validate_packet

ROOT = Path(__file__).parent
FIXTURE = ROOT / "tests" / "fixtures" / "current_packets.hex"
COMPARED_FIELDS = (
    ["indoor_temperature", "indoor_humidity", "absolute_pressure", "relative_pressure",
     "wind_average", "wind_gust", "wind_direction",
     "rain_last_hour", "rain_today", "rain_week", "rain_month", "rain_total"]
    + [f"ch{channel}_{name}" for channel in range(1, 9)
       for name in ("temperature", "humidity", "dew_point", "feels_like")]
)


def fixture_packets() -> list[bytes]:
    packets = []
    for line in FIXTURE.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            packets.append(bytes.fromhex(line))
    return packets


@unittest.skipUnless(shutil.which("g++"), "g++ is required to build the C++ decoder")
class ESPHomeDecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cli = Path(cls.tmp.name) / "decoder_cli"
        subprocess.run(
            ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1",
             str(ROOT / "tests" / "decoder_cli.cpp"), "-o", str(cls.cli)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def run_cli(self, packets: list[bytes]) -> list[dict[str, float | str]]:
        stdin = "".join(packet.hex() + "\n" for packet in packets)
        output = subprocess.run([str(self.cli)], input=stdin, capture_output=True, text=True, check=True).stdout
        decoded, current = [], {}
        for line in output.splitlines():
            if line == "--":
                decoded.append(current)
                current = {}
                continue
            name, value = line.split("=", 1)
            current[name] = value if name == "error" else float(value)
        return decoded

    def test_matches_python_decoder_on_real_packets(self) -> None:
        packets = fixture_packets()
        self.assertGreaterEqual(len(packets), 40)
        for index, (packet, cpp) in enumerate(zip(packets, self.run_cli(packets))):
            python = decode_current_packet(validate_packet(packet))["fields"]
            for name in COMPARED_FIELDS:
                expected = python[name]["value"]
                actual = cpp[name]
                if expected is None:
                    self.assertTrue(math.isnan(actual), f"packet {index} {name}: expected no data, got {actual}")
                else:
                    self.assertFalse(math.isnan(actual), f"packet {index} {name}: unexpected no data")
                    self.assertAlmostEqual(actual, expected, delta=1e-3, msg=f"packet {index} {name}")

    def test_fixture_covers_no_data_and_rain_cases(self) -> None:
        packets = fixture_packets()
        decoded = [decode_current_packet(validate_packet(packet))["fields"] for packet in packets]
        self.assertTrue(any(f["wind_average"]["value"] is None for f in decoded))
        self.assertTrue(any(f["ch1_temperature"]["value"] is None for f in decoded))
        self.assertTrue(any(f["ch5_temperature"]["value"] is not None for f in decoded))
        self.assertTrue(any(f["rain_total"]["value"] is not None for f in decoded))

    def test_corrupt_and_truncated_packets_are_rejected(self) -> None:
        good = fixture_packets()[0]
        bad_checksum = good[:-1] + bytes([(good[-1] + 1) & 0xFF])
        wrong_command = bytearray(good)
        wrong_command[1] = 0x01
        wrong_command[-1] = sum(wrong_command[:-1]) & 0xFF
        results = self.run_cli([bad_checksum, bytes(wrong_command), good[:40]])
        for result in results:
            self.assertEqual(result, {"error": "invalid"})


if __name__ == "__main__":
    unittest.main()
