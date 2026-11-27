
import os, site, sys

# First, discover all system-sites related paths.
original_sys_path = sys.path[:]
# Clear sys.path so addsitedir() will add system site paths and paths
# added by contained .pth files to sys.path reliably. This is necessary
# since Python 3.15, which notably no longer re-executes .pth files for
# known paths.
sys.path = []
known_paths = set()
for path in {'c:\\users\\luota\\desktop\\smarthome\\.venv', 'c:\\users\\luota\\desktop\\smarthome\\.venv\\lib\\site-packages'}:
    site.addsitedir(path, known_paths=known_paths)
system_paths = set(os.path.normcase(path) for path in sys.path)

# Drop discovered system-sites related paths.
original_sys_path = [
    path for path in original_sys_path
    if os.path.normcase(path) not in system_paths
]
sys.path = original_sys_path

# Second, add lib directories.
# ensuring .pth file are processed.
for path in ['C:\\Users\\luota\\Desktop\\SmartHome\\pip-build-env-xe5t4tjc\\overlay\\Lib\\site-packages', 'C:\\Users\\luota\\Desktop\\SmartHome\\pip-build-env-xe5t4tjc\\normal\\Lib\\site-packages']:
    assert not path in sys.path
    site.addsitedir(path)
