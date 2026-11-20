"""Build a complete source bundle for the Pi dataset service from tracked project files."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent.parent
FILES = (
    'lab/aggregation.py',
    'lab/collection_batches.py',
    'lab/collection_routes.py',
    'lab/dataset_api.py',
    'lab/dataset_settings.py',
    'lab/experiment_archive.py',
    'lab/model_manifest.py',
    'lab/record_schema.py',
    'lab/video_uploads.py',
    'deploy/nodered/data.html',
    'requirements-dataset.txt',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    output = parser.parse_args().output
    sources = {name:(ROOT/name).read_bytes().replace(b'\r\n', b'\n') for name in FILES}
    for name, data in sources.items():
        if name.endswith('.py'):
            compile(data,name,'exec')
    hashes = {name:hashlib.sha256(data).hexdigest() for name,data in sources.items()}
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as bundle:
        for name,data in sources.items():
            bundle.writestr(name,data)
        bundle.writestr('bundle-manifest.json',json.dumps({'sha256':hashes},indent=2))
    print(output)


if __name__ == '__main__':
    main()
