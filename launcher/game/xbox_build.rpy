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

# The Xbox screen's Build & Deploy section: builds a project into an Xbox
# package (.xvc) with the xbox DLC and the Microsoft GDK, and optionally
# installs and launches it on a devkit. The staging and GDK tool logic lives
# in xbox_package.py.

init python:

    import shutil
    import stat
    import threading
    import time
    import traceback

    XBOX_DOCS = "https://learn.microsoft.com/en-us/gaming/gdk/"
    XBOX_DOCS_SETUP = XBOX_DOCS + "docs/gdk-dev/get-started/dev-pc-setup"
    XBOX_DOCS_DEVKIT = XBOX_DOCS + "docs/gdk-dev/console-dev/dev-kits/setup/setting-up-your-devkit"

    XBOX_TARGETS = [
        ("scarlett", _("Xbox Series X|S")),
        ("xboxone", _("Xbox One (untested on hardware)")),
    ]

    xbox_gdk_cache = [ ]

    def xbox_gdk(refresh=False):
        """
        Returns the GDK edition directory with the Xbox extensions, or None.
        Cached until refresh is True.
        """

        if refresh or not xbox_gdk_cache:
            xbox_gdk_cache[:] = [ xbox_package.find_gdk() ]

        return xbox_gdk_cache[0]

    def xbox_target():
        if persistent.xbox_target not in xbox_config.GDK_PLATFORMS:
            persistent.xbox_target = "scarlett"

        return persistent.xbox_target

    def xbox_output_dir(p=None, target=None):
        """
        Returns <project>-dists\\xbox\\<platform> for the current target, or
        None if the project hasn't been scanned yet.
        """

        p = p or project.current
        build = p.dump.get("build") if p.dump else None

        if not build or not build.get("destination"):
            return None

        platform = xbox_config.GDK_PLATFORMS[target or xbox_target()]
        return os.path.join(p.parent_path, build["destination"], "xbox", platform)

    def xbox_rmtree(path):
        """
        Removes `path`, including read-only files.
        """

        def onerror(func, p, exc_info):
            os.chmod(p, stat.S_IWRITE)
            func(p)

        if os.path.isdir(path):
            shutil.rmtree(path, onerror=onerror)

    def xbox_background(message, f, **kwargs):
        """
        Runs `f` in a thread while showing `message`, and returns its result.
        Exceptions raised by `f` are re-raised here.
        """

        result = { }

        def run():
            try:
                result["value"] = f()
            except Exception as e:
                result["error"] = e
                result["traceback"] = traceback.format_exc()

        t = threading.Thread(target=run)
        t.daemon = True
        t.start()

        try:
            interface.processing(message, show_screen=True, **kwargs)

            while t.is_alive():
                renpy.pause(0)
                t.join(0.25)

        finally:
            interface.hide_screen()

        if "error" in result:
            result["error"].xbox_traceback = result["traceback"]
            raise result["error"]

        return result.get("value")

    def xbox_check_build():
        """
        Checks that the current project can be built for the current target,
        showing an error (which jumps back to the Xbox screen) if not. Returns
        (dlc, dev, gdk).
        """

        target = xbox_target()
        dlc, dev = xbox_dlc()

        if dlc is None:
            interface.yesno(_("Building for Xbox needs Xbox support (the xbox DLC), which isn't installed. Install it now?"),
                yes=Jump("xbox_install_dlc"), no=Jump("xbox"))

        problems = xbox_package.check_dlc(dlc, target, None if dev else renpy.version_only)

        if problems:
            interface.error(_("Xbox support can't build this target:"), _("[problems!q]"), problems="\n".join(problems), label="xbox")

        if not os.path.exists(xbox_config_path()):
            interface.error(_("There is no MicrosoftGame.config yet. Use Edit Game Config to create one."), label="xbox")

        if xbox_report(force=True).errors:
            interface.error(_("Fix the errors listed under Validation first."), label="xbox")

        gdk = xbox_gdk(refresh=True)

        if gdk is None:
            interface.error(_("The Microsoft GDK with the Xbox extensions was not found."),
                _("Install it as described in the GDK documentation (see the link on the Xbox screen), then try again."), label="xbox")

        return dlc, dev, gdk

    def xbox_build(install):
        """
        Builds the current project for the current target. If `install` is
        true, installs the package on the default devkit and launches it.
        """

        p = project.current
        target = xbox_target()
        dlc, dev, gdk = xbox_check_build()

        p.update_dump(force=True, gui=True, compile=True)

        if p.dump.get("error", False):
            interface.error(_("Ren'Py could not scan the project. Make sure it runs (Launch Project), then try again."), label="xbox")

        build = p.dump["build"]

        if "xbox" not in [ i["name"] for i in build["packages"] ]:
            interface.error(_("This project has no xbox package."),
                _("Projects use the xbox package defined by Ren'Py. If options.rpy redefines build.packages, add build.package(\"xbox\", \"directory\", \"xbox renpy all\", hidden=True, update=False, dlc=True)."),
                label="xbox")

        out = xbox_output_dir(p, target)
        loose = os.path.join(out, "Loose")
        package_dir = os.path.join(out, "Package")
        log_path = os.path.join(out, "build.log")

        with interface.error_handling(_("clearing the previous build")):
            xbox_rmtree(loose)
            os.makedirs(out, exist_ok=True)

        info = xbox_package.read_info(dlc) or { }

        log = open(log_path, "w", encoding="utf-8")
        log.write("Ren'Py {} Xbox build of {}\n".format(renpy.version_only, p.path))
        log.write("Target: {} ({})\n".format(target, xbox_config.GDK_PLATFORMS[target]))
        log.write("Xbox DLC: {} (built for Ren'Py {}, GDK {}){}\n".format(dlc, info.get("renpy_version", "?"), info.get("gdk", "?"), " [dev]" if dev else ""))
        log.write("GDK: {}\n".format(gdk))
        log.write("Output: {}\n\n".format(out))
        log.flush()

        start = time.time()

        try:
            distribute.Distributor(p, packages=[ "xbox" ], packagedest=loose, reporter=distribute.GuiReporter(),
                build_update=False, scan=False, report_success=False)
        except BaseException:
            log.close()
            raise

        if not os.path.isdir(os.path.join(loose, "renpy")):
            log.write("The Distributor did not produce {}.\n".format(os.path.join(loose, "renpy")))
            log.close()
            interface.error(_("Building the game files failed."), _("See [log!q] for details."),
                log=p.temp_filename("distribute.txt"), label="xbox")

        # The Distributor adds the PC launcher script; the console starts from the DLC's renpy.py.
        script = os.path.join(loose, build["executable_name"] + ".py")
        if os.path.exists(script):
            os.unlink(script)

        log.write("Game files: {:.0f}s\n".format(time.time() - start))
        log.flush()

        failure = None
        devkit = None
        xvc = None

        try:
            warnings = xbox_background(_("Adding the Xbox runtime and compiling Python..."),
                lambda : xbox_package.finish_layout(loose, dlc, target, xbox_config_path(p), renpy.version_dict))

            for w in warnings:
                log.write("Warning: {}\n".format(w))

            xvc = xbox_background(_("Packaging with makepkg. This can take a few minutes..."),
                lambda : xbox_package.pack(loose, package_dir, gdk, log, mapfile=os.path.join(out, "layout.xml")))

            log.write("\nPackage: {} ({:.0f}s)\n".format(xvc, time.time() - start))
            log.flush()

            if install:
                devkit = xbox_background(_("Looking for the default devkit..."), xbox_package.find_devkit)

                if devkit is None:
                    raise xbox_package.XboxBuildError(__("No devkit answered. Set the default devkit with xbconnect and make sure it's on and reachable."))

                log.write("\nDevkit: {}\n".format(devkit))

                aumid = xbox_package.package_identity(os.path.join(loose, "MicrosoftGame.config"))["aumid"]

                xbox_background(_("Installing on [devkit!q]. This can take a few minutes..."),
                    lambda : xbox_package.install(xvc, log), devkit=devkit)

                xbox_background(_("Launching on [devkit!q]..."),
                    lambda : xbox_package.launch(aumid, log), devkit=devkit)

                log.write("\nInstalled and launched ({:.0f}s)\n".format(time.time() - start))

        except xbox_package.XboxBuildError as e:
            failure = str(e)
            log.write("\nError: {}\n".format(failure))

        except Exception as e:
            failure = str(e) or type(e).__name__
            log.write("\n" + (getattr(e, "xbox_traceback", None) or traceback.format_exc()))

        finally:
            log.close()

        if failure is not None:
            if interface.yesno(_("The Xbox build failed:\n\n[failure!q]\n\nOpen the build log?"), failure=failure):
                renpy.run(editor.EditAbsolute(log_path))

            return

        if install:
            renpy.notify(__("Launched on the devkit."))
        elif interface.yesno(_("Built [name!q]. Open the output folder?"), name=os.path.basename(xvc)):
            renpy.run(OpenDirectory(package_dir, absolute=True))


