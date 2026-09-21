"""Scripted macros: a small language for acting on orders as events arrive.

`check(text)` is the way in: the parsed `Macro` and every `Diagnostic`,
the same ones the editor underlines. `MacroRunner` arms a checked macro
against an engine. `vocabulary()` is the language's words as data.
"""

from .check import check, errors
from .nodes import Diagnostic, Macro
from .parser import parse
from .runner import Run, MacroError, MacroRunner
from .vocab import vocabulary

__all__ = ["check", "errors", "parse", "vocabulary", "Diagnostic", "Macro",
           "MacroRunner", "MacroError", "Run"]
