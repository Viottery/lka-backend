"""Online authorization epochs revoke durable sessions without a hub read on use."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.domains.accounts import AccountConfig, AccountService
from app.domains.device_connections import DeviceConnectionService
from tests.test_device_connections import connections as connections_fixture
from tests.test_device_connections import node_app

connections = connections_fixture


def poll(f):
    page = f.hub.pending_grants(f.devices.authenticate(f.device["token"]))
    f.node.install_grants(page["items"], page["authorizations"])
    return page


def login(f, user="admin"):
    grant = f.hub.issue_grant(user, "pc")
    page = poll(f)
    return f.node.redeem(grant["grant"]), page


def test_online_revoke_invalidates_grants_and_sessions_and_is_monotonic(connections):
    f = connections
    session, old_page = login(f)
    pending = f.hub.issue_grant("admin", "pc")
    poll(f)
    other, _ = login(f, "another-account")
    revoked = f.hub.revoke_authorization("admin", "pc")
    assert revoked["authorization_epoch"] == 2
    # Until the online update arrives, existing offline sessions remain usable.
    assert f.node.authenticate(session["token"])
    page = poll(f)
    assert all(item["user_id"] != "admin" for item in page["items"])
    assert f.node.authenticate(session["token"]) is None
    assert f.node.authenticate(other["token"])
    with pytest.raises(PermissionError):
        f.node.redeem(pending["grant"])
    # Reordered old authenticated pages cannot roll back revocation.
    f.node.install_grants(old_page["items"], old_page["authorizations"])
    assert f.node.authenticate(session["token"]) is None
    new, page = login(f)
    assert any(item["authorization_epoch"] == 2 for item in page["items"])
    assert f.node.authenticate(new["token"])
    restored = DeviceConnectionService(f.node_factory, f.node.config, clock=lambda: f.clock[0])
    assert restored.authenticate(session["token"]) is None
    assert restored.authenticate(new["token"])


def test_install_epochs_is_atomic_and_audience_bound(connections):
    f = connections
    session, _ = login(f)
    epoch = {"node_id": "pc", "user_id": "admin", "authorization_epoch": 2}
    bad = {"node_id": "another", "user_id": "admin", "authorization_epoch": 3}
    with pytest.raises(PermissionError, match="audience"):
        f.node.install_grants([], [epoch, bad])
    assert f.node.authenticate(session["token"])
    f.node.install_grants([], [epoch])
    assert f.node.authenticate(session["token"]) is None
    f.node.install_grants([], [])
    assert f.node.authenticate(session["token"]) is None


def test_session_listing_and_revocation_are_account_scoped(connections):
    f = connections
    first, _ = login(f)
    second, _ = login(f)
    other, _ = login(f, "another-account")
    rows = f.node.list_sessions("admin", first["session_id"])
    assert {row["session_id"] for row in rows} == {first["session_id"], second["session_id"]}
    assert sum(row["current"] for row in rows) == 1
    assert not any(key in row for row in rows for key in ("token_hash", "csrf_hash", "token"))
    f.node.revoke_session("admin", other["session_id"])
    assert f.node.authenticate(other["token"])
    f.node.revoke_session("admin", second["session_id"])
    assert f.node.authenticate(second["token"]) is None
    assert f.node.authenticate(first["token"])


@pytest.mark.parametrize("change", ["change_password", "reset_password", "disable"])
def test_account_security_changes_publish_epoch_in_same_transaction(connections, change):
    f = connections
    accounts = AccountService(f.hub_factory, AccountConfig(enabled=True),
                              on_security_change=f.hub.bump_account_epoch)
    owner = accounts.create_admin("owner", "original owner password")
    other = accounts.create_admin("other", "original other password", actor_user_id=owner["user_id"])
    session, _ = login(f, other["user_id"])
    if change == "change_password":
        accounts.change_password(other["user_id"], "original other password", "changed other password")
    elif change == "reset_password":
        accounts.reset_password(owner["user_id"], other["user_id"], "changed other password")
    else:
        accounts.set_disabled(owner["user_id"], other["user_id"], True)
    poll(f)
    assert f.node.authenticate(session["token"]) is None


def test_failed_account_security_change_rolls_back_epoch(connections):
    f = connections
    def fail_after_bump(conn, user_id):
        f.hub.bump_account_epoch(conn, user_id)
        raise RuntimeError("security transaction failed")
    accounts = AccountService(f.hub_factory, AccountConfig(enabled=True), on_security_change=fail_after_bump)
    owner = accounts.create_admin("owner", "original owner password")
    session, _ = login(f, owner["user_id"])
    with pytest.raises(RuntimeError, match="security transaction"):
        accounts.change_password(owner["user_id"], "original owner password", "changed owner password")
    poll(f)
    assert f.node.authenticate(session["token"])
    assert accounts.login("owner", "original owner password", "local")


def test_legacy_sessions_default_to_epoch_one_and_empty_poll_is_not_revocation(connections):
    f = connections
    session, _ = login(f)
    with f.node_factory() as conn:
        conn.execute("DELETE FROM device_connection_session_epochs")
    f.node.install_grants([])
    assert f.node.authenticate(session["token"])
    poll(f)
    assert f.node.authenticate(session["token"])
    f.hub.revoke_authorization("admin", "pc")
    poll(f)
    assert f.node.authenticate(session["token"]) is None


def test_session_routes_enforce_csrf_and_current_account(connections):
    f = connections
    app = node_app(f)
    first, _ = login(f)
    second, _ = login(f)
    other, _ = login(f, "another-account")

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://pc.test.ts.net",
                               cookies={f.node.config.cookie_name: first["token"]}) as client:
            result = await client.get("/connections/sessions")
            assert result.status_code == 200
            assert {row["session_id"] for row in result.json()["items"]} == {
                first["session_id"], second["session_id"]}
            own = "/connections/sessions/" + second["session_id"]
            assert (await client.delete(own)).status_code == 403
            headers = {"Origin": "https://pc.test.ts.net", "X-CSRF-Token": first["csrf_token"]}
            assert (await client.delete("/connections/sessions/" + other["session_id"], headers=headers)).status_code == 204
            assert f.node.authenticate(other["token"])
            assert (await client.delete(own, headers=headers)).status_code == 204
            assert f.node.authenticate(second["token"]) is None
            assert (await client.post("/connections/sessions", headers=headers)).status_code == 403

    asyncio.run(asyncio.wait_for(exercise(), timeout=15))
