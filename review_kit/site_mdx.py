"""Make hk, GitHub and git text inert in the generated MDX pages.

Everything that reaches a snapshot page (task titles, comments, documents,
PR titles, branch subjects, curated summaries) was written by an agent or a
person and is untrusted. MDX evaluates ``{...}`` as JavaScript at build
time, parses ``<...>`` as JSX, and treats ``import``/``export`` lines as
ES modules; markdown adds raw links such as ``[x](javascript:...)``.

* ``inline`` -- one line with no markdown structure at all (table cells,
  headings, list items): every significant character is backslash-escaped,
  ``<`` becomes ``&lt;``.
* ``block`` -- multi-line markdown whose structure (paragraphs, lists,
  emphasis, tables, fenced code) is kept, with the MDX and link syntax
  neutralised. Fenced code is re-emitted with a fence this module chooses,
  so the parser never sees a fence the text opened itself; a code span is
  kept only when its content is harmless, otherwise it becomes ``<code>``
  with entity-escaped children.
* ``expr`` -- a JavaScript string literal inside ``{...}``, for JSX
  children and props: ``json.dumps`` plus ``\\u`` escapes for the characters
  MDX and GFM tables scan for, so the expression is always one string literal.
"""
import json
import re

# Characters that start MDX or markdown syntax inside a line. A backslash
# escape (CommonMark: any ASCII punctuation) makes each one literal text; the
# MDX parser documents `\{` as the literal form (`<` is an entity, see _escape).
_INLINE_SPECIAL = "\\`{}<>[]|*_~#!$&"
# Block text keeps * _ ~ # ! | > so emphasis, headings, tables and
# blockquotes still render; it never keeps what can execute or link.
_BLOCK_SPECIAL = "\\`{}<[]$&"
_INLINE_RE = re.compile("[%s]" % re.escape(_INLINE_SPECIAL))
_BLOCK_RE = re.compile("[%s]" % re.escape(_BLOCK_SPECIAL))
# A ::: line opens a nimbus admonition (container directive).
_DIRECTIVE = re.compile(r"^(\s*):")
# A list marker or ordered-list number at the start of a cell or item.
_LEADING_MARKER = re.compile(r"^(\d{1,9})([.)])|^([-+=])")
# An ES module statement is recognised at the start of a line.
_ESM = re.compile(r"^(\s*)(import|export)\b")
_FENCE_OPEN = re.compile(r"^(`{3,}|~{3,})(.*)$")
_FENCE_LIKE = re.compile(r"^\s*(`{3,}|~{3,})")
# Whole backtick runs only, as CommonMark counts them: a span opens with a
# run of n and closes at the next run of exactly n.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_SAFE_LANG = re.compile(r"^[A-Za-z0-9_+-]{1,30}$")
# A code span is kept as backtick markdown only when its content would be
# harmless even if the parser read the backticks differently.
_CODE_UNSAFE = re.compile(r"[{}<>\[\]&\\$|`]")
_JS_ESCAPES = {"{": "\\u007b", "}": "\\u007d", "<": "\\u003c", ">": "\\u003e", "|": "\\u007c",
               "`": "\\u0060", " ": "\\u2028", " ": "\\u2029"}


def _escape(match):
    # `<` is the one character a backslash does not cover: nimbus checks the
    # raw source for `<PascalCase` component tags before markdown runs, so
    # `\\<Type />` would fail the build. The entity is never a tag.
    char = match.group(0)
    return "&lt;" if char == "<" else "\\" + char


def _text(value):
    return "" if value is None else str(value)


def inline(value):
    """One-line literal text: no MDX, no HTML, no markdown structure."""
    text = re.sub(r"\s+", " ", _text(value)).strip()
    text = _INLINE_RE.sub(_escape, text)
    match = _LEADING_MARKER.match(text)
    if match:
        if match.group(1):
            text = match.group(1) + "\\" + match.group(2) + text[match.end():]
        else:
            text = "\\" + text
    return _ESM.sub(lambda m: m.group(1) + "&#%d;" % ord(m.group(2)[0]) + m.group(2)[1:],
                    text)


def code_text(value):
    """Children of a ``<code>`` JSX element: every special character as an entity.

    Backslash escapes are not enough inside JSX children: nimbus scans them
    for ``<Tag`` names before markdown runs, so ``\\<Type>`` fails the build.
    """
    text = re.sub(r"\s+", " ", _text(value))
    return re.sub(r"[%s>]" % re.escape(_INLINE_SPECIAL),
                  lambda m: "&#%d;" % ord(m.group(0)), text)


def js_string(value):
    """A JavaScript string literal that MDX's expression scanner reads whole."""
    encoded = json.dumps(_text(value), ensure_ascii=False)
    return "".join(_JS_ESCAPES.get(ch, ch) for ch in encoded)


def expr(value):
    """``{"..."}``: the text as a JSX child or prop value."""
    return "{" + js_string(value) + "}"


def mermaid(source):
    """A diagram the site's mermaid script renders (mermaid runs in strict mode)."""
    return '<pre class="mermaid">' + expr(_text(source).strip("\n")) + "</pre>"


def _block_line(line, linker):
    if _FENCE_LIKE.match(line):
        # Looks like a fence but is not one (indented, or a backtick in its
        # info string): no code spans, so no backtick run survives at all.
        return _block_text(line, linker).replace("~", "\\~")
    pieces = []
    last = 0
    for match in _CODE_SPAN.finditer(line):
        pieces.append(_block_text(line[last:match.start()], linker))
        content = match.group(2)
        if _CODE_UNSAFE.search(content):
            pieces.append("<code>" + code_text(content) + "</code>")
        else:
            pieces.append(match.group(0))
        last = match.end()
    pieces.append(_block_text(line[last:], linker))
    out = _DIRECTIVE.sub(lambda m: m.group(1) + "\\:", "".join(pieces))
    return _ESM.sub(lambda m: m.group(1) + "&#%d;" % ord(m.group(2)[0]) + m.group(2)[1:], out)


def _block_text(text, linker):
    text = _BLOCK_RE.sub(_escape, text)
    return linker(text) if linker else text


def _fence(lang, lines):
    longest = max([len(run) for line in lines for run in re.findall(r"`+", line)] or [0])
    ticks = "`" * max(3, longest + 1)
    return [ticks + lang] + lines + [ticks]


def block(value, linker=None):
    """Multi-line markdown with structure kept and MDX/HTML/links neutralised.

    ``linker`` receives escaped text runs (never code) and may insert
    markdown links whose targets the caller controls.
    """
    lines = _text(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    i = 0
    while i < len(lines):
        opened = _FENCE_OPEN.match(lines[i])
        if opened and not (opened.group(1)[0] == "`" and "`" in opened.group(2)):
            marker = opened.group(1)
            info = opened.group(2).strip().split()
            lang = info[0] if info and _SAFE_LANG.match(info[0]) else ""
            close = re.compile(r"^%s{%d,}\s*$" % (re.escape(marker[0]), len(marker)))
            j = i + 1
            while j < len(lines) and not close.match(lines[j]):
                j += 1
            code = lines[i + 1:j]
            out.append("")
            if lang == "mermaid":
                out.append(mermaid("\n".join(code)))
            else:
                out.extend(_fence(lang, code))
            out.append("")
            i = j + 1
            continue
        out.append(_block_line(lines[i], linker))
        i += 1
    return "\n".join(out).strip("\n")


def frontmatter_str(value):
    """A YAML scalar: JSON strings are valid YAML and need no further quoting."""
    return json.dumps(re.sub(r"\s+", " ", _text(value)).strip(), ensure_ascii=False)
