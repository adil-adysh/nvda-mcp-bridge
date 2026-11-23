"""Role and name matching for ``locate`` (implementation plan P5).

This is the pure half of the search: what counts as "a button", and when does a
name match. It has no NVDA imports so the rules can be tested without a screen
reader, and so the two backends — the platform's own UIA search and the bounded
NVDA object walk — share one definition of a match instead of growing two.

Role names are NVDA's ``controlTypes.Role`` names (``BUTTON``, ``CHECKBOX``,
``EDITABLETEXT``, ...), because that is the vocabulary ``observe`` already
returns. Newer NVDA has no ``EDIT`` role, so the aliases below accept the names
people actually type.
"""

from __future__ import annotations

import re
from typing import Optional

#: Every name in NVDA 2026.3's ``controlTypes.Role``. A role outside this set
#: cannot match an object, so it is a caller mistake rather than an empty search.
#: The walk still accepts roles with no UIA control type (``TABLECELL``), which
#: is why this list is the authority instead of the UIA table above.
ROLE_NAMES = frozenset(
	"""
	UNKNOWN WINDOW TITLEBAR PANE DIALOG CHECKBOX RADIOBUTTON STATICTEXT
	EDITABLETEXT BUTTON MENUBAR MENUITEM POPUPMENU COMBOBOX LIST LISTITEM GRAPHIC
	HELPBALLOON TOOLTIP LINK TREEVIEW TREEVIEWITEM TAB TABCONTROL SLIDER
	PROGRESSBAR SCROLLBAR STATUSBAR TABLE TABLECELL TABLECOLUMN TABLEROW
	TABLECOLUMNHEADER TABLEROWHEADER FRAME TOOLBAR DROPDOWNBUTTON CLOCK SEPARATOR
	FORM HEADING HEADING1 HEADING2 HEADING3 HEADING4 HEADING5 HEADING6 PARAGRAPH
	BLOCKQUOTE TABLEHEADER TABLEBODY TABLEFOOTER DOCUMENT ANIMATION APPLICATION
	BOX GROUPING PROPERTYPAGE CANVAS CAPTION CHECKMENUITEM DATEEDITOR ICON
	DIRECTORYPANE EMBEDDEDOBJECT ENDNOTE FOOTER FOOTNOTE GLASSPANE HEADER IMAGEMAP
	INPUTWINDOW LABEL NOTE PAGE RADIOMENUITEM LAYEREDPANE REDUNDANTOBJECT ROOTPANE
	EDITBAR TERMINAL RICHEDIT RULER SCROLLPANE SECTION SHAPE SPLITPANE VIEWPORT
	TEAROFFMENU TEXTFRAME TOGGLEBUTTON BORDER CARET CHARACTER CHART CURSOR DIAGRAM
	DIAL DROPLIST SPLITBUTTON MENUBUTTON DROPDOWNBUTTONGRID MATH GRIP HOTKEYFIELD
	INDICATOR SPINBUTTON SOUND WHITESPACE TREEVIEWBUTTON IPADDRESS DESKTOPICON
	INTERNALFRAME DESKTOPPANE OPTIONPANE COLORCHOOSER FILECHOOSER FILLER MENU
	PANEL PASSWORDEDIT FONTCHOOSER LINE FONTNAME FONTSIZE BOLD ITALIC UNDERLINE
	FGCOLOR BGCOLOR SUPERSCRIPT SUBSCRIPT STYLE INDENT ALIGNMENT ALERT DATAGRID
	DATAITEM HEADERITEM THUMB CALENDAR VIDEO AUDIO CHARTELEMENT DELETED_CONTENT
	INSERTED_CONTENT LANDMARK ARTICLE REGION FIGURE MARKED_CONTENT BUSY_INDICATOR
	COMMENT SUGGESTION DEFINITION SWITCH
	""".split()
)

