# SPDX-License-Identifier: GPL-3.0-or-later
#
# Tick - active time tracker with session history for Blender projects
# Copyright (C) 2026 Denis Maidenkov, DMWorks Studio
#
# Based on "Project Timer" by Marcin Zielinski,
# Copyright (C) 2013, licensed under GPL-3.0-or-later.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
# <https://www.gnu.org/licenses/>
#
# Additional terms (GNU GPL v3, section 7):
#  1. This copyright notice and the author attribution must be preserved
#     in all copies and modified versions.
#  2. Modified versions must be clearly marked as modified and must not be
#     presented as the original Tick or as the work of Denis Maidenkov
#     or DMWorks Studio.
#  3. No rights are granted to use the names "Tick" and "DMWorks Studio"
#     as trademarks or product names of modified versions.

bl_info = {
    "name": "Tick",
    "author": "Denis Maidenkov, DMWorks Studio",
    "version": (1, 0, 0),
    "blender": (3, 2, 0),
    "location": "Top Bar / Status Bar / 3D Viewport Header",
    "description": "Active time tracker with session history",
    "category": "Interface",
}

import bpy
import json
import time
from bpy.app.handlers import persistent

PROP = "projectTime"        # то же имя, что и в старой версии, старые проекты читаются как есть
HIST_PROP = "tickSessions"  # история сессий (JSON-строка в сцене)
MIN_SESSION = 10            # сессия попадает в историю после стольких секунд активной работы
MAX_SESSIONS = 1000         # максимум записей в истории
HISTORY_ROWS = 20           # сколько последних сессий показывать в окне истории


class _State:
    total = 0.0            # секунды активной работы над проектом
    since_break = 0.0      # активные секунды с последнего напоминания
    last_tick = None
    last_write = 0.0
    last_activity = 0.0
    last_clock_key = None  # чтобы напоминание по времени сработало один раз
    loaded = False
    watcher_alive = False
    stop = False
    history = []           # сессии: {"s": начало, "e": конец, "a": активные секунды}
    session = None         # текущая сессия
    session_attached = False


state = _State()


# ---------- Настройки ----------

def _prefs():
    addon = bpy.context.preferences.addons.get(__name__)
    return addon.preferences if addon else None


def _idle_limit():
    p = _prefs()
    return p.idle_timeout if p else 5


def _write_interval():
    p = _prefs()
    return p.save_interval if p else 15


# ---------- Формат времени ----------

def _format(seconds, fmt='COLON', show_seconds=True):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if fmt == 'HMS':
        if h:
            parts = [f"{h}h", f"{m:02d}m"]
        elif m or not show_seconds:
            parts = [f"{m}m"]
        else:
            parts = []
        if show_seconds:
            parts.append(f"{s:02d}s" if parts else f"{s}s")
        return " ".join(parts)
    text = f"{h}:{m:02d}"
    if show_seconds:
        text += f":{s:02d}"
    return text


def _fmt_pref(seconds):
    p = _prefs()
    if p:
        return _format(seconds, p.time_format, p.show_seconds)
    return _format(seconds, 'HMS', True)


# ---------- Чтение / запись в сцены ----------

def _read_from_file():
    """Максимум по всем сценам: ничего не теряем в файлах с несколькими сценами."""
    best = 0
    for scene in bpy.data.scenes:
        try:
            best = max(best, int(scene.get(PROP, 0)))
        except (TypeError, ValueError):
            pass
    return float(best)


def _read_history():
    """Берём историю с наибольшим суммарным временем среди всех сцен."""
    best, best_sum = [], -1
    for scene in bpy.data.scenes:
        raw = scene.get(HIST_PROP)
        if not raw:
            continue
        try:
            items = [{"s": int(d["s"]), "e": int(d["e"]), "a": int(d["a"])}
                     for d in json.loads(raw)]
        except (ValueError, TypeError, KeyError):
            continue
        total = sum(i["a"] for i in items)
        if total > best_sum:
            best, best_sum = items, total
    return best


def _new_session():
    now = int(time.time())
    state.session = {"s": now, "e": now, "a": 0.0}
    state.session_attached = False


def _commit_session():
    """Обновляет текущую сессию; в историю она попадает после MIN_SESSION секунд."""
    s = state.session
    if s is None:
        return
    s["e"] = int(time.time())
    if not state.session_attached and s["a"] >= MIN_SESSION:
        state.history.append(s)
        state.session_attached = True
        if len(state.history) > MAX_SESSIONS:
            del state.history[:len(state.history) - MAX_SESSIONS]


