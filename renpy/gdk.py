# Copyright 2004-2026 Tom Rothamel <pytom@bishoujo.us>
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""
Microsoft GDK services (Xbox sign-in, achievements and, on PC, cloud saves)
through the _gdk extension module, on Windows and Xbox.

On Xbox, _gdk is a built-in module of the console runtime. On Windows, it's in
lib/gdk-windows-x86_64, and is used when a MicrosoftGame.config is next to the
executable (a build.windows_gdk distribution), or, when running from source,
when RENPY_GDK=1 is set and the project has xbox/MicrosoftGame.config.

The game-facing API is the gdk namespace (renpy/common/00gdk.rpy).
"""

from __future__ import division, absolute_import, with_statement, print_function, unicode_literals

import hashlib
import json
import os
import shutil
import sys

import renpy

# The directory, relative to the Ren'Py base, holding the PC module.
PC_LIB = os.path.join("lib", "gdk-windows-x86_64")

# The PC game configuration.
CONFIG = "MicrosoftGame.config"

# The _gdk module, once initialized.
module = None

# True once init() has run.
initialized = False

# Why GDK services are unavailable, or None.
error = None

# The XGameSaveFiles folder that holds the saves (PC), or None.
save_root = None

# A local persistent file to merge into the cloud one, once persistent is loaded.
local_persistent = None

# The ledger (filename, data) recording what was imported from the local saves.
import_ledger = None

# True if the saves should be in the cloud, but aren't.
save_notice = False

# Keep the DLL directories added.
_dll_directories = [ ]


def log(msg):
    """
    Writes `msg` to log.txt and the debugger output.
    """

    try:
        renpy.display.log.write("GDK: %s", msg)
    except Exception:
        pass

    if module is not None:
        try:
            module.log(msg)
        except Exception:
            pass


def is_xbox():
    return os.environ.get("RENPY_PLATFORM", "").startswith("xbox")


def find_config():
    """
    Returns (found, path): whether to use GDK on Windows, and the
    MicrosoftGame.config to pass to the runtime (None to let it find the one
    next to the executable).
    """

    setting = os.environ.get("RENPY_GDK", "")

    if setting == "0":
        return False, None

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))

    if os.path.isfile(os.path.join(exe_dir, CONFIG)):
        return True, None

    base = renpy.config.basedir or ""

    if os.path.isfile(os.path.join(base, CONFIG)):
        return True, os.path.join(base, CONFIG)

    # Running from source. The runtime ends the process when a silent
    # sign-in fails, so this is opt in.
    dev = os.path.join(base, "xbox", CONFIG)

    if setting and os.path.isfile(dev):
        return True, dev

    return False, None


def import_module():
    """
    Imports and returns _gdk, or None if it can't be.
    """

    global error

    if is_xbox():
        import _gdk # type: ignore
        return _gdk

    lib = os.path.join(renpy.config.renpy_base, PC_LIB)

    if not os.path.isfile(os.path.join(lib, "_gdk.pyd")):
        error = "{} is missing.".format(os.path.join(lib, "_gdk.pyd"))
        return None

    dirs = [ lib ]

    # From source, the GDK runtime DLLs come from the installed GDK.
    if not os.path.isfile(os.path.join(lib, "libHttpClient.dll")) and os.environ.get("GameDKLatest"):
        dirs.append(os.path.join(os.environ["GameDKLatest"], "windows", "bin", "x64"))

    for i in dirs:
        if os.path.isdir(i):
            try:
                _dll_directories.append(os.add_dll_directory(i))
            except Exception:
                pass

    sys.path.insert(0, lib)

    try:
        import _gdk # type: ignore
        return _gdk
    except Exception as e:
        error = "Could not import _gdk: {!r}".format(e)
        return None
    finally:
        sys.path.remove(lib)


def init():
    """
    Initializes the GDK runtime and signs in the default user, if this is
    a GDK build. Called once, before the save directory is chosen.
    """

    global initialized, module, error

    if initialized:
        return

    initialized = True

    if is_xbox():
        config = None
    elif renpy.windows:
        found, config = find_config()
        if not found:
            return
    else:
        return

    try:
        m = import_module()

        if m is None:
            log(error)
            return

        if not m.initialize(config):
            error = "Initializing the GDK runtime failed (0x{:08X}).".format(m.last_error())
            log(error)
            return

        module = m

        import atexit
        atexit.register(shutdown)
        # On PC, this may show the sign-in UI. A silent sign-in that fails
        # makes the runtime end the game. On Xbox, the launcher has already
        # signed in the default user.
        state = m.sign_in(allow_ui=True, wait=True)

        log("Initialized: platform {}, title {}, user {}.".format(
            m.platform, "{:08X}".format(m.title_id()) if m.title_id() else None, state))

    except Exception as e:
        error = "GDK initialization failed: {!r}".format(e)
        log(error)


def shutdown():
    global module

    if module is not None:
        try:
            module.shutdown()
        except Exception:
            pass

    module = None


# Saves (PC) ###################################################################

def _digest(fn):
    h = hashlib.sha256()

    with open(fn, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)

    return h.hexdigest()


def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst + ".new")
    os.replace(dst + ".new", dst)


def merge_tokens(src, dst):
    """
    Adds the lines of the save token file `src` missing from `dst`.
    """

    if not os.path.isfile(src):
        return

    with open(src, encoding="utf-8") as f:
        incoming = f.read().splitlines()

    existing = [ ]

    if os.path.isfile(dst):
        with open(dst, encoding="utf-8") as f:
            existing = f.read().splitlines()

    merged = list(dict.fromkeys(existing + incoming))

    if merged == existing:
        return

    os.makedirs(os.path.dirname(dst), exist_ok=True)

    with open(dst + ".new", "w", encoding="utf-8") as f:
        f.write("\n".join(merged) + "\n")

    os.replace(dst + ".new", dst)


def import_saves(local_saves, local_tokens, root, user):
    """
    Copies the saves in `local_saves` that are new or changed since the last
    import into the cloud folder `root`, for `user`. The originals are kept.

    A slot that differs from the cloud one is imported as an offline-* slot,
    and a persistent that does is merged into the cloud one after it loads
    (see merge_local_persistent). Returns the names of the imported files.
    """

    global local_persistent, import_ledger

    # The keys first, so the imported files are trusted.
    merge_tokens(os.path.join(local_tokens, "security_keys.txt"), os.path.join(root, "tokens", "security_keys.txt"))

    if not os.path.isdir(local_saves):
        return [ ]

    ledger_fn = os.path.join(local_saves, "gdk-imports.json")
    ledger = { }

    if os.path.isfile(ledger_fn):
        try:
            with open(ledger_fn, encoding="utf-8") as f:
                ledger = json.load(f)
        except Exception:
            ledger = { }

    done = ledger.setdefault(str(user), { })
    cloud = os.path.join(root, "saves")
    rv = [ ]

    for name in sorted(os.listdir(local_saves)):
        if name != "persistent" and not name.endswith(".save"):
            continue

        src = os.path.join(local_saves, name)

        if not os.path.isfile(src):
            continue

        digest = _digest(src)

        if done.get(name) == digest:
            continue

        dst = os.path.join(cloud, name)

        if os.path.exists(dst):

            if _digest(dst) == digest:
                done[name] = digest
                continue

            if name == "persistent":
                # Merged field by field, once the cloud persistent loads.
                local_persistent = (src, digest)
                continue

            dst = os.path.join(cloud, "offline-" + digest[:16] + "-LT1.save")

            if os.path.exists(dst):
                done[name] = digest
                continue

        _copy(src, dst)
        done[name] = digest
        rv.append(os.path.basename(dst))

    import_ledger = (ledger_fn, ledger)
    write_ledger()

    return rv


def write_ledger():
    if import_ledger is None:
        return

    fn, ledger = import_ledger

    with open(fn + ".new", "w", encoding="utf-8") as f:
        json.dump(ledger, f)

    os.replace(fn + ".new", fn)


def prepare_saves(gamedir):
    """
    On PC, when config.gdk_saves is true and a user is signed in, puts the
    saves and save tokens in the user's XGameSaveFiles folder, importing the
    ones in the usual location. Called before the save directory is chosen.
    """

    global save_root, save_notice

    save_root = None

    if is_xbox() or not renpy.config.gdk_saves:
        return

    if module is None or not module.signed_in():
        save_notice = module is not None
        log("Cloud saves are unavailable: {}".format(error or "no signed-in user."))
        return

    try:
        root = module.save_folder()
    except Exception as e:
        root = None
        log("Cloud saves are unavailable: {!r}".format(e))

    if not root:
        save_notice = True
        return

    local_saves = renpy.__main__.path_to_saves(gamedir)
    local_tokens = renpy.__main__.path_to_saves(gamedir, "tokens")

    try:
        imported = import_saves(local_saves, local_tokens, root, module.user_id())
        if imported:
            log("Imported local saves: " + ", ".join(imported))
    except Exception as e:
        # Keep the cloud saves; the local ones are retried next time.
        log("Importing local saves failed: {!r}".format(e))

    save_root = root
    log("Saves are in " + root)


def path_to_saves(save_directory):
    """
    Returns where the save directory (None) or tokens ("tokens") go when
    they're in the cloud folder, or None to use the usual location.
    """

    if save_root is None or save_directory not in (None, "tokens"):
        return None

    return os.path.join(save_root, save_directory or "saves")


def merge_local_persistent():
    """
    Merges a local persistent that differs from the cloud one into it.
    Called once persistent data has loaded.
    """

    global local_persistent

    if local_persistent is None or save_root is None or import_ledger is None:
        return

    fn, digest = local_persistent
    local_persistent = None

    other = renpy.persistent.load(fn)

    if other is None:
        log("Could not load the local persistent data in " + fn)
        return

    renpy.persistent.merge(other)
    renpy.persistent.update(True)

    _fn, ledger = import_ledger
    ledger[str(module.user_id())]["persistent"] = digest
    write_ledger()
    log("Merged the local persistent data.")
