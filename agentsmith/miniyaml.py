"""Just enough YAML to read a CI workflow, without a dependency.

agentsmith installs anywhere Python runs because it depends on nothing, and
PyYAML would be the first dependency. GitHub Actions workflows use a small,
regular subset of YAML — block mappings and sequences, literal and folded
scalars, quoted strings, short flow collections — so that subset is parsed
here.

It is tolerant rather than strict. A line it does not understand is skipped,
not fatal: callers ask narrow questions (which events trigger this, which
commands run, which matrix values exist) and a malformed corner of a workflow
should cost one answer, not the run. Anchors, aliases, tags, and complex keys
are rare in workflows; where they appear the affected value comes back as
``None`` rather than as a guess.

Scalars are always returned as strings. ``python-version: 3.10`` is the string
``"3.10"`` here, where a conforming YAML loader would hand back the float
``3.1`` — the classic way a version matrix gets silently mangled.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["parse"]

_BLOCK_SCALAR_RE = re.compile(r"^([|>])([+-]?)(\d?)([+-]?)$")
_PLAIN_KEY_RE = re.compile(r"^([\w][\w.\-/]*)\s*:(?:\s+(.*))?$")


def parse(text: str) -> Any:
    """Parse a YAML document into dicts, lists, and strings."""
    return _Parser(text).document()


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.i = 0

    # -- navigation --------------------------------------------------------

    def document(self) -> Any:
        indent = self._next_indent()
        if indent is not None and self.lines[self.i].strip() == "---":
            self.i += 1
            indent = self._next_indent()
        if indent is None:
            return None
        return self._block(indent)

    def _next_indent(self) -> Optional[int]:
        """Skip blank and comment lines; return the next line's indent."""
        while self.i < len(self.lines):
            stripped = self.lines[self.i].strip()
            if stripped and not stripped.startswith("#"):
                line = self.lines[self.i]
                return len(line) - len(line.lstrip(" "))
            self.i += 1
        return None

    def _block(self, indent: int) -> Any:
        content = self.lines[self.i][indent:]
        if _is_sequence_item(content):
            return self._sequence(indent)
        return self._mapping(indent)

    # -- block collections -------------------------------------------------

    def _mapping(self, indent: int) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        while True:
            n = self._next_indent()
            if n is None or n < indent:
                break
            if n > indent:
                # A deeper line nobody claimed — usually the continuation of a
                # multi-line plain scalar such as a long `if:`. Not needed.
                self.i += 1
                continue
            content = _strip_comment(self.lines[self.i][n:])
            if _is_sequence_item(content):
                break
            key, rest = _split_key(content)
            self.i += 1
            if key is None:
                continue
            result[key] = self._value(rest or "", indent)
        return result

    def _sequence(self, indent: int) -> List[Any]:
        items: List[Any] = []
        while True:
            n = self._next_indent()
            if n is None or n < indent:
                break
            if n > indent:
                self.i += 1
                continue
            content = self.lines[self.i][n:]
            if not _is_sequence_item(content):
                break
            after = content[1:]
            stripped = _strip_comment(after.strip())
            if not stripped:
                self.i += 1
                deeper = self._next_indent()
                items.append(
                    self._block(deeper)
                    if deeper is not None and deeper > indent
                    else None
                )
                continue
            if _is_sequence_item(stripped) or (
                stripped[0] not in "[{" and _split_key(stripped)[0] is not None
            ):
                # `- key: value` opens a mapping whose keys align one column
                # past the dash. Re-read this line as if the dash were a space.
                inner = n + 1 + (len(after) - len(after.lstrip(" ")))
                self.lines[self.i] = " " * inner + after.lstrip(" ")
                items.append(self._block(inner))
                continue
            self.i += 1
            items.append(self._value(stripped, n))
        return items

    # -- values ------------------------------------------------------------

    def _value(self, rest: str, indent: int) -> Any:
        rest = rest.strip()
        if rest.startswith("&"):
            # An anchor name; the value after it is still the value.
            rest = rest.partition(" ")[2].strip()
        if _BLOCK_SCALAR_RE.match(rest):
            return self._block_scalar(indent, rest)
        if not rest:
            n = self._next_indent()
            if n is None:
                return None
            if n > indent:
                return self._block(n)
            if n == indent and _is_sequence_item(self.lines[self.i][n:]):
                # YAML allows a sequence at the same indent as its key.
                return self._sequence(n)
            return None
        if rest[0] in "[{" and not _balanced(rest):
            parts = [rest]
            while self.i < len(self.lines) and not _balanced(" ".join(parts)):
                parts.append(_strip_comment(self.lines[self.i].strip()))
                self.i += 1
            rest = " ".join(parts)
        return _scalar(rest)

    def _block_scalar(self, indent: int, header: str) -> str:
        match = _BLOCK_SCALAR_RE.match(header)
        folded = bool(match and match.group(1) == ">")
        explicit = match.group(3) if match else ""
        content_indent = indent + int(explicit) if explicit else None
        collected: List[str] = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip():
                collected.append("")
                self.i += 1
                continue
            n = len(line) - len(line.lstrip(" "))
            if content_indent is None:
                if n <= indent:
                    break
                content_indent = n
            if n < content_indent:
                break
            collected.append(line[content_indent:])
            self.i += 1
        while collected and not collected[-1]:
            collected.pop()
        if not folded:
            return "\n".join(collected)
        # Folded: single newlines become spaces, blank lines become newlines.
        paragraphs: List[str] = []
        current: List[str] = []
        for line in collected:
            if line:
                current.append(line)
            elif current:
                paragraphs.append(" ".join(current))
                current = []
        if current:
            paragraphs.append(" ".join(current))
        return "\n".join(paragraphs)


