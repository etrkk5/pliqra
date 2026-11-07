"""Install release artifacts outside the source tree, with no model calls or credentials."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile


def check(directory: Path, language='all'):
    directory = directory.resolve()
    manifest = json.loads((directory / 'SDK-MANIFEST.json').read_text())
    for item in manifest['artifacts']:
        if Path(item['file']).name != item['file']:
            raise ValueError('Artifact filenames must be local')
        assert hashlib.sha256((directory / item['file']).read_bytes()).hexdigest() == item['sha256']
    with tempfile.TemporaryDirectory(prefix='pliqra-sdk-install-') as temporary:
        root = Path(temporary)
        if language in ('all', 'python'):
            wheel = next(directory.glob('pliqra_client-*.whl'))
            with zipfile.ZipFile(wheel) as archive:
                assert not any(p.startswith(('pliqra/', 'tests/', 'work/')) for p in archive.namelist())
                assert any(p.endswith('/licenses/LICENSE') for p in archive.namelist())
                metadata = archive.read(next(p for p in archive.namelist() if p.endswith('.dist-info/METADATA'))).decode()
                assert 'Requires-Dist:' not in metadata
            venv.create(root / 'venv', with_pip=True)
            python = root / 'venv/bin/python'
            subprocess.run([str(python), '-m', 'pip', 'install', '--no-index', '--no-deps', str(wheel)], cwd=root, check=True)
            subprocess.run([str(python), '-I', '-c', "import sys,pliqra_client,pliqra_conformance; assert 'fastapi' not in sys.modules; print(pliqra_client.__file__)"], cwd=root, check=True)
            for command in ('pliqra-client', 'pliqra-conformance'):
                subprocess.run([str(root / 'venv/bin' / command), '--help'], cwd=root, check=True, stdout=subprocess.DEVNULL)
        if language in ('all', 'javascript'):
            package = next(directory.glob('eturkok-pliqra-*.tgz'))
            project = root / 'node'
            project.mkdir()
            (project / 'package.json').write_text('{"private":true,"type":"module"}')
            subprocess.run(['npm', 'install', '--offline', '--ignore-scripts', '--no-audit', '--no-fund', str(package)], cwd=project, check=True)
            subprocess.run(['node', '--input-type=module', '-e', "import {Pliqra,ContractChanged} from '@eturkok/pliqra'; const c=new Pliqra('https://example.test','synthetic'); if(!c||!ContractChanged)throw Error('missing exports');"], cwd=project, check=True)
            installed = json.loads((project / 'node_modules/@eturkok/pliqra/package.json').read_text())
            assert installed['version'] == manifest['sdk_version']
            assert (project / 'node_modules/@eturkok/pliqra/pliqra.d.mts').is_file()
    print(json.dumps({'sdk_version': manifest['sdk_version'], 'language': language, 'clean_install': 'passed'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--language', choices=('all', 'python', 'javascript'), default='all')
    args = parser.parse_args()
    check(args.directory, args.language)
