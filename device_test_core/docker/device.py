"""Docker Device Adapter"""

import os
import logging
import tempfile
from typing import Any, Optional
import time
from datetime import datetime, timezone
from docker.models.containers import Container
from device_test_core.adapter import DeviceAdapter
from device_test_core.file_utils import make_tarfile
from device_test_core.utils import to_str
from device_test_core.command import CmdOutput


log = logging.getLogger()


def convert_docker_timestamp(value: str) -> datetime:
    """Convert a docker timestamp string to a python datetime object
    The milliseconds/nanoseconds will be stripped from the timestamp

    Args:
        value (str): Timestamp as a string

    Returns:
        datetime: Datetime
    """
    # Note: Strip the fractions of seconds as strptime does not support nanoseconds
    # (resolution of docker timestamp), and the fraction of seconds resolution is not
    # required for testing
    date, _, _ = value.partition(".")

    tz_suffix = "Z"
    if "+" in value:
        _, tz_sep, time_zone = value.partition("+")
        tz_suffix = tz_sep + time_zone

    if not date.endswith(tz_suffix):
        date = date + tz_suffix

    return datetime.strptime(date, "%Y-%m-%dT%H:%M:%S%z")


class DockerDeviceAdapter(DeviceAdapter):
    """Docker Device Adapter"""

    # pylint: disable=too-many-public-methods

    def __init__(
        self,
        name: str,
        device_id: Optional[str] = None,
        container: Optional[Container] = None,
        simulator=None,
        should_cleanup: Optional[bool] = None,
        use_sudo: bool = True,
        **kwargs,
    ):
        self._container: Optional[Container] = container
        self.simulator = simulator
        self._is_existing_device = False
        super().__init__(
            name,
            device_id,
            should_cleanup=should_cleanup,
            use_sudo=use_sudo,
            config=kwargs,
        )

    @property
    def container(self) -> Container:
        """Docker container

        Returns:
            Container: Container
        """
        assert self._container, "Container not found"
        return self._container

    @container.setter
    def container(self, container: Container):
        self._container = container

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
        """Get the start time of the container

        Returns:
            datetime: Device start time. None if the container does not exist
        """
        self.container.reload()
        return convert_docker_timestamp(self.container.attrs["State"]["StartedAt"])

    def get_uptime(self) -> float:
        """Get device uptime in seconds

        A zero is returned if the container does not exist

        Returns:
            float: Uptime in seconds
        """
        return (datetime.now(timezone.utc) - self.start_time).total_seconds()

    def get_device_stats(self) -> Any:
        """Get container statistics (i.e. cpu, network traffic etc.)

        Returns:
            Optional[Any]: Container stats object as provided by docker
        """
        return self.container.stats(stream=False)

    def execute_command(
        self, cmd: str, log_output: bool = True, shell: bool = True, **kwargs
    ) -> CmdOutput:
        """Execute a command inside the docker container

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            shell (bool, optional): Execute the command in a shell
            **kwargs (Any, optional): Additional keyword arguments

        Raises:
            Exception: Docker container not found error

        Returns:
            CmdOutput: Command output details, e.g. stdout, stderr and return_code
        """
        run_cmd = []

        use_sudo = kwargs.pop("sudo", self.use_sudo())
        if use_sudo:
            run_cmd.extend(["sudo", "-E"])

        if shell:
            shell_bin = self._config.get("shell_bin", "/bin/sh")
            run_cmd = [shell_bin, "-c"]

        if isinstance(cmd, (list, tuple)):
            run_cmd.extend(cmd)
        else:
            run_cmd.append(cmd)

        exit_code, output = self.container.exec_run(run_cmd, demux=True)
        stdout, stderr = output
        if log_output:
            log.info(
                "cmd: %s, exit code: %d\nstdout:\n%s\nstderr:\n%s",
                run_cmd,
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
        """Restart the docker container"""
        log.info("Restarting %s", self.name)
        startup_delay_sec = 1
        self.container.stop()
        if startup_delay_sec > 0:
            time.sleep(startup_delay_sec)
        log.info("Starting container %s", self.name)
        self.container.start()

    def get_ipaddress(self) -> Optional[str]:
        """Get IP address of the device"""
        networks = self.container.attrs["NetworkSettings"]["Networks"]

        if networks:
            network = list(networks.values())[0]
            return network["IPAddress"]

        return None

    def disconnect_network(self):
        """Disconnect the docker container from the network"""
        if self.simulator:
            self.simulator.disconnect_network(self.container)

    def connect_network(self):
        """Connect the docker container to the network"""
        if self.simulator:
            self.simulator.connect_network(self.container)

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
            dst (str): Destination (in container)
        """
        archive_path = ""
        try:
            total_files = 0

            # build archive
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".tar", delete=False
            ) as file:
                total_files = make_tarfile(file, [src], dst)
                archive_path = file.name

            # put archive
            with open(archive_path, "rb") as file:
                if total_files > 1 or dst.endswith("/"):
                    parent_dir = dst.rstrip("/") + "/"
                else:
                    parent_dir = os.path.dirname(dst)

                result = self.execute_command(f"mkdir -p {parent_dir}")
                assert result.return_code == 0
                self.container.put_archive(parent_dir, file)
        finally:
            if archive_path and os.path.exists(archive_path):
                os.unlink(archive_path)

    def cleanup(self, force: bool = False):
        """Cleanup the device. This will be called when the define is no longer needed"""
        # Note: Reconnecting the container only makes sense if it is not destroyed afterwards
        # Make sure device is connected again after the test
        # if self.simulator:
        #     self.simulator.connect_network(self.container)

        if not force and not self.should_cleanup:
            log.info("Skipping cleanup due to should_cleanup not being set")
            return

        if self.container:
            log.info(
                "Removing container (forcefully). id=%s, name=%s",
                self.container.id,
                self.container.name,
            )
            self.container.remove(force=True)
