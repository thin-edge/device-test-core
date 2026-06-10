# device-test-core

Device test core library for running tests against a device using a generic interface, e.g. SSH, Docker, or some other custom type.

This library can be used to build plugins for different test frameworks such as pytest and Robot Framework.

The project's goal is to create a common interface for interacting with devices under test. The adapter normalize the interface enabling you to write tests which are independent of the device interface being used (e.g. docker, ssh, local etc.).

## Installing from project

The `device-test-core` package includes several device adapters, each with their own dependencies.

Below shows how to either install all adapters, or just the adapters you are interested in. This allows you to keep the dependencies to a minimum.

### Installing all adapters

```
pip3 install "device-test-core[all] @ git+https://github.com/reubenmiller/device-test-core.git"
```

### Installing specific adapters

#### docker adapter

```
pip3 install "device-test-core[docker] @ git+https://github.com/reubenmiller/device-test-core.git"
```

This also includes the docker compose adapter. The compose adapter additionally requires the docker cli with the compose v2 plugin to be installed on the host.

#### ssh adapter

```
pip3 install "device-test-core[ssh] @ git+https://github.com/reubenmiller/device-test-core.git"
```

#### local adapter

```
pip3 install "device-test-core[local] @ git+https://github.com/reubenmiller/device-test-core.git"
```


## Docker Compose support

For test setups which need more than a single container (e.g. a device plus a broker, registry or other simulators), a whole stack can be created from a docker compose file using the compose adapter.

```python
from device_test_core.compose.factory import ComposeDeviceFactory

factory = ComposeDeviceFactory()
stack = factory.create_stack(
    "docker-compose.yaml",
    env={"DEVICE_ID": "device-001"},
)
try:
    # main device under test
    device = stack.get_device()
    device.assert_command("echo hello")

    # but any service of the stack can be used as a device
    broker = stack.get_device("broker")
    broker.assert_command("mosquitto_sub -t 'te/#' -C 1 -W 3")

    # resolve dynamically assigned host ports of published container ports
    host, port = stack.get_service_port("broker", 1883)

    # simulate a network outage of the device only
    device.disconnect_network()
    device.connect_network()
finally:
    stack.cleanup(force=True)
```

### Isolation / parallel execution

Test suites using this library generally run in parallel, so each stack is isolated by design:

* Every stack is created under a unique, randomly generated compose project name. Compose namespaces all containers, networks and volumes by the project name, so parallel stacks never clash.
* Each stack gets its own (compose default) network. Services reach each other via their service names. `disconnect_network`/`connect_network` only affect the given service, the rest of the stack stays connected.
* The compose file is validated before starting the stack, and rejected if it contains settings which would break parallel execution:
    * `container_name` (fixed container names collide across runs)
    * fixed host ports, e.g. `ports: ["8080:80"]`. Use ephemeral ports instead, e.g. `ports: ["80"]`, and resolve the assigned host port using `stack.get_service_port(service, port)`
    * `external` networks/volumes or networks/volumes with a fixed `name`

### Selecting the main device under test

One service of the stack acts as the main device under test. It is resolved in the following order:

1. The `device_service` argument passed to `create_stack`
2. The service marked with the label `device-test-core.role: main`:

    ```yaml
    services:
      device:
        image: debian-systemd
        labels:
          device-test-core.role: main
      broker:
        image: eclipse-mosquitto:2
    ```

3. The only service (if the compose file defines a single service)
4. A service named `device`

### Environment variables

The `env` values passed to `create_stack` (plus the values of the given `env_file`) are available for variable interpolation inside the compose file, e.g. `${DEVICE_ID}`.

### Running Tests

#### SSH tests using an ssh-agent

1. Add the target device's hostname and username

    ```sh
    echo "SSH_CONFIG_HOSTNAME=mydevice.local" >> .env
    echo "SSH_CONFIG_USERNAME=root" >> .env
    ```

2. Start the ssh-agent (if not already done by default in your shell profile)

    ```sh
    eval $(ssh-agent)
    ```

3. Import your SSH key that you want to the ssh-agent (so the test can access it)

    ```sh
    ssh-add ~/.ssh/<mysshkey>
    ```

4. Run the tests

    ```sh
    just test
    ```

    **Note** The compose adapter tests require a running docker daemon with the compose v2 plugin. They are automatically skipped if docker is not available.
