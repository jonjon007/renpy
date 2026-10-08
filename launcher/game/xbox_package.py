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

import io
import json
import os

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
