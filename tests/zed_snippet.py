"""Python port of Zed's snippet parser (crates/snippet/src/snippet.rs).

Used by the tests to check that every snippet parses the way Zed parses it, and to
expand snippets to the text Zed inserts when the user accepts every default.
"""


class SnippetError(ValueError):
    pass


def parse(source):
    """Return (text, tabstops) where tabstops maps index -> list of (start, end)."""
    text = []
    tabstops = {}
    rest = _parse_snippet(source, False, text, tabstops)
    assert rest == ""
    return "".join(text), tabstops


def _len(text):
    return sum(len(chunk) for chunk in text)


def _parse_snippet(source, nested, text, tabstops):
    while source:
        c = source[0]
        if c == "$":
            source = _parse_tabstop(source[1:], text, tabstops)
        elif c == "\\":
            source = source[1:]
            if source and source[0] in "$\\}":
                text.append(source[0])
                source = source[1:]
            else:
                text.append("\\")
        elif c == "}":
            if nested:
                return source
            text.append("}")
            source = source[1:]
        else:
            end = min((i for i in (source.find(ch) for ch in "}$\\") if i >= 0), default=len(source))
            text.append(source[:end])
            source = source[end:]
    return ""


def _parse_int(source):
    n = 0
    while n < len(source) and source[n].isdigit():
        n += 1
    if n == 0:
        raise SnippetError(f"expected an integer at {source[:20]!r}")
    return int(source[:n]), source[n:]


def _parse_choices(source, text):
    choices, current, found_default = [], "", False
    while True:
        if not source:
            return "", choices
        c = source[0]
        if c == "\\":
            source = source[1:]
            if source:
                current += source[0]
                source = source[1:]
        elif c == ",":
            if not found_default:
                text.append(current)
                found_default = True
            choices.append(current)
            current = ""
            source = source[1:]
        elif c == "|":
            if not found_default:
                text.append(current)
            choices.append(current)
            return source[1:], choices
        else:
            current += c
            source = source[1:]


def _parse_tabstop(source, text, tabstops):
    start = _len(text)
    if source.startswith("{"):
        index, source = _parse_int(source[1:])
        if source.startswith("|"):
            source, _ = _parse_choices(source[1:], text)
        if source.startswith(":"):
            source = _parse_snippet(source[1:], True, text, tabstops)
        if not source.startswith("}"):
            raise SnippetError("expected a closing brace")
        source = source[1:]
    else:
        index, source = _parse_int(source)
    tabstops.setdefault(index, []).append((start, _len(text)))
    return source


def body_source(snippet):
    body = snippet["body"]
    return "\n".join(body) if isinstance(body, list) else body


def expand(snippet):
    return parse(body_source(snippet))[0]
