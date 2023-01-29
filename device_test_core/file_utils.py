"""File utilities"""
import os
import logging
import glob
from typing import List, IO
import tarfile


log = logging.getLogger()


def _parse_base_path_from_pattern(pattern: str) -> str:
    output = []
    parts = pattern.split(os.path.sep)
    for part in parts:
        if "*" in part:
            break
        output.append(part)

    return os.path.sep.join(output)


def make_tarfile(
    fileobj: IO[bytes], patterns: List[str], compress: bool = False
) -> int:
    """Make a tar file given a list of patterns

    If the file already exists the files will be appended to it.

    Args:
        file (str): Tar file where the files will be added to. It does not have to exist.
        patterns (List[str]): List of glob patterns to be added to the tar file
        compress (bool, optional): Use compression when creating tar (e.g. gzip)
    """
    total_files = 0
    mode = "w:gz" if compress else "w"
    with tarfile.open(fileobj=fileobj, mode=mode) as tar:
        for pattern in patterns:
            source_dir = _parse_base_path_from_pattern(pattern)

            for match in glob.glob(pattern):
                if match == source_dir:
                    archive_path = os.path.basename(match)
                else:
                    archive_path = match[len(source_dir) :].lstrip("/")
                log.debug("Adding file: path=%s, archive_path=%s", match, archive_path)
                tar.add(match, arcname=archive_path)
                total_files += 1
                # tar.add(source_dir, arcname=os.path.basename(source_dir))

    return total_files
