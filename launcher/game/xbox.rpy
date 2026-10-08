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

# This file contains the launcher's Xbox (Microsoft GDK) support: creating,
# editing and validating a project's xbox\MicrosoftGame.config. The logic
# that doesn't need Ren'Py lives in xbox_config.py.

# An explicit path to GameConfigEditor.exe (or its folder), or None to auto-detect.
default persistent.xbox_gameconfig_editor = None

# Which editor "Edit Game Config" uses: "auto" (GUI if found, else text), "gui" or "text".
default persistent.xbox_config_editor = "auto"

# The xbox-build checkout, or None to use RENPY_XBOX_BUILD / auto-detection.
default persistent.xbox_build_dir = None

init python:

    import subprocess
    import xbox_config

    XBOX_TEMPLATE = os.path.join(config.gamedir, "xbox", "MicrosoftGame.config.template")
    # The validation issue list scrolls past this many issues / this height,
    # so the right column never runs into the bottom bar.
    XBOX_ISSUES_SCROLL = 3
    XBOX_ISSUES_HEIGHT = 170
    XBOX_ISSUES_ADJUSTMENT = ui.adjustment()

    class XboxState(object):
        """
        Session caches for the Xbox screen. Not persisted.
        """

        editor_key = ()
        editor = None

        report_key = None
        report = None

    xbox_state = XboxState()

    def find_gameconfig_editor(refresh=False):
        """
        Returns the path to GameConfigEditor.exe, or None. Cached for the
        session (and re-detected when the Preferences override changes).
        """

        key = persistent.xbox_gameconfig_editor

        if refresh or xbox_state.editor_key != (key,):
            xbox_state.editor_key = (key,)
            xbox_state.editor = xbox_config.find_gameconfig_editor(key)

        return xbox_state.editor

    def xbox_build_dir():
        return xbox_config.find_xbox_build(persistent.xbox_build_dir, renpy_base=config.renpy_base)

    def xbox_template():
        return xbox_config.template_path(xbox_build_dir(), XBOX_TEMPLATE)

    def xbox_config_path(p=None):
        p = p or project.current
        return xbox_config.config_path(p.path)

    def xbox_editor_mode():
        """
        Returns "gui" or "text": what the primary Edit Game Config button does.
        """

        pref = persistent.xbox_config_editor

        if pref == "text":
            return "text"

        if pref == "gui":
            return "gui"

        return "gui" if find_gameconfig_editor() else "text"

    def xbox_files_key(path):
        """
        A key that changes whenever the config or anything next to it
        (logo images) is added, removed or modified.
        """

        d = os.path.dirname(path)

        try:
            names = os.listdir(d)
        except OSError:
            return (path, None)

        rv = [ ]

        for n in sorted(names):
            try:
                rv.append((n, os.path.getmtime(os.path.join(d, n))))
            except OSError:
                pass

        return (path, tuple(rv))

    def xbox_report(force=False):
        """
        Returns the validation Report for the current project's config, or
        None if it doesn't exist yet. Revalidates when the files change.
        """

        path = xbox_config_path()

        if not os.path.exists(path):
            xbox_state.report_key = None
            xbox_state.report = None
            return None

        key = xbox_files_key(path)

        if force or key != xbox_state.report_key:
            xbox_state.report_key = key
            xbox_state.report = xbox_config.validate_config(path)

        return xbox_state.report

    def xbox_poll():
        """
        Called by a timer on the Xbox screen, so edits saved in the Game
        Config Editor or the text editor show up when the user returns.
        """

        path = xbox_config_path()
        key = xbox_files_key(path) if os.path.exists(path) else None

        if key != xbox_state.report_key:
            renpy.restart_interaction()

    def xbox_project_fields(p):
        """
        Returns (identity name, version, display name) for the template, from
        the project's build.name, config.version and config.name.
        """

        p.update_dump(True, gui=True)

        dump = p.dump or { }
        build = dump.get("build") or { }

        name = build.get("executable_name") or p.name
        version = dump.get("version") or (build.get("info") or { }).get("version") or ""
        display_name = dump.get("name") or build.get("display_name") or p.display_name or p.name

        return name, version, display_name

    def xbox_create_config(overwrite=False):
        """
        Creates the current project's config from the template (backing up
        any existing one when `overwrite` is true). Returns the path.
        """

        p = project.current
        path = xbox_config_path(p)

        if os.path.exists(path) and not overwrite:
            return path

        name, version, display_name = xbox_project_fields(p)
        template = xbox_template()

        with interface.error_handling(_("creating MicrosoftGame.config"), label="xbox"):
            xbox_config.create_config(path, template, name, version, display_name)

        xbox_state.report_key = None

        return path

    def xbox_open_gui(path):
        """
        Opens `path` in the Game Config Editor without waiting for it.
        """

        exe = find_gameconfig_editor()

        if exe is None:
            interface.error(
                _("The Game Config Editor was not found. Install the Microsoft GDK, or set the path to GameConfigEditor.exe in Preferences > Xbox."),
                label="xbox")

        try:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([ exe, path ], cwd=os.path.dirname(path), creationflags=flags, close_fds=True)
        except OSError as e:
            if interface.yesno(
                    _("The Game Config Editor could not be started.\n\nTried: [exe!q]\n[exception!q]\n\nOpen the config in your text editor instead?"),
                    exe=exe,
                    exception=str(e)):
                renpy.jump("xbox_edit_text")

            renpy.jump("xbox")

        renpy.notify(_("Opened in Game Config Editor. Save there, then press Validate."))

    def xbox_choose_editor_path():
        try:
            import renpy.tfd as tfd
        except ImportError:
            tfd = None

        if tfd is None:
            interface.error(_("File dialogs are not available on this platform."), label="preferences")

        default = persistent.xbox_gameconfig_editor or find_gameconfig_editor() or ""
        rv = tfd.openFileDialog(__("Select GameConfigEditor.exe"), default, [ "*.exe" ], "GameConfigEditor.exe")

        if not rv:
            return

        rv = renpy.fsdecode(rv)

        if not os.path.isfile(rv):
            interface.error(_("The selected file does not exist."), label="preferences")

        persistent.xbox_gameconfig_editor = rv
        find_gameconfig_editor(refresh=True)

    def xbox_choose_build_dir():
        try:
            import renpy.tfd as tfd
        except ImportError:
            tfd = None

        if tfd is None:
            interface.error(_("File dialogs are not available on this platform."), label="preferences")

        rv = tfd.selectFolderDialog(__("Select the xbox-build folder"), persistent.xbox_build_dir or xbox_build_dir() or "")

        if not rv:
            return

        rv = renpy.fsdecode(rv)

        if not xbox_config.is_xbox_build(rv):
            interface.error(_("[path!q] is not an xbox-build folder (it needs package_xbox.bat and launcher\\MicrosoftGame.config)."), path=rv, label="preferences")

        persistent.xbox_build_dir = rv


