"""Unit tests for the compose file validation and device service resolution"""

import unittest

from device_test_core.compose.factory import (
    PROJECT_NAME_PATTERN,
    ComposeValidationError,
    build_override_config,
    map_service_networks,
    resolve_device_service,
    sanitize_project_name,
    validate_compose_config,
)

PROJECT = "tst-project1"


class TestValidateComposeConfig(unittest.TestCase):
    def test_valid_minimal_config(self):
        config = {
            "services": {
                "device": {"image": "debian-systemd"},
                "broker": {"image": "eclipse-mosquitto:2"},
            }
        }
        self.assertEqual(validate_compose_config(config, PROJECT), [])

    def test_rejects_container_name(self):
        config = {
            "services": {
                "device": {"image": "debian-systemd", "container_name": "mydevice"},
            }
        }
        errors = validate_compose_config(config, PROJECT)
        self.assertEqual(len(errors), 1)
        self.assertIn("container_name", errors[0])

    def test_rejects_fixed_host_port_long_syntax(self):
        config = {
            "services": {
                "broker": {
                    "image": "eclipse-mosquitto:2",
                    "ports": [
                        {
                            "mode": "ingress",
                            "target": 1883,
                            "published": "1883",
                            "protocol": "tcp",
                        }
                    ],
                },
            }
        }
        errors = validate_compose_config(config, PROJECT)
        self.assertEqual(len(errors), 1)
        self.assertIn("fixed host port", errors[0])

    def test_rejects_fixed_host_port_short_syntax(self):
        config = {
            "services": {
                "broker": {"image": "eclipse-mosquitto:2", "ports": ["8883:1883"]},
            }
        }
        errors = validate_compose_config(config, PROJECT)
        self.assertEqual(len(errors), 1)
        self.assertIn("fixed host port", errors[0])

    def test_allows_ephemeral_ports(self):
        config = {
            "services": {
                "broker1": {
                    "image": "eclipse-mosquitto:2",
                    "ports": [{"mode": "ingress", "target": 1883, "protocol": "tcp"}],
                },
                "broker2": {"image": "eclipse-mosquitto:2", "ports": ["1883"]},
            }
        }
        self.assertEqual(validate_compose_config(config, PROJECT), [])

    def test_rejects_external_network(self):
        config = {
            "services": {"device": {"image": "debian-systemd"}},
            "networks": {"shared": {"external": True, "name": "shared"}},
        }
        errors = validate_compose_config(config, PROJECT)
        self.assertEqual(len(errors), 1)
        self.assertIn("external", errors[0])

    def test_rejects_fixed_network_name(self):
        config = {
            "services": {"device": {"image": "debian-systemd"}},
            "networks": {"internal": {"name": "my-fixed-network"}},
        }
        errors = validate_compose_config(config, PROJECT)
        self.assertEqual(len(errors), 1)
        self.assertIn("fixed name", errors[0])

    def test_allows_project_scoped_resource_names(self):
        # docker compose config renders default resource names with the
        # project name prefix
        config = {
            "services": {"device": {"image": "debian-systemd"}},
            "networks": {"default": {"name": f"{PROJECT}_default"}},
            "volumes": {"data": {"name": f"{PROJECT}_data"}},
        }
        self.assertEqual(validate_compose_config(config, PROJECT), [])

    def test_rejects_external_volume(self):
        config = {
            "services": {"device": {"image": "debian-systemd"}},
            "volumes": {"data": {"external": True, "name": "data"}},
        }
        errors = validate_compose_config(config, PROJECT)
        self.assertEqual(len(errors), 1)
        self.assertIn("external", errors[0])

    def test_collects_multiple_errors(self):
        config = {
            "services": {
                "device": {
                    "image": "debian-systemd",
                    "container_name": "fixed",
                    "ports": ["8080:80"],
                },
            },
            "networks": {"shared": {"external": True}},
        }
        errors = validate_compose_config(config, PROJECT)
        self.assertEqual(len(errors), 3)


