"""Command types and common functions"""
from typing import Union
from device_test_core.utils import to_str


class CmdOutput:
    def __init__(
        self, return_code: int, stdout: Union[str, bytes], stderr: Union[str, bytes]
    ) -> None:
        self.return_code = return_code
        self._stdout = stdout
        self._stderr = stderr

    @property
    def stdout(self) -> str:
        """Standard output as a string"""
        return to_str(self._stdout)

    @property
    def stderr(self) -> str:
        """Standard error as a string"""
        return to_str(self._stderr)

    @property
    def raw_stdout(self) -> Union[str, bytes]:
        """Standard output as raw type. Can be either a str or bytes"""
        return self._stdout

    @property
    def raw_stderr(self) -> Union[str, bytes]:
        """Standard error as raw type. Can be either a str or bytes"""
        return self._stderr
