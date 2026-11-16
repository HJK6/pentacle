"""Runtime palette assets must travel together through selective inventories."""
import ast
from pathlib import Path


def test_runtime_asset_inventories_keep_lane_palettes_together():
    root = Path(__file__).resolve().parents[3]
    icons, hues = 'pentacle-chat-core/lane_icons.json', 'pentacle-chat-core/lane_hues.json'
    checked = []
    # Scan all tracked-source deployment/install/packaging inventories, including
    # future ones; ignore import loaders and prose references outside inventories.
    for path in root.rglob('*.py'):
        if any(part in {'.git', 'node_modules', '.venv', 'venv', '__pycache__'} for part in path.parts):
            continue
        if not any(word in str(path.relative_to(root)).lower() for word in ('deploy', 'install', 'packag', 'manifest')):
            continue
        text = path.read_text()
        if icons not in text and hues not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
                continue
            children = [child.value for child in ast.iter_child_nodes(node) if isinstance(child, ast.Constant)]
            if icons in children or hues in children:
                assert icons in children and hues in children, str(path.relative_to(root))
                checked.append(str(path.relative_to(root)))
    assert 'services/chat-stream-v2/deploy/coordinated_harness.py' in checked
