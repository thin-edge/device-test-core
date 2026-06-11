"""Docker Compose device factory

Brings up a multi-service test stack from a user provided docker compose file
whilst keeping the parallel-safety guarantees of the single container docker
adapter:

* Each setup gets its own randomly generated Compose project name, so all
  containers, networks and volumes are namespaced per test setup
  (no shared network, no name clashes between parallel test runners)
* Compose files are validated up front and rejected if they contain settings
  which would break parallel execution (fixed container names, fixed host
  ports, externally named networks/volumes)
* Test metadata (labels, extra hosts) is injected via a generated compose
  override file, so the existing label based cleanup keeps working

The lifecycle (config/up/logs/down) is driven via the ``docker compose`` v2
CLI (docker-py has no compose support), whilst all per-container interactions
reuse the existing :class:`DockerDeviceAdapter` via docker-py.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import docker
from docker.errors import APIError
from docker.models.containers import Container

from device_test_core.docker.device import DockerDeviceAdapter
from device_test_core.docker.factory import get_docker_host
from device_test_core.utils import generate_name

# pylint: disable=broad-except

log = logging.getLogger()

# Service label used inside a compose file to mark the main device under test
ROLE_LABEL = "device-test-core.role"
ROLE_MAIN = "main"

# Labels automatically added by docker compose to all project resources
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"

# https://docs.docker.com/compose/project-name/
PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ComposeError(RuntimeError):
    """Error whilst running a docker compose command"""


class ComposeValidationError(ValueError):
    """Compose file validation error"""


def normalize_labels(labels: Any) -> Dict[str, str]:
    """Normalize compose service labels to a dictionary

    Compose allows labels to be defined either as a map or as a list
    of 'key=value' strings.

    Args:
        labels (Any): Labels as a dict or list

    Returns:
        Dict[str, str]: Labels
    """
    if not labels:
        return {}
    if isinstance(labels, dict):
        return {str(key): str(value) for key, value in labels.items()}
    result = {}
    for item in labels:
        key, _, value = str(item).partition("=")
        result[key] = value
    return result


def validate_compose_config(config: Dict[str, Any], project_name: str) -> List[str]:
    """Validate a canonical compose configuration (as produced by
    ``docker compose config --format json``) and return a list of problems
    which would break running multiple test setups in parallel.

    Rules:
        * services must not use ``container_name`` (fixed names collide across
          parallel runs, compose generates unique names per project)
        * services must not publish fixed host ports (use ephemeral ports
          instead, e.g. ``ports: ["1883"]``, and look up the assigned port)
        * networks/volumes must not be ``external`` or use a fixed ``name``
          (project-scoped resources are isolated per setup)

    Args:
        config (Dict[str, Any]): Canonical compose configuration
        project_name (str): Compose project name used to render the config

    Returns:
        List[str]: List of validation errors. Empty if the file is ok
    """
    errors = []
    services = config.get("services") or {}
    for name, service in services.items():
        service = service or {}
        if service.get("container_name"):
            errors.append(
                f"service '{name}' uses 'container_name: {service['container_name']}'. "
                "Fixed container names collide between parallel test runs. "
                "Remove it and rely on the compose generated name"
            )

        for port in service.get("ports") or []:
            published = None
            target = None
            if isinstance(port, dict):
                published = port.get("published")
                target = port.get("target")
            else:
                # short syntax, e.g. "8080:80" or "80"
                parts = str(port).rsplit(":", 1)
                if len(parts) == 2:
                    published, target = parts[0], parts[1]
                else:
                    target = parts[0]
            if published not in (None, "", 0, "0"):
                errors.append(
                    f"service '{name}' publishes a fixed host port "
                    f"({published}->{target}). Fixed host ports conflict between "
                    "parallel test runs. Use an ephemeral port instead, e.g. "
                    f"'ports: [\"{target}\"]', and resolve the assigned host "
                    "port via get_service_port"
                )

    resource_prefix = f"{project_name}_"
    for kind in ("networks", "volumes"):
        for name, resource in (config.get(kind) or {}).items():
            resource = resource or {}
            if resource.get("external"):
                errors.append(
                    f"{kind[:-1]} '{name}' is external. External {kind} are "
                    "shared state between parallel test runs and are not allowed"
                )
                continue
            resource_name = resource.get("name")
            if resource_name and not resource_name.startswith(resource_prefix):
                errors.append(
                    f"{kind[:-1]} '{name}' uses a fixed name '{resource_name}'. "
                    f"Fixed {kind[:-1]} names collide between parallel test "
                    "runs. Remove the 'name' setting so it is scoped to the "
                    "compose project"
                )

    return errors


def resolve_device_service(
    services: Dict[str, Any], requested: Optional[str] = None
) -> str:
    """Resolve which compose service should act as the main device under test

    Resolution order (first match wins):
        1. Explicitly requested service
        2. The service marked with the label 'device-test-core.role: main'
        3. The only service (if the stack has a single service)
        4. A service named 'device'

    Args:
        services (Dict[str, Any]): Services from the canonical compose config
        requested (str, optional): Explicitly requested service name

    Raises:
        ComposeValidationError: The main device service could not be resolved

    Returns:
        str: Name of the service acting as the main device under test
    """
    if requested:
        if requested not in services:
            raise ComposeValidationError(
                f"device service '{requested}' not found in the compose file. "
                f"Available services: {sorted(services.keys())}"
            )
        return requested

    labelled = [
        name
        for name, service in services.items()
        if normalize_labels((service or {}).get("labels")).get(ROLE_LABEL) == ROLE_MAIN
    ]
    if len(labelled) > 1:
        raise ComposeValidationError(
            f"Multiple services are marked with the '{ROLE_LABEL}: {ROLE_MAIN}' "
            f"label: {sorted(labelled)}. Only one service can be the main device"
        )
    if labelled:
        return labelled[0]

    if len(services) == 1:
        return next(iter(services))

    if "device" in services:
        return "device"

    raise ComposeValidationError(
        "Could not determine which service is the main device under test. "
        f"Available services: {sorted(services.keys())}. Either pass the "
        "device_service argument, add the label "
        f"'{ROLE_LABEL}: {ROLE_MAIN}' to one service, or name one "
        "service 'device'"
    )


def build_override_config(
    services: List[str],
    labels: Dict[str, str],
    extra_hosts: Dict[str, str],
    device_service: str,
) -> Dict[str, Any]:
    """Build the compose override configuration which injects test metadata
    (labels, extra hosts) into every service of the user provided compose file

    Args:
        services (List[str]): Names of all services in the stack
        labels (Dict[str, str]): Labels to add to each service
        extra_hosts (Dict[str, str]): Hostname to ip address entries to add
            to each service's /etc/hosts
        device_service (str): The service acting as the main device under test

    Returns:
        Dict[str, Any]: Compose override configuration
    """
    override: Dict[str, Any] = {"services": {}}
    for name in services:
        service_override: Dict[str, Any] = {"labels": dict(labels)}
        if name == device_service:
            service_override["labels"][ROLE_LABEL] = ROLE_MAIN
        if extra_hosts:
            service_override["extra_hosts"] = dict(extra_hosts)
        override["services"][name] = service_override
    return override


def sanitize_project_name(name: str) -> str:
    """Convert a name (e.g. a device serial number) to a valid compose
    project name. Compose project names must start with a lowercase letter
    or digit and may only contain lowercase letters, digits, dashes and
    underscores.

    Args:
        name (str): Name, e.g. 'TST_Device.01'

    Returns:
        str: Valid compose project name, e.g. 'tst_device-01'
    """
    name = re.sub(r"[^a-z0-9_-]+", "-", name.lower())
    return name.lstrip("_-")


def map_service_networks(
    config: Dict[str, Any], project_name: str
) -> Dict[str, List[str]]:
    """Map each service to the (docker) names of the networks it is attached
    to, based on the canonical compose configuration.

    This is used to restore the original network connections of a container
    after a simulated network outage, without attaching it to project
    networks it was never connected to.

    Args:
        config (Dict[str, Any]): Canonical compose configuration
        project_name (str): Compose project name used to render the config

    Returns:
        Dict[str, List[str]]: Service name to list of docker network names
    """
    top_networks = config.get("networks") or {}

    def network_name(key: str) -> str:
        resource = top_networks.get(key) or {}
        return resource.get("name") or f"{project_name}_{key}"

    result = {}
    for service_name, service in (config.get("services") or {}).items():
        service = service or {}
        if service.get("network_mode"):
            # e.g. host/none/container:<name>, not attachable to project networks
            result[service_name] = []
            continue
        networks = service.get("networks") or {"default": None}
        if isinstance(networks, dict):
            keys = list(networks.keys())
        else:
            keys = [str(key) for key in networks]
        result[service_name] = [network_name(key) for key in keys]
    return result


class ComposeServiceAdapter(DockerDeviceAdapter):
    """Device adapter for a single service of a docker compose stack

    It behaves exactly like the docker device adapter (commands, file
    transfer, logs etc. all operate on the service's container), however
    cleanup tears down the whole compose stack the service belongs to
    """

    def __init__(
        self,
        name: str,
        device_id: Optional[str] = None,
        container: Optional[Container] = None,
        stack: Optional["ComposeStack"] = None,
        should_cleanup: Optional[bool] = None,
        use_sudo: bool = True,
        **kwargs,
    ):
        super().__init__(
            name,
            device_id=device_id,
            container=container,
            simulator=stack,
            should_cleanup=should_cleanup,
            use_sudo=use_sudo,
            **kwargs,
        )

    @property
    def stack(self) -> Optional["ComposeStack"]:
        """Compose stack which the service belongs to"""
        return self.simulator

    def cleanup(self, force: bool = False):
        """Cleanup the compose stack the service belongs to.
        The underlying 'compose down' is only executed once per stack
        regardless of how many service adapters trigger it
        """
        if not force and not self.should_cleanup:
            log.info("Skipping cleanup due to should_cleanup not being set")
            return

        if self.simulator:
            self.simulator.cleanup(force=True)


class ComposeStack:
    """A running docker compose project created from a compose file

    Provides access to each service as a device adapter, dynamic host port
    lookup, service logs and network connect/disconnect simulation
    """

    def __init__(
        self,
        docker_client,
        compose_cli: List[str],
        project_name: str,
        compose_file: str,
        override_file: str,
        env_file: Optional[str],
        device_service: str,
        services: List[str],
        env: Dict[str, str],
        keep_stack: bool = False,
        service_networks: Optional[Dict[str, List[str]]] = None,
        use_sudo: bool = True,
    ):
        self._docker_client = docker_client
        self._compose_cli = list(compose_cli)
        self._project_name = project_name
        self._compose_file = compose_file
        self._override_file = override_file
        self._env_file = env_file
        self._device_service = device_service
        self._services = list(services)
        self._env = env
        self._keep_stack = keep_stack
        self._use_sudo = use_sudo
        self._adapters: Dict[str, ComposeServiceAdapter] = {}
        # Networks each service is attached to (from the compose config),
        # used to restore connectivity after a simulated network outage
        self._service_networks: Dict[str, List[str]] = service_networks or {}
        self._removed = False

    @property
    def project_name(self) -> str:
        """Compose project name (unique per test setup)"""
        return self._project_name

    @property
    def device_service(self) -> str:
        """Name of the service acting as the main device under test"""
        return self._device_service

    @property
    def services(self) -> List[str]:
        """Names of all services in the stack"""
        return list(self._services)

    def _compose_command(self, *args: str) -> List[str]:
        return [*self._compose_cli, "--project-name", self._project_name, *args]

    def get_container(self, service: str, timeout: float = 10) -> Container:
        """Get the container of a compose service

        Args:
            service (str): Compose service name
            timeout (float, optional): Time to wait for the container to be
                resolvable. Defaults to 10 seconds.

        Raises:
            ComposeError: Container for the service could not be found

        Returns:
            Container: Container of the service
        """
        filters = {
            "label": [
                f"{COMPOSE_PROJECT_LABEL}={self._project_name}",
                f"{COMPOSE_SERVICE_LABEL}={service}",
            ]
        }
        deadline = time.monotonic() + timeout
        while True:
            containers = self._docker_client.containers.list(all=True, filters=filters)
            if containers:
                return containers[0]
            if time.monotonic() >= deadline:
                raise ComposeError(
                    f"Could not find a container for service '{service}' in "
                    f"compose project '{self._project_name}'"
                )
            time.sleep(0.25)

    def get_device(
        self,
        service: Optional[str] = None,
        name: Optional[str] = None,
        device_id: Optional[str] = None,
        use_sudo: Optional[bool] = None,
        **kwargs,
    ) -> ComposeServiceAdapter:
        """Get a device adapter for a service of the stack

        Args:
            service (str, optional): Compose service name. Defaults to the
                main device service.
            name (str, optional): Adapter/device name. Defaults to the
                service name.
            device_id (str, optional): Device id. Defaults to the name.
            use_sudo (bool, optional): Whether to use sudo for command
                execution. Defaults to the stack setting.

        Returns:
            ComposeServiceAdapter: Device adapter for the service
        """
        service = service or self._device_service
        if service not in self._services:
            raise ValueError(
                f"Unknown service '{service}'. "
                f"Available services: {sorted(self._services)}"
            )
        if service in self._adapters:
            return self._adapters[service]

        container = self.get_container(service)
        adapter = ComposeServiceAdapter(
            name or service,
            device_id=device_id,
            container=container,
            stack=self,
            use_sudo=self._use_sudo if use_sudo is None else use_sudo,
            **kwargs,
        )
        self._adapters[service] = adapter
        return adapter

    def get_service_port(
        self, service: str, port: int, protocol: str = "tcp"
    ) -> Tuple[str, int]:
        """Resolve the dynamically assigned host port of a service's
        (ephemeral) published container port

        Args:
            service (str): Compose service name
            port (int): Container port, e.g. 1883
            protocol (str, optional): Protocol, e.g. tcp/udp. Defaults to tcp.

        Raises:
            ComposeError: The service does not publish the given port

        Returns:
            Tuple[str, int]: Host address and host port which the container
                port is reachable on from the test host
        """
        container = self.get_container(service)
        container.reload()
        ports = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
        key = f"{int(port)}/{protocol}"
        for binding in ports.get(key) or []:
            host_port = binding.get("HostPort")
            if host_port:
                host_ip = binding.get("HostIp") or ""
                if host_ip in ("", "0.0.0.0", "::"):
                    host_ip = self._resolve_host()
                return host_ip, int(host_port)
        raise ComposeError(
            f"Service '{service}' does not publish container port {key}. "
            f"Add 'ports: [\"{port}\"]' (without a host port!) to the service "
            "in the compose file"
        )

    def _resolve_host(self) -> str:
        """Resolve the address under which published ports are reachable
        from the test host"""
        docker_host = self._env.get("DOCKER_HOST", "")
        if docker_host.startswith(("tcp://", "ssh://")):
            hostname = urlsplit(docker_host).hostname
            if hostname:
                return hostname
        return "127.0.0.1"

    def get_logs(self, service: Optional[str] = None, since: Any = None) -> List[str]:
        """Get the container logs (docker compose logs) of one or all services

        Args:
            service (str, optional): Only include logs of the given service.
                Defaults to all services.
            since (Any, optional): Only include logs since the given value
                (passed to 'docker compose logs --since')

        Returns:
            List[str]: Log lines
        """
        args = ["logs", "--no-color", "--timestamps"]
        if since:
            args.extend(["--since", str(since)])
        if service:
            args.append(service)
        proc = subprocess.run(
            self._compose_command(*args),
            capture_output=True,
            text=True,
            env=self._env,
            check=False,
        )
        if proc.returncode != 0:
            log.warning(
                "Could not retrieve compose logs. project=%s, exit_code=%d, stderr=%s",
                self._project_name,
                proc.returncode,
                proc.stderr,
            )
        return proc.stdout.splitlines()

    def _project_networks(self):
        return self._docker_client.networks.list(
            filters={"label": f"{COMPOSE_PROJECT_LABEL}={self._project_name}"}
        )

    def disconnect_network(self, container: Container):
        """Disconnect a container from all of the project's networks to
        simulate a loss of connectivity. Other services stay connected.

        Args:
            container (Container): Container
        """
        for network in self._project_networks():
            try:
                network.disconnect(container, force=True)
                log.info(
                    "Disconnected [%s] from network [%s]",
                    container.name,
                    network.name,
                )
            except APIError as ex:
                if ex.explanation and "is not connected" not in ex.explanation:
                    raise
                log.info(
                    "Container [%s] already disconnected from network [%s]",
                    container.name,
                    network.name,
                )

    def connect_network(self, container: Container):
        """Reconnect a container to the project networks it is attached to
        according to the compose configuration

        Args:
            container (Container): Container
        """
        # Resolve the service from the compose label so the lookup is stable
        # even if the container was recreated or resolved whilst disconnected
        service = container.labels.get(COMPOSE_SERVICE_LABEL)
        original_networks = self._service_networks.get(service) if service else None
        if original_networks is None:
            log.warning(
                "Could not determine the original networks of container [%s] "
                "(service=%s). Reconnecting it to all project networks",
                container.name,
                service,
            )
        for network in self._project_networks():
            if original_networks is not None and network.name not in original_networks:
                continue
            try:
                network.connect(container)
                log.info("Connected [%s] to network [%s]", container.name, network.name)
            except APIError as ex:
                if (
                    ex.explanation
                    and "already exists in network" not in ex.explanation
                    and "already connected to network" not in ex.explanation
                ):
                    raise
                log.info(
                    "Container [%s] already connected to network [%s]",
                    container.name,
                    network.name,
                )

    def cleanup(self, force: bool = False):
        """Tear down the compose stack (containers, networks and volumes).
        The operation is idempotent, the stack is only removed once.

        Args:
            force (bool): Tear down the stack even if the stack is configured
                to be kept. Defaults to False.
        """
        if self._removed:
            return

        if self._keep_stack and not force:
            log.info(
                "Keeping compose stack for debugging. Remove it manually using: "
                "docker compose --project-name %s down --volumes --remove-orphans",
                self._project_name,
            )
            return

        proc = subprocess.run(
            self._compose_command(
                "down", "--volumes", "--remove-orphans", "--timeout", "10"
            ),
            capture_output=True,
            text=True,
            env=self._env,
            check=False,
        )
        if proc.returncode != 0:
            log.error(
                "Could not tear down compose stack. project=%s, stderr=%s",
                self._project_name,
                proc.stderr,
            )
        else:
            log.info("Removed compose stack. project=%s", self._project_name)
            self._removed = True

        if self._override_file and os.path.exists(self._override_file):
            try:
                os.unlink(self._override_file)
            except OSError as ex:
                log.warning("Could not remove override file. exception=%s", ex)


class ComposeDeviceFactory:
    """Docker Compose device factory

    Creates isolated test stacks from docker compose files. Each stack is
    created under a unique compose project name so multiple test setups can
    run in parallel without conflicts (names, networks, volumes, host ports)
    """

    def __init__(self, keep_stacks: bool = False, strict_validation: bool = True):
        env = os.environ.copy()

        # Lookup default docker context using the docker cli (if installed)
        # Then set the DOCKER_HOST variable so the API behaves similar to the context
        if "DOCKER_HOST" not in env:
            docker_host = get_docker_host()
            if docker_host:
                env["DOCKER_HOST"] = docker_host.strip()

        self._env = env
        self._docker_client = docker.from_env(environment=env)
        self._keep_stacks = keep_stacks
        self._strict_validation = strict_validation
        self._compose_cli = self._detect_compose_cli()
        self._stacks: Dict[str, ComposeStack] = {}

    def _detect_compose_cli(self) -> List[str]:
        """Detect the docker compose v2 cli plugin

        Raises:
            ComposeError: docker compose v2 is not available

        Returns:
            List[str]: Base command to invoke docker compose
        """
        docker_cli = shutil.which("docker")
        if not docker_cli:
            raise ComposeError(
                "docker cli not found. The compose adapter requires the "
                "docker cli with the compose v2 plugin"
            )
        proc = subprocess.run(
            [docker_cli, "compose", "version", "--short"],
            capture_output=True,
            text=True,
            env=self._env,
            check=False,
        )
        if proc.returncode != 0:
            raise ComposeError(
                "docker compose v2 plugin not found. " f"stderr={proc.stderr.strip()}"
            )
        version = proc.stdout.strip()
        if version and not version.lstrip("v").startswith("2"):
            log.warning(
                "Unexpected docker compose version. Only v2 is supported. got=%s",
                version,
            )
        return [docker_cli, "compose"]

    @staticmethod
    def generate_project_name() -> str:
        """Generate a random, valid compose project name"""
        return generate_name(sep="-").replace("_", "-").lower()

    def _compose_files_args(
        self, compose_file: str, override_file: Optional[str], env_file: Optional[str]
    ) -> List[str]:
        args = ["-f", compose_file]
        if override_file:
            args.extend(["-f", override_file])
        if env_file:
            args.extend(["--env-file", env_file])
        return args

    def _render_config(
        self,
        compose_file: str,
        project_name: str,
        env_file: Optional[str],
        env: Dict[str, str],
    ) -> Dict[str, Any]:
        """Render the canonical compose configuration (with interpolation
        applied) using 'docker compose config'"""
        cmd = [
            *self._compose_cli,
            "--project-name",
            project_name,
            *self._compose_files_args(compose_file, None, env_file),
            "config",
            "--format",
            "json",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
        if proc.returncode != 0:
            raise ComposeError(
                f"Invalid compose file '{compose_file}'. "
                f"stderr={proc.stderr.strip()}"
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as ex:
            raise ComposeError(
                f"Could not parse compose config output. error={ex}"
            ) from ex

    def _write_override_file(self, override: Dict[str, Any]) -> str:
        """Write the override configuration to a temporary file.

        The file is written as json which is also a valid yaml document,
        avoiding an additional yaml dependency
        """
        file_handle, path = tempfile.mkstemp(
            prefix="device-test-core-override-", suffix=".yaml"
        )
        with os.fdopen(file_handle, "w") as file:
            json.dump(override, file, indent=2)
        return path

    def create_stack(
        self,
        compose_file: str,
        project_name: Optional[str] = None,
        device_service: Optional[str] = None,
        env_file: str = ".env",
        env: Optional[Dict[str, str]] = None,
        extra_hosts: Optional[Dict[str, str]] = None,
        test_suite: str = "",
        test_id: str = "",
        build: bool = False,
        wait_timeout: float = 120,
        use_sudo: bool = True,
        **kwargs,
    ) -> ComposeStack:
        """Create a new test stack from a docker compose file

        Args:
            compose_file (str): Path to the docker compose file
            project_name (str, optional): Compose project name. A unique
                random name is generated if not provided. Pass a name (e.g.
                the device serial number) to make the project easier to
                identify, however it MUST be unique across parallel test runs
                as the project name is the isolation boundary. The name is
                sanitized to the allowed character set automatically
            device_service (str, optional): Service which acts as the main
                device under test. If not set, it is resolved from the compose
                file (label 'device-test-core.role: main', single service, or
                a service named 'device')
            env_file (str, optional): Environment file passed to compose for
                variable interpolation. Defaults to '.env' (ignored when the
                file does not exist)
            env (Dict[str, str], optional): Additional environment variables
                made available to compose interpolation, e.g. DEVICE_ID.
                Reference them in the compose file via ${DEVICE_ID}
            extra_hosts (Dict[str, str], optional): Hostname to ip address
                entries added to /etc/hosts of every service
            test_suite (str, optional): Test suite name added to the
                "device.test_group_id" label of every service
            test_id (str, optional): Test id added to the "device.test_id"
                label of every service
            build (bool, optional): Build images before starting (up --build).
                Defaults to False.
            wait_timeout (float, optional): Maximum time in seconds to wait
                for all services to be running/healthy. Defaults to 120.
            use_sudo (bool, optional): Whether to use sudo when executing
                commands on the devices (services) of the stack.
                Defaults to True.

        Raises:
            ComposeValidationError: The compose file contains settings which
                would break parallel execution
            ComposeError: The stack could not be started

        Returns:
            ComposeStack: The running stack
        """
        if kwargs:
            log.warning(
                "Ignoring unsupported compose adapter options: %s",
                sorted(kwargs.keys()),
            )

        compose_file = os.path.abspath(compose_file)
        if not os.path.exists(compose_file):
            raise ValueError(f"compose file not found: {compose_file}")

        if project_name:
            sanitized = sanitize_project_name(project_name)
            if sanitized != project_name:
                log.info(
                    "Sanitized compose project name. from=%s, to=%s",
                    project_name,
                    sanitized,
                )
            project_name = sanitized
        else:
            project_name = self.generate_project_name()
        if not PROJECT_NAME_PATTERN.match(project_name):
            raise ValueError(
                f"Invalid compose project name '{project_name}'. It must "
                "start with a lowercase letter or digit and only contain "
                "lowercase letters, digits, dashes and underscores"
            )

        run_env = {
            **self._env,
            **{key: str(value) for key, value in (env or {}).items()},
        }
        device_id = (env or {}).get("DEVICE_ID", project_name)

        env_file_abs: Optional[str] = None
        if env_file and os.path.exists(env_file):
            env_file_abs = os.path.abspath(env_file)

        config = self._render_config(compose_file, project_name, env_file_abs, run_env)
        services = config.get("services") or {}
        if not services:
            raise ComposeValidationError(
                f"Compose file does not define any services: {compose_file}"
            )

        problems = validate_compose_config(config, project_name)
        if problems:
            message = (
                f"Compose file is not parallel-safe: {compose_file}\n - "
                + "\n - ".join(problems)
            )
            if self._strict_validation:
                raise ComposeValidationError(message)
            log.warning(message)

        resolved_service = resolve_device_service(services, device_service)

        labels = {
            "device.inttest": "1",
            "device.device_id": device_id,
            "device.test_group_id": test_suite,
            "device.test_id": test_id,
        }
        override = build_override_config(
            list(services.keys()), labels, extra_hosts or {}, resolved_service
        )
        override_file = self._write_override_file(override)

        up_cmd = [
            *self._compose_cli,
            "--project-name",
            project_name,
            *self._compose_files_args(compose_file, override_file, env_file_abs),
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            str(int(wait_timeout)),
        ]
        if build:
            up_cmd.append("--build")

        log.info(
            "Creating compose stack. project=%s, file=%s, device_service=%s",
            project_name,
            compose_file,
            resolved_service,
        )
        start = time.time()
        proc = subprocess.run(
            up_cmd, capture_output=True, text=True, env=run_env, check=False
        )
        if proc.returncode != 0:
            stack_logs = "\n".join(self._try_get_logs(project_name, run_env)[-200:])
            if not self._keep_stacks:
                self._down(project_name, run_env)
                if os.path.exists(override_file):
                    os.unlink(override_file)
            raise ComposeError(
                f"Failed to start compose stack. project={project_name}, "
                f"exit_code={proc.returncode}\n"
                f"stderr:\n{proc.stderr}\n"
                f"service logs:\n{stack_logs}"
            )

        log.info(
            "Compose stack ready. project=%s, duration=%.3fs",
            project_name,
            time.time() - start,
        )

        stack = ComposeStack(
            docker_client=self._docker_client,
            compose_cli=self._compose_cli,
            project_name=project_name,
            compose_file=compose_file,
            override_file=override_file,
            env_file=env_file_abs,
            device_service=resolved_service,
            services=list(services.keys()),
            env=run_env,
            keep_stack=self._keep_stacks,
            service_networks=map_service_networks(config, project_name),
            use_sudo=use_sudo,
        )
        self._stacks[project_name] = stack
        return stack

    def _try_get_logs(self, project_name: str, env: Dict[str, str]) -> List[str]:
        try:
            proc = subprocess.run(
                [
                    *self._compose_cli,
                    "--project-name",
                    project_name,
                    "logs",
                    "--no-color",
                    "--timestamps",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            return proc.stdout.splitlines()
        except Exception as ex:
            log.warning("Could not collect compose logs. exception=%s", ex)
            return []

    def _down(self, project_name: str, env: Dict[str, str]):
        try:
            subprocess.run(
                [
                    *self._compose_cli,
                    "--project-name",
                    project_name,
                    "down",
                    "--volumes",
                    "--remove-orphans",
                    "--timeout",
                    "10",
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        except Exception as ex:
            log.warning(
                "Could not tear down compose stack. project=%s, exception=%s",
                project_name,
                ex,
            )

    def cleanup(self):
        """Cleanup all stacks created by this factory"""
        for stack in self._stacks.values():
            try:
                stack.cleanup()
            except Exception as ex:
                log.warning(
                    "Error during compose stack cleanup. project=%s, exception=%s",
                    stack.project_name,
                    ex,
                )

    def remove_stacks(self, group_id: str = ""):
        """Remove all compose stacks related to the integration testing
        (identified by the labels injected at creation time)

        Args:
            group_id (str, optional): Only remove stacks belonging to the
                given test group (label "device.test_group_id")
        """
        labels = ["device.inttest=1", COMPOSE_PROJECT_LABEL]
        if group_id:
            labels.append(f"device.test_group_id={group_id}")

        containers = self._docker_client.containers.list(
            all=True, filters={"label": labels}
        )
        projects = sorted(
            {
                container.labels.get(COMPOSE_PROJECT_LABEL)
                for container in containers
                if container.labels.get(COMPOSE_PROJECT_LABEL)
            }
        )
        for project in projects:
            log.info("Removing compose stack. project=%s", project)
            self._down(project, self._env)
