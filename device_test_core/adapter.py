"""Device adapter"""

import hashlib
import logging
import re
from typing import List, Any, Dict, Optional, Union
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from device_test_core.utils import to_str
from device_test_core.command import CmdOutput


log = logging.getLogger(__name__)


class DeviceAdapter(ABC):
    """Device Adapter

    Common interface which is the minimum support to write more a test
    which can have multiple device endpoints (e.g. docker, ssh, podman etc.)
    """

    def __init__(
        self,
        name: str,
        device_id: Optional[str] = None,
        should_cleanup: Optional[bool] = None,
        use_sudo: bool = True,
        config: Optional[Dict[str, Any]] = None,
    ):
        self._name = name
        self._device_id = (
            device_id or name
        )  # Default to using name if device_id is empty
        self._start_time = None
        self._test_start_time = datetime.now(timezone.utc)
        self._is_existing_device = False
        self._use_sudo = use_sudo
        self._config = config or {}
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

    def use_sudo(self) -> bool:
        """Use sudo

        Returns:
            bool: True if the command should be run using sudo
        """
        return self._use_sudo

    @abstractmethod
    def execute_command(
        self, cmd: str, log_output: bool = True, shell: bool = True, **kwargs
    ) -> CmdOutput:
        """Execute a command inside the docker container

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            shell (bool, optional): Execute the command in a shell
            **kwargs (Any, optional): Additional keyword arguments

        Returns:
            CmdOutput: Command output details, e.g. stdout, stderr and return_code
        """

    def assert_linux_permissions(
        self,
        path: str,
        mode: Optional[str] = None,
        owner_group: Optional[str] = None,
        **kwargs,
    ) -> List[str]:
        """Assert the linux group/ownership and permissions (mode) on a given path

        Args:
            path (str): Path to a file or folder to test against
            mode (str): Mode (as an octal), .eg. 644 or 755 etc. Defaults to None
            owner_group (str): Owner/group in the format of owner:group. Defaults to None

        Returns:
            List[str]: List of the actual mode and owner/group (e.g. ['644', 'root:root'])
        """
        result = self.assert_command(f"stat -c '%a %U:%G' '{path}'", **kwargs)
        actual_mode, _, actual_owner_group = (
            to_str(result.stdout).strip().partition(" ")
        )

        if mode is not None:
            assert actual_mode == mode, (
                f"File/dir mode does not match\n"
                f"  path: {path}\n"
                f"  got: {actual_mode}\n"
                f"  wanted: {mode}"
            )

        if owner_group is not None:
            assert owner_group == actual_owner_group, (
                f"File/dir path owner/group does not match\n"
                f"  path: {path}\n"
                f"  got: {actual_owner_group}\n"
                f"  wanted: {owner_group}"
            )

        return [actual_mode, actual_owner_group]

    def assert_file_checksum(self, file: str, reference_file: str, **kwargs) -> str:
        """Assert that two files are equal by checking their md5 checksum

        Args:
            file (str): path to file on the device
            reference_file (str): path to the local file which will be used to compare
                the checksum against

        Returns:
            str: checksum of the file on the device
        """
        with open(reference_file, "rb") as fp:
            expected_checksum = hashlib.md5(fp.read()).hexdigest().lower()

        output = self.assert_command(f"md5sum '{file}'", **kwargs)
        actual_checksum = output.stdout.split(" ")[0]
        assert actual_checksum == expected_checksum, (
            f"File is not equal\n"
            f"reference_file (local): {reference_file}\n"
            f"file (device): {file}\n"
            f"got: {actual_checksum}\n"
            f"wanted: {expected_checksum}"
        )
        return actual_checksum

    def assert_command(
        self,
        cmd: str,
        exp_exit_code: Optional[Union[int, str]] = 0,
        log_output: bool = True,
        **kwargs,
    ) -> CmdOutput:
        """Execute a command inside the docker container

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            exp_exit_code (Union[int, str], optional): Expected exit code, defaults to 0.
                You can use '!0' to invert the check, to assume it is not equal to 0.
            **kwargs (Any, optional): Additional keyword arguments

        Returns:
            CmdOutput: Command output
        """
        result = self.execute_command(cmd, log_output=log_output, **kwargs)

        if exp_exit_code is not None:
            cmd_snippet = cmd
            if len(cmd_snippet) > 30:
                cmd_snippet = cmd_snippet[0:30] + "..."

            try:
                # Coerce to integer if it is digit like
                expected_exit_code = int(exp_exit_code)
            except ValueError:
                expected_exit_code = exp_exit_code

            if isinstance(expected_exit_code, str) and expected_exit_code.startswith(
                "!"
            ):
                assert result.return_code != int(
                    expected_exit_code[1:].strip()
                ), f"`{cmd_snippet}` returned an unexpected exit code\nstdout:\n{to_str(result.stdout)}\nstderr:\n{to_str(result.stderr)}"
            else:
                assert (
                    result.return_code == expected_exit_code
                ), f"`{cmd_snippet}` returned an unexpected exit code\nstdout:\n{to_str(result.stdout)}\nstderr:\n{to_str(result.stderr)}"

        return result

    @property
    def name(self) -> str:
        """Get the name of the device

        Returns:
            str: Device name
        """
        return self._name

    def restart(self):
        """Restart device"""
        log.info("Restarting %s", self.name)
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
        result = self.execute_command(cmd, log_output=False)

        if result.return_code != 0:
            log.warning(
                "Could not retrieve journalctl logs. cmd=%d, exit_code=%d",
                cmd,
                result.return_code,
            )
        output.extend(to_str(result.stdout).splitlines())

        return output

    def assert_logs(
        self,
        services: Optional[List[str]] = None,
        text: Optional[str] = None,
        pattern: Optional[str] = None,
        date_from: Optional[Any] = None,
        date_to: Optional[Any] = None,
        max_lines: Optional[int] = 100000,
        current_only: bool = False,
        min_matches: int = 1,
        max_matches: Optional[int] = None,
    ) -> List[str]:
        """Assert that a given text or pattern is present in the journalctl logs

        Args:
            services (List[str], optional): List of systemd services to include in the logs.
                If not set then all services will be included.

            text (str, optional): Assert the present of a static string (case insensitive).
                Each line will be checked to see if it contains the given text.

            pattern (str, optional): Assert that a line should match a given regular expression
                (case insensitive). It must match the entire line.

            date_from (timestamp.Timestamp, optional): Only include log entires from a given
                datetime/timestamp. As a datetime object or a float (in seconds, e.g. linux timestamp).

            date_to (timestamp.Timestamp, optional): Only include log entires to a given
                datetime/timestamp. As a datetime object or a float (in seconds, e.g. linux timestamp).

            max_lines (int, optional): Maximum number of log lines to retrieve. Defaults to 100000.

            current_only (bool, optional): Only include logs from the last service invocation.
                Defaults to False. If set to True, then only a single service can be provided
                in the services argument.

            min_matches (int, optional): Minimum number of expected line matches (inclusive).
                Defaults to 1. Assertion will be ignored if set to None.

            max_matches (int, optional): Maximum number of expected line matches (inclusive).
                Defaults to None. Assertion will be ignored if set to None.


        Returns:
            List[str]: List of log matching entries
        """
        cmd = "journalctl --no-pager"

        if max_lines:
            cmd += f" --lines {max_lines}"

        date_from = (
            self.test_start_time
            if date_from is None and not current_only
            else date_from
        )
        if date_from:
            if isinstance(date_from, datetime):
                # use linux timestamp as it it simplifies the datetime/timezone parsing
                date_from = f"@{int(date_from.timestamp())}"

            cmd += f' --since "{date_from}"'

        if date_to:
            if isinstance(date_to, datetime):
                # use linux timestamp as it it simplifies the datetime/timezone parsing
                date_to = f"@{int(date_to.timestamp())}"
            cmd += f' --until "{date_to}"'

        if services:
            if current_only:
                # Only include logs from the last service invocation
                if len(services) > 1:
                    raise ValueError(
                        "When using current_only=True, only a single service can be provided"
                    )
                result = self.assert_command(
                    f"systemctl show -p InvocationID --value '{services[0]}'",
                    log_output=False,
                    retries=0,
                )
                invocation_id = to_str(result.stdout).strip()
                if invocation_id:
                    cmd += f" _SYSTEMD_INVOCATION_ID={invocation_id}"
            else:
                for service in services:
                    cmd += f' -u "{service}"'

        log.info("Getting logs from device using: %s", cmd)
        result = self.execute_command(cmd, log_output=False)
        if result.return_code != 0:
            log.warning(
                "Could not retrieve journalctl logs. cmd=%d, exit_code=%d",
                cmd,
                result.return_code,
            )

        entries = to_str(result.stdout).splitlines()
        matches = []
        if text:
            text_lower = text.lower()
            matches = [line for line in entries if text_lower in str(line).lower()]
        elif pattern:
            re_pattern = re.compile(pattern, re.IGNORECASE)
            matches = [line for line in entries if re_pattern.match(line) is not None]
        else:
            matches = entries

        if min_matches is not None:
            assert len(matches) >= min_matches, (
                "Total matching log entries is less than expected. "
                f"wanted={min_matches} (min)\n"
                f"got={len(matches)}\n\n"
                f"entries:\n{matches}"
            )

        if max_matches is not None:
            assert len(matches) <= max_matches, (
                "Total matching log entries is greater than expected. "
                f"wanted={max_matches} (max)\n"
                f"got={len(matches)}\n\n"
                f"entries:\n{matches}"
            )

        return matches

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
        return bool(self._should_cleanup)

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
