"""Device adapter"""
import logging
from typing import List, Any, Tuple, Dict, Optional, Union
from datetime import datetime, timezone
from abc import ABC, abstractmethod


class DeviceAdapter(ABC):
    """Device Adapter

    Common interface which is the minimum support to write more a test
    which can have multiple device endpoints (e.g. docker, ssh, podman etc.)
    """

    def __init__(
        self,
        name: str,
        device_id: str = None,
        should_cleanup: bool = None,
        config: Dict[str, Any] = None,
    ):
        self._name = name
        self._device_id = (
            device_id or name
        )  # Default ot using name if device_id is empty
        self._start_time = None
        self._test_start_time = datetime.now(timezone.utc)
        self._is_existing_device = False
        self._config = config
        self._should_cleanup = should_cleanup

    @property
    def test_start_time(self) -> datetime:
        """Test start time (in utc)

        Returns:
            datetime: Start time of the test
        """
        return self._test_start_time

    @test_start_time.setter
    @abstractmethod
    def test_start_time(self, now: datetime):
        """Set the test start time

        Args:
            now (datetime): Datetime when the test started
        """
        self._test_start_time = now

    @property
    @abstractmethod
    def start_time(self) -> datetime:
        """Get the start time of the device

        Returns:
            datetime: Device start time. None if the device does not exist
        """

    def get_uptime(self) -> float:
        """Get device uptime in seconds

        A zero is returned if the container does not exist

        Returns:
            float: Uptime in seconds
        """
        return (datetime.now(timezone.utc) - self.start_time).total_seconds()

    @abstractmethod
    def execute_command(
        self, cmd: str, log_output: bool = True, shell: bool = True, **kwargs
    ) -> Tuple[int, Any]:
        """Execute a command inside the docker container

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            shell (bool, optional): Execute the command in a shell
            **kwargs (Any, optional): Additional keyword arguments

        Returns:
            Tuple[int, Any]: Docker command output (exit_code, output)
        """

    def assert_command(
        self, cmd: str, exp_exit_code: Union[int, str] = 0, log_output: bool = True, **kwargs
    ) -> Any:
        """Execute a command inside the docker container

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            exp_exit_code (Union[int, str], optional): Expected exit code, defaults to 0.
                You can use '!0' to invert the check, to assume it is not equal to 0.
            **kwargs (Any, optional): Additional keyword arguments

        Returns:
            Any: Command output
        """
        exit_code, output = self.execute_command(cmd, log_output=log_output, **kwargs)

        if exp_exit_code is not None:
            cmd_snippet = cmd
            if len(cmd_snippet) > 30:
                cmd_snippet = cmd_snippet[0:30] + "..."

            try:
                # Coerce to integer if it is digit like
                expected_exit_code = int(exp_exit_code)
            except ValueError:
                expected_exit_code = exp_exit_code

            if isinstance(expected_exit_code, str) and expected_exit_code.startswith("!"):
                assert (
                    exit_code != int(expected_exit_code[1:].strip())
                ), f"`{cmd_snippet}` returned an unexpected exit code\nOutput:\n{output.decode('utf8')}"
            else:
                assert (
                    exit_code == expected_exit_code
                ), f"`{cmd_snippet}` returned an unexpected exit code\nOutput:\n{output.decode('utf8')}"

        return output

    @property
    def name(self) -> str:
        """Get the name of the device

        Returns:
            str: Device name
        """
        return self._name

    def restart(self):
        """Restart device"""
        logging.info("Restarting %s", self.name)
        self.execute_command("shutdown -r now")

    @abstractmethod
    def get_ipaddress(self) -> Optional[str]:
        """Get the ip address of the device"""

    @abstractmethod
    def disconnect_network(self):
        """Disconnect device from the network"""

    @abstractmethod
    def connect_network(self):
        """Connect the device to the network"""

    def get_logs(self, since: Any = None) -> List[str]:
        """Get a list of log entries from the docker container

        Args:
            timestamps (bool, optional): Included timestamps in the log entries. Defaults to False.
            since (Any, optional): Get logs since the provided data. If set to None
                then the test start time will be used.

        Returns:
            List[str]: List of log entries
        """
        cmd = "journalctl --lines 100000 --no-pager -u 'tedge*' -u 'c8y*' -u mosquitto -u 'mqtt-logger'"

        since = since if since is not None else self.test_start_time
        output = []
        if since:
            if isinstance(since, datetime):
                # use linux timestamp as it it simplifies the datetime/timezone parsing
                since = f"@{int(since.timestamp())}"
            cmd += f' --since "{since}"'
        exit_code, logs = self.execute_command(cmd, log_output=False)

        if exit_code != 0:
            logging.warning(
                "Could not retrieve journalctl logs. cmd=%d, exit_code=%d",
                cmd,
                exit_code,
            )
        output.extend(logs.decode("utf8").splitlines())

        return output

    def get_id(self) -> str:
        """Get the device id

        Returns:
            str: Device id
        """
        return self._device_id

    @property
    def should_cleanup(self) -> bool:
        """Should the device be cleaned up after

        Returns:
            bool: True if the cleanup method should be executed
        """
        return self._should_cleanup

    @should_cleanup.setter
    def should_cleanup(self, value: bool):
        """Set if the device should be cleaned up or not

        Args:
            value (bool): New value to set
        """
        self._should_cleanup = value

    @abstractmethod
    def cleanup(self, force: bool = False):
        """Cleanup the device. This will be called when the define is no longer needed

        Args:
            force (bool): Force the cleanup process. Ignore the should_cleanup setting. Defaults to False
        """

    @abstractmethod
    def copy_to(self, src: str, dst: str):
        """Copy file to the device

        Args:
            src (str): Source file (on host)
            dst (str): Destination (in container)
        """
