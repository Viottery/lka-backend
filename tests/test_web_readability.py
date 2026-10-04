from __future__ import annotations

import httpx

from app.integrations.web_search import PublicPageFetcher


def _open(html: str) -> dict:
    return PublicPageFetcher(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                text=html,
            )
        )
    ).open("https://8.8.8.8/example")


def test_open_prefers_article_content_and_removes_page_chrome_and_hidden_text():
    page = _open("""<!doctype html><html><head>
      <title>Research title</title><script>head secret</script>
      <style>.x { display:none }</style>
    </head><body>
      <nav>Home Topics Subscribe</nav>
      <header>Site masthead</header>
      <form><label>Search this site</label><input value="noise"></form>
      <div hidden>hidden attribute secret</div>
      <div aria-hidden="true">aria secret</div>
      <div style="display: none">style secret</div>
      <main><article><h1>Study findings</h1>
        <p>The measured result was 42.</p>
        <p>Source: Field Report <a href="/data">supporting dataset</a>.</p>
        <footer>Article source note: archive record 17.</footer>
      </article></main>
      <footer>Privacy Terms Related stories</footer>
    </body></html>""")

    text = page["text"]
    assert "Study findings" in text
    assert "The measured result was 42." in text
    assert "Source: Field Report" in text
    assert "supporting dataset" in text
    assert "https://8.8.8.8/data" in text
    assert "archive record 17" in text
    for noise in ("Home Topics", "Site masthead", "Search this site", "hidden attribute",
                  "aria secret", "style secret", "head secret", "Privacy Terms"):
        assert noise not in text
    assert page["url"] == "https://8.8.8.8/example"
    assert page["fetched_at"]
    assert page["truncated"] is False
    assert {"url", "fetched_at", "text", "truncated"} <= set(page)
    assert page["total_chars"] == len(text) and page["has_more"] is False
    assert page["next_offset"] is None and page["snapshot_stable"] is False


def test_open_falls_back_to_body_text_when_page_has_no_main_or_article():
    page = _open("""<html><head><title>Simple page</title></head><body>
      <p>A useful standalone page.</p><a href="/source">Original source</a>
      <footer>Cookie settings</footer>
    </body></html>""")

    assert "Simple page" in page["text"]
    assert "A useful standalone page." in page["text"]
    assert "Original source" in page["text"]
    assert "https://8.8.8.8/source" in page["text"]
    assert "Cookie settings" not in page["text"]


def test_open_preserves_case_sensitive_relative_link_url():
    page = _open("""<html><body><main><article>
      <a href="/Docs/Release?Token=AbC123">release notes</a>
    </article></main></body></html>""")

    assert "https://8.8.8.8/Docs/Release?Token=AbC123" in page["text"]


def test_hidden_void_image_does_not_hide_following_article_text_and_br_is_neutral():
    page = _open("""<html><body><main><article>
      Before<img hidden src="/private.png"><br>After the image and break.
    </article></main></body></html>""")

    assert "Before" in page["text"]
    assert "After the image and break." in page["text"]
