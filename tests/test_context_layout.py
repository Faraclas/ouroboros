"""Unit tests for the context_layout doc-layout SSOT (low/max)."""

from pathlib import Path

from ouroboros import context_layout as cl


def test_tier0_protected_core_declared():
    """The protected always-full core is a data invariant; future context-mode
    work must not silently demote any of these (BIBLE P1 / P4). The marks I keep
    in view joined it when conversations became the memory view's, whose physical
    floor alone turns old lines into addresses: the recent-dialogue horizon left."""
    kept = {"system", "bible", "identity", "scratchpad", "knowledge_index"}
    assert kept <= set(cl.TIER0_ALWAYS_FULL)
    assert "memory_marks" in cl.TIER0_ALWAYS_FULL
    assert "recent_dialogue" not in cl.TIER0_ALWAYS_FULL
    assert set(cl.TIER0_ALWAYS_FULL) == kept | {"memory_marks"}


def test_the_memory_floor_never_takes_the_marks_it_keeps_in_tier_zero():
    """``memory_marks`` in the tier-0 core is true only while no floor step addresses a mark."""
    from ouroboros import memory_floor
    from tests._memory_view_synthetic import actor

    view = actor("main")
    assert view.marks, "the synthetic Main view carries marks"
    elements = memory_floor.floor_elements(view)
    assert elements, "the floor has elements to take on this view"
    mark_ids = {mark["id"] for mark in view.marks}
    assert not mark_ids & {ident for _step, ident, _whole, _short in elements}
    assert all(mark["text"] not in whole for mark in view.marks for _step, _ident, whole, _short in elements)


def test_nav_map_lists_h2_through_h4_as_inclusive_complete_subtrees():
    text = "\n".join([
        "# Title",                   # 1
        "intro",                     # 2
        "## Alpha",                  # 3
        "alpha BODYSENT",            # 4
        "### Shared",                # 5
        "sub intro",                 # 6
        "#### Child one",            # 7
        "child one body",            # 8
        "#### Child two",            # 9
        "child two body",            # 10
        "### Shared",                # 11 (duplicate title)
        "second shared body",        # 12
        "#### Last child",            # 13
        "last body",                 # 14
        "## Beta",                   # 15
        "beta body",                 # 16
        "### Final",                 # 17
        "final body",                # 18 (EOF)
    ])
    m = cl.generate_doc_nav_map(text, title="ARCHITECTURE.md", rel_path="docs/ARCHITECTURE.md")
    assert m == "\n".join([
        "## ARCHITECTURE.md (navigation map)",
        "",
        "Full text is NOT inlined to keep the working context window fit. Read any "
        "section on demand with `read_file(root=\"system_repo\", "
        "path=\"docs/ARCHITECTURE.md\", start_line=A, max_lines=N)` (untruncated). "
        "Ranges are inclusive; a parent includes its complete descendant group, "
        "and `max_lines=B-A+1` for `lines A-B`. Sections:",
        "",
        "- Alpha — lines 3-14",
        "  - Shared — lines 5-10",
        "    - Child one — lines 7-8",
        "    - Child two — lines 9-10",
        "  - Shared — lines 11-14",
        "    - Last child — lines 13-14",
        "- Beta — lines 15-18",
        "  - Final — lines 17-18",
    ])
    # Structure only — the section bodies are NOT inlined.
    assert "BODYSENT" not in m
    assert "final body" not in m


def test_nav_map_is_fence_aware_at_every_supported_depth():
    """Supported heading forms inside a backtick fence stay out of the map."""
    text = (
        "## Real\n\n```markdown\n## fake-h2\n### fake-h3\n#### fake-h4\n"
        "```\n\n### Real child\n\n#### Real grandchild\n"
    )
    m = cl.generate_doc_nav_map(text, title="X", rel_path="x.md")
    assert "- Real — lines 1-11" in m
    assert "  - Real child — lines 9-11" in m
    assert "    - Real grandchild — lines 11-11" in m
    assert "fake-h2" not in m
    assert "fake-h3" not in m
    assert "fake-h4" not in m