#: Alias -> canonical NVDA role name. Aliases exist because the caller types what
#: it knows: HTML's "text box", UIA's "Hyperlink", ARIA's "tab", and the role list
#: from an older NVDA all mean something NVDA names differently today.
ROLE_ALIASES = {
	"PUSHBUTTON": "BUTTON",
	"CLICKABLE": "BUTTON",
	"EDIT": "EDITABLETEXT",
	"TEXTFIELD": "EDITABLETEXT",
	"TEXTBOX": "EDITABLETEXT",
	"TEXTINPUT": "EDITABLETEXT",
	"TEXTAREA": "EDITABLETEXT",
	"HYPERLINK": "LINK",
	"ANCHOR": "LINK",
	"RADIO": "RADIOBUTTON",
	"COMBO": "COMBOBOX",
	"DROPDOWN": "COMBOBOX",
	"SELECT": "COMBOBOX",
	"LISTBOX": "LIST",
	"TABITEM": "TAB",
	"PAGETAB": "TAB",
	"CHECK": "CHECKBOX",
	"IMAGE": "GRAPHIC",
	"IMG": "GRAPHIC",
	"HEADERBAR": "TOOLBAR",
	"CONTAINER": "PANE",
	"WINDOWFRAME": "WINDOW",
}

#: Canonical NVDA role -> UIA control type constant, without the ``UIA_`` prefix
#: and ``ControlTypeId`` suffix (the NVDA side resolves the real constant). Used
#: to push the whole match into the platform's provider, which is what makes a
#: search over a 2,346-element window cost one call instead of 2,346 reads.
ROLE_TO_UIA_CONTROL_TYPE = {
	"BUTTON": "Button",
	"TOGGLEBUTTON": "Button",
	"SPLITBUTTON": "SplitButton",
	"LINK": "Hyperlink",
	"EDITABLETEXT": "Edit",
	"PASSWORDEDIT": "Edit",
	"CHECKBOX": "CheckBox",
	"RADIOBUTTON": "RadioButton",
	"COMBOBOX": "ComboBox",
	"LIST": "List",
	"LISTITEM": "ListItem",
	"MENU": "Menu",
	"MENUBAR": "MenuBar",
	"MENUITEM": "MenuItem",
	"TAB": "TabItem",
	"TABCONTROL": "Tab",
	"SLIDER": "Slider",
	"SPINBUTTON": "Spinner",
	"PROGRESSBAR": "ProgressBar",
	"TREEVIEW": "Tree",
	"TREEVIEWITEM": "TreeItem",
	"TABLE": "Table",
	"DATAGRID": "DataGrid",
	"DOCUMENT": "Document",
	"PANE": "Pane",
	"GROUPING": "Group",
	"TOOLBAR": "ToolBar",
	"STATUSBAR": "StatusBar",
	"TOOLTIP": "ToolTip",
	"WINDOW": "Window",
	"DIALOG": "Window",
	"TITLEBAR": "TitleBar",
	"SCROLLBAR": "ScrollBar",
	"GRAPHIC": "Image",
	"HEADER": "Header",
	"HEADERITEM": "HeaderItem",
	"STATICTEXT": "Text",
	"HEADING": "Text",
}

