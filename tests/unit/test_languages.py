"""What contestants and agents are told about the languages must match how the worker runs them."""

import re

from api import mcp, pages
from worker.judge import CPP_FLAGS


def test_the_cpp_standard_shown_is_the_one_compiled():
    standard = next(f for f in CPP_FLAGS if f.startswith("-std=")).removeprefix("-std=c++")
    shown = f"C++{standard}"
    assert pages.LANGUAGES["cpp"] == shown
    assert shown in mcp.INSTRUCTIONS
    assert set(re.findall(r"C\+\+\d+", mcp.INSTRUCTIONS)) == {shown}
    assert shown in mcp.Language.__metadata__[0].description  # the tools' language argument
