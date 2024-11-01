"""Local Device Adapter"""
import logging
import os
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta
from device_test_core.adapter import DeviceAdapter
from device_test_core.file_utils import make_tarfile
from device_test_core.utils import to_str
from device_test_core.command import CmdOutput


log = logging.getLogger(__name__)


class LocalDeviceAdapter(DeviceAdapter):
    """Local device"""

    # pylint: disable=too-many-public-methods

    def __init__(
        self,
        name: str,
        device_id: str = None,
        env: Dict[str, str] = None,
        should_cleanup: bool = None,
        use_sudo: bool = True,
        config: Dict[str, Any] = None,
    ):
        super().__init__(
            name,
            device_id,
            should_cleanup=should_cleanup,
            use_sudo=use_sudo,
            config=config,
        )
        self._env = env or {}
        self._adapter = "local"

    @property
    def is_existing_device(self) -> bool:
        """Is existing device

        Returns:
            bool: If this device is an existing device
        """
        return self._is_existing_device

    @is_existing_device.setter
    def is_existing_device(self, is_existing_device: bool):
        """Set the is_existing_device

        Args:
            is_existing_device (bool): If this device is an existing device
        """
        self._is_existing_device = is_existing_device

    @property
    def test_start_time(self) -> datetime:
        """Test start time (in utc)

        Returns:
            datetime: Start time of the test
        """
        return self._test_start_time

    @test_start_time.setter
    def test_start_time(self, now: datetime):
        """Set the test start time

        Args:
            now (datetime): Datetime when the test started
        """
        self._test_start_time = now

    @property
    def start_time(self) -> datetime:
        """Get the start time of the device

        Returns:
            datetime: Device start time. None if the device does not exist
        """
        result = self.assert_command("awk '{print $1}' /proc/uptime")
        uptime = int(float(result.stdout.strip()))
        return datetime.now(timezone.utc) - timedelta(seconds=uptime)

    def get_uptime(self) -> float:
        """Get device uptime in seconds

        A zero is returned if the device does not exist

        Returns:
            int: Uptime in seconds
        """
        return (datetime.now(timezone.utc) - self.start_time).total_seconds()

    def get_device_stats(self) -> Any:
        """Get device statistics (i.e. cpu, network traffic etc.)

        Returns:
            Optional[Any]: Device stats object
        """
        raise NotImplementedError(
            f"Device statistics is not supported when using {self._adapter}"
        )

    def execute_command(
        self, cmd: str, log_output: bool = True, shell: bool = True, user: str = "", **kwargs
    ) -> CmdOutput:
        """Execute a command inside the docker container

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            shell (bool, optional): Execute the command in a shell
            user (bool, optional): User that the shell is executed under
            **kwargs (Any, optional): Additional keyword arguments

        Raises:
            Exception: Docker container not found error

        Returns:
            CmdOutput: Command output details, e.g. stdout, stderr and return_code
        """
        run_cmd = []

        # Note: the local adapter has limited options to change which user is used to run, so use
        # sudo to run as a different user
        user = user or self.user()
        if user:
            run_cmd.extend(["sudo", "-E", "-u", user])

        use_sudo = kwargs.pop("sudo", self.use_sudo())
        if use_sudo:
            run_cmd.extend(["sudo", "-E"])

        if self._env:
            log.info("Setting environment variables")
            envs = ["env"] + [f"{key}={value}" for key, value in self._env.items()]
            run_cmd.extend(envs)

        if shell:
            shell_bin = self._config.get("shell_bin", "/bin/sh")
            run_cmd.extend([shell_bin, "-c"])

        if isinstance(cmd, (list, tuple)):
            run_cmd.extend(cmd)
        else:
            run_cmd.append(cmd)

        timeout = kwargs.pop("timeout", 120)

        proc = subprocess.Popen(
            run_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        exit_code = proc.wait(timeout)
        stdout = proc.stdout.read()
        stderr = proc.stderr.read()

        if log_output:
            log.info(
                "cmd: %s, exit code: %d\nstdout:\n%s\nstderr:\n%s",
                cmd,
                exit_code,
                to_str(stdout) or "<<empty>>",
                to_str(stderr) or "<<empty>>",
            )
        return CmdOutput(stdout=stdout, stderr=stderr, return_code=exit_code)

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
        self.assert_command("shutdown -r now")
        time.sleep(120)  # Wait for system to go down (incase it gets this far)
        raise Exception("System did not restart")

    def get_ipaddress(self) -> Optional[str]:
        """Get IP address of the device"""
        return self._config.get("hostname")

    def disconnect_network(self):
        """Disconnect device from the network"""
        raise NotImplementedError(
            "Disconnecting the network is not possible when using SSH"
        )

    def connect_network(self):
        """Connect device to the network"""
        raise NotImplementedError(
            "Disconnecting the network is not possible when using SSH"
        )

    def get_id(self) -> str:
        """Get the device id

        Raises:
            Exception: Device id not found

        Returns:
            str: Device id
        """
        return self._device_id

    def copy_to(self, src: str, dst: str):
        """Copy file to the device

        Args:
            src (str): Source file (on host)
            dst (str): Destination (on device)
        """
        try:
            total_files = 0
            archive_path = ""

            # build archive
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".tar", delete=False
            ) as file:
                total_files = make_tarfile(file, [src], dst)
                archive_path = file.name

            if total_files > 1 or dst.endswith("/") or dst in [".", ".."]:
                parent_dir = dst.rstrip("/") + "/"
            else:
                parent_dir = os.path.dirname(dst)

            self.assert_command(f"mkdir -p '{parent_dir}'")
            self.assert_command(f"tar xf '{archive_path}' -C '{parent_dir}'")

        finally:
            if archive_path and os.path.exists(archive_path):
                os.unlink(archive_path)

    def cleanup(self, force: bool = False):
        """Cleanup the device. This will be called when the define is no longer needed"""
        if not force and not self.should_cleanup:
            log.info("Skipping cleanup due to should_cleanup not being set")
            return
