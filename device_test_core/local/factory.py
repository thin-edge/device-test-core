"""Local device factory"""

import logging
import os
import dotenv
from typing import Dict
from device_test_core.local.device import LocalDeviceAdapter, DeviceAdapter

# pylint: disable=broad-except

log = logging.getLogger(__name__)


class LocalDeviceFactory:
    """Local Device factory"""

    def create_device(
        self,
        device_id: str,
        env_file=".env",
        env: Dict[str, str] = None,
        **kwargs,
    ) -> DeviceAdapter:
        """Create a new device adapter using the local device

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

        env_options = {}

        if os.path.exists(env_file):
            log.info("Loading environment from file: %s", env_file)
            env_options = dotenv.dotenv_values(env_file) or {}

        if env is not None:
            log.info("Using additional custom environment settings")
            env_options = {**env_options, **env}

        log.info("Connecting to device [%s]", device_id)

        device = LocalDeviceAdapter(device_id, env=env_options, config=kwargs)
        return device
