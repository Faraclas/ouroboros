"""Resident operational floors stay paired with the on-demand runbooks.

These are source-contract assertions, not a claim about model compliance.
Full native prompt delivery is exercised in test_book_context_capture.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text(path):
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def test_system_retains_live_owner_and_custody_floors():
    prompt = text("prompts/SYSTEM.md")
    for safeguard in (
        "Each request gets exactly ONE routing decision",
        "one self-contained final response",
        "Human author/reviewer/route/acceptance choices bind",
        "disclosure authorizes no substitution",
        "physical Off holds",
        "Money, depth, mutation, access, Stop/Panic and unknown-effect custody bind",
        "A possibly live run is not safe to replay",
        "Read-only assignments stay read-only",
        "Runtime `data/` is never a child write surface",
        "Panic acts immediately; nothing I produce may delay it",
        "Unconfirmed stops remain unconfirmed",
        "Operative human words, my outward dialogue and new arrivals stay explicit",
        "Retell for a later reader: situation, purpose",
        "later decisions supersede only what they actually change",
    ):
        assert safeguard in prompt, safeguard


def test_vendor_and_write_surface_runbook_has_trigger_and_complete_retry_duty():
    prompt = text("prompts/SYSTEM.md")
    runbook = text("docs/development/06-rules-by-change-class.md")
    assert "Before scheduling or supervising a vendor session, read DEVELOPMENT" in prompt
    assert '"Live subagents"' in prompt
    for detail in (
        "Before a typed zero-run receipt, retry the exact route",
        "`started_uncustodied` may be live",
        "prove absent/terminal and dispose captured results before replaying THAT start with `retry_of`",
        "other starts remain available",
        "restored work follows its receipt",
        "Only `delegate_start`/`schedule_subagent` resets burn baseline",
        "cognitive route defaults to parent's unless another row was named",
        "Vendor children are opaque",
        "Cooperative builders use it without `write_root`",
        "parent is sole integrating committer",
        "`genesis` is a standalone child repo",
        "Build other data artifacts externally/genesis, parent materializes",
    ):
        assert detail in runbook, detail


def test_skill_and_ui_runbooks_preserve_real_consumer_and_review_boundaries():
    prompt = text("prompts/SYSTEM.md")
    skills = text("docs/CREATING_SKILLS.md")
    design = text("docs/development/11-design-system.md")
    assert 'read `docs/CREATING_SKILLS.md`, "Top-level short-form authoring' in prompt
    for detail in (
        "Author new skills in `external`, manifest-first",
        "inspect its retained work",
        "admitted, enabled and verified through the real consumer",
        '`skill_review(author_disposition="accepted", …)`',
        "ask reviewers again when changes reach what they did not see",
        "Preserve the verdict and exact reviewed hash",
    ):
        assert detail in skills, detail
    assert '"Responsive and accessible behavior"' in prompt
    for detail in (
        "inspect the rendered result with vision",
        "producing a screenshot is not inspection",
        "a missing optional engine is not degradation",
        "Necessary visual evidence that could not be obtained makes the result best-effort",
    ):
        assert detail in design, detail


def test_commit_runbook_preserves_attribution_release_and_test_evidence():
    prompt = text("prompts/SYSTEM.md")
    commit = text("docs/development/05-review-and-commit-protocol.md")
    assert "Before body work read DEVELOPMENT `docs/development/05-review-and-commit-protocol.md`" in prompt
    assert "selected is not full" in prompt
    assert "Restart alone adopts nothing" in prompt
    for detail in (
        "Co-authored-by: Ouroboros <311266734+ouroboros-agent@users.noreply.github.com>",
        "unless human-scoped otherwise",
        "never pre-existing owner dirt hidden by a path list",
        "Only the owner raises Blocking's ceiling",
        "Numbered releases synchronize all carriers",
        "PEP 440 canonical form",
        "Contributions remain untagged with unchanged carriers",
    ):
        assert detail in commit, detail
