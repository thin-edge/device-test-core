"""Docker device factory unit tests.

These test ``DockerDeviceFactory.parse_docker_options`` directly (it is a static
method), so they do not require a docker daemon.
"""

import os
import unittest
from unittest import mock

from device_test_core.docker.factory import DockerDeviceFactory


class TestParseDockerOptions(unittest.TestCase):
    """Tests for how ``DOCKER_OPTIONS_*`` are turned into docker run options."""

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_options_from_env_options(self):
        """DOCKER_OPTIONS_* supplied via env_options (e.g. the env file) are parsed,
        and unrelated keys are ignored."""
        options = DockerDeviceFactory.parse_docker_options(
            {
                "DOCKER_OPTIONS_CPU_QUOTA": "5000",
                "DOCKER_OPTIONS_CPU_PERIOD": "20000",
                "DOCKER_OPTIONS_MEM_LIMIT": "256m",
                "DEVICE_ID": "device-01",
                "SOME_UNRELATED_VAR": "ignored",
            }
        )
        self.assertEqual(
            options,
            {"cpu_quota": 5000, "cpu_period": 20000, "mem_limit": "256m"},
        )

    @mock.patch.dict(
        os.environ,
        {"DOCKER_OPTIONS_CPU_QUOTA": "5000", "DOCKER_OPTIONS_CPU_PERIOD": "20000"},
        clear=True,
    )
    def test_options_from_process_environment(self):
        """DOCKER_OPTIONS_* exported in the process environment are honoured.

        Regression test: previously only the env file was read, so exporting these
        variables (as e.g. an ``invoke ... --slow`` task does) silently had no effect.
        """
        options = DockerDeviceFactory.parse_docker_options({})
        self.assertEqual(options, {"cpu_quota": 5000, "cpu_period": 20000})

    @mock.patch.dict(os.environ, {"DOCKER_OPTIONS_CPU_QUOTA": "1000"}, clear=True)
    def test_env_options_take_precedence_over_process_environment(self):
        """When a key is set in both places, the explicit env_options value wins."""
        options = DockerDeviceFactory.parse_docker_options(
            {"DOCKER_OPTIONS_CPU_QUOTA": "5000"}
        )
        self.assertEqual(options["cpu_quota"], 5000)

    @mock.patch.dict(
        os.environ, {"UNRELATED_HOST_VAR": "host-value"}, clear=True
    )
    def test_non_docker_process_env_vars_are_ignored(self):
        """Only DOCKER_OPTIONS_* keys are taken from the process environment, so the
        container is not polluted with unrelated host variables."""
        options = DockerDeviceFactory.parse_docker_options({})
        self.assertEqual(options, {})

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_value_type_coercion(self):
        """Numeric and string values are coerced to appropriate python types."""
        options = DockerDeviceFactory.parse_docker_options(
            {
                "DOCKER_OPTIONS_CPU_QUOTA": "5000",  # int
                "DOCKER_OPTIONS_CPU_PERIOD": "1.5",  # float
                "DOCKER_OPTIONS_NAME": "my-container",  # str
            }
        )
        self.assertIsInstance(options["cpu_quota"], int)
        self.assertEqual(options["cpu_quota"], 5000)
        self.assertIsInstance(options["cpu_period"], float)
        self.assertEqual(options["cpu_period"], 1.5)
        self.assertEqual(options["name"], "my-container")


if __name__ == "__main__":
    unittest.main()
