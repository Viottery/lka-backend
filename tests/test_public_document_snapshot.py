from app.integrations.public_document_snapshot import extract_article_snapshot


def test_extract_article_snapshot_uses_main_mediawiki_article_only():
    html = """
    <html><head><title>Example - Wiki</title></head><body>
      <nav>navigation text must not be included</nav>
      <div id="mw-content-text"><div class="mw-parser-output">
        <p>First factual paragraph.</p>
        <h2>Details</h2><p>Second factual paragraph.</p>
        <table><tr><td>table noise</td></tr></table>
        <script>script noise</script>
      </div></div>
    </body></html>
    """

    title, text = extract_article_snapshot(html)

    assert title == "Example - Wiki"
    assert "First factual paragraph." in text
    assert "Second factual paragraph." in text
    assert "navigation text" not in text
    assert "table noise" not in text
    assert "script noise" not in text
