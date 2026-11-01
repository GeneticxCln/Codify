"""The HTML reader behind `fetch_page`: a document tree, text extraction and a small CSS subset.

This module replaces Scrapling's `Selector`, which was the only part of Scrapling this engine used. The reasons are
the same ones `docs/12` gives for refusing the rest of that package, applied to the parser too: it is a dependency
whose own defaults open a SQLite file, and the engine needs three things from an HTML document — its text, its
title, and the links in a chosen part of it. Those are a tree walk, not a scraping framework.

What is supported, deliberately:

* **A document tree.** `html.parser` from the standard library, with void elements, implicit closes and a matching
  end tag found by walking back up the open stack. Comments, declarations and processing instructions are dropped.
  This is not a browser's recovery algorithm: a document bad enough to confuse it comes back with less text, never
  with an exception.
* **Text extraction.** The text nodes under a node, each stripped, joined with a newline. `script`, `style`,
  `noscript`, `template`, `svg` and `head` contribute nothing, so a page's code is never mistaken for its prose and
  the title is reported on its own line instead of being repeated in the body.
* **A CSS subset, checked.** Type, `*`, `.class`, `#id`, `[attr]`, `[attr=value]` (and `~=`, `|=`, `^=`, `$=`, `*=`)
  compounds, joined by descendant or child combinators, with comma groups. A selector outside the subset is a
  `SelectorError` rather than a silent match-nothing: the model asked a question and is owed the answer that its
  selector was not understood.
* **Nothing executable.** No script is run, no entity is resolved to a character reference that is then expanded,
  and no network or filesystem call is made here. The input is a string and the output is a tree.

`select` and `text` are the two calls `web_fetch.extract` makes. The rest is public because a test can hold each
part on its own, which is how the parser's limits are kept from becoming folklore.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: Elements that never have children, so an end tag is not expected and a following start tag is not nested.
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr",
})

#: Elements whose text is not the page's. `head` goes too: the title is read on its own and must not appear twice.
IGNORED_TEXT = frozenset({"script", "style", "noscript", "template", "svg", "head"})

#: A selector that is not in the supported subset. `web_fetch.extract` turns it into the sentence the model reads.
class SelectorError(ValueError):
    """The selector is not one this reader accepts."""


@dataclass
class Node:
    """One element, or the document root (`tag == "#document"`), with its children in document order."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = None


class _Builder(HTMLParser):
    """`html.parser`, keeping the tree instead of printing events."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]

    def _append(self, tag: str, attrs: list[tuple[str, str | None]]) -> Node:
        node = Node(tag.lower(), {name.lower(): value or "" for name, value in attrs})
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._append(tag, attrs)
        if node.tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.lower()
        # A stray end tag is ignored. A real one closes the nearest open element of that name, which is what a
        # browser does with the common case of an unclosed <p> before a </div>.
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == wanted:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def parse(markup: str) -> Node:
    """The document root for `markup`. Never raises for bad HTML: a tree that is less complete is still a tree."""
    builder = _Builder()
    builder.feed(markup)
    builder.close()
    return builder.root


def iter_nodes(node: Node) -> Iterator[Node]:
    """Every element under `node`, depth first, in document order."""
    for child in node.children:
        if isinstance(child, Node):
            yield child
            yield from iter_nodes(child)


def find_first(node: Node, tag: str) -> Node | None:
    wanted = tag.lower()
    return next((found for found in iter_nodes(node) if found.tag == wanted), None)


def find_all(node: Node, tag: str) -> Iterator[Node]:
    wanted = tag.lower()
    for found in iter_nodes(node):
        if found.tag == wanted:
            yield found


def text(node: Node) -> str:
    """The node's own text: each text node stripped, joined with a newline, with ignored subtrees left out."""
    if node.tag in IGNORED_TEXT:
        return ""
    pieces: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            stripped = child.strip()
            if stripped:
                pieces.append(stripped)
        else:
            nested = text(child)
            if nested:
                pieces.append(nested)
    return "\n".join(pieces)


# ── the CSS subset ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Compound:
    tag: str | None
    ids: tuple[str, ...]
    classes: tuple[str, ...]
    attrs: tuple[tuple[str, str, str], ...]  # (name, operator, value); operator "" means "is present"


@dataclass(frozen=True)
class _Part:
    compound: _Compound
    #: How this part relates to the one before it: "" for the first, " " for a descendant, ">" for a child.
    combinator: str


def _split_groups(selector: str) -> list[str]:
    groups: list[str] = []
    start = 0
    depth = 0
    quote = ""
    for index, char in enumerate(selector):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise SelectorError("a `]` without a matching `[`")
        elif char == "," and depth == 0:
            groups.append(selector[start:index])
            start = index + 1
    if depth != 0:
        raise SelectorError("a `[` without a matching `]`")
    groups.append(selector[start:])
    if any(not group.strip() for group in groups):
        raise SelectorError("an empty part in a comma-separated selector")
    return groups