class TestResolveDeviceService(unittest.TestCase):
    def test_explicit_service(self):
        services = {"main": {}, "broker": {}}
        self.assertEqual(resolve_device_service(services, "broker"), "broker")

    def test_explicit_service_not_found(self):
        services = {"main": {}, "broker": {}}
        with self.assertRaises(ComposeValidationError):
            resolve_device_service(services, "does-not-exist")

    def test_labelled_service_as_map(self):
        services = {
            "gateway": {"labels": {"device-test-core.role": "main"}},
            "broker": {},
        }
        self.assertEqual(resolve_device_service(services), "gateway")

    def test_labelled_service_as_list(self):
        services = {
            "gateway": {"labels": ["device-test-core.role=main"]},
            "broker": {},
        }
        self.assertEqual(resolve_device_service(services), "gateway")

    def test_multiple_labelled_services_is_an_error(self):
        services = {
            "gateway": {"labels": {"device-test-core.role": "main"}},
            "broker": {"labels": {"device-test-core.role": "main"}},
        }
        with self.assertRaises(ComposeValidationError):
            resolve_device_service(services)

    def test_single_service(self):
        self.assertEqual(resolve_device_service({"anyname": {}}), "anyname")

    def test_service_named_device(self):
        services = {"device": {}, "broker": {}}
        self.assertEqual(resolve_device_service(services), "device")

    def test_ambiguous_services_is_an_error(self):
        services = {"gateway": {}, "broker": {}}
        with self.assertRaises(ComposeValidationError):
            resolve_device_service(services)

    def test_explicit_wins_over_label(self):
        services = {
            "gateway": {"labels": {"device-test-core.role": "main"}},
            "broker": {},
        }
        self.assertEqual(resolve_device_service(services, "broker"), "broker")


class TestBuildOverrideConfig(unittest.TestCase):
    def test_injects_labels_and_extra_hosts_into_all_services(self):
        override = build_override_config(
            ["device", "broker"],
            {"device.inttest": "1", "device.device_id": "abc"},
            {"example.com": "1.2.3.4"},
            "device",
        )
        self.assertEqual(sorted(override["services"].keys()), ["broker", "device"])
        for name, service in override["services"].items():
            self.assertEqual(service["labels"]["device.inttest"], "1")
            self.assertEqual(service["labels"]["device.device_id"], "abc")
            self.assertEqual(service["extra_hosts"], {"example.com": "1.2.3.4"})
            if name == "device":
                self.assertEqual(service["labels"]["device-test-core.role"], "main")
            else:
                self.assertNotIn("device-test-core.role", service["labels"])

    def test_no_extra_hosts(self):
        override = build_override_config(["device"], {}, {}, "device")
        self.assertNotIn("extra_hosts", override["services"]["device"])


class TestSanitizeProjectName(unittest.TestCase):
    def test_device_serial_number(self):
        name = sanitize_project_name("TST_coy_handler")
        self.assertEqual(name, "tst_coy_handler")
        self.assertTrue(PROJECT_NAME_PATTERN.match(name))

    def test_replaces_invalid_characters(self):
        name = sanitize_project_name("TST_Device.01:foo")
        self.assertEqual(name, "tst_device-01-foo")
        self.assertTrue(PROJECT_NAME_PATTERN.match(name))

    def test_strips_invalid_leading_characters(self):
        name = sanitize_project_name("_-device01")
        self.assertEqual(name, "device01")
        self.assertTrue(PROJECT_NAME_PATTERN.match(name))

    def test_valid_name_is_unchanged(self):
        self.assertEqual(sanitize_project_name("tst-device-01"), "tst-device-01")


class TestMapServiceNetworks(unittest.TestCase):
    def test_defaults_to_default_network(self):
        config = {"services": {"device": {"image": "alpine"}}}
        self.assertEqual(
            map_service_networks(config, PROJECT),
            {"device": [f"{PROJECT}_default"]},
        )

    def test_networks_as_map(self):
        # canonical config form: per-service networks rendered as a map and
        # top-level networks include the resolved docker network name
        config = {
            "services": {
                "device": {"networks": {"frontend": None}},
                "broker": {"networks": {"frontend": None, "backend": None}},
            },
            "networks": {
                "frontend": {"name": f"{PROJECT}_frontend"},
                "backend": {"name": f"{PROJECT}_backend"},
            },
        }
        self.assertEqual(
            map_service_networks(config, PROJECT),
            {
                "device": [f"{PROJECT}_frontend"],
                "broker": [f"{PROJECT}_frontend", f"{PROJECT}_backend"],
            },
        )

    def test_networks_as_list(self):
        config = {
            "services": {"device": {"networks": ["internal"]}},
            "networks": {"internal": None},
        }
        self.assertEqual(
            map_service_networks(config, PROJECT),
            {"device": [f"{PROJECT}_internal"]},
        )

    def test_network_mode_has_no_project_networks(self):
        config = {"services": {"device": {"network_mode": "host"}}}
        self.assertEqual(map_service_networks(config, PROJECT), {"device": []})


if __name__ == "__main__":
    unittest.main()
