"""Build small SDK artifacts from canonical sources, never from a gateway installation."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path, language='all'):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = (ROOT / 'sdk/VERSION').read_text().strip()
    match = re.fullmatch(r'(\d+\.\d+\.\d+)(?:-rc\.([1-9][0-9]*))?', version)
    if not match:
        raise ValueError('Unsupported SDK version')
    python_version = match[1] + ('rc' + match[2] if match[2] else '')
    with tempfile.TemporaryDirectory(prefix='pliqra-sdk-build-') as stage:
        stage = Path(stage)
        if language in ('all', 'python'):
            project = stage / 'python'
            shutil.copytree(ROOT / 'packaging/python', project)
            metadata = tomllib.loads((project / 'pyproject.toml').read_text())['project']
            if metadata['version'] != python_version:
                raise ValueError('Python SDK version differs from sdk/VERSION')
            shutil.copy2(ROOT / 'sdk/pliqra_client.py', project / 'pliqra_client.py')
            shutil.copy2(ROOT / 'sdk/conformance.py', project / 'pliqra_conformance.py')
            for name in ('LICENSE', 'NOTICE'):
                shutil.copy2(ROOT / name, project / name)
            subprocess.run([sys.executable, '-m', 'build', '--no-isolation', '--outdir', str(output), str(project)], check=True)
        if language in ('all', 'javascript'):
            project = stage / 'javascript'
            shutil.copytree(ROOT / 'packaging/javascript', project)
            if json.loads((project / 'package.json').read_text())['version'] != version:
                raise ValueError('JavaScript SDK version differs from sdk/VERSION')
            for name in ('pliqra.mjs', 'pliqra.d.mts'):
                shutil.copy2(ROOT / 'sdk/javascript' / name, project / name)
            for name in ('LICENSE', 'NOTICE'):
                shutil.copy2(ROOT / name, project / name)
            subprocess.run(['npm', 'pack', '--ignore-scripts', '--pack-destination', str(output)], cwd=project, check=True)
    names={f'pliqra_client-{python_version}-py3-none-any.whl',f'pliqra_client-{python_version}.tar.gz',f'eturkok-pliqra-{version}.tgz'}
    files = sorted(p for p in output.iterdir() if p.name in names)
    manifest = {'sdk_version': version, 'python_version': python_version,
                'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'artifacts': [{'file': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
    (output / 'SDK-MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/sdk')
    parser.add_argument('--language', choices=('all', 'python', 'javascript'), default='all')
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.language)))
