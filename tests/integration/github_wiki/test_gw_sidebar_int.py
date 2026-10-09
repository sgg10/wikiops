"""Real git: the managed ``_Sidebar.md`` an apply writes, commits and leaves behind (SB1-SB14).

The real provider writes through the real ``local_files`` backend with ``generate_sidebar``
on; every claim is checked on the clone with real git: what the commit holds, what stays
untouched, what a failure leaves behind. Each ``wiki.provider()`` is a NEW instance, so
placement that survives from one run to the next proves it lives in the file and nowhere else.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from wikiops_sdk.domain import OperationStatus

from tests.support.real_git import SEED_PAGE, Wiki
from tests.support.write_ops import ScriptedResolver, change_set, create, ref, sidebar_hint, update
from wikiops.providers.github_wiki import sidebar
from wikiops.providers.github_wiki.manifest import PendingManifest

pytestmark = pytest.mark.git_integration

SIDEBAR = "_Sidebar.md"
SIDEBAR_OPERATION = "wikiops-sidebar"
APPLIED, SKIPPED, FAILED = OperationStatus.APPLIED, OperationStatus.SKIPPED, OperationStatus.FAILED
USER_SIDEBAR = "# my own sidebar\n\n- [Home](Home)\n- [Hand written](Hand-written)\n"
ENTRY_TARGET = re.compile(r"^- \[[^\]]*\]\(([^)]*)\)", re.MULTILINE)


def statuses(result) -> list[OperationStatus]:  # noqa: ANN001
    return [item.status for item in result.results]


def message_of(result, index: int) -> str:  # noqa: ANN001
    return result.results[index].message or ""


def disk(wiki: Wiki) -> bytes:
    return (wiki.workdir / SIDEBAR).read_bytes()


def listed(wiki: Wiki) -> list[str]:
    """The page targets of the sidebar on disk, in order."""
    return ENTRY_TARGET.findall(disk(wiki).decode())


def placements(wiki: Wiki) -> dict[str, sidebar.Placement]:
    return sidebar.parse(disk(wiki).decode())


def sidebar_in_head(wiki: Wiki) -> bytes:
    return wiki.git("show", f"HEAD:{SIDEBAR}").encode()


def on(wiki: Wiki, **settings: Any):  # noqa: ANN201
    return wiki.provider(generate_sidebar=True, **settings)


def fail_the_sidebar_write(resolver: ScriptedResolver) -> None:
    """Make the backend raise AFTER it wrote the sidebar (the file is on disk when it throws)."""
    assert resolver.backend is not None

    def raise_for_the_sidebar(changeset, real):  # noqa: ANN001, ANN202
        if changeset.operations[0].operation_id == SIDEBAR_OPERATION:
            raise RuntimeError("disk exploded")
        return real

    resolver.backend.on_apply = raise_for_the_sidebar


# -- one commit with the pages (SB11) ---------------------------------------------------------------


def test_two_new_pages_and_the_sidebar_share_one_commit_that_counts_two_pages(wiki: Wiki) -> None:
    result = on(wiki, commit={"message": "wiki: {page_count} pages"}).apply_changes(
        change_set(create("Guide.md", "# Guide\n"), create("Setup.md", "# Setup\n"))
    )

    assert statuses(result) == [APPLIED, APPLIED]
    assert wiki.commit_count() == 2  # the seed plus ONE commit
    assert wiki.last_commit_files() == ["Guide.md", "Setup.md", SIDEBAR]
    assert wiki.subjects()[0] == "wiki: 2 pages"
    text = disk(wiki).decode()
    assert text.startswith(sidebar.MARKER + "\n")
    assert listed(wiki) == ["Home", "Guide", "Setup"]
    assert sidebar_in_head(wiki) == disk(wiki)
    assert wiki.status() == []
    assert "sidebar." not in " ".join(item.message or "" for item in result.results)


def test_an_identical_rerun_makes_no_commit_and_the_file_round_trips(wiki: Wiki) -> None:
    changes = change_set(create("Guide.md", "# Guide\n"), create("Setup.md", "# Setup\n"))
    on(wiki).apply_changes(changes)
    head, written = wiki.head(), disk(wiki)

    again = on(wiki).apply_changes(change_set(update("Guide.md", "# Guide\n"), update("Setup.md", "# Setup\n")))

    assert statuses(again) == [SKIPPED, SKIPPED]
    assert wiki.head() == head and wiki.commit_count() == 2 and wiki.status() == []
    assert disk(wiki) == written
    pages = ["Home", "Guide", "Setup"]
    assert sidebar.render(pages, sidebar.merge(sidebar.parse(written.decode()), ())).encode() == written


# -- a sidebar the user owns is never touched (SB4) ---------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [USER_SIDEBAR, " " + sidebar.MARKER + "\n" + USER_SIDEBAR, sidebar.MARKER + " (mine)\n" + USER_SIDEBAR],
    ids=["unmarked", "leading-space-marker", "marker-with-trailing-text"],
)
def test_an_unmarked_sidebar_is_kept_byte_identical_and_reported_once(wiki: Wiki, content: str) -> None:
    wiki.remote.edit(SIDEBAR, content)

    result = on(wiki).apply_changes(change_set(create("Guide.md", "# Guide\n"), create("Setup.md", "# Setup\n")))

    assert statuses(result) == [APPLIED, APPLIED]
    assert "[github_wiki:sidebar.unmanaged_exists]" in message_of(result, 0)
    assert "sidebar." not in message_of(result, 1)
    assert disk(wiki) == content.encode() and sidebar_in_head(wiki) == content.encode()
    assert wiki.last_commit_files() == ["Guide.md", "Setup.md"]  # the sidebar is not in the commit
    assert wiki.status() == []


def test_adopting_a_sidebar_by_prepending_the_marker_regenerates_it(wiki: Wiki) -> None:
    wiki.remote.edit(SIDEBAR, sidebar.MARKER + "\n" + USER_SIDEBAR)

    result = on(wiki).apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [APPLIED]
    assert "sidebar." not in message_of(result, 0)
    assert disk(wiki).decode().startswith(sidebar.MARKER + "\n")
    assert listed(wiki) == ["Home", "Guide"] and b"Hand written" not in disk(wiki)
    assert wiki.last_commit_files() == ["Guide.md", SIDEBAR]


# -- a sidebar git ignores (SB12) -----------------------------------------------------------------------


def test_a_gitignored_sidebar_is_reported_not_staged_and_fails_no_page(wiki: Wiki) -> None:
    provider = on(wiki)
    provider.exists(ref(SEED_PAGE))  # clone first: the ignore file lives in the clone
    (wiki.workdir / ".gitignore").write_text(f"{SIDEBAR}\n")
    wiki.git("add", "--", ".gitignore")
    wiki.git("commit", "-m", "ignore the sidebar")

    result = provider.apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [APPLIED]
    assert "[github_wiki:sidebar.write_failed]" in message_of(result, 0)
    assert "ignored by git" in message_of(result, 0) and "commit.failed" not in message_of(result, 0)
    assert wiki.last_commit_files() == ["Guide.md"]
    assert SIDEBAR not in wiki.committed_files() and not (wiki.workdir / SIDEBAR).exists()
    assert wiki.status() == []


# -- a failing write is confined to the sidebar (SB12) -----------------------------------------------------


def test_a_failed_sidebar_write_restores_the_previous_managed_bytes_and_keeps_the_pages(wiki: Wiki) -> None:
    resolver = ScriptedResolver()
    wiki.resolver = resolver
    first = on(wiki)
    first.apply_changes(change_set(create("First.md", "# first\n")))
    previous = disk(wiki)
    provider = on(wiki)
    provider.exists(ref(SEED_PAGE))
    fail_the_sidebar_write(resolver)

    result = provider.apply_changes(change_set(create("Second.md", "# second\n")))

    assert statuses(result) == [APPLIED]
    assert "[github_wiki:sidebar.write_failed]" in message_of(result, 0) and "disk exploded" in message_of(result, 0)
    assert wiki.last_commit_files() == ["Second.md"]  # the page went in, the sidebar did not
    assert disk(wiki) == previous == sidebar_in_head(wiki)
    assert b"Second" not in disk(wiki)
    assert wiki.status() == []
    assert PendingManifest(wiki.manifest_file, workdir=wiki.workdir).entries() == {}


def test_a_failed_first_sidebar_write_leaves_no_file_behind(wiki: Wiki) -> None:
    resolver = ScriptedResolver()
    wiki.resolver = resolver
    provider = on(wiki)
    provider.exists(ref(SEED_PAGE))
    fail_the_sidebar_write(resolver)

    result = provider.apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [APPLIED]
    assert "[github_wiki:sidebar.write_failed]" in message_of(result, 0)
    assert wiki.last_commit_files() == ["Guide.md"]
    assert not (wiki.workdir / SIDEBAR).exists() and wiki.status() == []
    assert PendingManifest(wiki.manifest_file, workdir=wiki.workdir).entries() == {}


# -- what changes the sidebar alone (SB7, SB11) ---------------------------------------------------------------


def test_a_hint_only_change_on_an_identical_page_commits_only_the_sidebar(wiki: Wiki) -> None:
    on(wiki).apply_changes(change_set(create("Install.md", "# Install\n", metadata=sidebar_hint(group="Guides"))))
    count = wiki.commit_count()

    result = on(wiki).apply_changes(
        change_set(update("Install.md", "# Install\n", metadata=sidebar_hint(group="Ops", order=1)))
    )

    assert statuses(result) == [SKIPPED]
    assert wiki.commit_count() == count + 1
    assert wiki.last_commit_files() == [SIDEBAR]
    assert placements(wiki)["Install"] == sidebar.Placement("Ops", 1, None)
    assert wiki.status() == []


def test_without_auto_commit_the_sidebar_stays_pending_and_the_next_commit_takes_it(wiki: Wiki) -> None:
    held = on(wiki, allow_auto_commit=False).apply_changes(change_set(create("First.md", "# first\n")))
    manifest = PendingManifest(wiki.manifest_file, workdir=wiki.workdir)

    assert statuses(held) == [APPLIED]
    assert wiki.commit_count() == 1
    assert set(manifest.entries()) == {"First.md", SIDEBAR}
    assert sorted(wiki.status()) == ["?? First.md", f"?? {SIDEBAR}"]

    committed = on(wiki, allow_auto_commit=True).apply_changes(change_set(create("Second.md", "# second\n")))

    assert statuses(committed) == [APPLIED]
    assert wiki.last_commit_files() == ["First.md", "Second.md", SIDEBAR]
    assert listed(wiki) == ["Home", "First", "Second"]
    assert manifest.entries() == {} and wiki.status() == []


# -- placement lives in the file (SB8) ----------------------------------------------------------------------------


def test_placement_is_sticky_across_provider_instances_and_hints_clear_it(wiki: Wiki) -> None:
    on(wiki).apply_changes(
        change_set(
            create("Install.md", "# Install\n", metadata=sidebar_hint(group="Guides", order=10, label="Install guide")),
            create("Other.md", "# Other\n"),
        )
    )
    assert placements(wiki)["Install"] == sidebar.Placement("Guides", 10, "Install guide")

    on(wiki).apply_changes(change_set(create("Third.md", "# Third\n")))  # an untouched page keeps it
    assert placements(wiki)["Install"] == sidebar.Placement("Guides", 10, "Install guide")

    on(wiki).apply_changes(change_set(update("Install.md", "# Install v2\n")))  # no hint keeps it
    assert placements(wiki)["Install"] == sidebar.Placement("Guides", 10, "Install guide")

    on(wiki).apply_changes(change_set(update("Install.md", "# Install v2\n", metadata=sidebar_hint(order=5))))
    assert placements(wiki)["Install"] == sidebar.Placement("Guides", 5, "Install guide")

    on(wiki).apply_changes(change_set(update("Install.md", "# Install v2\n", metadata=sidebar_hint(group=None))))
    assert placements(wiki)["Install"] == sidebar.Placement(None, 5, "Install guide")

    on(wiki).apply_changes(change_set(update("Install.md", "# Install v2\n", metadata={"github_wiki": {"sidebar": None}})))
    assert placements(wiki).get("Install", sidebar.Placement()) == sidebar.Placement()
    assert wiki.status() == []


def test_hints_group_order_and_label_are_rendered(wiki: Wiki) -> None:
    on(wiki).apply_changes(
        change_set(
            create("Install.md", "# Install\n", metadata=sidebar_hint(group="Guides", order=10, label="Install guide")),
            create("Upgrade.md", "# Upgrade\n", metadata=sidebar_hint(group="Guides", order=20)),
            create("Overview.md", "# Overview\n"),
        )
    )

    text = disk(wiki).decode()

    assert listed(wiki) == ["Home", "Overview", "Install", "Upgrade"]
    assert "**Guides**" in text and "[Install guide](Install)" in text
    assert text.index("[Overview](Overview)") < text.index("**Guides**") < text.index("[Install guide](Install)")


def test_a_page_removed_on_the_web_drops_out_of_the_next_sidebar(wiki: Wiki) -> None:
    on(wiki, allow_auto_push=True).apply_changes(change_set(create("Guide.md", "# Guide\n"), create("Setup.md", "# Setup\n")))
    assert listed(wiki) == ["Home", "Guide", "Setup"]
    wiki.remote.delete("Setup.md")

    result = on(wiki, allow_auto_push=True).apply_changes(change_set(create("Extra.md", "# Extra\n")))

    assert statuses(result) == [APPLIED]
    assert not (wiki.workdir / "Setup.md").exists()
    assert listed(wiki) == ["Home", "Extra", "Guide"]


def test_a_hand_edited_managed_body_falls_back_to_defaults_and_is_regenerated(wiki: Wiki) -> None:
    wiki.remote.edit(SIDEBAR, sidebar.MARKER + "\n\nreformatted by hand\n* Home -> /Home\n")

    result = on(wiki).apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [APPLIED] and "sidebar." not in message_of(result, 0)
    assert disk(wiki).decode().startswith(sidebar.MARKER + "\n")
    assert listed(wiki) == ["Home", "Guide"] and b"reformatted" not in disk(wiki)
    assert wiki.last_commit_files() == ["Guide.md", SIDEBAR]


def test_an_oversized_managed_sidebar_is_not_parsed_and_the_run_succeeds(wiki: Wiki) -> None:
    wiki.remote.edit(SIDEBAR, sidebar.MARKER + "\n" + "x" * (sidebar.MAX_PARSE_BYTES + 10) + "\n")

    result = on(wiki).apply_changes(change_set(create("Guide.md", "# Guide\n")))

    assert statuses(result) == [APPLIED] and "sidebar." not in message_of(result, 0)
    assert listed(wiki) == ["Home", "Guide"] and len(disk(wiki)) < sidebar.MAX_PARSE_BYTES
    assert wiki.last_commit_files() == ["Guide.md", SIDEBAR]


# -- the setting off is 1.4.0 (SB1) --------------------------------------------------------------------------------------


def test_with_the_setting_off_a_managed_sidebar_is_an_ordinary_untouched_page(wiki: Wiki) -> None:
    managed = sidebar.MARKER + "\n\n- [Home](Home)\n"
    wiki.remote.edit(SIDEBAR, managed)

    result = wiki.provider().apply_changes(
        change_set(create("Guide.md", "# Guide\n", metadata={"github_wiki": {"sidbar": {"group": 1}}}))
    )

    assert statuses(result) == [APPLIED] and "sidebar." not in message_of(result, 0)
    assert wiki.last_commit_files() == ["Guide.md"]
    assert disk(wiki) == managed.encode() and wiki.status() == []


def test_with_the_setting_off_the_sidebar_page_can_be_written_like_any_page(wiki: Wiki) -> None:
    result = wiki.provider().apply_changes(change_set(create(SIDEBAR, "# written by a plugin\n")))

    assert statuses(result) == [APPLIED]
    assert wiki.last_commit_files() == [SIDEBAR]
    assert disk(wiki) == b"# written by a plugin\n"


def test_with_the_setting_on_a_plugin_cannot_write_the_sidebar_page(wiki: Wiki) -> None:
    result = on(wiki).apply_changes(change_set(create(SIDEBAR, "# forged\n"), create("Guide.md", "# Guide\n")))

    assert statuses(result) == [FAILED, APPLIED]
    assert "path.reserved" in message_of(result, 0) and "generate_sidebar" in message_of(result, 0)
    assert "# forged" not in disk(wiki).decode()
    assert listed(wiki) == ["Home", "Guide"]


# -- the plan note (SB14) -------------------------------------------------------------------------------------------------


def test_the_plan_note_shows_the_flag_and_the_action_from_the_clone_alone(wiki: Wiki) -> None:
    assert "sidebar=false" in wiki.provider().describe_target()
    assert "sidebar_action" not in wiki.provider().describe_target()

    before = len(wiki.runner.argvs)
    assert "sidebar=true sidebar_action=create" in on(wiki).describe_target()  # no clone yet
    assert len(wiki.runner.argvs) == before

    on(wiki).apply_changes(change_set(create("Guide.md", "# Guide\n")))
    assert "sidebar_action=regenerate" in on(wiki).describe_target()

    (wiki.workdir / SIDEBAR).write_text("# not managed any more\n")
    assert "sidebar_action=skipped-unmanaged" in on(wiki).describe_target()
