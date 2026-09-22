import json
from pathlib import Path
def read_order(path):
    return json.loads(Path(path).read_text())
def save_order(path, payload):
    Path(path).write_text(json.dumps(payload))
