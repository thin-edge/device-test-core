from setuptools import setup, find_packages

setup(
    packages=find_packages(include=["device_test_core", "device_test_core.*"]),
)