#: UIA control type id -> the role name a match reports. Explicit rather than an
#: inversion of the table above, because several NVDA roles share one UIA type
#: and the report should name the general case ("BUTTON", not "TOGGLEBUTTON").
UIA_CONTROL_TYPE_TO_ROLE = {
	"Button": "BUTTON",
	"SplitButton": "SPLITBUTTON",
	"Hyperlink": "LINK",
	"Edit": "EDITABLETEXT",
	"CheckBox": "CHECKBOX",
	"RadioButton": "RADIOBUTTON",
	"ComboBox": "COMBOBOX",
	"List": "LIST",
	"ListItem": "LISTITEM",
	"Menu": "MENU",
	"MenuBar": "MENUBAR",
	"MenuItem": "MENUITEM",
	"TabItem": "TAB",
	"Tab": "TABCONTROL",
	"Slider": "SLIDER",
	"Spinner": "SPINBUTTON",
	"ProgressBar": "PROGRESSBAR",
	"Tree": "TREEVIEW",
	"TreeItem": "TREEVIEWITEM",
	"Table": "TABLE",
	"DataGrid": "DATAGRID",
	"Document": "DOCUMENT",
	"Pane": "PANE",
	"Group": "GROUPING",
	"ToolBar": "TOOLBAR",
	"StatusBar": "STATUSBAR",
	"ToolTip": "TOOLTIP",
	"Window": "WINDOW",
	"TitleBar": "TITLEBAR",
	"ScrollBar": "SCROLLBAR",
	"Image": "GRAPHIC",
	"Header": "HEADER",
	"HeaderItem": "HEADERITEM",
	"Text": "STATICTEXT",
}

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def normalize_role(value: Optional[str]) -> str:
	"""Return a role name in the form the tables use: ``PUSH_BUTTON`` -> ``PUSHBUTTON``.

	Empty or missing input becomes an empty string, which callers treat as "no
	role filter" rather than as the ``UNKNOWN`` role.
	"""
	if not value:
		return ""
	text = _NON_ALNUM.sub("", str(value)).upper()
	if not text:
		return ""
	return ROLE_ALIASES.get(text, text)


_NORMALIZED_ROLE_NAMES = frozenset(normalize_role(role) for role in ROLE_NAMES)


def validate_role(value: Optional[str]) -> str:
	"""Return the canonical role name, or raise for one NVDA does not have.

	``locate(role="banana")`` used to succeed with zero matches after a full
	window walk, which reads like "no such control" rather than "that is not a
	role". Rejecting it before the search turns a silent miss into a correction.
	"""
	canonical = normalize_role(value)
	if not canonical or canonical in _NORMALIZED_ROLE_NAMES:
		return canonical
	from difflib import get_close_matches

	near = get_close_matches(canonical, sorted(_NORMALIZED_ROLE_NAMES), n=3, cutoff=0.6)
	suggestion = f" Did you mean {', '.join(near)}?" if near else ""
	raise ValueError(
		f"Unknown NVDA role {value!r}. Use a controlTypes.Role name such as "
		f"BUTTON, LINK or EDITABLETEXT.{suggestion}"
	)


def role_matches(wanted: Optional[str], actual: Optional[str]) -> bool:
	"""Whether ``actual`` (an NVDA role name) satisfies the ``wanted`` filter."""
	canonical = normalize_role(wanted)
	if not canonical:
		return True
	return normalize_role(actual) == canonical


def name_matches(wanted: Optional[str], actual: Optional[str], exact: bool = False) -> bool:
	"""Whether ``actual`` satisfies the name filter.

	Substring and case-insensitive by default, because a search for "Save" should
	find "Save document" and a search for "save" should find "Save". ``exact``
	makes it whole-string equality, still case-insensitive: the difference the
	caller cares about is "this control" versus "any control mentioning this".
	"""
	if not wanted:
		return True
	if actual is None:
		return False
	candidate = str(actual)
	if exact:
		return candidate.strip().casefold() == str(wanted).strip().casefold()
	return str(wanted).strip().casefold() in candidate.casefold()


def uia_control_type_name(role: Optional[str]) -> Optional[str]:
	"""Return the UIA control type name (e.g. ``Button``) for an NVDA role.

	``None`` means the role has no UIA equivalent, so a provider-side search
	cannot honour it and the caller must fall back to the bounded walk.
	"""
	return ROLE_TO_UIA_CONTROL_TYPE.get(normalize_role(role))


def role_for_uia_control_type(control_type_name: Optional[str]) -> str:
	"""Map a UIA control type name back to the role name a match reports."""
	if not control_type_name:
		return "UNKNOWN"
	return UIA_CONTROL_TYPE_TO_ROLE.get(control_type_name, "UNKNOWN")
