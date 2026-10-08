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
Support code for the launcher's Xbox screen: creating a project's
MicrosoftGame.config from a template, validating it, and locating the GDK
tools (Game Config Editor, makepkg) and the xbox-build checkout.

This module doesn't depend on Ren'Py, so it can be tested with a plain
Python interpreter.
"""

import collections
import contextlib
import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib

import xml.etree.ElementTree as ET

CONFIG_DIR = "xbox"
CONFIG_NAME = "MicrosoftGame.config"
EXECUTABLE_NAME = "renpy_xbox.exe"
TARGET_FAMILIES = ("Scarlett", "XboxOne")
GDK_PLATFORMS = {
    "scarlett": "Gaming.Xbox.Scarlett.x64",
    "xboxone": "Gaming.Xbox.XboxOne.x64",
}

# ShellVisuals image attributes and the sizes Store ingestion expects.
IMAGE_ATTRIBUTES = collections.OrderedDict([
    ("StoreLogo", (100, 100)),
    ("Square480x480Logo", (480, 480)),
    ("Square150x150Logo", (150, 150)),
    ("Square44x44Logo", (44, 44)),
    ("SplashScreenImage", (1920, 1080)),
])

ERROR = "error"
WARNING = "warning"
INFO = "info"

Issue = collections.namedtuple("Issue", [ "level", "message", "line" ])

PLACEHOLDER_PUBLISHER = "CN=Developer"

_VERSION_PART = r"(0|[1-9][0-9]{0,4})"
VERSION_RE = re.compile(r"^" + _VERSION_PART + r"(\." + _VERSION_PART + r"){3}$")
IDENTITY_NAME_RE = re.compile(r"^[A-Za-z0-9.-]{3,50}$")
PUBLISHER_RE = re.compile(r"^[A-Za-z]+(\.[0-9.]+)?=\S")
TITLE_ID_RE = re.compile(r"^[0-9a-fA-F]{8}$")
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


################################################################################
# Paths


def config_path(project_path):
    """
    Returns the path to a project's MicrosoftGame.config.
    """

    return os.path.join(project_path, CONFIG_DIR, CONFIG_NAME)


def find_xbox_build(override=None, environ=None, renpy_base=None):
    """
    Returns the path of the xbox-build checkout, or None if it can't be found.
    Tries, in order: `override` (a Preferences setting), the
    RENPY_XBOX_BUILD environment variable, and the parent of `renpy_base`
    (the launcher's Ren'Py lives in xbox-build\\renpy in a dev checkout).
    """

    if environ is None:
        environ = os.environ

    candidates = [ override, environ.get("RENPY_XBOX_BUILD") ]

    if renpy_base:
        candidates.append(os.path.dirname(os.path.abspath(renpy_base)))

    for c in candidates:
        if c and is_xbox_build(c):
            return os.path.abspath(c)

    return None


def is_xbox_build(path):
    return (os.path.isfile(os.path.join(path, "package_xbox.bat"))
        and os.path.isfile(os.path.join(path, "launcher", CONFIG_NAME)))


def template_path(xbox_build, bundled):
    """
    Returns the template to create new configs from: xbox-build's
    launcher\\MicrosoftGame.config if available, else the copy bundled with
    the launcher.
    """

    if xbox_build:
        fn = os.path.join(xbox_build, "launcher", CONFIG_NAME)
        if os.path.isfile(fn):
            return fn

    return bundled


def _is_windows(platform):
    return platform.startswith("win")


def find_gdk_tool(exe, override=None, environ=None, platform=None):
    """
    Finds a tool shipped in the GDK's shared bin directory, returning its
    path or None. `override` may be the tool's path or its directory.
    """

    if environ is None:
        environ = os.environ

    if platform is None:
        platform = sys.platform

    if not _is_windows(platform):
        return None

    candidates = [ ]

    if override:
        if os.path.isdir(override):
            candidates.append(os.path.join(override, exe))
        else:
            candidates.append(override)

    gamedk = environ.get("GameDK")
    if gamedk:
        candidates.append(os.path.join(gamedk, "bin", exe))

    pf86 = environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    gdk_root = os.path.join(pf86, "Microsoft GDK")
    candidates.append(os.path.join(gdk_root, "bin", exe))

    # Per-edition copies, newest edition (highest number) first.
    editions = glob.glob(os.path.join(gdk_root, "*", "bin", exe))
    editions.sort(key=lambda p : os.path.basename(os.path.dirname(os.path.dirname(p))), reverse=True)
    candidates.extend(editions)

    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)

    found = shutil.which(exe, path=environ.get("PATH"))
    if found:
        return os.path.abspath(found)

    return None


def find_gameconfig_editor(override=None, environ=None, platform=None):
    return find_gdk_tool("GameConfigEditor.exe", override, environ, platform)


def find_makepkg(environ=None, platform=None):
    return find_gdk_tool("makepkg.exe", None, environ, platform)


################################################################################
# Template filling


def sanitize_identity_name(name, fallback="RenPyGame"):
    """
    Converts `name` into a valid Identity Name: [A-Za-z0-9.-], 3-50
    characters, no leading or trailing period.
    """

    rv = re.sub(r"[^A-Za-z0-9.-]", "", name or "")
    rv = re.sub(r"\.{2,}", ".", rv).strip(".")
    rv = rv[:50].rstrip(".")

    if len(rv) < 3:
        return fallback

    return rv


def normalize_version(version):
    """
    Converts a version like "1.2" or "v1.5b" to a four-part numeric version
    ("1.2.0.0", "1.5.0.0"). Versions without digits become "1.0.0.0".
    """

    parts = re.findall(r"\d+", str(version or ""))[:4]

    if not parts:
        return "1.0.0.0"

    parts = [ str(min(int(i), 65535)) for i in parts ]
    parts += [ "0" ] * (4 - len(parts))

    return ".".join(parts)


def _escape_attr(value):
    return (value.replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;"))


def set_attribute(text, element, attribute, value):
    """
    Sets `attribute` on the first `element` start tag in the XML `text`,
    preserving the rest of the file's formatting. Returns the new text.
    """

    tag_re = re.compile(r"<" + element + r"\b[^>]*>", re.S)
    m = tag_re.search(text)

    if m is None:
        raise ValueError("The template has no <{}> element.".format(element))

    tag = m.group(0)
    escaped = _escape_attr(value)
    attr_re = re.compile(r"(\s" + attribute + r"\s*=\s*)(\"[^\"]*\"|'[^']*')")

    if attr_re.search(tag):
        new_tag = attr_re.sub(lambda am : am.group(1) + '"' + escaped + '"', tag, count=1)
    else:
        end = len(tag) - (2 if tag.endswith("/>") else 1)
        new_tag = tag[:end].rstrip() + ' {}="{}"'.format(attribute, escaped) + (" " if tag.endswith("/>") else "") + tag[end:]

    return text[:m.start()] + new_tag + text[m.end():]


def fill_template(text, name, version, display_name):
    """
    Fills the project-specific fields of the template `text`.
    """

    name = sanitize_identity_name(name)
    version = normalize_version(version)
    display_name = (display_name or "").strip()[:256] or name

    text = set_attribute(text, "Identity", "Name", name)
    text = set_attribute(text, "Identity", "Version", version)
    text = set_attribute(text, "ShellVisuals", "DefaultDisplayName", display_name)
    text = set_attribute(text, "ShellVisuals", "Description", display_name)

    return text


def placeholder_png():
    """
    Returns the bytes of a 1x1 white PNG.
    """

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
        + chunk(b"IEND", b""))


def png_size(path):
    """
    Returns the (width, height) of the PNG at `path`, or None if it isn't a PNG.
    """

    with open(path, "rb") as f:
        header = f.read(24)

    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None

    return struct.unpack(">II", header[16:24])


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _children(element, name):
    return [ i for i in element if _local(i.tag) == name ]


def _child(element, name):
    rv = _children(element, name)
    return rv[0] if rv else None


def referenced_images(root):
    """
    Returns a list of (attribute, relative path) pairs for the images the
    config's ShellVisuals reference.
    """

    rv = [ ]

    for sv in _children(root, "ShellVisuals"):
        for attr, value in sv.attrib.items():
            if attr in IMAGE_ATTRIBUTES or value.lower().endswith(".png"):
                rv.append((attr, value))

    return rv


def image_path(config, relpath):
    return os.path.normpath(os.path.join(os.path.dirname(config), relpath.replace("/", os.sep)))


def ensure_images(config):
    """
    Writes a placeholder PNG for every image the config references that
    doesn't exist yet. Returns the list of files created.
    """

    root = ET.parse(config).getroot()
    created = [ ]

    for _attr, rel in referenced_images(root):
        fn = image_path(config, rel)

        if os.path.exists(fn):
            continue

        d = os.path.dirname(fn)
        if not os.path.isdir(d):
            os.makedirs(d)

        with open(fn, "wb") as f:
            f.write(placeholder_png())

        created.append(fn)

    return created


def create_config(config, template, name, version, display_name):
    """
    Creates `config` from `template`, filling in the project fields and
    creating placeholder images. An existing config is backed up to
    MicrosoftGame.config.bak first; existing images are kept.
    """

    with open(template, "r", encoding="utf-8-sig") as f:
        text = f.read()

    text = fill_template(text, name, version, display_name)

    # Fail before touching the destination if the result is malformed.
    ET.fromstring(text.encode("utf-8"))

    d = os.path.dirname(config)
    if not os.path.isdir(d):
        os.makedirs(d)

    if os.path.exists(config):
        shutil.copy2(config, config + ".bak")

    tmp = config + ".new"

    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    os.replace(tmp, config)

    ensure_images(config)

    return config


################################################################################
# Validation


class Report(object):
    """
    The result of validating a config.

    `issues`
        A list of Issue tuples.

    `info`
        A dict with the Identity name/version/publisher, display name,
        executable and target device family (when they could be read).
    """

    def __init__(self, path):
        self.path = path
        self.issues = [ ]
        self.info = { }
        self.mtime = None

    def add(self, level, message, line=None):
        self.issues.append(Issue(level, message, line))

    def count(self, level):
        return sum(1 for i in self.issues if i.level == level)

    @property
    def errors(self):
        return self.count(ERROR)

    @property
    def warnings(self):
        return self.count(WARNING)

    @property
    def error_line(self):
        for i in self.issues:
            if i.line is not None:
                return i.line

        return None

    def summary(self):
        if not self.issues:
            return "OK"

        parts = [ ]

        if self.errors:
            parts.append("{} error{}".format(self.errors, "" if self.errors == 1 else "s"))
        if self.warnings:
            parts.append("{} warning{}".format(self.warnings, "" if self.warnings == 1 else "s"))

        if not parts:
            return "OK"

        return ", ".join(parts)


def validate_config(path):
    """
    Validates the MicrosoftGame.config at `path`, returning a Report. This
    never changes the file.
    """

    rv = Report(path)

    if not os.path.isfile(path):
        rv.add(ERROR, "{} does not exist.".format(path))
        return rv

    rv.mtime = os.path.getmtime(path)

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        line = e.position[0] if getattr(e, "position", None) else None
        rv.add(ERROR, "Not well-formed XML: {}".format(e), line)
        return rv

    if _local(root.tag) != "Game":
        rv.add(ERROR, "The root element is <{}>; expected <Game>.".format(_local(root.tag)))
        return rv

    if root.get("configVersion") is None:
        rv.add(ERROR, "<Game> is missing the required configVersion attribute.")

    _validate_identity(root, rv)
    _validate_executables(root, rv)
    _validate_shell_visuals(root, rv)
    _validate_xbox_live(root, rv)

    return rv


def _validate_identity(root, rv):
    identity = _children(root, "Identity")

    if len(identity) != 1:
        rv.add(ERROR, "Expected exactly one <Identity> element, found {}.".format(len(identity)))
        if not identity:
            return

    identity = identity[0]

    name = identity.get("Name")
    publisher = identity.get("Publisher")
    version = identity.get("Version")

    rv.info["name"] = name
    rv.info["publisher"] = publisher
    rv.info["version"] = version

    if not name:
        rv.add(ERROR, "Identity Name is missing.")
    elif not IDENTITY_NAME_RE.match(name) or name.startswith(".") or name.endswith("."):
        rv.add(ERROR, "Identity Name '{}' must be 3-50 characters of A-Z, a-z, 0-9, '.' and '-'.".format(name))

    if not publisher:
        rv.add(ERROR, "Identity Publisher is missing.")
    elif not PUBLISHER_RE.match(publisher):
        rv.add(ERROR, "Identity Publisher '{}' isn't a distinguished name (for example CN=Studio).".format(publisher))
    elif publisher == PLACEHOLDER_PUBLISHER:
        rv.add(INFO, "Publisher is the template's CN=Developer; set it from Partner Center before submission.")

    if not version:
        rv.add(ERROR, "Identity Version is missing.")
    elif not VERSION_RE.match(version) or any(int(i) > 65535 for i in version.split(".")):
        rv.add(ERROR, "Identity Version '{}' must have four numeric parts, each 0-65535 (for example 1.0.0.0).".format(version))


def _validate_executables(root, rv):
    elist = _child(root, "ExecutableList")
    executables = _children(elist, "Executable") if elist is not None else [ ]

    if len(executables) != 1:
        rv.add(ERROR, "Expected exactly one <Executable> in <ExecutableList>, found {}.".format(len(executables)))
        if not executables:
            return

    e = executables[0]
    exe = e.get("Name")
    family = e.get("TargetDeviceFamily")

    rv.info["executable"] = exe
    rv.info["target"] = family
    rv.info["executable_id"] = e.get("Id")

    if not exe:
        rv.add(ERROR, "The Executable has no Name.")
    elif exe.lower() != EXECUTABLE_NAME:
        rv.add(WARNING, "Executable Name is '{}', but the Ren'Py launcher is staged as {}; the title won't start.".format(exe, EXECUTABLE_NAME))

    if family not in TARGET_FAMILIES:
        rv.add(ERROR, "Executable TargetDeviceFamily is '{}'; expected Scarlett or XboxOne.".format(family or ""))


def _validate_shell_visuals(root, rv):
    sv = _children(root, "ShellVisuals")

    if not sv:
        rv.add(ERROR, "<ShellVisuals> is missing; packaging requires the logo images.")
        return

    sv = sv[0]

    rv.info["display_name"] = sv.get("DefaultDisplayName")

    if not sv.get("DefaultDisplayName"):
        rv.add(WARNING, "ShellVisuals has no DefaultDisplayName.")

    for attr in IMAGE_ATTRIBUTES:
        if sv.get(attr) is None:
            rv.add(WARNING, "ShellVisuals has no {} image.".format(attr))

    for attr, rel in referenced_images(root):
        fn = image_path(rv.path, rel)

        if not rel.lower().endswith(".png"):
            rv.add(ERROR, "{} '{}' must be a .png file.".format(attr, rel))
            continue

        if not os.path.isfile(fn):
            rv.add(ERROR, "{} image '{}' is missing next to the config.".format(attr, rel))
            continue

        try:
            size = png_size(fn)
        except Exception:
            size = None

        if size is None:
            rv.add(ERROR, "{} image '{}' isn't a valid PNG.".format(attr, rel))
        elif size == (1, 1):
            rv.add(WARNING, "{} '{}' is a 1x1 placeholder; replace it with real art.".format(attr, rel))
        elif attr in IMAGE_ATTRIBUTES and size != IMAGE_ATTRIBUTES[attr]:
            rv.add(WARNING, "{} '{}' is {}x{}; expected {}x{}.".format(attr, rel, size[0], size[1], *IMAGE_ATTRIBUTES[attr]))


def _validate_xbox_live(root, rv):
    title_id = _child(root, "TitleId")
    msa_app_id = _child(root, "MSAAppId")
    sgs = _child(root, "SaveGameStorage")
    scid = _child(sgs, "SCID") if sgs is not None else None

    title_text = (title_id.text or "").strip() if title_id is not None else ""
    msa_text = (msa_app_id.text or "").strip() if msa_app_id is not None else ""
    if title_id is not None and not TITLE_ID_RE.match(title_text):
        rv.add(ERROR, "TitleId must be 8 hexadecimal digits.")

    if msa_app_id is not None and not msa_text:
        rv.add(ERROR, "MSAAppId must not be empty; use the value from Partner Center.")

    if scid is not None and not GUID_RE.match((scid.text or "").strip()):
        rv.add(ERROR, "SaveGameStorage SCID must be a GUID.")

    missing = [ n for n, e in (("TitleId", title_id), ("MSAAppId", msa_app_id)) if e is None ]

    if missing:
        rv.add(INFO, "Cloud saves disabled (needs Partner Center IDs: {}).".format(", ".join(missing)))

    if TITLE_ID_RE.match(title_text):
        derived = "00000000-0000-0000-0000-0000" + title_text.lower()
        rv.info["scid"] = derived
        rv.add(INFO, "The native Xbox launcher derives SCID {} from TitleId; Connected Storage must be enabled in Partner Center.".format(derived))
        if scid is not None and GUID_RE.match((scid.text or "").strip()) and (scid.text or "").strip().lower() != derived:
            rv.add(WARNING, "SaveGameStorage SCID differs from the TitleId-derived SCID used by the native Xbox launcher.")


################################################################################
# Packaging helpers


def loose_dir(xbox_build, target="scarlett"):
    return os.path.join(xbox_build, "output", GDK_PLATFORMS[target], "Layout", "Image", "Loose")


@contextlib.contextmanager
def _preserve_loose_config(loose, config):
    """
    Restores the Loose layout's MicrosoftGame.config and the images staging
    may touch (top-level PNGs and anything `config` references) on exit, so
    validating one project's config doesn't change what the next quick
    deploy sends (or its AUMID).
    """

    referenced = set()

    try:
        for _attr, rel in referenced_images(ET.parse(config).getroot()):
            referenced.add(os.path.normpath(rel.replace("/", os.sep)))
    except Exception:
        pass

    def snapshot_names():
        try:
            rv = set(n for n in os.listdir(loose) if n == "MicrosoftGame.config" or n.lower().endswith(".png"))
        except OSError:
            rv = set()

        return rv | set(n for n in referenced if os.path.isfile(os.path.join(loose, n)))

    saved = { }

    for n in snapshot_names():
        fn = os.path.join(loose, n)
        if os.path.isfile(fn):
            with open(fn, "rb") as f:
                saved[n] = f.read()

    try:
        yield
    finally:
        for n in snapshot_names() - set(saved):
            try:
                os.unlink(os.path.join(loose, n))
            except OSError:
                pass

        for n, data in saved.items():
            try:
                with open(os.path.join(loose, n), "wb") as f:
                    f.write(data)
            except OSError:
                pass


def full_validation(config, xbox_build, log_path, target="scarlett", makepkg=None):
    """
    Stages `config` (and its images) into xbox-build's existing Loose layout,
    then runs `makepkg validate`. The Loose layout's own config and logos are
    restored afterwards. Output goes to `log_path`. Returns the
    makepkg exit code (or the staging script's, if staging failed).

    Raises an Exception with a user-readable message if prerequisites are
    missing.
    """

    makepkg = makepkg or find_makepkg()
    if makepkg is None:
        raise Exception("makepkg.exe was not found. Install the Microsoft GDK.")

    loose = loose_dir(xbox_build, target)
    if not os.path.isfile(os.path.join(loose, EXECUTABLE_NAME)):
        raise Exception("No staged layout at {}. Run package_xbox.bat {} once first.".format(loose, target))

    stage = os.path.join(xbox_build, "scripts", "stage_gameconfig.ps1")
    if not os.path.isfile(stage):
        raise Exception("{} is missing.".format(stage))

    pkgdir = os.path.join(os.path.dirname(log_path), "xbox-validate")
    if os.path.isdir(pkgdir):
        shutil.rmtree(pkgdir, ignore_errors=True)
    os.makedirs(pkgdir)

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    with open(log_path, "w", encoding="utf-8", errors="replace") as log, _preserve_loose_config(loose, config):
        log.write("Staging {} into {}\n\n".format(config, loose))
        log.flush()

        rc = subprocess.call(
            [ "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", stage,
                "-Config", config, "-Loose", loose, "-Target", target ],
            stdout=log, stderr=subprocess.STDOUT, creationflags=flags)

        if rc:
            return rc

        log.write("\n> makepkg validate /d {} /pd {}\n\n".format(loose, pkgdir))
        log.flush()

        return subprocess.call(
            [ makepkg, "validate", "/d", loose, "/pd", pkgdir ],
            stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