screen xbox():

    $ path = xbox_config_path()
    $ exists = os.path.exists(path)
    $ report = xbox_report()
    $ gui_exe = find_gameconfig_editor()
    $ mode = xbox_editor_mode()
    $ prefer_gui = persistent.xbox_config_editor != "text"

    timer 1.0 repeat True action Function(xbox_poll, _update_screens=False)

    frame:
        style_group "l"
        style "l_root"

        window:

            has vbox

            label _("Xbox: [project.current.display_name!q]")

            add HALF_SPACER

            hbox:

                # Left side: the config and its editors.
                frame:
                    style "l_indent"
                    xmaximum ONEHALF
                    xfill True

                    has vbox

                    add SEPARATOR2
                    add HALF_SPACER

                    frame:
                        style "l_indent"
                        has vbox

                        text _("Game Config:")

                        add HALF_SPACER

                        frame style "l_indent":
                            has vbox

                            if exists:
                                text "[path!q]" style "l_small_text"

                                if report is not None and report.info:
                                    $ ident_name = report.info.get("name") or "?"
                                    $ ident_version = report.info.get("version") or "?"
                                    $ target = report.info.get("target") or "?"
                                    text _("Identity: [ident_name!q] [ident_version!q], target [target!q]") style "l_small_text"
                            else:
                                text _("Not created yet. Editing creates it from the template, filled in from the project.") style "l_small_text"

                        add HALF_SPACER

                        frame style "l_indent":
                            has vbox

                            textbutton _("Edit Game Config") action Jump("xbox_edit")

                            if mode == "gui":
                                textbutton _("Edit in Text Editor") action Jump("xbox_edit_text")
                            else:
                                textbutton _("Edit with Game Config Editor") action (Jump("xbox_edit_gui") if gui_exe else None)

                                if not gui_exe:
                                    text _("GDK not found — install the Microsoft GDK or set the path in Preferences.") style "l_small_text"

                            textbutton _("Validate") action Jump("xbox_validate")
                            textbutton _("Open Folder") action (OpenDirectory(os.path.dirname(path), absolute=True) if exists else None)
                            textbutton _("Reset from Template") action Jump("xbox_reset")

                            add HALF_SPACER

                            textbutton _("Prefer Game Config Editor"):
                                style "l_checkbox"
                                action SetField(persistent, "xbox_config_editor", "text" if prefer_gui else "auto")
                                selected prefer_gui

                            if mode == "text" and persistent.editor == "Visual Studio Code":
                                add HALF_SPACER
                                text _("Tip: the Red Hat XML extension (redhat.vscode-xml) flags XML mistakes as you type.") style "l_small_text"

                # Right side: validation, then build and deploy.
                frame:
                    style "l_indent"
                    xmaximum ONEHALF
                    xfill True

                    has vbox

                    add SEPARATOR2
                    add HALF_SPACER

                    frame:
                        style "l_indent"
                        has vbox

                        if report is None:
                            text _("Validation: no config yet.")
                        else:
                            $ summary = report.summary()
                            text _("Validation: [summary!q]")

                            add HALF_SPACER

                            frame style "l_indent":
                                has vbox

                                if len(report.issues) > XBOX_ISSUES_SCROLL:
                                    side "c r":
                                        ymaximum XBOX_ISSUES_HEIGHT

                                        viewport:
                                            yadjustment XBOX_ISSUES_ADJUSTMENT
                                            mousewheel True
                                            use xbox_issues(report.issues)

                                        vbar:
                                            style "l_vscrollbar"
                                            adjustment XBOX_ISSUES_ADJUSTMENT
                                else:
                                    use xbox_issues(report.issues)

                                if report.error_line is not None:
                                    $ error_line = report.error_line
                                    textbutton _("Open at line [error_line]") action Jump("xbox_edit_text_line")

                                textbutton _("Full Validation (makepkg)") action Jump("xbox_full_validate")

                    add SPACER
                    add SEPARATOR2
                    add HALF_SPACER

                    frame:
                        style "l_indent"
                        has vbox

                        text _("Build & Deploy:")

                        add HALF_SPACER

                        frame style "l_indent":
                            has vbox

                            text _("Coming later. For now, run xbox-build\\package_xbox.bat with GAMECONFIG set to this config.") style "l_small_text"

    textbutton _("Return") action Jump("front_page") style "l_left_button"


