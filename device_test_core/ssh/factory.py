"""SSH device factory"""

import logging
import dotenv
from typing import Dict
from device_test_core.ssh.device import SSHDeviceAdapter, DeviceAdapter

# pylint: disable=broad-except

log = logging.getLogger()


class SSHDeviceFactory:
    """SSH Device factory"""

    def create_device(
        self,
        device_id: str,
        env_file=".env",
        env: Dict[str, str] = None,
        **kwargs,
    ) -> DeviceAdapter:
        """Create a new device adapter using SSH

        Args:
            device_id (str, optional): Device id. defaults to device-01
            env_file (str, optional): Environment file to be passed to the container.
                Defaults to '.env'.
            env (Dict[str,str], optional): Additional environment variables to be added to
                the container.
                These will override any values provided by the env_file. (docker devices only!).
                Defaults to None.

        Returns:
            DeviceAdapter: Device adapter
        """
        env_options = dotenv.dotenv_values(env_file) or {}

        if env is not None:
            logging.info("Using custom environment settings. %s", env)
            env_options = {**env_options, **env}

        logging.info("Connecting to device [%s]", device_id)

        device = SSHDeviceAdapter(device_id, config=kwargs)
        return device

    def cleanup(self):
        """Cleaup operation"""
