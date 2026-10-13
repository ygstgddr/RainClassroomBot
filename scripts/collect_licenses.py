"""Collect license files from installed build dependencies without personal data."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys


def collect(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    versions = {}
    for name in ('playwright', 'PyYAML', 'psutil', 'Pillow', 'pywebview', 'pythonnet',
                 'clr_loader', 'bottle', 'cffi', 'pycparser', 'greenlet', 'pyee',
                 'typing_extensions', 'pyinstaller'):
        distribution = importlib.metadata.distribution(name)
        versions[name] = distribution.version
        copied = 0
        for relative in distribution.files or []:
            # Preserve both package metadata and embedded driver notices.
            if not relative.name.lower().startswith(('license', 'copying', 'notice')):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file() or '..' in relative.parts:
                continue
            target = destination / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied += 1
        if not copied:
            raise RuntimeError(f'No license found for {name}; inspect build environment')
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    # The legacy proxy_tools wheel omits its license; keep an upstream copy with source.
    proxy_license = Path(__file__).resolve().parents[1] / 'third_party_licenses/proxy_tools/LICENSE.txt'
    if not proxy_license.is_file():
        raise RuntimeError('Bundled proxy_tools LICENSE.txt is missing')
    proxy_target = destination / 'proxy_tools'
    proxy_target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(proxy_license, proxy_target / 'LICENSE.txt')
    versions['proxy_tools'] = importlib.metadata.version('proxy_tools')
    if not python_license.is_file():
        raise RuntimeError('Python LICENSE.txt not found in base interpreter')
    shutil.copyfile(python_license, destination / 'Python-LICENSE.txt')
    versions['Python'] = sys.version.split()[0]
    tcl_root = Path(sys.base_prefix) / 'tcl'
    for source in tcl_root.glob('*/license*'):
        if source.is_file():
            shutil.copyfile(source, destination / (source.parent.name + '-' + source.name))
    (destination / 'versions.json').write_text(
        json.dumps(versions, indent=2) + '\n', encoding='utf8')
    print('Collected third-party notices:', ', '.join(versions))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination')
    collect(parser.parse_args().destination)
