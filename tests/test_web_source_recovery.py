from __future__ import annotations

import httpx
import pytest

from app.integrations.web_search import PublicPageFetcher, WebSearchError


def _fetch(body: str, content_type: str = "text/html; charset=utf-8", *, max_text_chars: int = 20_000) -> dict:
    fetcher = PublicPageFetcher(max_text_chars=max_text_chars, transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": content_type}, content=body.encode("utf-8"),
    )))
    return fetcher.read("https://8.8.8.8/source")


def test_sparse_visible_html_uses_bounded_hidden_structured_text_fallback():
    body = """<html><body><nav>Home Topics Subscribe</nav>
      <section data-title="语音资料项">
        <div class="voice-item-detail" style="display:none">这是完整的中文语音资料正文，包含需要恢复的页面内容。</div>
      </section>
      <script>window.secret='must not appear'</script>
    </body></html>"""

    page = _fetch(body)

    assert "这是完整的中文语音资料正文" in page["text"]
    assert "Home Topics" not in page["text"]
    assert "must not appear" not in page["text"]
    assert page["rendered_visibility"] == "alternative_hidden_structured_text"
    assert page["extraction_method"] == "hidden_structured_data_title_fallback"
    assert page["extraction_version"] == "readable-html-v4"
    assert page["text_sha256"]


def test_hidden_structured_fallback_excludes_blocked_descendants_but_keeps_body():
    page = _fetch("""<section data-title="Voice record"><div hidden>
      <script>script-secret</script><style>style-secret</style>
      <form>form-secret<input value="credential-secret"><textarea>textarea-secret</textarea>
        <button>button-secret</button></form>
      legitimate hidden body text remains available.
    </div></section>""")

    assert "legitimate hidden body text remains available" in page["text"]
    for secret in ("script-secret", "style-secret", "form-secret", "credential-secret",
                   "textarea-secret", "button-secret"):
        assert secret not in page["text"]
    assert page["rendered_visibility"] == "alternative_hidden_structured_text"


def test_visible_content_wins_and_hidden_spam_login_and_scripts_stay_excluded():
    body = """<main><article><p>""" + ("Visible article content remains preferred. " * 45) + """</p>
      <div hidden data-title="spam">hidden noise hidden noise</div>
      <form hidden data-title="login"><input value="credential-secret">sign in password</form>
      <script hidden data-title="script">script-secret</script>
      </article></main>"""

    page = _fetch(body)

    assert "Visible article content remains preferred." in page["text"]
    assert "hidden noise" not in page["text"]
    assert "credential-secret" not in page["text"]
    assert "script-secret" not in page["text"]
    assert page["rendered_visibility"] == "visible_text"
    assert page["extraction_version"] == "readable-html-v3"


def test_void_tags_do_not_corrupt_records_and_siblings_keep_newline_boundaries():
    page = _fetch("""<div data-title="First"><img src="x"><br><span hidden>first body</span></div>
      <div data-title="Second"><input value="ignored"><wbr><span hidden>second body</span></div>""")

    assert page["text"] == "First\nfirst body\nSecond\nsecond body"


def test_title_only_hidden_record_does_not_replace_visible_extraction():
    page = _fetch("""<main><article><p>A short visible explanation.</p>
      <section data-title="label only" hidden></section></article></main>""")

    assert page["text"] == "A short visible explanation."
    assert page["rendered_visibility"] == "visible_text"


def test_hidden_record_fallback_hard_cap_includes_labels_and_separators():
    page = _fetch("""<div data-title="Record"><span hidden>long hidden body for cap check</span></div>
      <div data-title="Another"><span hidden>more hidden body</span></div>""", max_text_chars=18)

    assert len(page["text"]) <= 18


def test_text_x_wiki_is_handled_as_plain_text():
    page = _fetch("Wiki page content\r\nwith a second line.", "text/x-wiki; charset=utf-8")
    assert page["text"] == "Wiki page content\nwith a second line."
    assert page["rendered_visibility"] == "not_applicable_plain_text"


def test_unsupported_binary_type_reports_bounded_actual_media_type():
    fetcher = PublicPageFetcher(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "application/octet-stream; name=large-secret-name"}, content=b"\x00\x01",
    )))
    with pytest.raises(WebSearchError, match=r"Unsupported page content type: application/octet-stream"):
        fetcher.read("https://8.8.8.8/source")
