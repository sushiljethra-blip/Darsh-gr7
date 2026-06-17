"""
Build-time helper: downloads the pyVoIP wheel from PyPI and repacks it as
pyvoip.zip (lowercase) next to this script, so PyInstaller can bundle it.

A .whl file is a zip, so we never need to *import* pyvoip — we just extract
the relevant entries and rename pyVoIP/* → pyvoip/* for zipimport.

Run from the repo root: python sip-dialer-app/bundle_pyvoip.py
"""
import glob
import os
import subprocess
import sys
import tempfile
import zipfile

script_dir = os.path.dirname(os.path.abspath(__file__))
zip_path   = os.path.join(script_dir, 'pyvoip.zip')

with tempfile.TemporaryDirectory() as tmpdir:
    # Download the wheel without installing it (works for any Python/PATH)
    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'download', 'pyVoIP', '--no-deps', '-d', tmpdir],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f'ERROR: pip download pyVoIP failed:\n{result.stderr}', file=sys.stderr)
        sys.exit(1)

    wheels = glob.glob(os.path.join(tmpdir, '*.whl'))
    if not wheels:
        print(
            f'ERROR: no .whl found in {tmpdir}.\n'
            f'pip output:\n{result.stdout}\n{result.stderr}',
            file=sys.stderr,
        )
        sys.exit(1)

    whl = wheels[0]
    print(f'Downloaded: {os.path.basename(whl)}')

    # Repack: extract pyVoIP/* from the wheel, store as pyvoip/* (lowercase)
    with (
        zipfile.ZipFile(whl) as src,
        zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as dst,
    ):
        packed = 0
        for info in src.infolist():
            name = info.filename
            if name.endswith('.pyc') or '__pycache__' in name:
                continue
            lo = name.lower()
            if not (lo.startswith('pyvoip/') or lo == 'pyvoip'):
                continue
            if lo.endswith('/'):
                continue  # skip directory entries
            # Normalise prefix to lowercase pyvoip/
            new_name = 'pyvoip/' + name.split('/', 1)[1]
            dst.writestr(new_name, src.read(name))
            packed += 1

print(f'Created {zip_path} ({packed} entries)')
with zipfile.ZipFile(zip_path) as z:
    names = z.namelist()
print(f'Verified {len(names)} entries, e.g.: {names[:5]}')
