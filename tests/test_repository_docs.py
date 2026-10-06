from pathlib import Path

from scripts.check_repository_docs import PUBLIC_DOCS, validate


def install_docs(root):
    (root / "docs").mkdir()
    for name in PUBLIC_DOCS:
        (root / "docs" / name).write_text("# Guide\n", encoding="utf-8")
    for name in ["README.md", "AGENTS.md", "evals/README.md"]:
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("# Guide\n", encoding="utf-8")


def test_published_docs_are_self_contained():
    assert validate(Path(__file__).resolve().parents[1]) == []


def test_missing_html_asset_and_markdown_link_are_reported(tmp_path):
    install_docs(tmp_path)
    (tmp_path / "README.md").write_text(
        '<img src="docs/assets/missing.svg">\n[Guide](docs/missing.md)\n', encoding="utf-8"
    )
    errors = validate(tmp_path)
    assert len(errors) == 2 and all("Missing link" in error for error in errors)


def test_local_notes_cannot_become_a_public_dependency(tmp_path):
    install_docs(tmp_path)
    local = tmp_path / "docs/_engineering"
    local.mkdir()
    (local / "notes.md").write_text("# Local\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("[Notes](docs/_engineering/notes.md)\n", encoding="utf-8")
    assert any("Local-only link" in error for error in validate(tmp_path))


def test_new_root_docs_require_explicit_classification(tmp_path):
    install_docs(tmp_path)
    (tmp_path / "docs/new_todolist.md").write_text("# Plan\n", encoding="utf-8")
    assert any("extra=['new_todolist.md']" in error for error in validate(tmp_path))


def test_code_example_paths_are_not_document_links(tmp_path):
    install_docs(tmp_path)
    (tmp_path / "README.md").write_text(
        '```markdown\n[Sample](missing.md)\n<img src="missing.svg">\n```\n', encoding="utf-8"
    )
    assert validate(tmp_path) == []
