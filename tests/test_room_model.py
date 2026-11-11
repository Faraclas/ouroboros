"""One saved room primary reaches runtime routing; unrelated roles remain independent."""
import json
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import config, composer_cognition as cognition, projects_registry as projects
from ouroboros.gateway.owner_effort import api_owner_primary_model
from ouroboros.llm import LLMClient
from ouroboros.model_slots import MODEL_ACCOUNTS_KEY, model_role_option, task_model_binding
from ouroboros.route_choice import acting_binding, following, open_route, round_route
from ouroboros.settings_integrity import live_room_primary, task_settings_scope
from tests.test_project_cognition_runtime import snapshot
from tests.test_task_settings_snapshot import document, isolated_runtime, save_owner  # noqa: F401


def primary(model, pin="", local=False):
    return {"model": model, "use_local": local, "credential_profile_id": pin}


def client():
    app = Starlette(routes=[Route("/api/owner/primary-model", api_owner_primary_model, methods=["GET", "POST"])])
    app.state.drive_root = config.DATA_DIR
    return TestClient(app)


def seed():
    save_owner(document("old"))
    return projects.create_project(config.DATA_DIR, "alpha", origin="owner_ui")


def test_creation_independence_additive_legacy_freeze_and_main_save():
    seed()
    projects.create_project(config.DATA_DIR, "beta", origin="owner_ui")
    path = config.DATA_DIR / "state/projects.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data["projects"]:
        row["cognition"].pop("primary_model")
    path.write_text(json.dumps(data), encoding="utf-8")
    old = cognition.read_cognition(config.DATA_DIR, "alpha")
    with client() as api:
        result = api.post("/api/owner/primary-model", json={"primary_model": primary("openai::new"),
            "expected_primary_model": old["primary_model"]})
        assert result.status_code == 200, result.text
        assert api.get("/api/owner/primary-model").json()["primary_model"] == primary("openai::new")
        assert api.get("/api/owner/primary-model?project_id=alpha").json()["cognition"] == old
        assert cognition.read_cognition(config.DATA_DIR, "beta") == old
        assert projects.create_project(config.DATA_DIR, "new")["cognition"]["primary_model"] == primary("openai::new")
    assert all("primary_model" in row["cognition"] for row in json.loads(path.read_text(encoding="utf-8"))["projects"])


def test_project_api_cas_same_value_preserves_global_and_other_axes():
    seed()
    before_settings = config.SETTINGS_PATH.read_bytes()
    before = cognition.read_cognition(config.DATA_DIR, "alpha")
    route = primary("claudexor::codex=unknown-model", "explicit-profile")
    with client() as api:
        body = {"project_id": "alpha", "primary_model": route, "expected_primary_model": before["primary_model"]}
        response = api.post("/api/owner/primary-model", json=body)
        assert response.status_code == 200, response.text
        current = response.json()["cognition"]
        assert current == {**before, "primary_model": route}
        assert config.SETTINGS_PATH.read_bytes() == before_settings
        assert api.post("/api/owner/primary-model", json={**body, "primary_model": primary("openai::stale")}).status_code == 409
        assert api.get("/api/owner/primary-model?project_id=alpha").json()["primary_model"] == route
        invalid = api.post("/api/owner/primary-model", json={**body, "expected_primary_model": route,
            "primary_model": primary("openai::not-managed", "wrong-profile")})
        assert invalid.status_code == 400
        assert cognition.read_cognition(config.DATA_DIR, "alpha") == current


def test_live_root_route_context_and_explicit_choice_without_role_leaks():
    seed()
    task = {"id": "root", "project_id": "alpha"}
    chosen = primary("openai::project")
    cognition.update_project_cognition(config.DATA_DIR, "alpha", primary_model=chosen)
    with task_settings_scope(snapshot(task)):
        llm = LLMClient()
        ctx = SimpleNamespace(task_model_override=None, task_use_local_override=None, task_metadata={})
        model, local, ctx.route_choice = open_route(ctx, llm)
        ctx.primary_route = {"model": model, "use_local": local, "role": "main"}
        assert (model, local) == (chosen["model"], False)
        from ouroboros.context_fit import _context_route
        assert _context_route(task)[0]["model"] == chosen["model"]
        assert _context_route({})[0]["model"] == chosen["model"]
        assert llm.default_model() == "openai::old"  # global inheritance remains global
        assert config.get_light_model() == "openai::old-light"
        assert model_role_option(MODEL_ACCOUNTS_KEY, "main") == "account-old"
        assert task_model_binding({"model_role": "vision"}) == ("vision", None)
        assert task_model_binding({"model_role": "reviewer:critic"}) == ("reviewer:critic", None)
        cognition.update_project_cognition(config.DATA_DIR, "alpha", primary_model=primary("openai::later"))
        assert (model, local) == (chosen["model"], False)  # captured send is unchanged
        assert round_route(ctx, llm, (model, local)) == ("openai::later", False)
        ctx.route_choice = {"kind": "explicit", "source": "switch_model"}
        assert round_route(ctx, llm, ("openai::own", False)) is None


@pytest.mark.parametrize("extra", [{"parent_task_id": "parent"}, {"_presence_turn": True},
                                  {"type": "evolution"}, {"metadata": {"initiator": "consciousness"}}])