def _write_to_scenes():
    _commit_session()
    payload = json.dumps(
        [{"s": x["s"], "e": x["e"], "a": int(x["a"])} for x in state.history],
        separators=(",", ":"))
    value = int(state.total)
    for scene in bpy.data.scenes:
        scene[PROP] = value
        scene[HIST_PROP] = payload
    state.last_write = time.monotonic()


def _load_state():
    state.total = _read_from_file()
    state.history = _read_history()
    _new_session()
    state.since_break = 0.0
    state.last_clock_key = None
    now = time.monotonic()
    state.last_tick = now
    state.last_activity = now
    state.last_write = now
    state.loaded = True


def _is_active():
    if bpy.app.is_job_running('RENDER'):
        return True
    return time.monotonic() - state.last_activity < _idle_limit()


def _stats():
    now = time.time()
    lt = time.localtime(now)
    day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    tracked = sum(x["a"] for x in state.history)
    today = sum(x["a"] for x in state.history if x["s"] >= day_start)
    week = sum(x["a"] for x in state.history if x["s"] >= now - 7 * 86400)
    earlier = max(0.0, state.total - tracked)
    return tracked, today, week, earlier


# ---------- Отслеживание активности ----------

class TICK_OT_watch(bpy.types.Operator):
    bl_idname = "tick.watch"
    bl_label = "Tick Activity Watcher"
    bl_options = {'INTERNAL'}

    def modal(self, context, event):
        if state.stop:
            state.watcher_alive = False
            return {'CANCELLED'}
        if event.type not in {'TIMER', 'TIMER_REPORT', 'NONE'}:
            state.last_activity = time.monotonic()
        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        state.watcher_alive = True
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}


def _ensure_watcher():
    if state.watcher_alive:
        return
    windows = bpy.context.window_manager.windows
    if not windows:
        return
    try:
        with bpy.context.temp_override(window=windows[0]):
            bpy.ops.tick.watch('INVOKE_DEFAULT')
    except RuntimeError:
        pass


# ---------- Операторы ----------

class TICK_OT_reset(bpy.types.Operator):
    bl_idname = "tick.reset"
    bl_label = "Reset Timer"
    bl_description = "Reset the project time to zero (session history is kept)"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state.total = 0.0
        state.since_break = 0.0
        state.last_tick = time.monotonic()
        _write_to_scenes()
        return {'FINISHED'}


class TICK_OT_clear_history(bpy.types.Operator):
    bl_idname = "tick.clear_history"
    bl_label = "Clear Session History"
    bl_description = "Delete all recorded sessions of this project"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        state.history = []
        _new_session()
        _write_to_scenes()
        return {'FINISHED'}


class TICK_OT_history(bpy.types.Operator):
    bl_idname = "tick.history"
    bl_label = "Session History"
    bl_description = "Show the sessions of this project"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        layout = self.layout
        tracked, today, week, earlier = _stats()

        box = layout.box()
        col = box.column(align=True)

        def stat(label, value):
            row = col.row()
            row.label(text=label)
            row.label(text=value)

        stat("Project total", _fmt_pref(state.total))
        stat("Today", _fmt_pref(today))
        stat("Last 7 days", _fmt_pref(week))
        stat("Sessions", str(len(state.history)))
        if earlier >= 1:
            stat("Earlier (no session data)", _fmt_pref(earlier))

        layout.separator()
        layout.label(text="Recent sessions")

        if not state.history:
            layout.label(text="No sessions recorded yet")
        else:
            col = layout.column(align=True)
            for ss in reversed(state.history[-HISTORY_ROWS:]):
                row = col.row()
                start = time.localtime(ss["s"])
                end = time.localtime(ss["e"])
                is_current = ss is state.session
                row.label(text=time.strftime("%d.%m.%Y", start),
                          icon='REC' if is_current else 'NONE')
                row.label(text=time.strftime("%H:%M", start) + " - " + time.strftime("%H:%M", end))
                row.label(text=_fmt_pref(ss["a"]))
            older = len(state.history) - HISTORY_ROWS
            if older > 0:
                layout.label(text=f"... and {older} older")

        layout.separator()
        layout.operator("tick.clear_history", icon='TRASH')

    def execute(self, context):
        return {'FINISHED'}


