"""Compares a program's output with the expected output."""


def check(kind: str, actual: str, expected: str) -> bool:
    """'token' ignores all whitespace differences. 'exact' compares line by line,
    ignoring only trailing spaces and blank lines at the end."""
    if kind == "token":
        return actual.split() == expected.split()
    if kind == "exact":
        return _lines(actual) == _lines(expected)
    raise ValueError(f"unknown checker {kind!r}")


def _lines(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines
