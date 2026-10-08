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
Support code for building Xbox packages from the launcher: locating and
checking the "xbox" DLC (the native console runtime), staging a game into a
GDK loose layout, and packaging/deploying it with the GDK tools.

The xbox DLC is extracted into the SDK as ``xbox\\``; xbox-build produces the
same layout in ``dist\\xbox\\`` with make_xbox_dlc.ps1. This module doesn't
depend on Ren'Py, so it can be tested with a plain Python interpreter.
"""

import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys

import xml.etree.ElementTree as ET

import xbox_config

DLC_DIR = "xbox"
DLC_INFO = "dlc.json"

# Files every runtime\<platform> directory in the DLC must contain, relative
# to that directory. The launcher refuses to stage from an incomplete DLC.
RUNTIME_REQUIRED = (
    xbox_config.EXECUTABLE_NAME,
    "python312.dll",
    "SDL2.dll",
    "SDL2_image.dll",
    "SDL2_mixer.dll",
    "SDL2_ttf.dll",
    "avcodec-61.dll",
    "avformat-61.dll",
    "avutil-59.dll",
    "swresample-5.dll",
    "swscale-8.dll",
    "libHttpClient.GDK.dll",
    "Microsoft.Xbox.Services.GDK.C.Thunks.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "msvcp140.dll",
    "renpy.py",
    "lib/python3.12/site.py",
    "lib/python3.12/os.py",
    "lib/python3.12/six.py",
    "lib/python3.12/_socket.py",
    "lib/python3.12/select.py",
    "pygame_sdl2/__init__.py",
    "overrides/renpy/ecsign.py",
    "overrides/renpy/gl2/assimp.py",
    "modules/_renpy.pyd",
    "modules/pygame_sdl2.display.pyd",
    "modules/unicodedata.pyd",
)

# Minimum number of extension modules in runtime\<platform>\modules.
MIN_MODULES = 80


def dlc_path(sdk):
    """
    Returns the xbox DLC directory inside the Ren'Py SDK at `sdk`.
    """

    return os.path.join(sdk, DLC_DIR)


def runtime_path(dlc, target):
    """
    Returns the runtime directory for `target` ("scarlett" or "xboxone").
    """

    return os.path.join(dlc, "runtime", xbox_config.GDK_PLATFORMS[target])


def read_info(dlc):
    """
    Returns the parsed dlc.json, or None if it doesn't exist.
    """

    path = os.path.join(dlc, DLC_INFO)

    if not os.path.isfile(path):
        return None

    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def base_version(version):
    """
    Strips build metadata ("+unofficial.branch") from a Ren'Py version string.
    """

    return version.split("+", 1)[0].strip()


def check_runtime(dlc, target):
    """
    Checks that the DLC has a complete runtime for `target`. Returns a list
    of problems; an empty list means the runtime is usable.
    """

    rt = runtime_path(dlc, target)

    if not os.path.isdir(rt):
        return [ "The xbox DLC has no runtime for {} ({}).".format(target, xbox_config.GDK_PLATFORMS[target]) ]

    rv = [ ]

    for rel in RUNTIME_REQUIRED:
        if not os.path.isfile(os.path.join(rt, *rel.split("/"))):
            rv.append("The {} runtime is missing {}.".format(target, rel))

    modules = os.path.join(rt, "modules")
    count = len([ i for i in os.listdir(modules) if i.endswith(".pyd") ]) if os.path.isdir(modules) else 0

    if count < MIN_MODULES:
        rv.append("The {} runtime has {} extension modules; expected at least {}.".format(target, count, MIN_MODULES))

    return rv


def check_dlc(dlc, target, renpy_version=None):
    """
    Checks the DLC at `dlc` for `target`. If `renpy_version` is given, the
    DLC must have been built for that Ren'Py version. Returns a list of
    problems.
    """

    info = read_info(dlc)

    if info is None:
        return [ "The xbox DLC isn't installed ({} not found).".format(os.path.join(dlc, DLC_INFO)) ]

    rv = [ ]

    if renpy_version is not None:
        built = base_version(info.get("renpy_version", ""))

        if built != base_version(renpy_version):
            rv.append("The xbox DLC was built for Ren'Py {}, but this is Ren'Py {}. Install the matching xbox DLC.".format(
                built or "(unknown)", base_version(renpy_version)))

    rv.extend(check_runtime(dlc, target))
    return rv


def find_dlc(sdk, xbox_build=None):
    """
    Returns (path, dev) for the xbox DLC to build with, or (None, False) if
    there isn't one. An xbox-build checkout's dist\\xbox is preferred (dev
    mode: `dev` is True and the version check should be skipped), then the
    DLC installed in the SDK.
    """

    if xbox_build:
        d = os.path.join(xbox_build, "dist", "xbox")
        if os.path.isfile(os.path.join(d, DLC_INFO)):
            return d, True

    d = dlc_path(sdk)
    if os.path.isfile(os.path.join(d, DLC_INFO)):
        return d, False

    return None, False


# The name of the update index in an xbox DLC download (the updater's
# updates.json, with updates.ecdsa and rpu\ next to it).
UPDATES_JSON = "updates.json"


def is_sdk(sdk):
    """
    Returns true if `sdk` is an installed Ren'Py SDK (one that the updater
    can add DLC to), rather than a source checkout.
    """

    return os.path.isfile(os.path.join(sdk, "update", "current.json"))


def find_dlc_source(path, workdir):
    """
    Finds the update directory of a downloaded xbox DLC. `path` may be the
    DLC's .zip (extracted into `workdir`, which is emptied first), its
    updates.json, or a directory containing updates.json directly or in a
    single subdirectory. Returns the directory containing updates.json.
    Raises XboxBuildError if it can't be found.
    """

    path = os.path.abspath(path)

    if os.path.isfile(path) and path.lower().endswith(".zip"):
        import zipfile

        if os.path.exists(workdir):
            shutil.rmtree(workdir)

        os.makedirs(workdir)

        try:
            with zipfile.ZipFile(path) as zf:
                zf.extractall(workdir)
        except (zipfile.BadZipFile, OSError) as e:
            raise XboxBuildError("Couldn't extract {}: {}".format(path, e))

        path = workdir

    elif os.path.isfile(path):
        if os.path.basename(path).lower() != UPDATES_JSON:
            raise XboxBuildError("{} isn't an xbox DLC download (expected a .zip or {}).".format(path, UPDATES_JSON))

        return os.path.dirname(path)

    if not os.path.isdir(path):
        raise XboxBuildError("{} doesn't exist.".format(path))

    if os.path.isfile(os.path.join(path, UPDATES_JSON)):
        return path

    subdirs = [ os.path.join(path, i) for i in os.listdir(path) if os.path.isfile(os.path.join(path, i, UPDATES_JSON)) ]

    if len(subdirs) == 1:
        return subdirs[0]

    raise XboxBuildError("{} doesn't contain an xbox DLC ({} not found).".format(path, UPDATES_JSON))


def check_dlc_source(source, renpy_version=None):
    """
    Checks the xbox DLC update directory `source` (as returned by
    find_dlc_source). If `renpy_version` is given, the DLC must have been
    built for that version of Ren'Py. Returns (problems, pretty_version).
    The signature (updates.ecdsa) is verified by the updater itself.
    """

    try:
        with io.open(os.path.join(source, UPDATES_JSON), "r", encoding="utf-8") as f:
            updates = json.load(f)
    except (OSError, ValueError) as e:
        return [ "Couldn't read {}: {}".format(UPDATES_JSON, e) ], None

    entry = updates.get(DLC_DIR) if isinstance(updates, dict) else None

    if not isinstance(entry, dict):
        return [ "This download doesn't contain the xbox DLC." ], None

    rv = [ ]

    if not os.path.isfile(os.path.join(source, "updates.ecdsa")):
        rv.append("The download is missing its signature (updates.ecdsa).")

    rpu = entry.get("rpu_url")

    if not rpu:
        rv.append("The xbox DLC entry has no rpu_url.")
    elif not os.path.isfile(os.path.join(source, *rpu.split("/"))):
        rv.append("The download is missing {}.".format(rpu))

    if renpy_version is not None:
        built = base_version(entry.get("renpy_version", ""))

        if built != base_version(renpy_version):
            rv.append("This xbox DLC is for Ren'Py {}, but this is Ren'Py {}. Download the xbox DLC released with this SDK.".format(
                built or "(unknown)", base_version(renpy_version)))

    return rv, entry.get("pretty_version")


def source_url(source):
    """
    Returns the file: URL of the updates.json in the DLC update directory
    `source`, for the updater.
    """

    import pathlib
    return pathlib.Path(os.path.abspath(source), UPDATES_JSON).as_uri()


def make_dlc_bundle(update_dir, out_zip, name=DLC_DIR, folder=None):
    """
    Writes the release download for DLC `name` to `out_zip`, from the
    distribute output `update_dir` (updates.json, updates.ecdsa, rpu\\). Only
    the rpu blocks containing the DLC's data are included. updates.json is
    copied unchanged, so its signature stays valid. Files are placed in
    `folder` inside the zip (default: the zip's base name). Returns the
    number of block files included.
    """

    import zipfile
    import zlib

    with io.open(os.path.join(update_dir, UPDATES_JSON), "r", encoding="utf-8") as f:
        entry = json.load(f).get(name)

    if not entry or not entry.get("rpu_url"):
        raise XboxBuildError("{} has no rpu entry for {}.".format(os.path.join(update_dir, UPDATES_JSON), name))

    rpu_url = entry["rpu_url"]
    rpu_dir = os.path.dirname(rpu_url)

    with open(os.path.join(update_dir, *rpu_url.split("/")), "rb") as f:
        filelist = json.loads(zlib.decompress(f.read()).decode("utf-8"))

    needed = set(seg["hash"] for i in filelist["files"] for seg in i["segments"])
    blocks = [ ]
    covered = set()

    for b in filelist["blocks"]:
        hashes = set(seg["hash"] for seg in b["segments"])

        if hashes & needed:
            blocks.append(b["name"])
            covered |= hashes

    missing = needed - covered

    if missing:
        raise XboxBuildError("{} segments of {} aren't in any block.".format(len(missing), name))

    if folder is None:
        folder = os.path.splitext(os.path.basename(out_zip))[0]

    files = [ UPDATES_JSON, "updates.ecdsa", rpu_url ] + [ rpu_dir + "/" + i for i in blocks ]

    tmp = out_zip + ".tmp"

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for rel in files:
            path = os.path.join(update_dir, *rel.split("/"))

            if not os.path.isfile(path):
                raise XboxBuildError("{} is missing.".format(path))

            compress = zipfile.ZIP_DEFLATED if rel.endswith(".json") else zipfile.ZIP_STORED
            zf.write(path, folder + "/" + rel, compress_type=compress)

    os.replace(tmp, out_zip)
    return len(blocks)


class XboxBuildError(Exception):
    """
    A build step failed. The message is meant to be shown to the user.
    """


################################################################################
# Staging


def _up_to_date(src, dst):
    try:
        s = os.stat(src)
        d = os.stat(dst)
    except OSError:
        return False

    return s.st_size == d.st_size and int(d.st_mtime) >= int(s.st_mtime)


def copy_tree(src, dst, incremental=False, skip=(), include=None):
    """
    Copies the files under `src` into `dst`, merging with what's there.
    __pycache__ directories are skipped.

    `incremental`
        If true, files whose copy has the same size and isn't older are
        skipped.

    `skip`
        Names of top-level files and directories to leave out.

    `include`
        If given, a function that takes a path relative to `src` (with
        forward slashes) and returns true if the file should be copied.

    Returns the number of files copied.
    """

    rv = 0

    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        rel = "" if rel == "." else rel

        if not rel:
            dirnames[:] = [ i for i in dirnames if i not in skip ]
            filenames = [ i for i in filenames if i not in skip ]

        dirnames[:] = [ i for i in dirnames if i != "__pycache__" ]

        for fn in filenames:
            relfn = os.path.join(rel, fn)

            if include is not None and not include(relfn.replace(os.sep, "/")):
                continue

            s = os.path.join(src, relfn)
            d = os.path.join(dst, relfn)

            if incremental and _up_to_date(s, d):
                continue

            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            rv += 1

    return rv


def stage_runtime(dlc, target, layout, incremental=False):
    """
    Copies the DLC runtime for `target` into `layout`, then lays its
    overrides over layout\\renpy. The layout must already contain renpy\\.
    Returns the number of files copied.
    """

    if not os.path.isfile(os.path.join(layout, "renpy", "__init__.py")):
        raise XboxBuildError("{} doesn't contain Ren'Py (renpy\\__init__.py).".format(layout))

    rt = runtime_path(dlc, target)
    rv = copy_tree(rt, layout, incremental, skip=("overrides",))

    overrides = os.path.join(rt, "overrides")
    if os.path.isdir(overrides):
        rv += copy_tree(overrides, layout, incremental)

    return rv


def stage_renpy_source(renpy_root, layout, incremental=False):
    """
    Copies Ren'Py's Python code and renpy\\common from a Ren'Py checkout or
    SDK at `renpy_root` into layout\\renpy. (The launcher uses the
    Distributor instead, which also honors the project's build settings.)
    """

    def include(rel):
        return rel.startswith("common/") or rel.endswith(".py")

    return copy_tree(os.path.join(renpy_root, "renpy"), os.path.join(layout, "renpy"), incremental, include=include)


def stage_game(game, layout, incremental=False):
    """
    Copies a game directory into layout\\game, leaving out saves.
    """

    if not os.path.isdir(game):
        raise XboxBuildError("The game directory {} doesn't exist.".format(game))

    return copy_tree(game, os.path.join(layout, "game"), incremental, skip=("saves",))


def write_vc_version(layout, version_dict):
    """
    Writes layout\\renpy\\vc_version.py (as renpy.versions.generate_vc_version
    does) if it doesn't exist, so the console doesn't need git to know
    Ren'Py's version. `version_dict` is renpy.version_dict, or the result of
    renpy.versions.get_git_version().
    """

    path = os.path.join(layout, "renpy", "vc_version.py")

    if os.path.exists(path):
        return False

    lines = [ "branch = {!r}".format(version_dict["branch"]) ]

    if version_dict.get("dirty"):
        lines.append("dirty = True")

    lines.append("official = {!r}".format(bool(version_dict.get("official"))))
    lines.append("nightly = {!r}".format(bool(version_dict.get("nightly"))))
    lines.append("version = {!r}".format(version_dict["version"].split("+")[0]))
    lines.append("version_name = {!r}".format(version_dict["name"]))

    with io.open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return True


def git_version_dict(renpy_root):
    """
    Computes Ren'Py's version dict from the git checkout at `renpy_root`,
    using renpy\\versions.py without importing Ren'Py.
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location("_xbox_renpy_versions", os.path.join(renpy_root, "renpy", "versions.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    old = os.getcwd()
    os.chdir(renpy_root)

    try:
        return module.get_git_version()
    finally:
        os.chdir(old)


def stage_gameconfig(config, layout, target):
    """
    Copies MicrosoftGame.config into `layout` (for xboxone, the Scarlett
    TargetDeviceFamily is rewritten to XboxOne) along with every
    ShellVisuals image it references, from the config's folder. Images that
    don't exist get a 1x1 placeholder so makepkg can still pack a dev build.

    Returns a list of warnings.
    """

    if os.path.isdir(config):
        config = os.path.join(config, xbox_config.CONFIG_NAME)

    if not os.path.isfile(config):
        raise XboxBuildError("{} was not found.".format(config))

    with open(config, "rb") as f:
        data = f.read()

    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise XboxBuildError("{} is not well-formed XML: {}".format(config, e))

    if target == "xboxone":
        data = re.sub(br"""(TargetDeviceFamily\s*=\s*["'])Scarlett""", br"\1XboxOne", data)

    os.makedirs(layout, exist_ok=True)
    layout = os.path.abspath(layout)

    with open(os.path.join(layout, xbox_config.CONFIG_NAME), "wb") as f:
        f.write(data)

    rv = [ ]

    for attr, rel in xbox_config.referenced_images(root):
        src = xbox_config.image_path(config, rel)
        dst = os.path.normpath(os.path.join(layout, rel.replace("/", os.sep)))

        if os.path.commonpath([ layout, dst ]) != layout:
            raise XboxBuildError("{} '{}' points outside the package.".format(attr, rel))

        os.makedirs(os.path.dirname(dst), exist_ok=True)

        if os.path.isfile(src):
            shutil.copyfile(src, dst)
        else:
            rv.append("{} '{}' was not found next to the config; using a 1x1 placeholder.".format(attr, rel))

            with open(dst, "wb") as f:
                f.write(xbox_config.placeholder_png())

    return rv


PRECOMPILE_DIRS = ("renpy", "pygame_sdl2", os.path.join("lib", "python3.12"))


def can_precompile():
    """
    The console runs CPython 3.12, so only a 3.12 interpreter (like the
    launcher's) can write .pyc files it will load.
    """

    return sys.version_info[:2] == (3, 12)


def precompile(layout, incremental=False):
    """
    Writes hash-checked .pyc files for the Python code in `layout`, since the
    console can't write them to the read-only package. Returns the number of
    files compiled (0 if this interpreter isn't CPython 3.12).
    """

    import py_compile

    if not can_precompile():
        return 0

    tag = sys.implementation.cache_tag
    rv = 0

    for d in PRECOMPILE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(layout, d)):
            dirnames[:] = [ i for i in dirnames if i != "__pycache__" ]

            for fn in filenames:
                if not fn.endswith(".py"):
                    continue

                src = os.path.join(dirpath, fn)
                pyc = os.path.join(dirpath, "__pycache__", "{}.{}.pyc".format(fn[:-3], tag))

                if incremental and os.path.exists(pyc) and os.path.getmtime(pyc) >= os.path.getmtime(src):
                    continue

                try:
                    py_compile.compile(src, cfile=pyc, doraise=True,
                        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
                    rv += 1
                except py_compile.PyCompileError:
                    pass

    return rv


def finish_layout(layout, dlc, target, config, version_dict, incremental=False):
    """
    Turns a layout holding renpy\\ and game\\ (from the Distributor or
    stage_renpy_source/stage_game) into a loose Xbox layout: adds the DLC
    runtime, vc_version.py, the MicrosoftGame.config and its images, and
    .pyc files.

    `version_dict`
        renpy.version_dict, or a function returning it (only called if the
        layout has no vc_version.py).

    Returns a list of warnings about the config.
    """

    stage_runtime(dlc, target, layout, incremental)

    if not os.path.exists(os.path.join(layout, "renpy", "vc_version.py")):
        write_vc_version(layout, version_dict() if callable(version_dict) else version_dict)

    rv = stage_gameconfig(config, layout, target)
    precompile(layout, incremental)

    return rv


################################################################################
# Package identity


_PUBLISHER_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def publisher_id(publisher):
    """
    Returns the package publisher ID for `publisher` (e.g. "CN=Developer"):
    the first 8 bytes of SHA-256 of its UTF-16LE form, plus a zero bit,
    as 13 base32 characters.
    """

    digest = hashlib.sha256(publisher.encode("utf-16-le")).digest()[:8]
    bits = int.from_bytes(digest, "big") << 1

    return "".join(_PUBLISHER_ID_ALPHABET[(bits >> (60 - 5 * i)) & 31] for i in range(13))


def package_identity(config):
    """
    Returns a dict with the package identifiers derived from `config`:
    name, version, publisher_id, aumid (for xbapp launch), full_name
    (xbapp uninstall/suspend/resume), and family_name.
    """

    try:
        root = ET.parse(config).getroot()
    except (OSError, ET.ParseError) as e:
        raise XboxBuildError("Could not read {}: {}".format(config, e))

    identity = xbox_config._child(root, "Identity")

    if identity is None:
        raise XboxBuildError("{} has no Identity element.".format(config))

    executables = xbox_config._child(root, "ExecutableList")
    exe = xbox_config._child(executables, "Executable") if executables is not None else None

    name = identity.get("Name", "")
    version = identity.get("Version") or "1.0.0.0"
    pub = publisher_id(identity.get("Publisher", ""))
    app = (exe.get("Id") if exe is not None else None) or "Game"

    return {
        "name" : name,
        "version" : version,
        "publisher_id" : pub,
        "aumid" : "{}_{}!{}".format(name, pub, app),
        "full_name" : "{}_{}_neutral__{}".format(name, version, pub),
        "family_name" : "{}_{}".format(name, pub),
    }


################################################################################
# GDK tools


def gameos_path(gdk):
    return os.path.join(gdk, "xbox", "redist", "GameOS.xvd")


def find_gdk(environ=None):
    """
    Returns the GDK edition directory (e.g. ...\\Microsoft GDK\\260402) that
    has the console GameOS, or None. Prefers GDK_DIR, then GameDKXboxLatest,
    then the newest installed edition.
    """

    if environ is None:
        environ = os.environ

    candidates = [ environ.get("GDK_DIR"), environ.get("GameDKXboxLatest") ]

    pf86 = environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    editions = glob.glob(os.path.join(pf86, "Microsoft GDK", "[0-9]*"))
    editions.sort(key=os.path.basename, reverse=True)
    candidates.extend(editions)

    for c in candidates:
        if c and os.path.isfile(gameos_path(c)):
            return os.path.abspath(c)

    return None


def gdk_edition(gdk):
    return os.path.basename(os.path.normpath(gdk)) if gdk else ""


def _tool(exe):
    rv = xbox_config.find_gdk_tool(exe)

    if rv is None:
        raise XboxBuildError("{} was not found. Install the Microsoft GDK with the Xbox extensions.".format(exe))

    return rv


def run(cmd, log=None):
    """
    Runs `cmd`, sending its output to the file `log` (or this process's
    output if None). Returns the exit code.
    """

    line = "\n> " + subprocess.list2cmdline(cmd) + "\n\n"

    if log is None:
        sys.stdout.write(line)
        sys.stdout.flush()
        return subprocess.call(cmd)

    log.write(line)
    log.flush()

    return subprocess.call(cmd, stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def pack(layout, package_dir, gdk, log=None, mapfile=None):
    """
    Runs makepkg genmap and pack on `layout`, writing the package to
    `package_dir`. Returns the path to the .xvc.
    """

    makepkg = _tool("makepkg.exe")
    gameos = gameos_path(gdk)

    if not os.path.isfile(gameos):
        raise XboxBuildError("{} was not found.".format(gameos))

    # A loose deploy copies GameOS.xvd into the layout; packages get it via /gameos.
    stale = os.path.join(layout, "GameOS.xvd")
    if os.path.exists(stale):
        os.unlink(stale)

    if mapfile is None:
        mapfile = os.path.join(os.path.dirname(os.path.abspath(layout)), "layout.xml")

    os.makedirs(package_dir, exist_ok=True)

    for old in glob.glob(os.path.join(package_dir, "*.xvc")):
        os.unlink(old)

    if run([ makepkg, "genmap", "/f", mapfile, "/d", layout ], log):
        raise XboxBuildError("makepkg genmap failed. See the log for details.")

    if run([ makepkg, "pack", "/v", "/f", mapfile, "/d", layout, "/pd", package_dir,
            "/gameos", gameos, "/skipvalidation", "/skipsymbolbundling" ], log):
        raise XboxBuildError("makepkg pack failed. See the log for details.")

    return find_package(package_dir)


def find_package(package_dir):
    xvcs = glob.glob(os.path.join(package_dir, "*.xvc"))

    if not xvcs:
        raise XboxBuildError("No .xvc package was found in {}.".format(package_dir))

    return max(xvcs, key=os.path.getmtime)


def validate(layout, package_dir, log=None):
    """
    Runs makepkg validate on `layout`. Returns the exit code.
    """

    makepkg = _tool("makepkg.exe")
    os.makedirs(package_dir, exist_ok=True)
    return run([ makepkg, "validate", "/d", layout, "/pd", package_dir ], log)


def _xbapp(args, log=None, console=None):
    cmd = [ _tool("xbapp.exe") ]

    if console:
        cmd.append("/x:" + console)

    return run(cmd + args, log)


def install(xvc, log=None, console=None):
    """
    Installs a package on the devkit (xbapp install).
    """

    if _xbapp([ "install", xvc ], log, console):
        raise XboxBuildError("xbapp install failed. Check that the devkit is connected (xbconnect) and see the log.")


def deploy(layout, gdk, log=None, console=None):
    """
    Deploys a loose layout to the devkit (xbapp deploy). Loose deploys need
    GameOS.xvd in the layout.
    """

    gameos = gameos_path(gdk)

    if not os.path.isfile(gameos):
        raise XboxBuildError("{} was not found.".format(gameos))

    dst = os.path.join(layout, "GameOS.xvd")

    if not _up_to_date(gameos, dst):
        shutil.copy2(gameos, dst)

    if _xbapp([ "deploy", layout ], log, console):
        raise XboxBuildError("xbapp deploy failed. Check that the devkit is connected (xbconnect) and see the log.")


def launch(aumid, log=None, console=None):
    """
    Launches an installed title on the devkit (xbapp launch).
    """

    if _xbapp([ "launch", aumid ], log, console):
        raise XboxBuildError("xbapp launch {} failed. See the log for details.".format(aumid))


def find_devkit(timeout=15):
    """
    Returns the name or address of the default devkit (set with xbconnect),
    or None if there isn't one or it doesn't answer within `timeout` seconds.
    """

    exe = xbox_config.find_gdk_tool("xbconnect.exe")

    if exe is None:
        return None

    try:
        p = subprocess.run([ exe, "/B" ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None

    lines = p.stdout.decode("utf-8", "replace").strip().splitlines()

    if p.returncode or not lines:
        return None

    return lines[-1].strip() or None


def full_validation(config, dlc, target, workdir, log=None):
    """
    Stages the DLC runtime and `config` (with its images) into
    `workdir`\\Loose, then runs makepkg validate. Returns makepkg's exit code.
    """

    loose = os.path.join(workdir, "Loose")

    if os.path.isdir(loose):
        shutil.rmtree(loose)

    rt = runtime_path(dlc, target)
    copy_tree(rt, loose, skip=("overrides",))

    for w in stage_gameconfig(config, loose, target):
        if log is not None:
            log.write("Warning: " + w + "\n")

    return validate(loose, os.path.join(workdir, "Package"), log)


################################################################################
# Command line (used by xbox-build's package_xbox.bat and quick_deploy.bat)


def _main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(prog="xbox_package.py", description="Stage, package and deploy Ren'Py games for Xbox.")
    sub = ap.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("stage", help="Stage a game into a loose layout.")
    sp.add_argument("--target", choices=sorted(xbox_config.GDK_PLATFORMS), default="scarlett")
    sp.add_argument("--dlc", required=True, help="The xbox DLC (or make_xbox_dlc.ps1 output) directory.")
    sp.add_argument("--layout", required=True, help="The loose layout directory to create.")
    sp.add_argument("--renpy", required=True, help="The Ren'Py checkout or SDK to take renpy\\ from.")
    sp.add_argument("--project", help="A project directory (with game\\ and xbox\\MicrosoftGame.config).")
    sp.add_argument("--game", help="A game directory, if --project isn't given.")
    sp.add_argument("--config", help="MicrosoftGame.config (or its folder). Defaults to the project's.")
    sp.add_argument("--incremental", action="store_true", help="Update an existing layout instead of recreating it.")
    sp.add_argument("--bootstrap", help="A script to use as renpy.py instead of the runtime's (e.g. a smoke test).")

    sp = sub.add_parser("pack", help="Package a loose layout into an .xvc.")
    sp.add_argument("--layout", required=True)
    sp.add_argument("--package-dir", required=True)
    sp.add_argument("--map", help="Where to write layout.xml.")

    sp = sub.add_parser("install", help="Install the newest .xvc in a directory on the devkit.")
    sp.add_argument("--package-dir", required=True)

    sp = sub.add_parser("deploy", help="Deploy a loose layout to the devkit.")
    sp.add_argument("--layout", required=True)

    sp = sub.add_parser("launch", help="Launch the title described by a MicrosoftGame.config.")
    sp.add_argument("--config", required=True)

    sp = sub.add_parser("id", help="Print a package identifier.")
    sp.add_argument("--config", required=True)
    sp.add_argument("--part", choices=[ "aumid", "full_name", "family_name", "publisher_id" ], default="aumid")

    sp = sub.add_parser("bundle", help="Make the xbox DLC release download from a distribute output directory.")
    sp.add_argument("--update-dir", required=True, help="The distribute output with updates.json, updates.ecdsa and rpu\\.")
    sp.add_argument("--out", required=True, help="The .zip to write.")

    args = ap.parse_args(argv)

    try:
        if args.command == "stage":
            config = args.config
            game = args.game

            if args.project:
                game = game or os.path.join(args.project, "game")
                config = config or xbox_config.config_path(args.project)

            if not game or not config:
                raise XboxBuildError("Give --project, or both --game and --config.")

            problems = check_runtime(args.dlc, args.target)
            if problems:
                raise XboxBuildError("\n".join(problems))

            if not args.incremental and os.path.isdir(args.layout):
                shutil.rmtree(args.layout)

            n = stage_renpy_source(args.renpy, args.layout, args.incremental)
            print("  renpy\\: {} files".format(n))

            n = stage_game(game, args.layout, args.incremental)
            print("  game\\: {} files from {}".format(n, game))

            for w in finish_layout(args.layout, args.dlc, args.target, config,
                    lambda : git_version_dict(args.renpy), args.incremental):
                print("  WARN: " + w)

            print("  runtime from {}".format(runtime_path(args.dlc, args.target)))
            print("  MicrosoftGame.config staged from {}".format(config))

            if not can_precompile():
                print("  .pyc files not compiled (needs Python 3.12; the console compiles them in memory)")

            if args.bootstrap:
                shutil.copyfile(args.bootstrap, os.path.join(args.layout, "renpy.py"))
                print("  renpy.py replaced with {}".format(args.bootstrap))

        elif args.command == "pack":
            gdk = find_gdk()
            if gdk is None:
                raise XboxBuildError("No GDK with Xbox extensions (xbox\\redist\\GameOS.xvd) was found.")

            xvc = pack(args.layout, args.package_dir, gdk, mapfile=args.map)
            print("\nPackage: {} ({:,} bytes)".format(xvc, os.path.getsize(xvc)))

        elif args.command == "install":
            install(find_package(args.package_dir))

        elif args.command == "deploy":
            gdk = find_gdk()
            if gdk is None:
                raise XboxBuildError("No GDK with Xbox extensions (xbox\\redist\\GameOS.xvd) was found.")

            deploy(args.layout, gdk)

        elif args.command == "launch":
            launch(package_identity(args.config)["aumid"])

        elif args.command == "id":
            print(package_identity(args.config)[args.part])

        elif args.command == "bundle":
            n = make_dlc_bundle(args.update_dir, args.out)
            print("{} ({} blocks, {:,} bytes)".format(args.out, n, os.path.getsize(args.out)))

    except XboxBuildError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(_main())
