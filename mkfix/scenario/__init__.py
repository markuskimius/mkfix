"""Scripted scenarios: a small language for acting on orders as events arrive.

`check(text)` is the way in: the parsed `Scenario` and every `Diagnostic`,
the same ones the editor underlines. `ScenarioRunner` arms a checked script
against an engine. `vocabulary()` is the language's words as data.
"""

from .check import check, errors
from .nodes import Diagnostic, Scenario
from .parser import parse
from .runner import Run, ScenarioError, ScenarioRunner
from .vocab import vocabulary

__all__ = ["check", "errors", "parse", "vocabulary", "Diagnostic", "Scenario",
           "ScenarioRunner", "ScenarioError", "Run"]
