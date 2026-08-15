"""TE3 — XSS / HTML escaping regression tests (pure logic, no OCP geometry).

Validates the two shared escaping helpers in cad_core that every generated
preview/HTML page funnels user-derived strings through:

  * html_escape_text  -> html.escape(quote=True)  (text + attribute safe)
  * json_for_script    -> json.dumps(ensure_ascii=False) with the extra
                          ``</script>`` / angle-bracket / ampersand / line-
                          separator neutralisation needed inside a <script> block.

These tests must stay GREEN and FAST: they only import cad_core (which pulls
in OCP) but exercise no geometry, so they are the canary for "did pytest
collect and run at all".
"""
from __future__ import annotations

from cad_core import html_escape_text, json_for_script


# --------------------------------------------------------------------------
# html_escape_text
# --------------------------------------------------------------------------
def test_escape_script_tag_in_text():
    payload = "<script>alert(1)</script>"
    out = html_escape_text(payload)
    assert "<script>" not in out
    assert "</script>" not in out
    assert out == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_escape_quotes_and_ampersand():
    # quote=True must escape both double and single quotes; & becomes &amp;
    # (so the escaped output legitimately CONTAINS '&' as the entity prefix --
    # what matters is the raw input substring does not survive).
    out = html_escape_text('" & < >')
    assert '"' not in out           # quote escaped
    assert "<" not in out           # angle brackets escaped
    assert ">" not in out
    assert '" & < >' not in out     # raw input must not survive
    assert out == "&quot; &amp; &lt; &gt;"


def test_escape_filenames_with_spaces_and_brackets():
    name = "part (2) [final].step"
    out = html_escape_text(name)
    # parentheses are harmless; only the dangerous chars get escaped
    assert "(" in out and ")" in out
    assert "<" not in out and ">" not in out


# --------------------------------------------------------------------------
# json_for_script  (must be safe to embed inside <script>...</script>)
# --------------------------------------------------------------------------
def test_json_no_raw_closing_script_tag():
    payload = {"x": "</script><script>alert(1)</script>"}
    out = json_for_script(payload)
    # The bare closing tag must never appear in the emitted script source,
    # otherwise it would terminate the surrounding <script> block. (The '<'
    # is turned into \u003c, so the literal "</script>" cannot occur.)
    assert "</script>" not in out
    assert "<" not in out
    assert ">" not in out


def test_json_escapes_angle_brackets_and_ampersand():
    out = json_for_script({"a": "<>&"})
    assert "<" not in out
    assert ">" not in out
    assert "&" not in out
    assert "\\u003c" in out
    assert "\\u003e" in out
    assert "\\u0026" in out


def test_json_neutralises_line_separators():
    # U+2028 / U+2029 are illegal in JS string literals and must be escaped.
    out = json_for_script({"a": "line1\u2028line2\u2029end"})
    assert "\u2028" not in out
    assert "\u2029" not in out
    assert "\\u2028" in out
    assert "\\u2029" in out


def test_json_preserves_unicode_and_is_valid_json():
    import json
    obj = {"name": "中文名.step", "n": 3}
    out = json_for_script(obj)
    # round-trips back to the original object
    assert json.loads(out) == obj
    assert "中文名.step" in out
