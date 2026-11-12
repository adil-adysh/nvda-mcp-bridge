# Build customizations
# Change this file instead of sconstruct or manifest files, whenever possible.

from site_scons.site_tools.NVDATool.typings import AddonInfo, BrailleTables, SymbolDictionaries

# Since some strings in `addon_info` are translatable,
# we need to include them in the .po files.
# Gettext recognizes only strings given as parameters to the `_` function.
# To avoid initializing translations in this module we simply import a "fake" `_` function
# which returns whatever is given to it as an argument.
from site_scons.site_tools.NVDATool.utils import _


# Add-on information variables
addon_info = AddonInfo(
	# add-on Name/identifier, internal for NVDA
	addon_name="nvdaMcpBridge",
	# Add-on summary/title, usually the user visible name of the add-on
	# Translators: Summary/title for this add-on
	# to be shown on installation and add-on information found in add-on store
	addon_summary=_("NVDA MCP Bridge"),
	# Add-on description
	# Translators: Long description to be shown for this add-on on add-on information from add-on store
	addon_description=_("""Enable external applications and AI tools to control NVDA through a simple HTTP interface.

Speak text, read screen content, navigate UI elements, and access window information remotely.
Perfect for building automated accessibility solutions, AI assistants, and integration tools.

The add-on runs in the background on localhost:8765 and safely queues all NVDA operations to the main thread
to prevent crashes. Use it with any tool that can make HTTP requests—no special protocol required."""),
	# version
	addon_version="1.0.0",
	# Brief changelog for this version
	# Translators: what's new content for the add-on version to be shown in the add-on store
	addon_changelog=_("""Version 1.0.0:
- Improved request validation and error handling for bridge endpoints
- Added safer token comparison and clarified API response behavior
- Updated tool/action wiring and documentation for current project structure
- General cleanup for maintainability"""),
	# Author(s)
	addon_author="adil shaikh <ah.shaikh97@gmail.com>",
	# URL for the add-on documentation support
	addon_url="https://github.com/adil-adysh/nvda-mcp-bridge",
	# URL for the add-on repository where the source code can be found
	addon_sourceURL="https://github.com/adil-adysh/nvda-mcp-bridge",
	# Documentation file name
	addon_docFileName="readme.html",
	# Minimum NVDA version supported (e.g. "2019.3.0", minor version is optional)
	addon_minimumNVDAVersion="2026.1.0",
	# Last NVDA version supported/tested (e.g. "2024.4.0", ideally more recent than minimum version)
	addon_lastTestedNVDAVersion="2026.1.0",
	# Add-on update channel (default is None, denoting stable releases,
	# and for development releases, use "dev".)
	# Do not change unless you know what you are doing!
	addon_updateChannel=None,
	# Add-on license such as GPL 2
	addon_license="GPL v2",
	# URL for the license document the ad-on is licensed under
	addon_licenseURL="https://www.gnu.org/licenses/gpl-2.0.html",
)

# Define the python files that are the sources of your add-on.
# You can either list every file (using ""/") as a path separator,
# or use glob expressions.
# For example to include all files with a ".py" extension from the "globalPlugins" dir of your add-on
# the list can be written as follows:
# pythonSources = ["addon/globalPlugins/*.py"]
# For more information on SCons Glob expressions please take a look at:
# https://scons.org/doc/production/HTML/scons-user/apd.html
pythonSources: list[str] = [
	"addon/globalPlugins/nvdaMcp-bridge/*.py",
	"addon/globalPlugins/nvdaMcp-bridge/lib/**/*.py",
	"addon/globalPlugins/nvdaMcp-bridge/nvda_bridge/*.py",
	"addon/globalPlugins/nvdaMcp-bridge/tools/*.py",
]

# Files that contain strings for translation. Usually your python sources
i18nSources: list[str] = pythonSources + ["buildVars.py"]

# Files that will be ignored when building the nvda-addon file
# Paths are relative to the addon directory, not to the root directory of your addon sources.
# You can either list every file (using ""/") as a path separator,
# or use glob expressions.
#
# Bytecode is the one thing that must never ship. Running the add-on from source
# and running the test suite both leave `__pycache__` directories in the add-on
# tree, compiled by whatever Python touched it last (3.13 and 3.14 here — NVDA
# itself is neither), and the packager walks the tree. `Path.match` matches one
# component at a time, so the depth is spelled out rather than written as `**`.
excludedFiles: list[str] = [".DS_Store", "Thumbs.db"]
for _depth in range(1, 8):
	excludedFiles.append("*/" * _depth + "__pycache__/*")
	excludedFiles.append("*/" * _depth + "*.pyc")
	excludedFiles.append("*/" * _depth + "*.pyo")
	excludedFiles.append("*/" * _depth + ".pytest_cache/*")
excludedFiles.append("*.pyc")

# Base language for the NVDA add-on
# If your add-on is written in a language other than english, modify this variable.
# For example, set baseLanguage to "es" if your add-on is primarily written in spanish.
# You must also edit .gitignore file to specify base language files to be ignored.
baseLanguage: str = "en"

# Markdown extensions for add-on documentation
# Most add-ons do not require additional Markdown extensions.
# If you need to add support for markup such as tables, fill out the below list.
# Extensions string must be of the form "markdown.extensions.extensionName"
# e.g. "markdown.extensions.tables" to add tables.
markdownExtensions: list[str] = []

# Custom braille translation tables
# If your add-on includes custom braille tables (most will not), fill out this dictionary.
# Each key is a dictionary named according to braille table file name,
# with keys inside recording the following attributes:
# displayName (name of the table shown to users and translatable),
# contracted (contracted (True) or uncontracted (False) braille code),
# output (shown in output table list),
# input (shown in input table list).
brailleTables: BrailleTables = {}

# Custom speech symbol dictionaries
# Symbol dictionary files reside in the locale folder, e.g. `locale\en`, and are named `symbols-<name>.dic`.
# If your add-on includes custom speech symbol dictionaries (most will not), fill out this dictionary.
# Each key is the name of the dictionary,
# with keys inside recording the following attributes:
# displayName (name of the speech dictionary shown to users and translatable),
# mandatory (True when always enabled, False when not.
symbolDictionaries: SymbolDictionaries = {}