class TICK_OT_break(bpy.types.Operator):
    bl_idname = "tick.break_reminder"
    bl_label = "Break Reminder"
    bl_options = {'INTERNAL'}

    message: bpy.props.StringProperty(default="Time for a break!")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        self.layout.label(text=self.message, icon='TIME')

    def execute(self, context):
        return {'FINISHED'}


def _show_break(message):
    windows = bpy.context.window_manager.windows
    if not windows:
        return
    try:
        with bpy.context.temp_override(window=windows[0]):
            bpy.ops.tick.break_reminder('INVOKE_DEFAULT', message=message)
    except RuntimeError:
        pass


# ---------- Preferences ----------

class TickPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    # отображение
    show_counter: bpy.props.BoolProperty(name="Show counter", default=True)
    location: bpy.props.EnumProperty(
        name="Location",
        items=[
            ('TOPBAR', "Top Bar", "Right side of the top bar"),
            ('STATUSBAR', "Status Bar", "Bottom status bar"),
            ('VIEW3D', "3D Viewport Header", "Header of 3D viewports"),
        ],
        default='TOPBAR')
    time_format: bpy.props.EnumProperty(
        name="Format",
        items=[
            ('COLON', "1:05:09", "Hours:minutes:seconds"),
            ('HMS', "1h 05m 09s", "Hours, minutes, seconds with letters"),
        ],
        default='HMS')
    show_seconds: bpy.props.BoolProperty(name="Show seconds", default=True)
    show_icon: bpy.props.BoolProperty(
        name="Show clock icon",
        description="Clock/pause icon next to the time",
        default=True)

    # активность
    idle_timeout: bpy.props.IntProperty(
        name="Idle timeout (s)",
        description="Stop counting after this many seconds without input",
        default=5, min=5, max=3600)
    save_interval: bpy.props.IntProperty(
        name="Write interval (s)",
        description="How often time and session history are written into the scene, so autosaves keep them",
        default=15, min=5, max=300)

    # напоминания
    break_interval_enabled: bpy.props.BoolProperty(
        name="Remind every N minutes of work", default=False)
    break_interval: bpy.props.IntProperty(
        name="Interval (min)", default=45, min=1, max=600)
    break_reset_idle: bpy.props.IntProperty(
        name="Idle counts as break (min)",
        description="If you are away this long, the interval countdown restarts. 0 = off",
        default=5, min=0, max=120)
    break_at_enabled: bpy.props.BoolProperty(
        name="Remind at a specific time", default=False)
    break_at_hour: bpy.props.IntProperty(name="Hour", default=13, min=0, max=23)
    break_at_minute: bpy.props.IntProperty(name="Minute", default=0, min=0, max=59)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Display")
        box.prop(self, "show_counter")
        col = box.column()
        col.active = self.show_counter
        col.prop(self, "location")
        col.prop(self, "time_format")
        col.prop(self, "show_seconds")
        col.prop(self, "show_icon")

        box = layout.box()
        box.label(text="Activity")
        box.prop(self, "idle_timeout")
        box.prop(self, "save_interval")

        box = layout.box()
        box.label(text="Break reminders")
        box.prop(self, "break_interval_enabled")
        col = box.column()
        col.active = self.break_interval_enabled
        col.prop(self, "break_interval")
        col.prop(self, "break_reset_idle")
        box.prop(self, "break_at_enabled")
        row = box.row(align=True)
        row.active = self.break_at_enabled
        row.prop(self, "break_at_hour")
        row.prop(self, "break_at_minute")
        op = box.operator("tick.break_reminder", text="Test reminder", icon='TIME')
        op.message = "This is a test reminder"

        layout.operator("tick.history", icon='TEXT')
        layout.operator("tick.reset", icon='LOOP_BACK')


# ---------- Поповер ----------

class TICK_PT_popover(bpy.types.Panel):
    bl_idname = "TICK_PT_popover"
    bl_label = "Tick"
    bl_space_type = 'TOPBAR'
    bl_region_type = 'HEADER'

    def draw(self, context):
        layout = self.layout
        prefs = _prefs()
        active = _is_active()

        layout.label(text=_format(state.total, 'COLON', True),
                     icon='TIME' if active else 'PAUSE')
        layout.label(text="Active" if active else "Paused (idle)")
        if state.session:
            layout.label(text="This session: " + _format(state.session["a"], 'COLON', True))
        layout.separator()

        if prefs:
            layout.prop(prefs, "show_seconds")
            layout.prop(prefs, "show_icon")
            layout.separator()

        layout.operator("tick.history", text="Session History", icon='TEXT')
        layout.operator("tick.reset", icon='LOOP_BACK')
        op = layout.operator("preferences.addon_show", text="Addon Settings", icon='PREFERENCES')
        op.module = __name__


