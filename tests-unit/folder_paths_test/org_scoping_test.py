"""Tests for multi-tenant org scoping in folder_paths.py

Covers:
- get_public_user_directory(user, org): org-rooted user dir + System User block
- The containment invariant user_manager relies on (regression for the bug
  where org userdata always resolved outside the org root -> None)
- Execution-org context scoping get_output_directory / temp / input
- Single-tenant behavior is unchanged
"""
import os

import pytest

import folder_paths


@pytest.fixture
def tenant_env(tmp_path, monkeypatch):
    """Point folder_paths at a temp base and enable multi-tenant mode."""
    base = str(tmp_path)
    monkeypatch.setattr(folder_paths, "base_path", base)
    monkeypatch.setattr(folder_paths, "output_directory", os.path.join(base, "output"))
    monkeypatch.setattr(folder_paths, "input_directory", os.path.join(base, "input"))
    monkeypatch.setattr(folder_paths, "temp_directory", os.path.join(base, "temp"))
    monkeypatch.setattr(folder_paths, "user_directory", os.path.join(base, "user"))
    monkeypatch.setattr(folder_paths, "_org_id", None)
    folder_paths.set_execution_org(None)
    yield base
    folder_paths.set_execution_org(None)


def test_single_tenant_userdir_unchanged(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", False)
    assert folder_paths.get_public_user_directory("default") == \
        os.path.join(tenant_env, "user", "default")


def test_single_tenant_output_unchanged(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", False)
    folder_paths.set_execution_org("acme")  # ignored when not multi-tenant
    assert folder_paths.get_output_directory() == os.path.join(tenant_env, "output")


def test_system_user_blocked_with_org(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", True)
    assert folder_paths.get_public_user_directory("__system", "acme") is None
    assert folder_paths.get_public_user_directory("", "acme") is None
    assert folder_paths.get_public_user_directory(None, "acme") is None


def test_org_user_dir_is_org_rooted(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", True)
    path = folder_paths.get_public_user_directory("alice", "acme")
    assert path == os.path.join(tenant_env, "orgs", "acme", "user", "alice")


def test_containment_invariant(tenant_env, monkeypatch):
    """user_manager requires user_root to be a child of the org user root.

    This is the exact relation that regressed: get_org_user_directory(org) and
    get_public_user_directory(user, org) must share the org root as commonpath.
    """
    monkeypatch.setattr(folder_paths, "_multi_tenant", True)
    root = folder_paths.get_org_user_directory("acme")
    user_root = folder_paths.get_public_user_directory("alice", "acme")
    assert os.path.commonpath((root, user_root)) == root


def test_two_orgs_isolated(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", True)
    a = folder_paths.get_public_user_directory("alice", "acme")
    b = folder_paths.get_public_user_directory("alice", "globex")
    assert a != b
    # Distinct org roots: they share only the orgs/ parent, never each other.
    assert os.path.commonpath((a, b)) == os.path.join(tenant_env, "orgs")


def test_execution_org_scopes_output(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", True)
    folder_paths.set_execution_org("acme")
    assert folder_paths.get_output_directory() == \
        os.path.join(tenant_env, "orgs", "acme", "output")
    assert folder_paths.get_temp_directory() == \
        os.path.join(tenant_env, "orgs", "acme", "temp")
    assert folder_paths.get_input_directory() == \
        os.path.join(tenant_env, "orgs", "acme", "input")


def test_execution_org_reset(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", True)
    folder_paths.set_execution_org("acme")
    assert "acme" in folder_paths.get_output_directory()
    folder_paths.set_execution_org(None)
    # No active execution org and no process org -> base output dir
    assert folder_paths.get_output_directory() == os.path.join(tenant_env, "output")


# ── The org's own directory: one resolver for every per-org file ───────────────
#
# templates, documents, films, held jobs and stacks.db are all siblings of the
# org's output tree, and each of the five used to re-derive that parent for
# itself. They ask here now, so a change of layout is one edit rather than five
# that can disagree.

def test_org_directory_is_the_parent_of_the_output_tree(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", True)
    org = folder_paths.get_org_directory("acme")
    assert org == os.path.join(tenant_env, "orgs", "acme")
    assert folder_paths.get_org_output_directory("acme") == os.path.join(org, "output")


def test_a_named_subdirectory_is_created_on_demand(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", True)
    films = folder_paths.get_org_directory("acme", "films")
    assert films == os.path.join(tenant_env, "orgs", "acme", "films")
    assert os.path.isdir(films)


def test_one_org_never_resolves_into_another(tenant_env, monkeypatch):
    monkeypatch.setattr(folder_paths, "_multi_tenant", True)
    assert folder_paths.get_org_directory("a", "films") != \
        folder_paths.get_org_directory("b", "films")


def test_single_tenant_keeps_per_org_files_inside_output(tenant_env, monkeypatch):
    """There is no orgs/<org> to be the parent of, so the output directory is its
    own base — which is what the five copies did, and why this is not
    get_org_base_path (that answers the repository root)."""
    monkeypatch.setattr(folder_paths, "_multi_tenant", False)
    assert folder_paths.get_org_directory("default", "films") == \
        os.path.join(tenant_env, "output", "films")
