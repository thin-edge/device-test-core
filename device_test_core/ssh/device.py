"""SSH Device Adapter"""
import logging
import os
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta
from device_test_core.adapter import DeviceAdapter
from device_test_core.file_utils import make_tarfile
from device_test_core.command import CmdOutput


try:
    import paramiko
    from paramiko.agent import AgentRequestHandler
    from paramiko.config import SSHConfig
except ImportError:
    raise ImportError(
        "Importing Paramiko library failed. " "Make sure you have Paramiko installed."
    )

try:
    from scp import SCPClient
except ImportError:
    raise ImportError(
        "Importing SCP library failed. " "Make sure you have SCP installed."
    )


log = logging.getLogger(__name__)


class PrintableProxyCommand(paramiko.ProxyCommand):
    """Printable version of the paramiko.ProxyCommand
    so that the proxy command is shown in log entries
    """

    def __repr__(self):
        cmd = " ".join(self.cmd)
        return f"'cmd: \"{cmd}\"'"


def hide_sensitive_ssh_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Hide sensitive configuration info so it can be
    safely logged

    Args:
        config (Dict[str, Any]): SSH configuration

    Returns:
        Dict[str, Any]: SSH config with redacted sensitive fields
    """
    output = {**config}
    if "password" in output:
        output["password"] = "<redacted>"

    if "passphrase" in output:
        output["passphrase"] = "<redacted>"

    return output


class SSHDeviceAdapter(DeviceAdapter):
    """SSH connected Device"""

    # pylint: disable=too-many-public-methods

    def __init__(
        self,
        name: str,
        device_id: Optional[str] = None,
        env: Optional[Dict[str, Optional[str]]] = None,
        should_cleanup: Optional[bool] = None,
        use_sudo: bool = True,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name,
            device_id,
            should_cleanup=should_cleanup,
            use_sudo=use_sudo,
            config=config,
        )
        self._env = env or {}
        self._connect()

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
        raise NotImplementedError("Device statistics is not supported when using SSH")

    def load_ssh_config(self, path="~/.ssh/config") -> paramiko.SSHConfig:
        """Load the ssh config given a specific path

        Nested configuration references will be parsed to a depth of 1. E.g. if the
        config file uses the "include" directive, then it will also be parsed.

        Args:
            path (str): Path to the ssh config file, e.g. ~/.ssh/config

        Returns:
            paramiko.SSHConfig: Parsed configuration file
        """
        expanded_path = Path(path).expanduser()

        if not os.path.exists(expanded_path):
            log.info(
                "Skipping loading of ssh config file as it does not exist. %s",
                expanded_path,
            )
            return paramiko.SSHConfig()

        ssh_config = paramiko.SSHConfig.from_path(Path(path).expanduser())

        # Import and referenced include files
        for host_entry in ssh_config.get_hostnames():
            config = ssh_config.lookup(host_entry)
            include_path = config.get("include")
            if include_path and os.path.exists(include_path):
                with open(include_path, encoding="utf8") as file:
                    ssh_config.parse(file)

        return ssh_config

    def _get_config_value(self, name: str, default: Any = None) -> Any:
        return self._config.get(name, "").strip() or default

    def _connect(self):
        hostname = self._get_config_value("hostname")
        username = self._get_config_value("username", None)
        password = self._get_config_value("password", None)
        ssh_config_path = self._get_config_value("configpath", "~/.ssh/config")
        port = self._get_config_value("port", None)

        config = {
            "hostname": hostname,
            "timeout": 30,
            "allow_agent": True,
        }

        if password:
            config["password"] = password

        if username:
            config["username"] = username

        if port:
            config["port"] = port

        ssh_config = self.load_ssh_config(ssh_config_path)
        user_config = ssh_config.lookup(hostname)
        for k in ("hostname", "username", "port"):
            if k in user_config:
                config[k] = user_config[k]

        host_key_policy = paramiko.AutoAddPolicy()

        # Check if a proxycommand is used in the ssh config, namely when using c8y remoteaccess server
        if "proxycommand" in user_config:
            proxy_command = user_config["proxycommand"]
            if hostname:
                proxy_command = proxy_command.replace("%n", hostname)

            print("Proxy Command: ", proxy_command)
            config["sock"] = PrintableProxyCommand(proxy_command)

        assert config["hostname"], "Missing hostname from adapter configuration"
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(host_key_policy)

        log.warn("Connecting to ssh with config. %s", hide_sensitive_ssh_config(config))
        self._client.connect(**config)
        transport = self._client.get_transport()
        assert transport, "Transport is not defined"
        session = transport.open_session()
        AgentRequestHandler(session)

    def execute_command(
        self, cmd: str, log_output: bool = True, shell: bool = True, **kwargs
    ) -> CmdOutput:
        """Execute a command

        Args:
            cmd (str): Command to execute
            log_output (bool, optional): Log the stdout after the command has executed
            shell (bool, optional): Execute the command in a shell
            **kwargs (Any, optional): Additional keyword arguments

        Raises:
            Exception: Device not found error

        Returns:
            CmdOutput: Command output details, e.g. stdout, stderr and return_code
        """
        run_cmd = []

        use_sudo = kwargs.pop("sudo", self.use_sudo())
        if use_sudo:
            run_cmd.extend(["sudo", "-E"])

        if self._env:
            log.info("Setting environment variables")
            envs = ["env"] + [
                f"{key}={value}"
                for key, value in self._env.items()
                if value is not None
            ]
            run_cmd.extend(envs)

        if shell:
            shell_bin = self._config.get("shell_bin", "/bin/sh")
            run_cmd.extend([shell_bin, "-c"])

        if isinstance(cmd, (list, tuple)):
            run_cmd.extend(cmd)
        else:
            run_cmd.append(cmd)

        result = self._execute(shlex.join(run_cmd), **kwargs)

        if log_output:
            logging.info(
                "cmd: %s, exit code: %d\nstdout:\n%s\nstderr:\n%s",
                cmd,
                result.return_code,
                result.stdout or "<<empty>>",
                result.stderr or "<<empty>>",
            )
        return result

    def _execute(self, command: str, **kwargs) -> CmdOutput:
        transport = self._client.get_transport()
        assert transport, "Transport is not defined"
        timeout = kwargs.pop("timeout", 120)
        chan = transport.open_session(timeout=timeout)

        # Note: stderr is only returned if it is NOT a pty terminal
        # if stderr:
        #     chan.get_pty()
        f_stdout = chan.makefile()
        f_stderr = chan.makefile_stderr()
        chan.exec_command(command)
        stdout = f_stdout.read()
        stderr = f_stderr.read()

        # Note: Replace the \r which are added to due the simulated terminal
        # https://stackoverflow.com/questions/35887380/why-does-paramiko-returns-r-n-as-newline-instead-of-n
        stdout = stdout.replace(b"\r\n", b"\n")
        stderr = stderr.replace(b"\r\n", b"\n")
        # Check exist status after calling read, otherwise it hangs
        # https://github.com/paramiko/paramiko/issues/448
        exit_code = chan.recv_exit_status()
        f_stdout.close()
        f_stderr.close()
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
        logging.info("Restarting %s", self.name)
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
        archive_path = ""
        try:
            total_files = 0

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

            # copy archive to device
            tmp_dst = f"/tmp/{Path(archive_path).name}"
            transport = self._client.get_transport()
            assert transport, "Transport is not defined"
            with SCPClient(transport) as scp_client:
                scp_client.put(archive_path, recursive=True, remote_path=tmp_dst)

            self.assert_command(f"mkdir -p '{parent_dir}'")
            self.assert_command(
                f"tar xf '{tmp_dst}' -C '{parent_dir}' && rm -f '{tmp_dst}'"
            )

        finally:
            if archive_path and os.path.exists(archive_path):
                os.unlink(archive_path)

    def cleanup(self, force: bool = False):
        """Cleanup the device. This will be called when the define is no longer needed"""
        if not force and not self.should_cleanup:
            log.info("Skipping cleanup due to should_cleanup not being set")
            return

        if self._client:
            try:
                self._client.close()
            except Exception as ex:
                log.info("Error whilst closing connection. %s", ex)