screen xbox_build_frame():

    $ dlc, dev = xbox_dlc()
    $ gdk = xbox_gdk()
    $ out = xbox_output_dir()
    $ log_path = os.path.join(out, "build.log") if out else None

    frame:
        style "l_indent"
        has vbox

        text _("Build & Deploy:")

        add HALF_SPACER

        frame style "l_indent":
            has vbox

            if dlc is None:
                text _("Xbox support (the xbox DLC) isn't installed.") style "l_small_text"
                textbutton _("Install Xbox Support...") action Jump("xbox_install_dlc")

            else:
                for t, name in XBOX_TARGETS:
                    textbutton name style "l_checkbox" action SetField(persistent, "xbox_target", t)

                add HALF_SPACER

                textbutton _("Build Package (.xvc)") action (Jump("xbox_build") if gdk else None)
                textbutton _("Build, Install and Launch on Devkit") action (Jump("xbox_build_install") if gdk else None)

                hbox:
                    spacing 15

                    if out and os.path.isdir(out):
                        textbutton _("Open Output Folder") action OpenDirectory(out, absolute=True)

                    if log_path and os.path.exists(log_path):
                        textbutton _("Open Build Log") action editor.EditAbsolute(log_path)

                add HALF_SPACER

                hbox:
                    spacing 10

                    if gdk:
                        $ edition = xbox_package.gdk_edition(gdk)
                        text _("GDK [edition!q].") style "l_small_text" yalign 0.5
                        textbutton _("Devkit setup (GDK docs)") style "l_small_button" action OpenURL(XBOX_DOCS_DEVKIT)
                    else:
                        text _("Microsoft GDK (Xbox) not found.") style "l_small_text" color ERROR_COLOR yalign 0.5
                        textbutton _("GDK setup (GDK docs)") style "l_small_button" action OpenURL(XBOX_DOCS_SETUP)

                if dev:
                    text _("Dev mode: using xbox-build's dist\\xbox DLC.") style "l_small_text"


label xbox_build:
    $ xbox_build(False)
    jump xbox

label xbox_build_install:
    $ xbox_build(True)
    jump xbox

label xbox_install_dlc:
    $ interface.info(_("Installing Xbox support from the launcher isn't available yet. Extract the xbox DLC into the Ren'Py SDK's xbox folder."))
    jump xbox