def test_other_actor_faculties_do_not_follow_project_primary(extra):
    seed()
    cognition.update_project_cognition(config.DATA_DIR, "alpha", primary_model=primary("openai::project"))
    with task_settings_scope(snapshot({"id": "other", "project_id": "alpha", **extra})):
        assert live_room_primary() is None
        assert open_route(SimpleNamespace(task_model_override=None, task_use_local_override=None,
            task_metadata={}), LLMClient())[0] == "openai::old"


def test_account_binding_and_nanny_inherit_actual_room_not_global_role():
    seed()
    chosen = primary("claudexor::codex=gpt-example", "room-account")
    cognition.update_project_cognition(config.DATA_DIR, "alpha", primary_model=chosen)
    with task_settings_scope(snapshot({"id": "root", "project_id": "alpha"})):
        assert task_model_binding({}) == ("main", "room-account")
        assert acting_binding(chosen["model"], False, "main")[2] == "room-account"
        assert acting_binding(chosen["model"], False, "vision")[2] == ""
        assert acting_binding(chosen["model"], False, "main", overrides={"main": {"model_account_override": ""}})[2] == ""
        from ouroboros.supervisor_route import _inherited_cognition
        inherited = _inherited_cognition(SimpleNamespace(task_metadata={}, context_fit_plan=None,
            active_model=chosen["model"], active_use_local=False, active_effort="medium", route_choice=following("main")), config.load_settings())
        assert inherited["model"] == chosen["model"]
        assert inherited["credential_profile_id"] == "room-account"
        assert inherited["follows_role"] == ""


def test_primary_seed_uses_same_direct_provider_spelling_as_runtime():
    values = document('old')
    values.pop('OPENROUTER_API_KEY')
    values['OUROBOROS_MODEL'] = 'openai/saved-model'
    save_owner(values)
    project = projects.create_project(config.DATA_DIR, 'alpha')
    with task_settings_scope(snapshot({'id': 'root', 'project_id': 'alpha'})):
        live = open_route(SimpleNamespace(task_model_override=None, task_use_local_override=None, task_metadata={}), LLMClient())[0]
        assert project['cognition']['primary_model']['model'] == live == LLMClient().default_model() == 'openai::saved-model'


@pytest.mark.parametrize('same_main', [True, False])
def test_fresh_and_explicit_rebind_have_identical_binding_scoped_windows(monkeypatch, same_main):
    from ouroboros import capability_evidence
    from ouroboros.context_fit import resolve_context_fit_route
    seed()
    model = 'openai::old' if same_main else 'openai::other-room'
    cognition.update_project_cognition(config.DATA_DIR, 'alpha', primary_model=primary(model))
    monkeypatch.setattr(capability_evidence, 'probe', lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(capability_evidence, 'with_role_window', lambda evidence, window: window)
    task = {'id': 'root', 'project_id': 'alpha'}
    with task_settings_scope(snapshot(task)):
        fresh, fresh_window = resolve_context_fit_route(task, allow_fetch=False)
        rebound, rebound_window = resolve_context_fit_route({**task, 'model': model, 'use_local_model': False}, allow_fetch=False)
        assert fresh['model'] == rebound['model'] == model
        assert fresh_window == rebound_window == (200000 if same_main else 0)


def test_hot_saved_primary_does_not_reproject_global_settings(monkeypatch):
    seed()
    with task_settings_scope(snapshot({'id': 'root', 'project_id': 'alpha'})):
        monkeypatch.setattr(cognition, 'main_cognition', lambda *args, **kwargs: pytest.fail('Hot seeded row needs no global projection'))
        assert live_room_primary() == primary('openai::old')


def test_saved_primary_and_root_route_survive_fresh_interpreter():
    import pathlib
    import subprocess
    import sys
    seed()
    chosen = primary('claudexor::codex=future-choice', 'room-profile')
    cognition.update_project_cognition(config.DATA_DIR, 'alpha', primary_model=chosen)
    code = '''import json,sys
from pathlib import Path
from ouroboros import config
from ouroboros.composer_cognition import read_primary
from ouroboros.settings_integrity import live_room_primary
config.DATA_DIR=Path(sys.argv[1]);config.SETTINGS_PATH=config.DATA_DIR/'settings.json'
print(json.dumps([read_primary(config.DATA_DIR,'alpha'),live_room_primary(task={'id':'restored-root','project_id':'alpha'})]))
'''
    result = subprocess.check_output([sys.executable, '-c', code, str(config.DATA_DIR)], cwd=pathlib.Path(__file__).parents[1], text=True)
    assert json.loads(result) == [chosen, chosen]


def test_binding_window_before_actor_scope_keeps_matching_managed_main_pin(monkeypatch):
    from ouroboros import capability_evidence
    from ouroboros.context_fit import resolve_context_fit_route
    values = document('old')
    values['OUROBOROS_MODEL'] = 'claudexor::codex=same-model'
    save_owner(values)
    projects.create_project(config.DATA_DIR, 'alpha')
    monkeypatch.setattr(capability_evidence, 'probe', lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(capability_evidence, 'model_account_options', lambda *args, **kwargs: {})
    monkeypatch.setattr(capability_evidence, 'with_role_window', lambda evidence, window: window)
    route, window = resolve_context_fit_route({'id':'fresh-root','project_id':'alpha'},allow_fetch=False)
    assert route['model']=='claudexor::codex=same-model'
    assert window==200000