# ---------- Отрисовка счётчика ----------

def _draw(layout, location):
    prefs = _prefs()
    if not prefs or not prefs.show_counter or prefs.location != location:
        return
    if prefs.show_icon:
        icon = 'TIME' if _is_active() else 'PAUSE'
    else:
        icon = 'NONE'
    layout.popover(
        panel="TICK_PT_popover",
        text=_format(state.total, prefs.time_format, prefs.show_seconds),
        icon=icon)


def draw_topbar(self, context):
    if context.region.alignment == 'RIGHT':
        _draw(self.layout, 'TOPBAR')


def draw_statusbar(self, context):
    _draw(self.layout, 'STATUSBAR')


def draw_view3d(self, context):
    _draw(self.layout, 'VIEW3D')


_HEADERS = (
    (bpy.types.TOPBAR_HT_upper_bar, draw_topbar),
    (bpy.types.STATUSBAR_HT_header, draw_statusbar),
    (bpy.types.VIEW3D_HT_header, draw_view3d),
)


def _redraw():
    p = _prefs()
    if not p or not p.show_counter:
        return
    area_type = {'TOPBAR': 'TOPBAR', 'STATUSBAR': 'STATUSBAR', 'VIEW3D': 'VIEW_3D'}[p.location]
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != area_type:
                continue
            if area_type == 'VIEW_3D':
                # только заголовок, чтобы не перерисовывать весь вьюпорт
                for region in area.regions:
                    if region.type == 'HEADER':
                        region.tag_redraw()
            else:
                area.tag_redraw()


# ---------- Напоминания ----------

def _check_breaks(now):
    p = _prefs()
    if not p:
        return

    if p.break_reset_idle > 0 and now - state.last_activity >= p.break_reset_idle * 60:
        state.since_break = 0.0

    if p.break_interval_enabled and state.since_break >= p.break_interval * 60:
        state.since_break = 0.0
        _show_break(f"You've been working for {p.break_interval} min. Time for a break!")

    if p.break_at_enabled:
        t = time.localtime()
        key = (t.tm_yday, t.tm_hour, t.tm_min)
        if (t.tm_hour == p.break_at_hour and t.tm_min == p.break_at_minute
                and key != state.last_clock_key):
            state.last_clock_key = key
            _show_break(f"It's {p.break_at_hour:02d}:{p.break_at_minute:02d}. Time for a break!")


# ---------- Главный таймер ----------

def _tick():
    now = time.monotonic()

    if not state.loaded:
        # первый тик после включения аддона: bpy.data уже доступен
        _load_state()
        _ensure_watcher()
        return 1.0

    dt = now - state.last_tick if state.last_tick is not None else 0.0
    state.last_tick = now
    if _is_active():
        state.total += dt
        state.since_break += dt
        if state.session:
            state.session["a"] += dt

    _check_breaks(now)

    if now - state.last_write >= _write_interval():
        _write_to_scenes()

    _ensure_watcher()
    _redraw()
    return 1.0


# ---------- Хендлеры ----------

@persistent
def on_save_pre(*args):
    if state.loaded:
        _write_to_scenes()


@persistent
def on_load_post(*args):
    # при загрузке файла (в том числе автосейва) модальные операторы умирают
    state.watcher_alive = False
    _load_state()


# ---------- Регистрация ----------

classes = (
    TICK_OT_watch,
    TICK_OT_reset,
    TICK_OT_clear_history,
    TICK_OT_history,
    TICK_OT_break,
    TickPreferences,
    TICK_PT_popover,
)


def register():
    state.stop = False
    state.loaded = False
    state.watcher_alive = False
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.projectTime = bpy.props.IntProperty(
        name="Project Time", description="Seconds of active work", default=0)
    bpy.app.handlers.load_post.append(on_load_post)
    bpy.app.handlers.save_pre.append(on_save_pre)
    for header, fn in _HEADERS:
        header.append(fn)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)


def unregister():
    state.stop = True
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for header, fn in _HEADERS:
        try:
            header.remove(fn)
        except (ValueError, RuntimeError):
            pass
    if on_save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(on_save_pre)
    if on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post)
    del bpy.types.Scene.projectTime
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()