screen xbox_issues(issues):

    vbox:
        for issue in issues:
            if issue.level == "error":
                text _("Error: [issue.message!q]") style "l_small_text" color ERROR_COLOR
            elif issue.level == "warning":
                text _("Warning: [issue.message!q]") style "l_small_text" color INTERACTION_COLOR
            else:
                text _("Note: [issue.message!q]") style "l_small_text"

screen xbox_preferences():

    $ gui_exe = find_gameconfig_editor()
    $ build_dir = xbox_build_dir()

    frame:
        style "l_indent"
        has vbox

        text _("Game Config Editor:")

        add HALF_SPACER

        frame style "l_indent":
            has vbox

            if persistent.xbox_gameconfig_editor:
                textbutton "[persistent.xbox_gameconfig_editor!q]" action Jump("xbox_editor_path_preference")
                textbutton _("Auto-detect instead") action [ SetField(persistent, "xbox_gameconfig_editor", None), Function(find_gameconfig_editor, True) ]

                if not gui_exe:
                    text _("That file no longer exists.") style "l_small_text" color ERROR_COLOR

            else:
                textbutton _("Auto-detect") action Jump("xbox_editor_path_preference")

                if gui_exe:
                    text _("Found: [gui_exe!q]") style "l_small_text"
                else:
                    text _("Not found. Install the Microsoft GDK, or click to choose GameConfigEditor.exe.") style "l_small_text"

    add SPACER
    add SEPARATOR2

    frame:
        style "l_indent"
        has vbox

        text _("Edit Game Config opens:")

        add HALF_SPACER

        textbutton _("Game Config Editor if installed, else the text editor") style "l_checkbox" action SetField(persistent, "xbox_config_editor", "auto")
        textbutton _("Always the Game Config Editor") style "l_checkbox" action SetField(persistent, "xbox_config_editor", "gui")
        textbutton _("Always the text editor") style "l_checkbox" action SetField(persistent, "xbox_config_editor", "text")

    add SPACER
    add SEPARATOR2

    frame:
        style "l_indent"
        has vbox

        text _("xbox-build Folder:")

        add HALF_SPACER

        frame style "l_indent":
            has vbox

            if persistent.xbox_build_dir:
                textbutton "[persistent.xbox_build_dir!q]" action Jump("xbox_build_dir_preference")
                textbutton _("Auto-detect instead") action SetField(persistent, "xbox_build_dir", None)

                if build_dir is None:
                    text _("That folder is no longer an xbox-build checkout.") style "l_small_text" color ERROR_COLOR

            else:
                textbutton _("Auto-detect") action Jump("xbox_build_dir_preference")

                if build_dir:
                    text _("Found: [build_dir!q]") style "l_small_text"
                else:
                    text _("Not found (set RENPY_XBOX_BUILD or click to choose). New configs use the launcher's bundled template.") style "l_small_text"


