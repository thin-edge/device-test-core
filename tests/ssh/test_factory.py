"""Software assertion tests
"""
import unittest
from device_test_core.ssh.factory import SSHDeviceFactory
from tests.fixtures import create_config


class TestSSHFactory(unittest.TestCase):
    def setUp(self) -> None:
        self.config = create_config()
        return super().setUp()

    def test_execute_command(self):
        device = SSHDeviceFactory().create_device("device", **self.config)

        output = device.assert_command("ls -l /")
        assert output.return_code == 0
        assert output.stdout
        assert not output.stderr


if __name__ == "__main__":
    unittest.main()
