"""Smoke tests for XDG desktop-session autostart integration."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "install.sh").read_text()
APP = (ROOT / "primegrid-affinity-gui.py").read_text()
README = (ROOT / "README.txt").read_text()


class MonitorAutostartTests(unittest.TestCase):
    def test_installer_creates_user_autostart_entry(self):
        self.assertIn('AUTOSTART_FILE="$AUTOSTART_DIR/primegrid-affinity-monitor.desktop"', INSTALLER)
        self.assertIn('cat > "$AUTOSTART_FILE"', INSTALLER)
        self.assertIn('X-GNOME-Autostart-enabled=true', INSTALLER)

    def test_autostart_runs_monitor_not_gui(self):
        self.assertIn('--monitor', INSTALLER)
        self.assertIn('Name=PrimeGrid Affinity Monitor', INSTALLER)
        self.assertNotIn('install -m 644 "$HERE/primegrid-affinity-monitor.service"', INSTALLER)

    def test_installer_migrates_old_systemd_service(self):
        self.assertIn('systemctl --user disable --now "$MONITOR_SERVICE"', INSTALLER)
        self.assertIn('rm -f "$USER_SERVICE_DIR/$MONITOR_SERVICE"', INSTALLER)

    def test_gui_retains_legacy_service_support_but_falls_back_to_pid_monitor(self):
        self.assertIn('if MONITOR_SERVICE_FILE.is_file():', APP)
        self.assertIn('subprocess.Popen(', APP)
        self.assertIn('MONITOR_LOCK_FILE', APP)

    def test_readme_documents_startup_folder_and_disable_steps(self):
        self.assertIn('~/.config/autostart/primegrid-affinity-monitor.desktop', README)
        self.assertIn('startup applications', README.lower())
        self.assertIn('.desktop.disabled', README)

    def test_installer_smoke_creates_autostart_file_in_user_home(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / 'home'
            home.mkdir()
            fakebin = root / 'bin'
            fakebin.mkdir()
            fake_systemctl = fakebin / 'systemctl'
            fake_systemctl.write_text('#!/bin/sh\nexit 1\n')
            fake_systemctl.chmod(0o755)
            env = os.environ.copy()
            env['HOME'] = str(home)
            env['PATH'] = f'{fakebin}:/usr/bin:/bin'
            proc = subprocess.run(['bash', str(ROOT / 'install.sh')], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            auto = home / '.config/autostart/primegrid-affinity-monitor.desktop'
            self.assertTrue(auto.is_file(), proc.stdout)
            self.assertTrue(os.access(auto, os.X_OK))
            self.assertIn('--monitor', auto.read_text())
            self.assertFalse((home / '.config/systemd/user/primegrid-affinity-monitor.service').exists())
            self.assertTrue((home / '.local/bin/primegrid-affinity-gui').exists())

    def test_stop_message_mentions_next_login_autostart(self):
        self.assertIn('MONITOR_AUTOSTART_FILE', APP)
        self.assertIn('automatic startup remains enabled for your next login', APP)


if __name__ == "__main__":
    unittest.main()
