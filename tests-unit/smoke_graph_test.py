import json
import sys
from unittest.mock import MagicMock

if "torch" not in sys.modules:
    sys.modules["torch"] = MagicMock()

import folder_paths
import node_helpers


def test_folder_paths_base_directories():
    assert folder_paths.get_input_directory()
    assert folder_paths.get_output_directory()
    assert folder_paths.get_temp_directory()


def test_node_helpers_conditioning_roundtrip():
    cond = [("tensor_stub", {"pooled_output": "stub"})]
    updated = node_helpers.conditioning_set_values(cond, {"strength": 0.75})
    assert len(updated) == 1
    assert updated[0][1]["strength"] == 0.75
    assert cond[0][1].get("strength") is None
