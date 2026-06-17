"""
Build-time helper: zips pyvoip from site-packages into sip-dialer-app/pyvoip.zip
so PyInstaller can bundle it with --add-data without needing --collect-all.
Run from the repo root: python sip-dialer-app/bundle_pyvoip.py
"""
import importlib.util
import zipfile
import os
import sys

spec = importlib.util.find_spec('pyvoip')
if not spec or not spec.origin:
    print('ERROR: pyvoip not found. sys.path:', sys.path, file=sys.stderr)
    sys.exit(1)

pkg_dir    = os.path.dirname(spec.origin)
parent_dir = os.path.dirname(pkg_dir)
zip_path   = os.path.join('sip-dialer-app', 'pyvoip.zip')

with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for fname in files:
            if not fname.endswith('.pyc'):
                fpath   = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, parent_dir).replace(os.sep, '/')
                z.write(fpath, arcname)

print(f'Created {zip_path}')
with zipfile.ZipFile(zip_path) as z:
    names = z.namelist()
print(f'{len(names)} entries, e.g.: {names[:4]}')
