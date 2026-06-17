"""
Build-time helper: zips pyvoip from site-packages into pyvoip.zip next to
this script, so PyInstaller can bundle it with --add-data.
Run from the repo root: python sip-dialer-app/bundle_pyvoip.py
"""
import os
import sys
import zipfile

# Use plain `import pyvoip` — Python's import on Windows is case-insensitive
# and finds pyVoIP/ even if find_spec('pyvoip') would miss the capital V.
try:
    import pyvoip
except ImportError as e:
    print(f'ERROR: import pyvoip failed: {e}', file=sys.stderr)
    sys.exit(1)

pkg_dir    = os.path.dirname(pyvoip.__file__)
parent_dir = os.path.dirname(pkg_dir)
script_dir = os.path.dirname(os.path.abspath(__file__))
zip_path   = os.path.join(script_dir, 'pyvoip.zip')

with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for fname in files:
            if not fname.endswith('.pyc'):
                fpath   = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, parent_dir).replace(os.sep, '/')
                # Normalise to lowercase 'pyvoip/' so zipimport finds it
                arcname = 'pyvoip/' + arcname.split('/', 1)[-1]
                z.write(fpath, arcname)

print(f'Created {zip_path}')
with zipfile.ZipFile(zip_path) as z:
    names = z.namelist()
print(f'{len(names)} entries, e.g.: {names[:5]}')
