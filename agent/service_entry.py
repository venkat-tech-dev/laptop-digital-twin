"""Service entry point executed by the Windows Service Control Manager.

The SCM runs the base Python interpreter with this file. It puts the agent package and the virtual
environment's site-packages (including pywin32's DLL directory) on the path, switches to the agent
directory so ``.env`` / ``../.env`` are found, then hands over to the service host.

    python service_entry.py --startup auto install   # administrator
    python service_entry.py start | stop | remove
"""

import os
import site
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_SITE = os.path.join(ROOT, ".venv", "Lib", "site-packages")
sys.path.insert(0, ROOT)
if os.path.isdir(VENV_SITE):
    site.addsitedir(VENV_SITE)  # processes pywin32.pth (adds pywin32_system32 DLL directory)
    dll_dir = os.path.join(VENV_SITE, "pywin32_system32")
    if os.path.isdir(dll_dir):
        os.add_dll_directory(dll_dir)
os.chdir(ROOT)

from app.service.windows_service import main  # noqa: E402

if __name__ == "__main__":
    main(sys.argv)