label xbox:

    if not renpy.windows:
        jump front_page

    call screen xbox
    jump front_page

label xbox_edit:

    if xbox_editor_mode() == "gui":
        jump xbox_edit_gui
    else:
        jump xbox_edit_text

label xbox_edit_gui:

    python hide:
        path = xbox_create_config()
        xbox_open_gui(path)

    jump xbox

label xbox_edit_text:

    python hide:
        path = xbox_create_config()
        renpy.run(editor.EditAbsolute(path))

    jump xbox

label xbox_edit_text_line:

    python hide:
        report = xbox_report(force=True)
        line = report.error_line if report is not None else None
        renpy.run(editor.EditAbsolute(xbox_config_path(), line=line))

    jump xbox

label xbox_validate:

    python hide:
        if not os.path.exists(xbox_config_path()):
            interface.error(_("There is no MicrosoftGame.config yet. Use Edit Game Config to create one."), label="xbox")

        report = xbox_report(force=True)
        renpy.notify(__("Validation: ") + report.summary())

    jump xbox

label xbox_reset:

    python hide:
        path = xbox_config_path()

        if os.path.exists(path):
            interface.yesno(_("Replace MicrosoftGame.config with a fresh copy of the template? The current file is saved as MicrosoftGame.config.bak, and existing images are kept."), no=Jump("xbox"))

        xbox_create_config(overwrite=True)
        renpy.notify(__("MicrosoftGame.config was recreated from the template."))

    jump xbox

label xbox_full_validate:

    python hide:
        path = xbox_config_path()
        build_dir = xbox_build_dir()

        if not os.path.exists(path):
            interface.error(_("There is no MicrosoftGame.config yet. Use Edit Game Config to create one."), label="xbox")

        if build_dir is None:
            interface.error(_("Full validation stages the config into an xbox-build layout. Set the xbox-build folder in Preferences > Xbox, or set RENPY_XBOX_BUILD."), label="xbox")

        report = xbox_report(force=True)

        if report.errors:
            interface.error(_("Fix the errors listed under Validation first."), label="xbox")

        log = project.current.temp_filename("xbox_validate.txt")

        interface.processing(_("Staging the config and running makepkg validate. This can take a minute..."))

        try:
            rc = xbox_config.full_validation(path, build_dir, log)
        except Exception as e:
            interface.error(_("Full validation could not run."), _("[exception!q]"), exception=str(e), label="xbox")

        if rc == 0:
            message = _("makepkg validate passed. The log is at:\n[log!q]\n\nOpen the log?")
        else:
            message = _("makepkg validate reported problems (exit code [rc]). The log is at:\n[log!q]\n\nOpen the log?")

        if interface.yesno(message, log=log, rc=rc):
            renpy.run(editor.EditAbsolute(log))

    jump xbox

label xbox_editor_path_preference:
    $ xbox_choose_editor_path()
    jump preferences

label xbox_build_dir_preference:
    $ xbox_choose_build_dir()
    jump preferences
