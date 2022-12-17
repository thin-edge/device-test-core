"""SSH Device Adapter"""
import logging
from typing import List, Any, Tuple, Dict
import os
import shlex
import time
import tempfile
from datetime import datetime, timezone, timedelta
from docker.models.containers import Container
from device_test_core.adapter import DeviceAdapter
from device_test_core.file_utils import make_tarfile


try:
    import paramiko
except ImportError:
    raise ImportError(
        "Importing Paramiko library failed. " "Make sure you have Paramiko installed."
    )

from paramiko.client import SSHClient

try:
    import scp
except ImportError:
    raise ImportError(
        "Importing SCP library failed. " "Make sure you have SCP installed."
    )

from scp import SCPClient


log = logging.getLogger()


class SSHDeviceAdapter(DeviceAdapter):
    """SSH connected Device"""

    # pylint: disable=too-many-public-methods

    def __init__(self, name: str, device_id: str = None, config: Dict[str, Any] = None):
        super().__init__(name, device_id, config)
        self._client = SSHClient()
        self._client.load_system_host_keys()
        self._connect()

    @property
    def container(self) -> Container:
        """Docker container

        Returns:
            Container: Container
        """
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
        output = self.assert_command("awk '{print $1}' /proc/uptime")
        uptime = int(float(output.decode("utf-8").strip()))
        return datetime.now(timezone.utc) - timedelta(seconds=uptime)

    def get_uptime(self) -> float:
        """Get device uptime in seconds

        A zero is returned if the container does not exist

        Returns:
            int: Uptime in seconds
        """
        return (datetime.now(timezone.utc) - self.start_time).total_seconds()

    def get_device_stats(self) -> Any:
        """Get container statistics (i.e. cpu, network traffic etc.)

        Returns:
            Optional[Any]: Container stats object as provided by docker
        """
        return self.container.stats(stream=False)

    def _connect(self):
        hostname = self._config.get("hostname")
        username = self._config.get("username", None)
        password = self._config.get("password", None)

        assert hostname, "Missing hostname from adapter configuration"
        self._client.connect(hostname, username=username, password=password)

    def execute_command(
        self, cmd: str, log_output: bool = True, shell: bool = True, **kwargs
    ) -> Tuple[int, Any]:
        """Execute a command inside the docker container

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            shell (bool, optional): Execute the command in a shell
            **kwargs (Any, optional): Additional keyword arguments

        Raises:
            Exception: Docker container not found error

        Returns:
            Tuple[int, Any]: Docker command output (exit_code, output)
        """
        if not isinstance(cmd, list):
            cmd = [cmd]

        if shell:
            cmd = ["/bin/bash", "-c"] + cmd
            # cmd = ["/bin/bash", "-c", cmd, "2>\&1"]

        use_sudo = self.use_sudo()
        if use_sudo:
            cmd = ["sudo"] + cmd

        tran = self._client.get_transport()
        timeout = kwargs.pop("timeout", 30)
        chan = tran.open_session(timeout=timeout)

        chan.get_pty()
        f = chan.makefile()
        chan.exec_command(shlex.join(cmd))
        output = f.read()
        # Check exist status after calling read, otherwise it hangs
        # https://github.com/paramiko/paramiko/issues/448
        exit_code = chan.recv_exit_status()
        f.close()

        # Option 2: Use more simple approach, but the stdout and stderr is separated
        # stdin, stdout, stderr = self._client.exec_command(shlex.join(cmd))
        # exit_code = stdout.channel.recv_exit_status()
        # output = "\n".join(stdout.readlines())
        if log_output:
            logging.info(
                "cmd: %s, exit code: %d, stdout: %s",
                cmd,
                exit_code,
                output.decode("utf-8"),
            )
        return exit_code, output

    def assert_command(
        self, cmd: str, exp_exit_code: int = 0, log_output: bool = True, **kwargs
    ) -> Any:
        """Execute a command inside the docker container

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            exp_exit_code (int, optional): Expected exit code, defaults to 0.
                Ignored if set to None.
            **kwargs (Any, optional): Additional keyword arguments

        Raises:
            Exception: Docker container not found error

        Returns:
            Any: Command output
        """
        exit_code, output = self.execute_command(cmd, log_output=log_output, **kwargs)

        if exp_exit_code is not None:
            cmd_snippet = cmd
            if len(cmd_snippet) > 30:
                cmd_snippet = cmd_snippet[0:30] + "..."

            assert (
                exit_code == exp_exit_code
            ), f"`{cmd_snippet}` returned an unexpected exit code"

        return output

    @property
    def name(self) -> str:
        """Get the name of the device

        Returns:
            str: Device name
        """
        return self._name

    def restart(self):
        """Restart the docker container"""
        logging.info("Restarting %s", self.name)
        startup_delay_sec = 1
        self.container.stop()
        if startup_delay_sec > 0:
            time.sleep(startup_delay_sec)
        logging.info("Starting container %s", self.name)
        self.container.start()

    def disconnect_network(self):
        """Disconnect the docker container from the network"""
        assert NotImplementedError(
            "Disconnecting the network is not possible when using SSH"
        )

    def connect_network(self):
        """Connect the docker container to the network"""
        assert NotImplementedError(
            "Disconnecting the network is not possible when using SSH"
        )

    def get_id(self) -> str:
        """Get the device id

        Raises:
            Exception: Device id not found

        Returns:
            str: Device id
        """
        (code, output) = self.container.exec_run(
            'sh -c "tedge config get device.id"', stderr=True, demux=True
        )
        stdout, stderr = output
        if code != 0:
            raise Exception(
                "Failed to get device id. container: "
                f"name={self.container.name}, id={self.container.id}, "
                f"code={code}, stdout={stdout}, stderr={stderr}"
            )
        device_id = stdout.strip().decode("utf-8")

        logging.info("Device id: %s", device_id)
        return device_id

    def use_sudo(self) -> bool:
        return True

    def copy_to(self, src: str, dst: str):
        """Copy file to the device

        Args:
            src (str): Source file (on host)
            dst (str): Destination (in container)
        """

        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".tar", delete=False
        ) as file:
            total_files = make_tarfile(file, [src])
            archive_path = file.name

            remote_tmp_file = f"/tmp/{os.path.basename(file.name)}"

            with SCPClient(self._client.get_transport()) as scp:
                scp.put(archive_path, recursive=True, remote_path=remote_tmp_file)

            self.assert_command(f"tar xvf '{remote_tmp_file}' -C '{dst}'")

    def cleanup(self):
        """Cleanup the device. This will be called when the define is no longer needed"""
        if self._client:
            self._client.close()
