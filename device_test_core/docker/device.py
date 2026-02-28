"""Docker Device Adapter"""

import io
import os
import logging
import struct
import tempfile
from typing import Any, List, Optional, Tuple
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

    def _wait_for_exec(self, exec_id: str, timeout: float = 180.0) -> bool:
        """Poll exec_inspect until the exec instance is no longer running.

        Args:
            exec_id: The exec instance ID to wait on.
            timeout: Maximum seconds to wait before giving up.

        Returns:
            True if the process finished within the timeout, False otherwise.
        """
        assert self.container.client is not None, "Docker container client not found"
        deadline = time.monotonic() + timeout
        poll_interval = 0.5
        while time.monotonic() < deadline:
            exec_info = self.container.client.api.exec_inspect(exec_id)
            if not exec_info.get("Running"):
                return True
            time.sleep(poll_interval)
        return False

    def _read_exec_output(self, sock: Any) -> Tuple[bytes, bytes, bool]:
        """Read stdout and stderr from a Docker multiplexed exec socket.

        Docker exec sockets use a simple framing protocol (public Docker Engine API):
            [stream_type(1B) | padding(3B) | payload_size(4B big-endian)] [payload]
        stream_type: 1=stdout, 2=stderr

        ROOT CAUSE OF EMPTY OUTPUT
        --------------------------
        docker-py's exec_start(socket=True) returns
        ``response.raw._fp.fp.raw`` — the raw SocketIO that sits inside the
        BufferedReader that http.client uses to parse HTTP response headers.
        BufferedReader reads in 8 KB chunks, so after parsing headers it may
        have consumed the *entire* response body (the multiplexed output frames)
        into its internal buffer.  Creating a fresh SocketIO from the underlying
        raw socket (``sock._sock.makefile("rb")``) bypasses that buffer and reads
        from an already-drained kernel socket — producing silent empty output for
        any command whose output fits inside one 8 KB read-ahead (e.g. ``cat`` of
        a small file, ``date +%s``).

        The fix is to read from the BufferedReader itself
        (``sock._response.raw._fp.fp``), which holds both the already-buffered
        bytes and continues to read new ones from the socket.

        Returns:
            tuple[bytes, bytes, bool]: (stdout, stderr, truncated)
                truncated is True if the socket closed mid-frame or an OSError occurred.
        """
        HEADER_SIZE = 8
        stdout_chunks: List[bytes] = []
        stderr_chunks: List[bytes] = []
        truncated = False
        READ_TIMEOUT_SECS = 180
        try:
            if hasattr(sock, "_sock"):
                raw_socket = sock._sock
                if raw_socket is None:
                    # _sock is None means something already closed this SocketIO
                    # before we could read from it.
                    log.warning(
                        "_read_exec_output: SocketIO._sock is None — "
                        "the socket was closed before reading started; "
                        "all output will be empty. "
                        "sock=%r sock type=%s",
                        sock,
                        type(sock).__name__,
                    )
                    return b"", b"", True
                # Set the timeout on the underlying raw socket so long-running
                # commands don't block forever.
                raw_socket.settimeout(READ_TIMEOUT_SECS)
                # Read from http.client's BufferedReader, NOT from a new SocketIO.
                # The BufferedReader sits at sock._response.raw._fp.fp and already
                # holds any bytes that were read ahead during HTTP header parsing.
                # Bypassing it by creating a new SocketIO loses those bytes.
                try:
                    sockfile: io.BufferedIOBase = sock._response.raw._fp.fp
                    owns_sock = False
                except AttributeError:
                    # Unexpected docker-py internals — fall back to a fresh SocketIO.
                    # This may lose read-ahead data for small outputs.
                    log.warning(
                        "_read_exec_output: could not access http.client BufferedReader "
                        "(sock._response.raw._fp.fp); falling back to raw_socket.makefile(). "
                        "Small command outputs may be lost. sock type=%s",
                        type(sock).__name__,
                    )
                    sockfile = raw_socket.makefile("rb")
                    owns_sock = False
            else:
                try:
                    sock.settimeout(READ_TIMEOUT_SECS)
                except AttributeError:
                    pass
                sockfile = sock.makefile("rb")
                owns_sock = True
            try:
                while True:
                    header = sockfile.read(HEADER_SIZE)
                    if not header:
                        break  # clean EOF between frames
                    if len(header) < HEADER_SIZE:
                        truncated = True
                        break  # EOF mid-header
                    stream_type, _, payload_size = struct.unpack(">B3sI", header)
                    payload = sockfile.read(payload_size)
                    if len(payload) < payload_size:
                        truncated = True  # EOF mid-payload; save what arrived
                    if stream_type == 1:  # stdout
                        stdout_chunks.append(payload)
                    elif stream_type == 2:  # stderr
                        stderr_chunks.append(payload)
                    if truncated:
                        break
            finally:
                log.debug(
                    "_read_exec_output: done — stdout_bytes=%d stderr_bytes=%d "
                    "truncated=%s frames=%d",
                    sum(len(c) for c in stdout_chunks),
                    sum(len(c) for c in stderr_chunks),
                    truncated,
                    len(stdout_chunks) + len(stderr_chunks),
                )
                if owns_sock:
                    sockfile.close()
                    sock.close()
                else:
                    # We read from http.client's BufferedReader
                    # (sock._response.raw._fp.fp).  Do NOT close sockfile
                    # (the inner SocketIO) directly: that leaves the outer
                    # BufferedReader and http.client.HTTPResponse in a
                    # half-open state.  Their GC finalizers then call
                    # HTTPResponse.close() → flush() → self.fp.flush()
                    # (BufferedReader) → flush inner SocketIO → raises
                    # "ValueError: I/O operation on closed file".
                    # Instead, close the HTTPResponse from the outside in so
                    # it marks itself and its BufferedReader as closed before
                    # the GC ever sees them.
                    try:
                        sock._response.raw.close()
                    except (AttributeError, ValueError, OSError):
                        pass
                    try:
                        sock.close()
                    except (ValueError, OSError):
                        pass
        except OSError as exc:
            truncated = True
            log.warning("Socket error reading exec output: %s", exc)
        return b"".join(stdout_chunks), b"".join(stderr_chunks), truncated

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

        # NOTE: Use socket=True to read directly from the Docker multiplexed stream
        # socket rather than going through docker-py's CancellableStream wrapper.
        # CancellableStream silently converts connection errors (urllib3.ProtocolError,
        # OSError) into StopIteration, so it is impossible to distinguish a clean EOF
        # from a broken connection. Reading the raw socket lets us catch OSError
        # directly and detect truncation (partial header or partial payload).
        #
        # The Docker multiplexed stream protocol is part of the public Docker Engine API
        # (documented at https://docs.docker.com/engine/api/v1.43/#tag/Exec):
        #   [stream_type(1B) | padding(3B) | payload_size(4B big-endian)] [payload]
        # stream_type: 1=stdout, 2=stderr
        #
        # exec_inspect is called after the socket is drained so the exit code is final.
        #
        assert self.container.client is not None, "Docker container client not found"
        exec_id = self.container.client.api.exec_create(self.container.id, run_cmd)[
            "Id"
        ]
        sock = self.container.client.api.exec_start(exec_id, socket=True)
        stdout, stderr, stream_truncated = self._read_exec_output(sock)
        if stream_truncated:
            # Wait for the process to finish so exec_inspect gives a final exit
            # code and the caller gets a consistent state to work with.
            if not self._wait_for_exec(exec_id):
                log.warning(
                    "cmd: %s — process did not finish within the wait timeout "
                    "after stream truncation",
                    run_cmd,
                )
        exec_info = self.container.client.api.exec_inspect(exec_id)
        exit_code = exec_info["ExitCode"]
        if stream_truncated:
            log.warning(
                "cmd: %s — output stream was truncated; output may be incomplete. "
                "exec_info: %s",
                run_cmd,
                exec_info,
            )

            # check if the stream was truncated or not, as assertions are built upon this
            assert (
                not stream_truncated
            ), "Output stream was truncated; output may be incomplete"

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
