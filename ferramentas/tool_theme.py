"""Shared polished Tk/ttk theme for the standalone FF12 co-op tools."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


FONT = "Segoe UI"
FONT_DISPLAY = "Segoe UI Semibold"


COLORS = {
    "bg": "#0a0e14",
    "surface": "#101720",
    "card": "#151e29",
    "card_alt": "#192431",
    "field": "#0d141d",
    "border": "#283647",
    "border_soft": "#202c3a",
    "text": "#f5f7fb",
    "muted": "#94a1b2",
    "muted_dark": "#657286",
    "faint": "#657286",
    "blue": "#5aa7ff",
    "blue_hover": "#7cbbff",
    "blue_deep": "#183c62",
    "gold": "#f0c75e",
    "gold_hover": "#f7d77f",
    "green": "#57d39b",
    "green_hover": "#76e4b1",
    "red": "#ff747d",
    "red_deep": "#512b32",
    "selection": "#244d75",
}


def apply_theme(root: tk.Tk) -> ttk.Style:
    """Install the same modern dark theme in every co-op utility."""

    c = COLORS
    root.configure(bg=c["bg"])
    root.option_add("*Font", ("Segoe UI", 10))
    root.option_add("*TCombobox*Listbox.background", c["field"])
    root.option_add("*TCombobox*Listbox.foreground", c["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", c["selection"])
    root.option_add("*TCombobox*Listbox.selectForeground", c["text"])

    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure("TFrame", background=c["bg"])
    style.configure("Header.TFrame", background=c["surface"])
    style.configure("Surface.TFrame", background=c["surface"])
    style.configure("Card.TFrame", background=c["card"], relief="flat")
    style.configure("CardAlt.TFrame", background=c["card_alt"])
    # Compatibility aliases used by the original utility layouts.
    style.configure("Panel.TFrame", background=c["card"], relief="flat")
    style.configure("Toolbar.TFrame", background=c["surface"])
    style.configure("Status.TFrame", background=c["surface"])

    style.configure(
        "TLabel",
        background=c["bg"],
        foreground=c["text"],
        font=("Segoe UI", 10),
    )
    style.configure("Header.TLabel", background=c["surface"], foreground=c["text"])
    style.configure("Surface.TLabel", background=c["surface"], foreground=c["muted"])
    style.configure("Card.TLabel", background=c["card"], foreground=c["text"])
    style.configure("Panel.TLabel", background=c["card"], foreground=c["text"])
    style.configure(
        "CardMuted.TLabel",
        background=c["card"],
        foreground=c["muted"],
        font=("Segoe UI", 9),
    )
    style.configure(
        "Muted.TLabel",
        background=c["bg"],
        foreground=c["muted"],
        font=("Segoe UI", 9),
    )
    style.configure(
        "Status.TLabel",
        background=c["surface"],
        foreground=c["muted"],
        font=("Segoe UI", 9),
    )
    style.configure(
        "Eyebrow.TLabel",
        background=c["surface"],
        foreground=c["blue"],
        font=("Segoe UI Semibold", 9),
    )
    style.configure(
        "Title.TLabel",
        background=c["surface"],
        foreground=c["text"],
        font=("Segoe UI Semibold", 22),
    )
    style.configure(
        "HeaderTitle.TLabel",
        background=c["surface"],
        foreground=c["text"],
        font=(FONT_DISPLAY, 22),
    )
    style.configure(
        "HeaderMuted.TLabel",
        background=c["surface"],
        foreground=c["muted"],
        font=(FONT, 9),
    )
    style.configure(
        "PageTitle.TLabel",
        background=c["bg"],
        foreground=c["text"],
        font=("Segoe UI Semibold", 20),
    )
    style.configure(
        "Section.TLabel",
        background=c["card"],
        foreground=c["text"],
        font=("Segoe UI Semibold", 11),
    )
    style.configure(
        "Accent.TLabel",
        background=c["card"],
        foreground=c["gold"],
        font=("Segoe UI Semibold", 10),
    )

    style.configure(
        "TButton",
        background=c["card_alt"],
        foreground=c["text"],
        bordercolor=c["border"],
        lightcolor=c["card_alt"],
        darkcolor=c["card_alt"],
        padding=(13, 8),
        font=("Segoe UI Semibold", 9),
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("pressed", c["selection"]), ("active", "#263548")],
        bordercolor=[("focus", c["blue"]), ("active", "#3a4c62")],
        foreground=[("disabled", c["muted_dark"])],
    )
    style.configure(
        "Primary.TButton",
        background=c["blue"],
        foreground="#07111c",
        bordercolor=c["blue"],
        padding=(16, 9),
        font=("Segoe UI Semibold", 10),
    )
    style.map(
        "Primary.TButton",
        background=[("pressed", "#3c8fe8"), ("active", c["blue_hover"])],
        bordercolor=[("active", c["blue_hover"])],
    )
    style.configure(
        "Gold.TButton",
        background=c["gold"],
        foreground="#1b1403",
        bordercolor=c["gold"],
        padding=(16, 9),
        font=("Segoe UI Semibold", 10),
    )
    style.map(
        "Gold.TButton",
        background=[("pressed", "#d7aa3b"), ("active", c["gold_hover"])],
    )
    style.configure(
        "Danger.TButton",
        background=c["red_deep"],
        foreground="#ffdfe2",
        bordercolor="#75414a",
        padding=(14, 9),
    )
    style.map("Danger.TButton", background=[("active", "#68404a")])
    style.configure("Compact.TButton", padding=(10, 6), font=("Segoe UI Semibold", 9))
    style.configure(
        "Secondary.TButton",
        background=c["card_alt"],
        foreground=c["text"],
        bordercolor=c["border"],
        padding=(14, 8),
    )
    style.configure(
        "Bind.TButton",
        background=c["field"],
        foreground=c["text"],
        bordercolor=c["border"],
        padding=(9, 7),
        anchor="center",
    )
    style.map(
        "Bind.TButton",
        background=[("active", "#223044"), ("pressed", c["selection"])],
        bordercolor=[("focus", c["blue"])],
    )

    style.configure(
        "TCheckbutton",
        background=c["card"],
        foreground=c["text"],
        indicatorbackground=c["field"],
        indicatorforeground=c["blue"],
        bordercolor=c["border"],
        padding=(2, 3),
    )
    style.map(
        "TCheckbutton",
        background=[("active", c["card"])],
        indicatorbackground=[("selected", c["blue"]), ("active", "#223044")],
        indicatorforeground=[("selected", "#07111c")],
    )
    style.configure(
        "Header.TCheckbutton",
        background=c["surface"],
        foreground=c["text"],
        indicatorbackground=c["field"],
        indicatorforeground=c["blue"],
        padding=(2, 3),
    )
    style.map(
        "Header.TCheckbutton",
        background=[("active", c["surface"])],
        indicatorbackground=[("selected", c["blue"])],
    )
    style.configure(
        "Toggle.TCheckbutton",
        background=c["surface"],
        foreground=c["text"],
        indicatorbackground=c["field"],
        indicatorforeground=c["blue"],
        padding=(2, 3),
        font=("Segoe UI Semibold", 9),
    )
    style.map(
        "Toggle.TCheckbutton",
        background=[("active", c["surface"])],
        indicatorbackground=[("selected", c["blue"])],
    )

    style.configure(
        "TEntry",
        fieldbackground=c["field"],
        foreground=c["text"],
        insertcolor=c["text"],
        bordercolor=c["border"],
        lightcolor=c["border"],
        darkcolor=c["border"],
        padding=(10, 8),
    )
    style.map("TEntry", bordercolor=[("focus", c["blue"])])
    style.configure(
        "TSpinbox",
        fieldbackground=c["field"],
        foreground=c["text"],
        arrowcolor=c["muted"],
        bordercolor=c["border"],
        lightcolor=c["border"],
        darkcolor=c["border"],
        padding=(8, 7),
    )
    style.map("TSpinbox", bordercolor=[("focus", c["blue"])])
    style.configure(
        "TCombobox",
        fieldbackground=c["field"],
        background=c["card_alt"],
        foreground=c["text"],
        arrowcolor=c["muted"],
        bordercolor=c["border"],
        lightcolor=c["border"],
        darkcolor=c["border"],
        padding=(8, 7),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", c["field"])],
        foreground=[("readonly", c["text"]), ("disabled", c["muted_dark"])],
        bordercolor=[("focus", c["blue"])],
    )

    style.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=0)
    style.configure(
        "TNotebook.Tab",
        background=c["surface"],
        foreground=c["muted"],
        bordercolor=c["border_soft"],
        padding=(20, 10),
        font=("Segoe UI Semibold", 9),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", c["card"]), ("active", c["card_alt"])],
        foreground=[("selected", c["text"]), ("active", c["text"])],
        bordercolor=[("selected", c["blue"])],
    )

    style.configure(
        "Treeview",
        background=c["card"],
        fieldbackground=c["card"],
        foreground=c["text"],
        bordercolor=c["border_soft"],
        rowheight=30,
        font=("Segoe UI", 9),
    )
    style.configure(
        "Treeview.Heading",
        background=c["surface"],
        foreground=c["muted"],
        bordercolor=c["border"],
        font=("Segoe UI Semibold", 8),
        padding=(6, 7),
    )
    style.map(
        "Treeview",
        background=[("selected", c["selection"])],
        foreground=[("selected", "#ffffff")],
    )

    style.configure(
        "Vertical.TScrollbar",
        background=c["card_alt"],
        troughcolor=c["surface"],
        bordercolor=c["surface"],
        arrowcolor=c["muted"],
    )
    style.map("Vertical.TScrollbar", background=[("active", "#314157")])
    style.configure("TSeparator", background=c["border_soft"])
    return style


def card(master, *, padding=0) -> tk.Frame:
    """A bordered native Tk card for layouts that need true outlines."""

    return tk.Frame(
        master,
        bg=COLORS["card"],
        highlightbackground=COLORS["border"],
        highlightcolor=COLORS["border"],
        highlightthickness=1,
        bd=0,
        padx=padding,
        pady=padding,
    )


def entry(master, **kwargs) -> tk.Entry:
    """Flat, bordered text field that looks identical in every Tk build."""

    kwargs.setdefault("bg", COLORS["field"])
    kwargs.setdefault("fg", COLORS["text"])
    kwargs.setdefault("insertbackground", COLORS["text"])
    kwargs.setdefault("selectbackground", COLORS["selection"])
    kwargs.setdefault("selectforeground", COLORS["text"])
    kwargs.setdefault("relief", "flat")
    kwargs.setdefault("highlightthickness", 1)
    kwargs.setdefault("highlightbackground", COLORS["border"])
    kwargs.setdefault("highlightcolor", COLORS["blue"])
    return tk.Entry(master, **kwargs)


def spinbox(master, **kwargs) -> tk.Spinbox:
    """Flat numeric field with visible focus and unobtrusive arrows."""

    kwargs.setdefault("bg", COLORS["field"])
    kwargs.setdefault("fg", COLORS["text"])
    kwargs.setdefault("insertbackground", COLORS["text"])
    kwargs.setdefault("selectbackground", COLORS["selection"])
    kwargs.setdefault("selectforeground", COLORS["text"])
    kwargs.setdefault("buttonbackground", COLORS["card_alt"])
    kwargs.setdefault("relief", "flat")
    kwargs.setdefault("highlightthickness", 1)
    kwargs.setdefault("highlightbackground", COLORS["border"])
    kwargs.setdefault("highlightcolor", COLORS["blue"])
    kwargs.setdefault("font", (FONT, 10))
    return tk.Spinbox(master, **kwargs)