# -- line-level helpers -----------------------------------------------------


def _is_sequence_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _split_key(content: str) -> Tuple[Optional[str], Optional[str]]:
    """``key: rest`` into its parts, or ``(None, None)`` if not a key line."""
    if not content:
        return None, None
    if content[0] in "\"'":
        end = _closing_quote(content, 0)
        if end is None:
            return None, None
        tail = content[end + 1 :]
        if tail.startswith(":") and (len(tail) == 1 or tail[1] == " "):
            return _unquote(content[: end + 1]), tail[1:]
        return None, None
    match = _PLAIN_KEY_RE.match(content)
    if not match:
        return None, None
    return match.group(1), match.group(2) or ""


def _strip_comment(text: str) -> str:
    """Drop a trailing ``# comment``, respecting quoted strings.

    A ``#`` only starts a comment at the start of the text or after
    whitespace, so ``https://host/#anchor`` survives, and a quote only opens a
    quoted region at the start of a token, so the apostrophe in ``it's`` does
    not swallow the rest of the line.
    """
    quote = None
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\" and quote == '"':
                i += 2
                continue
            if char == quote:
                if quote == "'" and text[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif char in "\"'" and (i == 0 or text[i - 1] in " \t[{,:"):
            quote = char
        elif char == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _closing_quote(text: str, start: int) -> Optional[int]:
    quote = text[start]
    i = start + 1
    while i < len(text):
        if quote == '"' and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return None


def _unquote(text: str) -> str:
    body = text[1:-1]
    if text[0] == "'":
        return body.replace("''", "'")
    return (
        body.replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\\\", "\\")
    )


def _balanced(text: str) -> bool:
    depth = 0
    quote = None
    for i, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'" and (i == 0 or text[i - 1] in " [{,:"):
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth <= 0


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    if text[0] in "\"'":
        end = _closing_quote(text, 0)
        return _unquote(text[: end + 1]) if end is not None else text
    if text[0] in "[{":
        try:
            value, _ = _flow(text, 0)
            return value
        except (IndexError, ValueError):
            return None
    if text[0] == "*":
        return None  # an alias; resolving anchors is out of scope
    return text


# -- flow collections -------------------------------------------------------


def _flow(text: str, i: int) -> Tuple[Any, int]:
    i = _skip_spaces(text, i)
    opener = text[i]
    if opener == "[":
        items: List[Any] = []
        i += 1
        while True:
            i = _skip_spaces(text, i)
            if text[i] == "]":
                return items, i + 1
            value, i = _flow(text, i)
            items.append(value)
            i = _skip_spaces(text, i)
            if text[i] == ",":
                i += 1
            elif text[i] != "]":
                raise ValueError("unexpected %r in flow sequence" % text[i])
    if opener == "{":
        mapping: Dict[str, Any] = {}
        i += 1
        while True:
            i = _skip_spaces(text, i)
            if text[i] == "}":
                return mapping, i + 1
            key, i = _flow_plain(text, i, key=True)
            i = _skip_spaces(text, i)
            value: Any = None
            if text[i] == ":":
                value, i = _flow(text, i + 1)
                i = _skip_spaces(text, i)
            mapping[str(key)] = value
            if text[i] == ",":
                i += 1
            elif text[i] != "}":
                raise ValueError("unexpected %r in flow mapping" % text[i])
    return _flow_plain(text, i, key=False)


def _flow_plain(text: str, i: int, key: bool) -> Tuple[Any, int]:
    """A scalar inside ``[...]`` or ``{...}``: quoted, or plain up to a delimiter."""
    i = _skip_spaces(text, i)
    if text[i] in "\"'":
        end = _closing_quote(text, i)
        if end is None:
            raise ValueError("unterminated string")
        return _unquote(text[i : end + 1]), end + 1
    start = i
    while i < len(text):
        if text.startswith("${{", i):
            close = text.find("}}", i)
            i = len(text) if close == -1 else close + 2
            continue
        char = text[i]
        if char in ",]}":
            break
        if key and char == ":" and (i + 1 == len(text) or text[i + 1] in " ,]}"):
            break
        i += 1
    return _scalar(text[start:i]), i


def _skip_spaces(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i
