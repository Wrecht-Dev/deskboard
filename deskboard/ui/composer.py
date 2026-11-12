"""Composer window: NPF text/quote/link posts with tags and scheduling.

Owner: shell.  Writing is always an explicit user action: Publish / Queue /
Draft buttons or Ctrl+Enter.  Nothing here fires automatically, and in demo
mode every write path is inert and says so.
"""
from __future__ import annotations

import datetime
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from ..api.client import TumblrError


def _describe_error(error: BaseException) -> str:
    message = " ".join(str(error).split()) or type(error).__name__
    if len(message) > 320:
        message = message[:317] + "…"
    return message


class ComposerWindow(Adw.Window):
    """A writing window for text, quote and link posts.

    Attachments use exactly the dict returned by
    ``ctx.client.upload_image(path)``; each becomes an NPF image block.
    """

    __gtype_name__ = "DeskboardComposerWindow"

    def __init__(self, ctx, parent=None, attachments: list[dict] | None = None,
                 on_done=None, on_closed=None) -> None:
        super().__init__()
        self.ctx = ctx
        self.on_done = on_done
        self.on_closed = on_closed
        self._closed_notified = False
        self._demo = bool(getattr(ctx, "demo", False))
        self._attachments: list[dict] = [
            dict(item) for item in (attachments or []) if isinstance(item, dict)
        ]
        self._busy = False
        self._allow_close = False
        self._publish_on: str | None = None
        self._discard_dialog = None
        self._blogs = self._collect_blogs()

        self.set_title("New post")
        try:
            self.set_default_size(820, 680)
            self.set_size_request(620, 520)
        except Exception:  # noqa: BLE001
            pass
        if parent is not None:
            try:
                self.set_transient_for(parent)
                self.set_modal(False)
            except Exception:  # noqa: BLE001
                pass

        self._build_ui()
        self._update_actions()
        try:
            self.connect("close-request", self._on_close_request)
        except Exception:  # noqa: BLE001
            pass

        controller = Gtk.EventControllerKey()
        try:
            controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        except Exception:  # noqa: BLE001
            pass
        controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(controller)

    # -- construction ------------------------------------------------------ #
    def _collect_blogs(self) -> list[str]:
        blogs: list[str] = []
        for name in getattr(self.ctx, "user_blogs", None) or []:
            name = str(name or "").strip()
            if name and name not in blogs:
                blogs.append(name)
        default = str(getattr(self.ctx, "user_blog", "") or "").strip()
        if default and default not in blogs:
            blogs.insert(0, default)
        if not blogs:
            user = str(getattr(self.ctx, "user_name", "") or "").strip()
            if user:
                blogs.append(user)
        if not blogs and self._demo:
            blogs.append("demo")
        return blogs

    def _build_ui(self) -> None:
        self._window_title = Adw.WindowTitle(title="New post", subtitle=self._current_blog())
        header = Adw.HeaderBar()
        header.set_title_widget(self._window_title)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda *_args: self.close())
        header.pack_start(cancel)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(12)
        content.set_margin_bottom(8)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        blog_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        blog_label = Gtk.Label(label="Post to")
        blog_label.add_css_class("caption")
        blog_label.add_css_class("dim-label")
        blog_box.append(blog_label)
        self.blog_dropdown = Gtk.DropDown.new_from_strings(self._blogs or ["No blog"])
        self.blog_dropdown.set_valign(Gtk.Align.CENTER)
        if self._blogs:
            try:
                default = str(getattr(self.ctx, "user_blog", "") or "")
                index = self._blogs.index(default) if default in self._blogs else 0
                self.blog_dropdown.set_selected(index)
            except Exception:  # noqa: BLE001
                pass
        self.blog_dropdown.connect("notify::selected", self._on_blog_changed)
        blog_box.append(self.blog_dropdown)
        top.append(blog_box)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.stack.set_vexpand(True)
        self.switcher = Gtk.StackSwitcher()
        self.switcher.set_stack(self.stack)
        top.append(self.switcher)
        content.append(top)

        self.attachments_flow = Gtk.FlowBox()
        self.attachments_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.attachments_flow.set_column_spacing(8)
        self.attachments_flow.set_row_spacing(6)
        self.attachments_flow.set_max_children_per_line(8)
        self.attachments_flow.set_margin_top(2)
        self.attachments_flow.set_visible(False)
        content.append(self.attachments_flow)

        self.text_page, self.text_view = self._make_editor_page("What's on your mind?")
        self.stack.add_titled(self.text_page, "text", "Text")

        quote_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.quote_page, self.quote_view = self._make_editor_page("Quote text")
        quote_page.append(self.quote_page)
        self.quote_source = Gtk.Entry()
        self.quote_source.set_placeholder_text("Source (optional)")
        quote_page.append(self.quote_source)
        self.stack.add_titled(quote_page, "quote", "Quote")

        link_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.link_url = Gtk.Entry()
        self.link_url.set_placeholder_text("https://example.com/article")
        link_page.append(self.link_url)
        self.link_title = Gtk.Entry()
        self.link_title.set_placeholder_text("Title (optional)")
        link_page.append(self.link_title)
        self.link_commentary_page, self.link_commentary = self._make_editor_page(
            "Add a comment (optional)", min_height=140
        )
        link_page.append(self.link_commentary_page)
        self.stack.add_titled(link_page, "link", "Link")
        self.stack.set_visible_child_name("text")
        content.append(self.stack)

        tags_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tags_label = Gtk.Label(label="Tags")
        tags_label.add_css_class("caption")
        tags_label.add_css_class("dim-label")
        tags_label.set_valign(Gtk.Align.CENTER)
        tags_row.append(tags_label)
        self.tags_entry = Gtk.Entry()
        self.tags_entry.set_placeholder_text("art, photography, design (comma separated)")
        self.tags_entry.set_hexpand(True)
        tags_row.append(self.tags_entry)
        content.append(tags_row)

        self.error_label = Gtk.Label(label="")
        self.error_label.set_wrap(True)
        self.error_label.set_xalign(0)
        self.error_label.add_css_class("composer-error")
        self.error_label.set_visible(False)
        content.append(self.error_label)

        self.notice_label = Gtk.Label(label="")
        self.notice_label.set_wrap(True)
        self.notice_label.set_xalign(0)
        self.notice_label.add_css_class("composer-notice")
        self.notice_label.set_visible(False)
        content.append(self.notice_label)

        toolbar.set_content(content)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("composer-actionbar")
        self.attach_tray = None
        try:
            from .attach import AttachTray

            self.attach_tray = AttachTray(self.add_media)
            self.attach_tray.set_context(self.ctx)
            bar.append(self.attach_tray)
        except Exception:  # noqa: BLE001 - composer still accepts pre-uploaded media
            self.attach_tray = None
        self.schedule_button = Gtk.MenuButton()
        self.schedule_button.set_icon_name("alarm-symbolic")
        self.schedule_button.set_tooltip_text("Schedule")
        self.schedule_button.add_css_class("flat")
        self.schedule_popover = self._build_schedule_popover()
        self.schedule_button.set_popover(self.schedule_popover)
        bar.append(self.schedule_button)
        self.schedule_label = Gtk.Label(label="")
        self.schedule_label.add_css_class("caption")
        self.schedule_label.add_css_class("dim-label")
        self.schedule_label.set_visible(False)
        bar.append(self.schedule_label)

        self.spinner = Adw.Spinner()
        self.spinner.set_size_request(18, 18)
        self.spinner.set_visible(False)
        bar.append(self.spinner)
        self.status_label = Gtk.Label(label="")
        self.status_label.add_css_class("caption")
        self.status_label.add_css_class("dim-label")
        bar.append(self.status_label)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        self.draft_button = Gtk.Button(label="Draft")
        self.draft_button.connect("clicked", lambda *_args: self._submit("draft"))
        bar.append(self.draft_button)
        self.queue_button = Gtk.Button(label="Queue")
        self.queue_button.connect("clicked", lambda *_args: self._submit("queue"))
        bar.append(self.queue_button)
        self.publish_button = Gtk.Button(label="Publish")
        self.publish_button.add_css_class("suggested-action")
        self.publish_button.connect("clicked", lambda *_args: self._submit("published"))
        bar.append(self.publish_button)
        toolbar.add_bottom_bar(bar)
        self.set_content(toolbar)

        self._rebuild_attachments()

    def _make_editor_page(self, placeholder_text: str, min_height: int = 220):
        text_view = Gtk.TextView()
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_top_margin(10)
        text_view.set_bottom_margin(10)
        text_view.set_left_margin(12)
        text_view.set_right_margin(12)
        text_view.set_hexpand(True)
        text_view.set_vexpand(True)
        text_view.add_css_class("composer-textview")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(text_view)
        scroller.set_vexpand(True)
        scroller.set_min_content_height(min_height)
        scroller.add_css_class("composer-editor")

        overlay = Gtk.Overlay()
        overlay.set_child(scroller)
        overlay.set_vexpand(True)
        placeholder = Gtk.Label(label=placeholder_text, xalign=0, yalign=0)
        placeholder.set_halign(Gtk.Align.START)
        placeholder.set_valign(Gtk.Align.START)
        placeholder.set_margin_start(16)
        placeholder.set_margin_top(12)
        placeholder.add_css_class("composer-placeholder")
        placeholder.set_can_target(False)
        overlay.add_overlay(placeholder)
        buffer = text_view.get_buffer()

        def on_changed(_buffer) -> None:
            try:
                text = _buffer.get_text(
                    _buffer.get_start_iter(), _buffer.get_end_iter(), True
                )
                placeholder.set_visible(not text.strip())
            except Exception:  # noqa: BLE001
                pass

        buffer.connect("changed", on_changed)
        return overlay, text_view

    def _build_schedule_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        self.calendar = Gtk.Calendar()
        box.append(self.calendar)

        now = datetime.datetime.now()
        time_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        time_label = Gtk.Label(label="Time")
        time_label.add_css_class("caption")
        time_label.add_css_class("dim-label")
        time_row.append(time_label)
        self.hour_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.hour_spin.set_value(min(23, now.hour + 1))
        self.hour_spin.set_digits(0)
        time_row.append(self.hour_spin)
        colon = Gtk.Label(label=":")
        time_row.append(colon)
        self.minute_spin = Gtk.SpinButton.new_with_range(0, 59, 5)
        self.minute_spin.set_value((now.minute // 5) * 5)
        self.minute_spin.set_digits(0)
        time_row.append(self.minute_spin)
        box.append(time_row)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        clear = Gtk.Button(label="Clear")
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_args: self._clear_schedule())
        buttons.append(clear)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        buttons.append(spacer)
        apply_button = Gtk.Button(label="Set time")
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", lambda *_args: self._set_schedule())
        buttons.append(apply_button)
        box.append(buttons)
        popover.set_child(box)
        return popover

    # -- attachments ------------------------------------------------------- #
    def add_media(self, media: dict) -> None:
        """Attach one uploaded media dict (``client.upload_image`` result)."""
        if not isinstance(media, dict) or not media:
            self._show_error("That attachment could not be added.")
            return
        self._attachments.append(dict(media))
        self._rebuild_attachments()

    def _rebuild_attachments(self) -> None:
        try:
            child = self.attachments_flow.get_first_child()
            while child is not None:
                following = child.get_next_sibling()
                self.attachments_flow.remove(child)
                child = following
            for index, media in enumerate(list(self._attachments)):
                chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                chip.add_css_class("composer-attachment")
                image = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
                image.set_pixel_size(14)
                chip.append(image)
                label = Gtk.Label(label=f"Image {index + 1}")
                label.add_css_class("caption")
                chip.append(label)
                remove = Gtk.Button.new_from_icon_name("window-close-symbolic")
                remove.add_css_class("flat")
                remove.add_css_class("circular")
                remove.set_tooltip_text("Remove attachment")
                remove.connect("clicked", lambda _b, item=media: self._remove_attachment(item))
                chip.append(remove)
                self.attachments_flow.append(chip)
            self.attachments_flow.set_visible(bool(self._attachments))
        except Exception:  # noqa: BLE001
            pass

    def _remove_attachment(self, media: dict) -> None:
        try:
            for index, item in enumerate(self._attachments):
                if item is media:
                    del self._attachments[index]
                    break
            self._rebuild_attachments()
        except Exception:  # noqa: BLE001
            pass

    # -- small helpers ----------------------------------------------------- #
    def _current_blog(self) -> str:
        if not self._blogs:
            return ""
        try:
            index = int(self.blog_dropdown.get_selected())
        except Exception:  # noqa: BLE001
            index = 0
        if 0 <= index < len(self._blogs):
            return self._blogs[index]
        return self._blogs[0]

    def _on_blog_changed(self, *_args) -> None:
        try:
            self._window_title.set_subtitle(self._current_blog())
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _buffer_text(view: Gtk.TextView) -> str:
        buffer = view.get_buffer()
        return buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), True
        ).strip()

    def _editors_text(self) -> list[str]:
        values = [
            self._buffer_text(self.text_view),
            self._buffer_text(self.quote_view),
            self._buffer_text(self.link_commentary),
            self.quote_source.get_text().strip(),
            self.link_url.get_text().strip(),
            self.link_title.get_text().strip(),
            self.tags_entry.get_text().strip(),
        ]
        return [value for value in values if value]

    def _dirty(self) -> bool:
        return bool(self._attachments or self._editors_text())

    def _show_error(self, message: str) -> None:
        try:
            self.error_label.set_text(str(message or ""))
            self.error_label.set_visible(bool(message))
        except Exception:  # noqa: BLE001
            pass

    def _clear_error(self) -> None:
        try:
            self.error_label.set_text("")
            self.error_label.set_visible(False)
        except Exception:  # noqa: BLE001
            pass

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        try:
            self.spinner.set_visible(busy)
            self.status_label.set_text(status if busy else "")
            enabled = not busy and not self._demo and bool(self._blogs)
            for button in (self.publish_button, self.queue_button, self.draft_button):
                button.set_sensitive(enabled)
            self.schedule_button.set_sensitive(not busy and not self._demo and bool(self._blogs))
            if self.attach_tray is not None:
                self.attach_tray.set_sensitive(not busy and not self._demo and bool(self._blogs))
        except Exception:  # noqa: BLE001
            pass

    def _update_actions(self) -> None:
        can_write = bool(self._blogs) and not self._demo
        try:
            for button in (self.publish_button, self.queue_button, self.draft_button):
                button.set_sensitive(can_write)
            self.schedule_button.set_sensitive(can_write)
            if self.attach_tray is not None:
                self.attach_tray.set_sensitive(can_write)
        except Exception:  # noqa: BLE001
            pass
        if self._demo:
            self.notice_label.set_text(
                "Demo mode: publishing is disabled. Nothing you write here is sent anywhere."
            )
            self.notice_label.set_visible(True)
        elif not self._blogs:
            self.notice_label.set_text(
                "No blog is available to post to. Sign in or check your account blogs."
            )
            self.notice_label.set_visible(True)
        else:
            self.notice_label.set_visible(False)

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        try:
            control = bool(state & Gdk.ModifierType.CONTROL_MASK)
            if control and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                self._submit("published")
                return True
            if keyval == Gdk.KEY_Escape:
                try:
                    if self.schedule_popover is not None and self.schedule_popover.get_visible():
                        self.schedule_popover.popdown()
                        return True
                except Exception:  # noqa: BLE001
                    pass
                self.close()
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # -- scheduling -------------------------------------------------------- #
    def _set_schedule(self) -> None:
        try:
            date = self.calendar.get_date()
            year = int(date.get_year())
            month = int(date.get_month())
            day = int(date.get_day_of_month())
            hour = int(self.hour_spin.get_value())
            minute = int(self.minute_spin.get_value())
            scheduled = datetime.datetime(year, month, day, hour, minute).astimezone()
            if scheduled <= datetime.datetime.now().astimezone():
                self._show_error("Choose a time in the future.")
                return
            self._publish_on = scheduled.isoformat(timespec="minutes")
            self.schedule_label.set_text(scheduled.strftime("%b %d, %H:%M"))
            self.schedule_label.set_visible(True)
            self.publish_button.set_label("Schedule")
            self._clear_error()
            try:
                self.schedule_popover.popdown()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"Could not set that time: {exc}")

    def _clear_schedule(self) -> None:
        self._publish_on = None
        try:
            self.schedule_label.set_text("")
            self.schedule_label.set_visible(False)
            self.publish_button.set_label("Publish")
            self.schedule_popover.popdown()
        except Exception:  # noqa: BLE001
            pass

    # -- content ----------------------------------------------------------- #
    def _collect_tags(self) -> list[str]:
        raw = ""
        try:
            raw = self.tags_entry.get_text()
        except Exception:  # noqa: BLE001
            return []
        tags: list[str] = []
        seen = set()
        for part in raw.replace("\n", ",").split(","):
            tag = part.strip().lstrip("#").strip()
            if tag and tag.lower() not in seen:
                seen.add(tag.lower())
                tags.append(tag)
        return tags

    def _build_content(self) -> list[dict]:
        blocks: list[dict] = []
        for media in self._attachments:
            blocks.append({"type": "image", "media": [media]})

        kind = "text"
        try:
            kind = self.stack.get_visible_child_name() or "text"
        except Exception:  # noqa: BLE001
            kind = "text"
        if kind == "quote":
            text = self._buffer_text(self.quote_view)
            if not text:
                raise ValueError("Write a quote first.")
            blocks.append({"type": "text", "text": text, "subtype": "quote"})
            source = self.quote_source.get_text().strip()
            if source:
                blocks.append({"type": "text", "text": f"— {source}"})
        elif kind == "link":
            url = self.link_url.get_text().strip()
            if not url:
                raise ValueError("Add a link URL.")
            block: dict = {"type": "link", "url": url}
            title = self.link_title.get_text().strip()
            if title:
                block["title"] = title
            blocks.append(block)
            commentary = self._buffer_text(self.link_commentary)
            if commentary:
                blocks.append({"type": "text", "text": commentary})
        else:
            text = self._buffer_text(self.text_view)
            if text:
                blocks.append({"type": "text", "text": text})

        if not blocks:
            raise ValueError("Write something first.")
        if not self._current_blog():
            raise ValueError("Choose a blog to post to.")
        return blocks

    def _submit(self, state: str) -> None:
        if self._busy:
            return
        if self._demo:
            self.ctx.toast("Demo mode: publishing is disabled.")
            return
        try:
            signed_in = bool(self.ctx.is_signed_in)
        except Exception:  # noqa: BLE001
            signed_in = False
        if not signed_in:
            self._show_error("Sign in to publish posts.")
            return
        try:
            content = self._build_content()
        except ValueError as exc:
            self._show_error(str(exc))
            return
        blog = self._current_blog()
        if not blog:
            self._show_error("No blog is available to post to.")
            return
        tags = self._collect_tags()
        publish_on = self._publish_on if state == "published" else None
        self._busy = True
        self._clear_error()
        status = {
            "published": "Scheduling…" if publish_on else "Publishing…",
            "queue": "Adding to queue…",
            "draft": "Saving draft…",
        }.get(state, "Posting…")
        self._set_busy(True, status)
        client = self.ctx.client

        def work() -> None:
            error = ""
            result = None
            try:
                result = client.create_post(
                    blog,
                    content,
                    state=state,
                    tags=tags,
                    publish_on=publish_on,
                )
            except TumblrError as exc:
                error = _describe_error(exc)
            except Exception as exc:  # noqa: BLE001
                error = _describe_error(exc)
            try:
                GLib.idle_add(
                    self._finish_submit, state, error, publish_on, result
                )
            except Exception:  # noqa: BLE001
                pass

        try:
            threading.Thread(target=work, daemon=True, name="composer-post").start()
        except Exception as exc:  # noqa: BLE001
            self._finish_submit(state, _describe_error(exc), publish_on, None)

    def _finish_submit(self, state: str, error: str, publish_on, result) -> bool:
        try:
            if not self.get_visible():
                return False
            if error:
                self._set_busy(False)
                self._show_error(f"Couldn't post: {error}")
                self.ctx.toast(f"Couldn't post: {error}")
                return False
            self._set_busy(False)
            if state == "draft":
                message = "Saved as draft"
            elif state == "queue":
                message = "Added to queue"
            elif publish_on:
                message = f"Scheduled for {self.schedule_label.get_text()}"
            else:
                message = "Published"
            self.ctx.toast(message)
            callback = self.on_done
            if callable(callback):
                try:
                    callback(state, result)
                except Exception:  # noqa: BLE001
                    pass
            self._close_now()
        except Exception:  # noqa: BLE001
            pass
        return False

    # -- discard / close --------------------------------------------------- #
    def _notify_closed(self) -> None:
        if self._closed_notified:
            return
        self._closed_notified = True
        callback = self.on_closed
        if callable(callback):
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass

    def _close_now(self) -> None:
        """Destroy the window (``close()`` would only hide it on Adw.Window)."""
        self._allow_close = True
        self._notify_closed()

        def destroy() -> bool:
            try:
                self.destroy()
            except Exception:  # noqa: BLE001
                try:
                    self.close()
                except Exception:  # noqa: BLE001
                    pass
            return False

        try:
            GLib.idle_add(destroy)
        except Exception:  # noqa: BLE001
            destroy()

    def _on_close_request(self, *_args) -> bool:
        if self._allow_close:
            self._close_now()
            return True
        if self._busy:
            self.ctx.toast("Still posting — wait a moment.")
            return True
        if not self._dirty():
            self._close_now()
            return True
        self._confirm_discard()
        return True

    def _confirm_discard(self) -> None:
        try:
            if self._discard_dialog is not None:
                self._discard_dialog.present(self)
                return
            dialog = Adw.AlertDialog()
            dialog.set_heading("Discard this post?")
            dialog.set_body("Your text, tags and attachments will be lost.")
            dialog.add_response("cancel", "Keep writing")
            dialog.add_response("discard", "Discard")
            dialog.set_response_appearance(
                "discard", Adw.ResponseAppearance.DESTRUCTIVE
            )
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_response(_dialog, response: str) -> None:
                self._discard_dialog = None
                if response == "discard":
                    self._close_now()

            dialog.connect("response", on_response)
            self._discard_dialog = dialog
            dialog.present(self)
        except Exception:  # noqa: BLE001
            self._close_now()
