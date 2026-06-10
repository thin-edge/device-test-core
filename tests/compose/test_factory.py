"""Integration tests for the compose adapter

The tests require a running docker daemon and the docker cli with the
compose v2 plugin. They are skipped if docker is not available.
"""

import os
import unittest
import urllib.request

from device_test_core.compose.factory import (
    COMPOSE_PROJECT_LABEL,
    ComposeDeviceFactory,
)

COMPOSE_FILE = os.path.join(os.path.dirname(__file__), "data", "docker-compose.yaml")

# pylint: disable=protected-access


def create_factory() -> ComposeDeviceFactory:
    """Create a compose factory and check that the docker daemon is reachable"""
    factory = ComposeDeviceFactory()
    factory._docker_client.ping()
    return factory


def require_docker() -> ComposeDeviceFactory:
    """Return a compose factory, or skip the test if docker is not available"""
    try:
        return create_factory()
    except Exception as ex:
        raise unittest.SkipTest(f"docker/compose v2 not available: {ex}") from ex


class TestComposeStack(unittest.TestCase):
    """Tests against a shared stack which is created once for the class"""

    factory = None
    stack = None
    device = None

    @classmethod
    def setUpClass(cls):
        cls.factory = require_docker()
        cls.stack = cls.factory.create_stack(
            COMPOSE_FILE,
            env={"DEVICE_ID": "inttest-device-001"},
            extra_hosts={"example.mydomain.com": "1.2.3.4"},
            test_suite="inttest-suite",
            test_id="inttest-test",
        )
        cls.device = cls.stack.get_device(
            name="inttest-device-001", device_id="inttest-device-001"
        )

    @classmethod
    def tearDownClass(cls):
        if cls.stack:
            cls.stack.cleanup(force=True)

    def test_resolves_device_service_from_label(self):
        self.assertEqual(self.stack.device_service, "device")
        self.assertEqual(sorted(self.stack.services), ["device", "helper", "web"])

    def test_execute_command_with_env_interpolation(self):
        result = self.device.assert_command("echo hello from $DEVICE_ID")
        self.assertEqual(result.stdout.strip(), "hello from inttest-device-001")

    def test_extra_hosts_are_injected(self):
        result = self.device.assert_command("grep example.mydomain.com /etc/hosts")
        self.assertIn("1.2.3.4", result.stdout)

    def test_labels_are_injected(self):
        container = self.stack.get_container("device")
        self.assertEqual(container.labels.get("device.inttest"), "1")
        self.assertEqual(container.labels.get("device.device_id"), "inttest-device-001")
        self.assertEqual(container.labels.get("device.test_group_id"), "inttest-suite")
        self.assertEqual(container.labels.get("device.test_id"), "inttest-test")
        self.assertEqual(container.labels.get("device-test-core.role"), "main")

    def test_sidecar_service_is_addressable(self):
        helper = self.stack.get_device("helper")
        result = helper.assert_command("hostname")
        self.assertTrue(result.stdout.strip())

    def test_cross_service_networking_via_service_name(self):
        self.device.assert_command("nc -z -w 5 web 80")

    def test_ephemeral_host_port_is_resolvable_and_reachable(self):
        host, port = self.stack.get_service_port("web", 80)
        self.assertTrue(host)
        self.assertGreater(port, 0)
        with urllib.request.urlopen(f"http://{host}:{port}", timeout=10) as response:
            self.assertEqual(response.status, 200)

    def test_copy_to(self):
        self.device.copy_to(__file__, "/tmp/test_factory.py")
        self.device.assert_command("test -f /tmp/test_factory.py")

    def test_network_disconnect_and_reconnect(self):
        self.device.disconnect_network()
        try:
            result = self.device.execute_command("nc -z -w 2 web 80")
            self.assertNotEqual(result.return_code, 0)
        finally:
            self.device.connect_network()
        self.device.assert_command("nc -z -w 5 web 80")

    def test_service_logs(self):
        lines = self.stack.get_logs(service="web")
        self.assertTrue(lines)

    def test_use_sudo_defaults_to_true(self):
        self.assertTrue(self.device.use_sudo())


class TestComposeStackCleanup(unittest.TestCase):
    """Cleanup is tested with a dedicated stack as it tears the stack down"""

    def test_cleanup_removes_all_containers_and_networks(self):
        factory = require_docker()
        stack = factory.create_stack(COMPOSE_FILE)
        client = factory._docker_client
        project_filter = {"label": f"{COMPOSE_PROJECT_LABEL}={stack.project_name}"}

        containers = client.containers.list(all=True, filters=project_filter)
        self.assertTrue(containers, "expected the stack containers to be running")

        stack.cleanup(force=True)

        self.assertEqual(client.containers.list(all=True, filters=project_filter), [])
        self.assertEqual(client.networks.list(filters=project_filter), [])


if __name__ == "__main__":
    unittest.main()
