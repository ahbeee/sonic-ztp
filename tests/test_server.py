import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.config = root / "dhcpd.conf"
        cls.leases = root / "dhcpd.leases"
        cls.config.write_text("subnet 192.0.2.0 netmask 255.255.255.0 {}\n")
        cls.leases.write_text(
            """lease 192.0.2.10 {
  starts 4 2026/08/20 01:00:00;
  ends 4 2026/08/20 02:00:00;
  binding state active;
  hardware ethernet 52:54:00:12:34:56;
  set vendor-class-identifier = "onie_vendor:test";
}
lease 192.0.2.10 {
  starts 4 2026/08/20 02:00:00;
  ends 4 2026/08/20 03:00:00;
  binding state free;
  hardware ethernet 52:54:00:12:34:56;
}
"""
        )
        os.environ.update(
            ZTP_BASE_DIR=str(root),
            ZTP_DATA_DIR=str(root / "data"),
            ZTP_DHCP_CONFIG=str(cls.config),
            ZTP_DHCP_LEASES=str(cls.leases),
            ZTP_ARTIFACT_DIR=str(root / "artifacts"),
        )
        source = Path(__file__).resolve().parents[1] / "server.py"
        spec = importlib.util.spec_from_file_location("ztp_test_server", source)
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)
        cls.server.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_initial_candidate_copies_live_configuration(self):
        self.assertEqual(self.server.CANDIDATE_PATH.read_text(), self.config.read_text())

    def test_latest_lease_record_wins(self):
        leases = self.server.parse_leases()
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0]["address"], "192.0.2.10")
        self.assertEqual(leases[0]["state"], "free")

    def test_valid_and_invalid_dhcp_configuration(self):
        valid, _ = self.server.validate_config(self.config.read_text())
        invalid, _ = self.server.validate_config("this is not dhcp syntax")
        self.assertTrue(valid)
        self.assertFalse(invalid)


if __name__ == "__main__":
    unittest.main()
