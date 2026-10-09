"""Phase 6 - Windows toast: text is escaped and encoded; nothing from the message can execute."""

from __future__ import annotations

import base64

from app.platform.toast import build_command, toast_xml


def test_toast_xml_escapes_markup() -> None:
    xml = toast_xml("<b>CPU</b> & 'x'", "</text><script>")
    assert "<b>" not in xml and "&lt;b&gt;CPU&lt;/b&gt; &amp;" in xml and "&lt;/text&gt;&lt;script&gt;" in xml


def test_command_is_encoded_and_quotes_cannot_break_out() -> None:
    cmd = build_command(r"x'); Remove-Item C:\ -Recurse; ('", "body")
    assert cmd[0] == "powershell.exe" and cmd[-2] == "-EncodedCommand"
    script = base64.b64decode(cmd[-1]).decode("utf-16-le")
    # the single quote is doubled inside the PowerShell string literal: it stays text
    assert r"x''); Remove-Item C:\ -Recurse; (''" in script
    assert all("Remove-Item" not in part for part in cmd[:-1])
