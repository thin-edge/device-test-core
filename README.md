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

#### ssh adapter

```
pip3 install "device-test-core[ssh] @ git+https://github.com/reubenmiller/device-test-core.git"
```

#### local adapter

```
pip3 install "device-test-core[local] @ git+https://github.com/reubenmiller/device-test-core.git"
```


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
