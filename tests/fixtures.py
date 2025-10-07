"""Test fixtures"""

import os
import dotenv
from typing import Dict, Any


def create_config(device_id: str = "unittest001") -> Dict[str, Any]:
    dotenv.load_dotenv()
    config = {
        "hostname": os.getenv("SSH_CONFIG_HOSTNAME", ""),
        "username": os.getenv("SSH_CONFIG_USERNAME", ""),
        "password": os.getenv("SSH_CONFIG_PASSWORD", ""),
    }
    return config
