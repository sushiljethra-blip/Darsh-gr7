"""
Build-time helper: zips pyvoip from site-packages into pyvoip.zip next to
this script, so PyInstaller can bundle it with --add-data.
Run from the repo root: python sip-dialer-app/bundle_pyvoip.py
"""
import os
import sys
import zipfile
import subprocess


def find_pyvoip_dir():
    """
    Locate the pyVoIP package directory via pip show.
    Using sys.executable guarantees we query the same Python that is
    running this script, avoiding cross-installation confusion.
    Handles the pyVoIP / pyvoip capitalisation difference on Windows.
    """
    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'show', 'pyVoIP'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None, f'pip show pyVoIP failed: {result.stderr.strip()}'

    location = None
    for line in result.stdout.splitlines():
        if line.startswith('Location:'):
            location = line.split(':', 1)[1].strip()
            break

    if not location:
        return None, f'no Location: in pip show output:\n{result.stdout[:400]}'

    # Try exact names first
    for name in ('pyVoIP', 'pyvoip'):
        d = os.path.join(location, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, '__init__.py')):
            return d, None

    # Case-insensitive fallback scan
    try:
        entries = os.listdir(location)
    except OSError:
        entries = []
    match = next((e for e in entries if e.lower() == 'pyvoip'), None)
    if match:
        d = os.path.join(location, match)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, '__init__.py')):
            return d, None

    candidates = [e for e in entries if 'voip' in e.lower()]
    return None, (
        f'pyVoIP directory not found under {location}. '
        f'voip-related entries: {candidates}'
    )


pkg_dir, err = find_pyvoip_dir()
if not pkg_dir:
    print(f'ERROR: {err}', file=sys.stderr)
    sys.exit(1)

print(f'Found pyVoIP at: {pkg_dir}')

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