def _parse_compound(text: str) -> _Compound:
    if not text:
        raise SelectorError("an empty compound selector")
    index = 0
    tag: str | None = None
    if text[0] == "*":
        # `*` is a compound of its own, not the absence of one: a bare `*` has to pass the
        # "no element, id, class or attribute" check below, while `.warning` has a class.
        tag = "*"
        index = 1
    elif text[0].isalpha():
        end = index
        while end < len(text) and (text[end].isalnum() or text[end] in "-_"):
            end += 1
        tag = text[:end].lower()
        index = end
    ids: list[str] = []
    classes: list[str] = []
    attrs: list[tuple[str, str, str]] = []

    def name_at(start: int) -> tuple[str, int]:
        end = start
        while end < len(text) and (text[end].isalnum() or text[end] in "-_:"):
            end += 1
        if end == start:
            raise SelectorError(f"a name was expected at {text[start:]!r}")
        return text[start:end], end

    while index < len(text):
        char = text[index]
        if char == ".":
            value, index = name_at(index + 1)
            classes.append(value)
        elif char == "#":
            value, index = name_at(index + 1)
            ids.append(value)
        elif char == "[":
            close = index + 1
            quote = ""
            while close < len(text):
                if quote:
                    if text[close] == quote:
                        quote = ""
                elif text[close] in "\"'":
                    quote = text[close]
                elif text[close] == "]":
                    break
                close += 1
            if close >= len(text):
                raise SelectorError("a `[` without a matching `]`")
            body = text[index + 1:close].strip()
            if not body:
                raise SelectorError("an empty attribute selector")
            operator = ""
            name = body
            value = ""
            for candidate in ("~=", "|=", "^=", "$=", "*=", "="):
                if candidate in body:
                    name, _, raw = body.partition(candidate)
                    operator = candidate
                    value = raw.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    break
            name = name.strip().lower()
            if not name or any(c in name for c in " \t\r\n"):
                raise SelectorError("an attribute name was expected")
            if operator and not value:
                raise SelectorError("an attribute value was expected")
            attrs.append((name, operator, value))
            index = close + 1
        else:
            raise SelectorError(f"{char!r} is not supported")
    if tag is None and not ids and not classes and not attrs:
        raise SelectorError("a selector with no element, id, class or attribute")
    return _Compound(tag, tuple(ids), tuple(classes), tuple(attrs))


def _parse_group(group: str) -> list[_Part]:
    parts: list[_Part] = []
    current: list[str] = []
    combinator = ""
    index = 0
    while index < len(group):
        char = group[index]
        if char in " \t\r\n" or char == ">":
            if current:
                parts.append(_Part(_parse_compound("".join(current)), combinator if parts else ""))
                current = []
                combinator = " "
            if char == ">":
                combinator = ">"
            index += 1
            continue
        if char == "[":
            close = index + 1
            quote = ""
            while close < len(group):
                if quote:
                    if group[close] == quote:
                        quote = ""
                elif group[close] in "\"'":
                    quote = group[close]
                elif group[close] == "]":
                    break
                close += 1
            if close >= len(group):
                raise SelectorError("a `[` without a matching `]`")
            current.append(group[index:close + 1])
            index = close + 1
            continue
        current.append(char)
        index += 1
    if current:
        parts.append(_Part(_parse_compound("".join(current)), combinator if parts else ""))
    if not parts:
        raise SelectorError("an empty selector")
    if parts[0].combinator:
        raise SelectorError("a selector cannot start with a combinator")
    return parts


def parse_selector(selector: str) -> list[list[_Part]]:
    """The parsed form of a selector, or `SelectorError`. Public so a test can check the refusals."""
    text = selector.strip()
    if not text:
        raise SelectorError("an empty selector")
    parsed: list[list[_Part]] = []
    for group in _split_groups(text):
        parts = _parse_group(group.strip())
        if not parts:
            raise SelectorError("an empty selector")
        parsed.append(parts)
    return parsed


def _matches_attr(node: Node, name: str, operator: str, value: str) -> bool:
    actual = node.attrs.get(name)
    if actual is None:
        return False
    if not operator:
        return True
    if operator == "=":
        return actual == value
    if operator == "~=":
        return value in actual.split()
    if operator == "|=":
        return actual == value or actual.startswith(value + "-")
    if operator == "^=":
        return actual.startswith(value)
    if operator == "$=":
        return actual.endswith(value)
    return value in actual


def _matches_compound(node: Node, compound: _Compound) -> bool:
    if node.tag == "#document":
        return False
    if compound.tag not in (None, "*") and node.tag != compound.tag:
        return False
    if any(node.attrs.get("id") != wanted for wanted in compound.ids):
        return False
    classes = set(node.attrs.get("class", "").split())
    if any(wanted not in classes for wanted in compound.classes):
        return False
    return all(_matches_attr(node, name, operator, value) for name, operator, value in compound.attrs)


def _matches_parts(node: Node, parts: list[_Part]) -> bool:
    if not _matches_compound(node, parts[-1].compound):
        return False
    current: Node | None = node
    for index in range(len(parts) - 1, 0, -1):
        relation = parts[index].combinator
        wanted = parts[index - 1].compound
        current = current.parent if current is not None else None
        if relation == ">":
            if current is None or not _matches_compound(current, wanted):
                return False
            continue
        while current is not None and not _matches_compound(current, wanted):
            current = current.parent
        if current is None:
            return False
    return True


def select(node: Node, selector: str) -> list[Node]:
    """The elements under `node` matching `selector`, in document order. `node` itself is included if it matches.

    Raises `SelectorError` for a selector outside the subset. Every group is evaluated; an element matched by two
    groups is returned once.
    """
    groups = parse_selector(selector)
    candidates = [node] if node.tag != "#document" else []
    candidates.extend(iter_nodes(node))
    found: list[Node] = []
    seen: set[int] = set()
    for candidate in candidates:
        if id(candidate) in seen:
            continue
        if any(_matches_parts(candidate, parts) for parts in groups):
            seen.add(id(candidate))
            found.append(candidate)
    return found
