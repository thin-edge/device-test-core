"""Device fixture"""

import logging
import os
import time
from typing import Dict, Optional, Union
import dotenv
import docker
from docker.errors import NotFound, APIError
from docker.models.containers import Container
from docker.models.networks import Network
from device_test_core.docker.device import DockerDeviceAdapter, DeviceAdapter

# pylint: disable=broad-except

log = logging.getLogger()


class DockerDeviceFactory:
    """Docker device factory"""

    def __init__(self, keep_containers=False, force_network_recreate: bool = False):
        self._docker_client = docker.from_env()
        self._network_name = os.environ.get("INTTEST_NETWORK", "inttest-network")
        self._force_network_recreate = force_network_recreate
        self._keep_containers = keep_containers

        self._network = self._create_network()

        if self._network is None:
            raise Exception(f"Could not get or create network {self._network_name}")

        logging.info(
            "Initialized docker device. network: name=%s, id=%s",
            self._network.name,
            self._network.id,
        )

        self._device_containers = {}

    def _create_network(self):
        network = self._find_network(self._network_name)

        if self._force_network_recreate and network is not None:
            try:
                for container in network.containers:
                    try:
                        network.disconnect(container, force=True)
                    except Exception as ex:
                        logging.warning(
                            "Could not disconnect container. exception=%s", ex
                        )
            except Exception as ex:
                logging.warning("Could not access network containers. exception=%s", ex)
            network.remove()
            logging.info("Removed network: %s", self._network_name)
            network = None

        if network is None:
            network = self._docker_client.networks.create(
                self._network_name, driver="bridge", check_duplicate=True
            )

        return network

    def create_device(
        self,
        device_id: str = "device-01",
        device_type: str = "docker-debian",
        image: str = "debian-systemd",
        env_file=".env",
        test_suite: str = "",
        test_id: str = "",
        env: Dict[str, str] = None,
        **kwargs,
    ) -> DeviceAdapter:
        """Create a new device (container) from the provided image

        Args:
            device_id (str, optional): Device id. defaults to device-01
            device_type (str, optional): Device type. defaults to docker-debian

            env_file (str, optional): Environment file to be passed to the container.
                Defaults to '.env'.
            image (str, optional): Docker image to use to start the containers.
                                   Defaults to 'debian-systemd'.
            test_id (str, optional): Test id used to identify the container using a label
                called "device.test_id"
            test_suite (str, optional): Test set which the container belongs to.
                                        Added to the label "device.test_group_id"
            env (Dict[str,str], optional): Additional environment variables to be added to
                the container.
                These will override any values provided by the env_file. (docker devices only!).
                Defaults to None.

        Returns:
            DeviceAdapter: The device adapter
        """
        logging.info("Using container image: %s", image)
        env_options = dotenv.dotenv_values(env_file) or {}
        env_options["DEVICE_ID"] = device_id
        env_options["DEVICE_TYPE"] = device_type

        if env is not None:
            logging.info("Using custom environment settings. %s", env)
            env_options = {**env_options, **env}

        options = {
            "name": device_id,
            "detach": True,
            "tty": True,
            "environment": env_options,
            "restart_policy": {
                "Name": "always",
            },
            "tmpfs": {
                # support a non-persistent directories to mimic behaviour of real devices
                # /tmp is needed to make the reboot detection work, as the `uptime` shows the hosts
                # uptime and not the container's
                "/tmp": "size=64m",
                "/run": "size=64m",
            },
            "read_only": False,
            "mem_limit": "128m",
            "network": self._network.id,
            "volumes": {},
            "labels": {
                "device.inttest": "1",
                "device.device_id": device_id,
                "device.test_group_id": test_suite,
                "device.test_id": test_id,
            },
            "privileged": True,
        }

        logging.info(
            "Creating new container [%s] with device type [%s]", device_id, device_type
        )

        # check for existing container
        self.remove_device(device_id)

        container = self._docker_client.containers.run(image, None, **options)
        self._device_containers[device_id] = container

        # Wait for container to be ready
        self.wait_for_container_running(container, timeout=30)

        device = DockerDeviceAdapter(device_id, container=container, simulator=self)
        self.connect_network(container)
        return device

    def remove_device(self, container: Union[str, Container], alias: str = ""):
        """Remove device container

        Args:
            container (Union[str, Container]): Container, container id or container name
            alias (str): Device alias (i.e. device-01)
        """
        if isinstance(container, str):
            name = container
            container = self.get_container_by_name(name)
            if container is None:
                logging.info(
                    "Container does not exist, so no need to remove it. name=%s", name
                )
                return

        logging.info(
            "Found existing container. alias=%s, name=%s, id=%s",
            alias,
            container.name,
            container.id,
        )
        try:
            self.disconnect_network(container)
            logging.info("Disconnected container from the network")
        except Exception as ex:
            logging.warning(
                "Could not remove container from the network. exception=%s", ex
            )

        try:
            container.remove(force=True)
            logging.info(
                "Removed existing container [alias=%s, name=%s, id=%s]",
                alias,
                container.name,
                container.id,
            )
        except Exception as ex:
            logging.error("Failed to remove container. exception=%s", ex)

    def _find_network(self, name: str) -> Optional[Network]:
        """Find network by name

        Args:
            name (str): Network name or id

        Returns:
            Network: Network object
        """
        for network in self._docker_client.networks.list(greedy=True):
            if name in [network.name, network.id]:
                return network
        return None

    def _is_container_connected(self, container: Container) -> bool:
        """Test if a container is already connected to the network

        Args:
            container (Container): Container

        Returns:
            bool: True if the container is already connected to the internal network
        """
        # Use updated network object
        network = self._network

        if network is None:
            logging.info("Network object is empty")
            return False

        retries = 5
        network_containers = None
        while retries > 0:
            try:
                network.reload()
                network_containers = network.containers
                break
            except NotFound as ex:
                logging.debug("Could not get container list. exception=%s", ex)

            # Wait in case api is busy
            time.sleep(0.25)
            retries -= 1

        if network_containers is None:
            logging.warning(
                "Could not get list of containers on the network. name=%s",
                self._network_name,
            )
            return False

        found = False
        for i_container in network_containers:
            try:
                i_container.reload()
                if i_container.id == container.id:
                    found = True
                    break
            except NotFound as ex:
                logging.warning("Could not find container. exception=%s", ex)
        return found

    @classmethod
    def wait_for_container_running(cls, container: Container, timeout: float = 30):
        """Wait for the container to be in the running state

        Args:
            container (Container): Container
            timeout (float, optional): Timeout in seconds. Defaults to 30.

        Raises:
            TimeoutError: Container did not reach the running state within the given timeout period.
        """
        # Wait for container to be ready (10s max)
        timeout_limit = time.time() + timeout
        timed_out = True

        retries = 0

        start = time.time()

        while time.time() < timeout_limit:
            container.reload()
            if container.status == "running":
                logging.info(
                    "Container ready: name=%s, id=%s, duration=%.3f, retries=%d",
                    container.name,
                    container.id,
                    time.time() - start,
                    retries,
                )
                timed_out = False
                break
            retries += 1
            time.sleep(0.25)

        if timed_out:
            raise TimeoutError(
                f"Container not ready after {timeout} seconds. "
                f"name={container.name}, id={container.id}, status={container.status}"
            )

    def connect_network(self, container: Container):
        """Connect the container to the internal network

        Args:
            container (Container): Container

        Raises:
            APIError: Docker API Error
        """
        if self._network:
            name = container.name
            try:
                # Try connecting the container, and ignore already exists network
                # as checking if it is already connected is unreliable
                self._network.connect(container)
                logging.info("Connected [%s] to network [%s]", name, self._network.name)
            except APIError as ex:
                # Ignore errors if the network is already attached
                if "already exists in network" not in ex.explanation:
                    raise
                logging.info(
                    "Container [%s] already connected to network [%s]",
                    name,
                    self._network.name,
                )

    def disconnect_network(self, container: Container):
        """Disconnect a container to the internal network to simulate
        a loss of connectivity

        Args:
            container (Container): Container

        Raises:
            APIError: Docker API Error
        """
        if container and self._network:
            try:
                self._network.disconnect(container, force=True)
                logging.info(
                    "Disconnected [%s] from network [%s]",
                    container.name,
                    self._network.name,
                )
            except APIError as ex:
                if "is not connected to network" not in ex.explanation:
                    raise
                logging.info(
                    "Container [%s] already disconnected from network [%s]",
                    container.name,
                    self._network.name,
                )

    def get_container_by_name(self, name: str) -> Optional[Container]:
        """Get a container by it's name

        Args:
            name (str): Container name

        Returns:
            Optional[docker.Container]: Container object
        """
        try:
            return self._docker_client.containers.get(name)
        except NotFound:
            return None

    def get_device_container(self, name: str) -> Optional[Container]:
        """Get the device container by using it's alias.
        The actual container name will be autogenerated to keep it unique.

        Args:
            name (str): Device container alias (i.e. device-01)

        Returns:
            Optional[Container]: Device container
        """
        return self._device_containers.get(name, None)

    def cleanup(self):
        """Cleanup resources created by the fixture"""
        if not self._keep_containers:
            for alias, container in self._device_containers.items():
                self.remove_device(container, alias)

    def remove_container_devices(self, group_id: str = ""):
        """Remove the containers related to the integration testing"""
        logging.info("Removing all pre-existing docker device containers")
        labels = ["device.inttest=1"]
        if group_id:
            labels.append(f"device.test_group_id={group_id}")

        containers = self._docker_client.containers.list(
            all=True,
            filters={
                "label": labels,
            },
        )
        for container in containers:
            logging.info(
                "Removing container. name=%s, id=%s", container.name, container.id
            )
            self.remove_device(container)
