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

# Microsoft GDK services, the same on Windows (build.windows_gdk) and Xbox; safe when GDK is unavailable.

init -1500 python in gdk:
    # Do not participate in saves.
    _constant = True

    from store import renpy, config

    def _module():
        return renpy.gdk.module

    def available():
        """
        True if the GDK runtime is initialized.
        """

        return _module() is not None

    def xbl_available():
        """
        True if Xbox services (achievements) are available.
        """

        m = _module()
        return m is not None and m.xbl_available()

    def platform():
        """
        "pc" or "xbox" when GDK is available, otherwise None.
        """

        m = _module()
        return m.platform if m is not None else None

    def error():
        """
        Why GDK is unavailable, or None.
        """

        return renpy.gdk.error

    def title_id():
        m = _module()
        return m.title_id() if m is not None else None

    def scid():
        m = _module()
        return m.scid() if m is not None else None

    def sign_in(allow_ui=True):
        """
        Starts signing in the default user (silently on Xbox). Returns "none",
        "pending", "signed_in" or "failed".
        """

        m = _module()

        if m is None:
            return "none"

        try:
            return m.sign_in(allow_ui=allow_ui)
        except Exception:
            return "failed"

    def signed_in():
        m = _module()
        return m is not None and m.signed_in()

    def user_id():
        """
        The signed-in user's XUID, or None.
        """

        m = _module()
        return m.user_id() if m is not None else None

    def gamertag():
        """
        The signed-in user's gamertag, or None.
        """

        m = _module()
        return m.gamertag() if m is not None else None

    def unlock_achievement(id, percent=100):
        """
        Sets Xbox achievement `id` to `percent`; True if sent. Prefer achievement.grant.
        """

        m = _module()

        if m is None:
            return False

        try:
            return m.unlock_achievement(str(id), int(percent))
        except Exception:
            return False

    def task_queue_update():
        """
        Does nothing; GDK work runs on its own threads.
        """

    def save_folder():
        """
        The cloud save folder (PC with config.gdk_saves, or Xbox), or None.
        """

        if renpy.gdk.is_xbox():
            import os
            return os.environ.get("RENPY_XBOX_SAVES") or None

        return renpy.gdk.save_root

    def save_quota():
        """
        The free cloud save space in bytes, or None.
        """

        try:
            if renpy.gdk.is_xbox():
                import _xbox
                return _xbox.save_quota()

            if renpy.gdk.save_root is not None:
                return _module().save_quota()
        except Exception:
            pass

        return None

    def last_error():
        """
        The last failing GDK HRESULT, as an unsigned integer.
        """

        m = _module()
        return m.last_error() if m is not None else 0

    def _save_notice():
        renpy.notify(renpy.translate_string("Cloud saves are unavailable. Saves are stored on this PC."))


init 1500 python in gdk:
    if renpy.gdk.save_notice and not renpy.gdk.is_xbox():
        config.display_start_callbacks.append(_save_notice)


init -1499 python in achievement:

    from store import gdk

    class GdkBackend(Backend):
        """
        Sends achievements to Xbox services by their `xbox` id (default: name), including offline unlocks.
        """

        def __init__(self):
            # Achievement name -> Xbox id.
            self.ids = { }

            self.stat_max = { }

            # The user whose achievements were last synchronized.
            self.synced_user = None

        def register(self, name, xbox=None, stat_max=None, **kwargs):
            if xbox is not None:
                self.ids[name] = str(xbox)

            if stat_max:
                self.stat_max[name] = stat_max

        def confirmed(self):
            """
            Names the service confirmed for the signed-in user, or None.
            """

            user = gdk.user_id()

            if user is None:
                return None

            if persistent._gdk_achievements is None:
                persistent._gdk_achievements = _dict()

            return persistent._gdk_achievements.setdefault(str(user), _set())

        def send(self, name, percent):
            if not gdk.signed_in():
                return

            gdk.unlock_achievement(self.ids.get(name, name), percent)

        def grant(self, name):
            self.send(name, 100)

        def progress(self, name, completed):
            stat_max = self.stat_max.get(name)

            if not stat_max:
                return

            self.send(name, max(0, min(100, int(completed * 100 // stat_max))))

        def has(self, name):
            confirmed = self.confirmed()
            return confirmed is not None and name in confirmed

        def sync(self):
            """
            Sends local unlocks and progress the service hasn't confirmed.
            """

            confirmed = self.confirmed()

            if confirmed is None:
                return

            for name in list(persistent._achievements or ()):
                if name not in confirmed:
                    self.send(name, 100)

            for name, completed in list((persistent._achievement_progress or { }).items()):
                if name not in confirmed and name not in (persistent._achievements or ()):
                    self.progress(name, completed)

        def periodic(self):
            m = renpy.gdk.module

            if m is None:
                return

            if m.user_changed():
                # Xbox resumes with the default user; on PC, signing out ends the game.
                if m.platform == "xbox":
                    m.sign_in(allow_ui=False)
                return

            user = m.user_id()

            if user is not None and user != self.synced_user:
                self.synced_user = user
                self.sync()

            results = m.achievement_results()

            if not results:
                return

            confirmed = self.confirmed()

            if confirmed is None:
                return

            names = { v : k for k, v in self.ids.items() }

            for id, percent, hr in results:
                if hr == 0 and percent >= 100:
                    confirmed.add(names.get(id, id))
                elif hr != 0:
                    renpy.gdk.log("Achievement {} ({}%) was not updated: 0x{:08X}.".format(id, percent, hr))

    def merge_gdk(old, new, current):
        rv = _dict()

        for d in (old or { }, new or { }):
            for k, v in d.items():
                rv.setdefault(k, _set()).update(v)

        return rv

    renpy.register_persistent("_gdk_achievements", merge_gdk)

    if gdk.xbl_available():
        gdk_backend = GdkBackend()
        backends.append(gdk_backend)
        config.periodic_callbacks.append(gdk_backend.periodic)