def test_real_book_navigation_has_one_addressed_scope_per_chapter_not_subsection_trees():
    """The chapter is the compact navigation unit; the physical file still
    supplies every subsection and its complete body on demand."""
    from ouroboros.reference_books import BOOK_ENTRYPOINTS, load_reference_book, read_book_range

    repo = Path(__file__).resolve().parents[1]
    for book_id in BOOK_ENTRYPOINTS:
        book = load_reference_book(repo, book_id)
        nav = cl.book_navigation(book)
        assert nav.count('read_file(root="system_repo", path=...)') == 1
        assert "## Chapters" not in nav
        assert "(navigation map)" not in nav
        assert " — lines " not in nav
        assert not any(line.startswith(("- ", "  - ")) for line in nav.splitlines())
        assert nav.count("\n\nSource: ") == len(book.chapters)
        for chapter in book.chapters:
            assert nav.splitlines().count(f"# {chapter.headings[0].title}") == 1
            assert nav.count(f"Source: `{chapter.source_path}`") == 1
            assert read_book_range(book, chapter.source_path).text == chapter.text
            for heading in chapter.headings[1:]:
                assert f"{heading.title} — lines " not in nav


def test_real_book_context_modes_keep_full_composition_and_compact_maps_separate():
    from ouroboros.reference_books import BOOK_ENTRYPOINTS, compose_book, load_reference_book

    books = tuple(load_reference_book(Path(__file__).resolve().parents[1], name)
                  for name in BOOK_ENTRYPOINTS)
    arch, dev = books
    for mode in ("low", "nano", "max"):
        for include_dev in (False, True):
            parts, handbook = cl.reference_doc_sections(
                None, context_mode=mode, include_development=include_dev, books=books)
            if mode in {"low", "nano"}:
                assert parts[:2] == [cl.book_navigation(arch), cl.book_navigation(dev)]
                assert handbook == ""
            else:
                assert parts[0] == "## ARCHITECTURE.md\n\n" + compose_book(arch)
                assert handbook == ("## DEVELOPMENT.md\n\n" + compose_book(dev)
                                    if include_dev else cl.book_navigation(dev))


def test_a_legacy_revision_navigates_as_the_single_source_it_is():
    from ouroboros.reference_books import load_reference_book

    monolith = b"# Book\n\nOrientation.\n\n## Runtime\n\nBody.\n\n### Startup\n\nMore.\n"
    book = load_reference_book(Path("unused"), "architecture", lambda _: monolith)
    nav = cl.book_navigation(book)
    assert "docs/ARCHITECTURE.md" in nav and "Runtime" in nav and "Startup" in nav


def test_nav_map_no_heading_fallback_names_all_supported_depths():
    m = cl.generate_doc_nav_map("# Title\nbody", title="X", rel_path="x.md")
    assert "(no `##`/`###`/`####` headings; read `x.md` directly)" in m


def test_reference_doc_sections_preserve_max_and_orient_both_books_in_low():
    """Max retains its full bodies; Low names both complete retrievable books."""
    arch = "## Arch A\n\nARCHBODY\n"
    dev = "## Dev A\n\nDEVBODY\n"

    def _render(mode, include_dev):
        parts, development = cl.reference_doc_sections(
            None,
            context_mode=mode,
            include_development=include_dev,
            architecture_text=arch,
            development_text=dev,
        )
        return "\n\n".join([*parts, development])

    max_no_dev = _render("max", False)
    assert "ARCHBODY" in max_no_dev  # ARCH full in max even without dev context
    assert "DEVBODY" not in max_no_dev
    assert "docs/DEVELOPMENT.md" in max_no_dev  # pointer, never silent

    max_dev = _render("max", True)
    assert "ARCHBODY" in max_dev and "DEVBODY" in max_dev

    low_dev = _render("low", True)
    assert "ARCHBODY" not in low_dev  # nav map in low
    assert "navigation map" in low_dev
    assert "DEVBODY" not in low_dev
    assert "## DEVELOPMENT.md (navigation map)" in low_dev

    low_no_dev = _render("low", False)
    assert "DEVBODY" not in low_no_dev
    assert "docs/DEVELOPMENT.md" in low_no_dev
