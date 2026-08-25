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
        cls.config.write_text("""#option domain-name-servers wrong.example.org;
default-lease-time 600;
max-lease-time 7200;
subnet 192.0.2.0 netmask 255.255.255.0 {
  option routers 192.0.2.1;
  option domain-name-servers 192.0.2.2;
  pool { range 192.0.2.100 192.0.2.199; }
}
""")
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
  uid "SONiC##TEST##SERIAL";
}
"""
        )
        os.environ.update(
            ZTP_BASE_DIR=str(root),
            ZTP_DATA_DIR=str(root / "data"),
            ZTP_DHCP_CONFIG=str(cls.config),
            ZTP_DHCP_LEASES=str(cls.leases),
            ZTP_ARTIFACT_DIR=str(root / "artifacts"),
            ZTP_GENERATED_DIR=str(root / "generated"),
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
        self.assertEqual(leases[0]["option61"], "SONiC##TEST##SERIAL")

    def test_valid_and_invalid_dhcp_configuration(self):
        valid, _ = self.server.validate_config(self.config.read_text())
        invalid, _ = self.server.validate_config("this is not dhcp syntax")
        self.assertTrue(valid)
        self.assertFalse(invalid)

    def test_scope_parser_and_updater(self):
        scope = self.server.parse_scope(self.config.read_text())
        self.assertEqual(scope["subnet"], "192.0.2.0/24")
        self.assertEqual(scope["dns"], "192.0.2.2")
        scope.update(pool_start="192.0.2.50", pool_end="192.0.2.99", gateway="192.0.2.254", dns="192.0.2.3,192.0.2.4", default_lease="900", max_lease="3600")
        updated = self.server.update_scope(self.config.read_text(), scope)
        self.assertIn("range 192.0.2.50 192.0.2.99;", updated)
        self.assertIn("option routers 192.0.2.254;", updated)
        self.assertTrue(self.server.validate_config(updated)[0])

    def test_managed_reservation_block_is_replaceable(self):
        first = self.server.update_reservations(self.config.read_text(), [{"hostname":"leaf-01", "mac":"52:54:00:12:34:56", "ip_address":"192.0.2.20"}])
        second = self.server.update_reservations(first, [])
        self.assertEqual(second.count("BEGIN SONIC-ZTP MANAGED RESERVATIONS"), 1)
        self.assertNotIn("leaf-01", second)
        self.assertTrue(self.server.validate_config(second)[0])

    def test_onie_and_sonic_profiles_generate_valid_dhcp_syntax(self):
        artifact = {"id": 1, "stored_name": "installer.bin"}
        onie = {"id": 1, "stage": "onie", "enabled": 1, "option1": 60, "operator1": "starts_with", "value1": "onie_vendor", "option2": None, "operator2": None, "value2": None, "installer_artifact_id": 1}
        sonic = {"id": 2, "stage": "sonic", "enabled": 1, "option1": 61, "operator1": "starts_with", "value1": "SONiC##", "option2": 77, "operator2": "equals", "value2": "SONiC-ZTP", "installer_artifact_id": None}
        updated = self.server.update_profiles(self.config.read_text(), [onie, sonic], [artifact])
        self.assertIn("option default-url code 114 = text;", updated)
        self.assertIn('option bootfile-name "http://10.101.113.253/ztp/generated/profile-2.json";', updated)
        self.assertNotIn('  filename "', updated)
        self.assertIn('option user-class = "\\x09SONiC-ZTP"', updated)
        self.assertTrue(self.server.validate_config(updated)[0])
        replaced = self.server.update_profiles(updated, [sonic], [artifact])
        self.assertEqual(replaced.count("BEGIN SONIC-ZTP MANAGED PROFILES"), 1)
        self.assertIn('option user-class = "\\x09SONiC-ZTP"', replaced)
        self.assertTrue(self.server.validate_config(replaced)[0])

    def test_generated_sonic_ztp_document(self):
        profile = {"id": 9, "firmware_artifact_id": None, "config_artifact_id": 2, "script_artifact_id": 3}
        artifacts = [{"id": 2, "stored_name": "config.json"}, {"id": 3, "stored_name": "post.sh"}]
        url = self.server.write_generated_ztp(profile, artifacts)
        document = (self.server.GENERATED_DIR / "profile-9.json").read_text()
        self.assertIn("02-configdb-json", document)
        self.assertIn("03-provisioning-script", document)
        self.assertTrue(url.endswith("profile-9.json"))

if __name__ == "__main__":
    unittest.main()
