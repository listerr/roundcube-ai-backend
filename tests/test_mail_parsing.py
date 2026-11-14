import email
import email.policy
from email.message import EmailMessage

from rcai.mail import attachments, cut, html_to_text, part_text


def make_message():
    m = EmailMessage()
    m["From"] = "Alice <alice@example.org>"
    m["To"] = "rob@example.org"
    m["Subject"] = "Report"
    m.set_content("Plain body")
    m.add_alternative("<html><body><p>HTML <b>body</b></p></body></html>", subtype="html")
    m.add_attachment(b"col1,col2\n1,2\n", maintype="text", subtype="csv", filename="data.csv")
    m.add_attachment(b"\x89PNG....", maintype="image", subtype="png", filename="pic.png")
    return email.message_from_bytes(m.as_bytes(), policy=email.policy.default)


def test_body_and_attachments():
    msg = make_message()
    body = msg.get_body(preferencelist=("plain", "html"))
    assert part_text(body) == "Plain body"
    parts = attachments(msg, body)
    assert [p.get_filename() for p in parts] == ["data.csv", "pic.png"]
    assert part_text(parts[0]).startswith("col1,col2")


def test_html_to_text():
    html = """<html><head><style>p{}</style><title>x</title></head>
    <body><h1>Title</h1><p>One&nbsp;two &amp; three</p><ul><li>a</li><li>b</li></ul>
    <script>alert(1)</script><table><tr><td>c1</td><td>c2</td></tr></table></body></html>"""
    text = html_to_text(html)
    assert "alert" not in text and "p{}" not in text
    assert "One two & three" in text
    assert "- a\n" in text and "- b" in text
    assert "c1 c2" in text


def test_cut():
    assert cut("abc", 5) == "abc"
    assert cut("abcdef", 3).startswith("abc\n[... 3 more characters")
