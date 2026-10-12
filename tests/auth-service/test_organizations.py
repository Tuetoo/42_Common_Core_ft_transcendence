import json
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4
import httpx
import pyotp
import pytest
from helpers import (PASSWORD, current_user_id, demote_all_admins, enable_2fa,
                     events_named, login, login_response, make_user,
                     register_and_login, reset_rate_limits, set_role, uid)
from shared.enums import SystemRole
from shared.permissions import sorted_permissions
from shared.redis import get_events_client
from shared.user_state import user_state_key

ADMIN = "/api/v1/admin"
ORGS = "/api/v1/organizations"


async def stored_user_state(user_id):
    from config import settings

    raw = await get_events_client(settings).get(user_state_key(user_id))
    return json.loads(raw) if raw is not None else None


async def stored_role(db_session, user_id):
    from domains.auth.repository import UserRepository

    user = await UserRepository(db_session).get_by_id(UUID(user_id))
    return user.system_role


class SessionFactory:

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class FakeEngine:

    async def dispose(self):
        return None


async def org_with_admins(client, mock_email, tag):
    owner, _ = await register_and_login(
        client, mock_email, f"o{tag}@example.com", f"o{tag}")
    created = await client.post(ORGS, json={"name": tag}, headers=owner)
    org = created.json()["data"]
    members = {}
    for name, role in (("adm", "admin"), ("adm2", "admin"), ("mem", "member")):
        h, _ = await register_and_login(
            client, mock_email, f"{name}{tag}@example.com", f"{name}{tag}")
        mock_email.clear()
        await client.post(
            f"{ORGS}/{org['id']}/invitations", json={
                "email": f"{name}{tag}@example.com",
                "role": role
            }, headers=owner)
        token = mock_email[0]["html_body"].split("token=")[1].split('"')[0]
        accepted = await client.post(
            f"{ORGS}/invitations/accept", json={"token": token}, headers=h)
        assert accepted.status_code == 200
        members[name] = (h, await current_user_id(client, h))
    return owner, org, members


async def invite_and_join(
        client, mock_email, org_id, owner_headers, member, role):
    r = await client.post(
        f"{ORGS}/{org_id}/invitations", headers=owner_headers, json={
            "email": member["email"],
            "role": role
        })
    assert r.status_code == 201, r.text
    token = mock_email[-1]["html_body"].split("token=")[1].split('"')[0]
    token = token.split("<")[0].split("&")[0].strip()
    r = await client.post(
        f"{ORGS}/invitations/accept", json={"token": token},
        headers=member["headers"])
    assert r.status_code == 200, r.text


class TestOrganizations:

    async def test_role_rules_leave_internal_listing_and_cache(
            self, client, mock_email, db_session, monkeypatch):
        owner, org, m = await org_with_admins(client, mock_email, "roles")
        base = f"{ORGS}/{org['id']}"
        adm_h, _ = m["adm"]

        r = await client.patch(
            f"{base}/members/{m['mem'][1]}/role", json={"role": "owner"},
            headers=owner)
        assert r.status_code == 422
        r = await client.post(
            f"{base}/invitations", json={
                "email": "z@example.com",
                "role": "owner"
            }, headers=owner)
        assert r.status_code == 422
        r = await client.patch(
            f"{base}/members/{m['adm2'][1]}/role", json={"role": "member"},
            headers=adm_h)
        assert r.status_code == 403
        r = await client.post(
            f"{base}/members/{m['adm2'][1]}/remove", headers=adm_h)
        assert r.status_code == 403
        r = await client.patch(
            f"{base}/members/{m['mem'][1]}/role", json={"role": "admin"},
            headers=adm_h)
        assert r.status_code == 403
        r = await client.patch(
            f"{base}/members/{m['mem'][1]}/role", json={"role": "viewer"},
            headers=adm_h)
        assert r.status_code == 200
        r = await client.patch(
            f"{base}/members/{m['adm2'][1]}/role", json={"role": "member"},
            headers=owner)
        assert r.status_code == 200

        left = await client.post(f"{base}/leave", headers=m["mem"][0])
        assert left.status_code == 204
        assert (await client.get(base, headers=m["mem"][0])).status_code == 404
        assert (
            await client.post(f"{base}/leave",
                              headers=owner)).status_code == 422

        listed = (await client.get(f"{base}/members",
                                   headers=owner)).json()["data"]
        assert all(entry["avatar_url"] for entry in listed)

        await reset_rate_limits()
        owner = await make_user(client, mock_email, db_session, prefix="own")
        r = await client.post(
            ORGS, json={"name": "RoleCo"}, headers=owner["headers"])
        assert r.status_code == 201, r.text
        org_id = r.json()["data"]["id"]

        r = await client.get(f"/internal/users/{owner['id']}/organizations")
        assert r.status_code == 200, r.text
        items = r.json()
        assert [(i["id"], i["name"], i["role"])
                for i in items] == [(org_id, "RoleCo", "owner")]

        from config import settings
        from redis.exceptions import ConnectionError as RedisConnectionError
        import shared.user_state as cache_module

        class _Broken:

            async def delete(self, *keys):
                raise RedisConnectionError("down")

        monkeypatch.setattr(
            cache_module, "get_cache_client", lambda s: _Broken())
        invalidate = cache_module.invalidate_membership_cache
        assert await invalidate(settings, ["u1", None]) == 0
        assert await invalidate(settings, [None]) == 0

    async def test_deleted_owner_hands_the_organization_to_an_admin(
            self, client, mock_email, db_session, captured_events):
        from sqlalchemy import select
        from domains.organization.model import Organization, OrganizationMember

        owner_h, org, members = await org_with_admins(
            client, mock_email, "ownerdel")
        owner_id = await current_user_id(client, owner_h)
        admin = await make_user(
            client, mock_email, db_session, role="admin", prefix="a")
        deleted = await client.delete(
            f"{ADMIN}/users/{owner_id}", headers=admin["headers"])
        assert deleted.status_code == 204

        admin_ids = {members["adm"][1], members["adm2"][1]}
        stored = await db_session.get(Organization, UUID(org["id"]))
        successor = str(stored.owner_id)
        assert successor in admin_ids
        roles = dict((
            await db_session.execute(
                select(OrganizationMember.user_id,
                       OrganizationMember.role).where(
                           OrganizationMember.organization_id == stored.id)
            )).all())
        assert roles[UUID(successor)] == "owner"
        assert UUID(owner_id) not in roles
        changed = [
            p for n, p in captured_events if n == "organization.role.changed"
        ]
        assert changed and changed[-1]["user_id"] == successor
        assert changed[-1]["role"] == "owner"


class TestInvitations:

    async def test_invitation_rules_and_organization_delete(
            self, client, mock_email, db_session):
        owner = await make_user(client, mock_email, db_session)
        member = await make_user(client, mock_email, db_session)
        outsider = await make_user(client, mock_email, db_session)
        org = (
            await
            client.post(ORGS, json={"name": "Inv"},
                        headers=owner["headers"])).json()["data"]
        await invite_and_join(
            client, mock_email, org["id"], owner["headers"], member, "member")
        base = f"{ORGS}/{org['id']}/invitations"

        tokens = []
        for role in ("member", "viewer"):
            r = await client.post(
                base, headers=owner["headers"], json={
                    "email": "Later@Example.com",
                    "role": role
                })
            assert r.status_code == 201, r.text
            body = mock_email[-1]["html_body"].split("token=")[1]
            tokens.append(body.split('"')[0].split("<")[0].split("&")[0])
        listed = await client.get(base, headers=owner["headers"])
        rows = listed.json()["data"]
        assert [(r["email"], r["role"])
                for r in rows] == [("later@example.com", "viewer")]
        replaced = await client.post(
            f"{ORGS}/invitations/accept", json={"token": tokens[0]},
            headers=outsider["headers"])
        assert replaced.status_code == 404

        for who, status in ((member, 403), (outsider, 404)):
            r = await client.get(base, headers=who["headers"])
            assert r.status_code == status
            r = await client.delete(
                f"{base}/{rows[0]['id']}", headers=who["headers"])
            assert r.status_code == status
        r = await client.delete(
            f"{base}/{rows[0]['id']}", headers=owner["headers"])
        assert r.status_code == 204
        again = await client.delete(
            f"{base}/{rows[0]['id']}", headers=owner["headers"])
        assert again.status_code == 404
        assert (await
                client.get(base,
                           headers=owner["headers"])).json()["data"] == []

        await reset_rate_limits()
        owner = await make_user(client, mock_email, db_session)
        admin = await make_user(client, mock_email, db_session)
        org = (
            await
            client.post(ORGS, json={"name": "Adm"},
                        headers=owner["headers"])).json()["data"]
        await invite_and_join(
            client, mock_email, org["id"], owner["headers"], admin, "admin")
        base = f"{ORGS}/{org['id']}/invitations"
        email = f"boss{uuid4().hex[:6]}@example.com"
        first = await client.post(
            base, headers=owner["headers"], json={
                "email": email,
                "role": "admin"
            })
        assert first.status_code == 201
        replaced = await client.post(
            base, headers=admin["headers"], json={
                "email": email,
                "role": "viewer"
            })
        assert replaced.status_code == 403
        listed = await client.get(base, headers=owner["headers"])
        rows = listed.json()["data"]
        assert [(r["email"], r["role"]) for r in rows] == [(email, "admin")]
        again = await client.post(
            base, headers=owner["headers"], json={
                "email": email,
                "role": "viewer"
            })
        assert again.status_code == 201

        await reset_rate_limits()
        owner = await make_user(client, mock_email, db_session)
        member = await make_user(client, mock_email, db_session)
        outsider = await make_user(client, mock_email, db_session)
        org = (
            await client.post(
                ORGS, json={"name": "Hidden"},
                headers=owner["headers"])).json()["data"]
        await invite_and_join(
            client, mock_email, org["id"], owner["headers"], member, "member")
        url = f"{ORGS}/{org['id']}"
        assert (
            await
            client.delete(url, headers=outsider["headers"])).status_code == 404
        assert (
            await client.delete(url,
                                headers=member["headers"])).status_code == 403
        assert (
            await client.delete(url,
                                headers=owner["headers"])).status_code == 204

        await reset_rate_limits()
        headers, _ = await register_and_login(
            client, mock_email, f"p{uid()}@example.com", f"p{uid()}")
        r = await client.post(
            f"{ORGS}/invitations/some-token/accept", headers=headers)
        assert r.status_code in (404, 405, 422)


class TestAdminUsers:

    async def test_role_permission_matrix(
            self, client, mock_email, db_session):
        for role, manage, deactivate, stats in (("admin", 200, 200, 200),
                                                ("moderator", 403, 200,
                                                 403), ("user", 403, 403, 403),
                                                ("guest", 403, 403, 403)):
            await reset_rate_limits()
            actor = await make_user(client, mock_email, db_session, role=role)
            target = await make_user(client, mock_email, db_session)
            h = actor["headers"]
            url = f"{ADMIN}/users/{target['id']}"

            assert (
                await client.get(f"{ADMIN}/users",
                                 headers=h)).status_code == manage, role
            got = (await client.get(url, headers=h)).status_code
            assert got == manage, role
            assert (
                await
                client.patch(url, headers=h,
                             json={"bio": "x"})).status_code == manage, role
            assert (
                await client.post(f"{url}/deactivate",
                                  headers=h)).status_code == deactivate, role
            assert (
                await client.post(f"{url}/reactivate",
                                  headers=h)).status_code == deactivate, role
            assert (
                await client.get(f"{ADMIN}/stats",
                                 headers=h)).status_code == stats, role
            delete = await client.delete(url, headers=h)
            assert delete.status_code == (204 if manage == 200 else 403), role

        await reset_rate_limits()
        owner = await make_user(client, mock_email, db_session)
        friend = await make_user(client, mock_email, db_session)
        org_url = f"{ORGS}/" + (
            await client.post(
                ORGS, json={"name": "Demoted"},
                headers=owner["headers"])).json()["data"]["id"]
        for method, url in (("POST", ORGS), ("PATCH", org_url)):
            r = await client.request(
                method, url, headers=owner["headers"], json={"name": "   "})
            assert r.status_code == 422, method
        await set_role(db_session, owner["id"], "guest")
        guest, _ = await login(client, owner["email"])
        for method, url, body in (
                ("PATCH", org_url, {"name": "x"}), ("DELETE", org_url, None),
                ("POST", "/api/v1/connections",
                 {"addressee_username": friend["username"]})):
            r = await client.request(method, url, headers=guest, json=body)
            assert r.status_code == 403, (method, url)
        assert (await client.get(org_url, headers=guest)).status_code == 200

    async def test_list_search_filter_sort_paginate(
            self, client, mock_email, db_session):
        admin = await make_user(client, mock_email, db_session, role="admin")
        tag = uid()
        created = []
        for name in ("carol", "alice", "bob"):
            email = f"{name}.{tag}@example.com"
            r = await client.post(
                "/api/v1/auth/register", json={
                    "email": email,
                    "username": f"{name}_{tag}",
                    "password": PASSWORD
                })
            assert r.status_code == 201
            created.append(email)
        h = admin["headers"]
        users = f"{ADMIN}/users"

        r = await client.get(
            users, headers=h, params={
                "q": tag.upper(),
                "sort": "email",
                "order": "asc"
            })
        assert r.status_code == 200
        page = r.json()["data"]
        assert page["total"] == 3
        assert [u["email"] for u in page["items"]] == sorted(created)
        assert {"is_online", "avatar_url", "system_role",
                "last_seen_at"} <= (set(page["items"][0]))

        r = await client.get(users, headers=h, params={"q": f"bob_{tag}"})
        assert [u["username"]
                for u in r.json()["data"]["items"]] == [f"bob_{tag}"]

        r = await client.get(
            users, headers=h, params={
                "q": tag,
                "sort": "username",
                "order": "desc",
                "page": 2,
                "page_size": 2
            })
        data = r.json()["data"]
        assert data["total"] == 3 and data["page"] == 2
        assert data["page_size"] == 2
        assert [u["username"] for u in data["items"]] == [f"alice_{tag}"]

        from domains.auth.repository import UserRepository

        bob = await UserRepository(db_session).get_by_email(
            f"bob.{tag}@example.com")
        bob.system_role = "guest"
        bob.is_active = False
        await db_session.flush()
        r = await client.get(
            users, headers=h, params={
                "q": tag,
                "role": "guest"
            })
        assert [u["username"]
                for u in r.json()["data"]["items"]] == [f"bob_{tag}"]
        r = await client.get(
            users, headers=h, params={
                "q": tag,
                "is_active": "true"
            })
        assert r.json()["data"]["total"] == 2

        assert (
            await client.get(users, headers=h,
                             params={"page_size": 101})).status_code == 422
        assert (
            await client.get(users, headers=h,
                             params={"sort": "password"})).status_code == 422
        assert (
            await client.get(users, headers=h,
                             params={"role": "owner"})).status_code == 422
        r = await client.get(users, headers=h, params={"q": "%"})
        assert r.status_code == 200
        assert all(
            "%" in u["email"] + u["username"]
            for u in r.json()["data"]["items"])

    async def test_patch_fields_role_and_active_publish_events(
            self, client, mock_email, db_session, captured_events):
        admin = await make_user(client, mock_email, db_session, role="admin")
        target = await make_user(client, mock_email, db_session)
        other = await make_user(client, mock_email, db_session)
        h = admin["headers"]
        url = f"{ADMIN}/users/{target['id']}"

        new_name = f"renamed_{uid()}"
        r = await client.patch(
            url, headers=h, json={
                "username": new_name,
                "first_name": "Ada",
                "bio": None
            })
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["username"] == new_name
        assert d["profile"]["first_name"] == "Ada"
        updated = events_named(captured_events, "user.updated")
        assert updated[-1] == {
            "user_id": target["id"],
            "actor_id": admin["id"],
            "role": "user",
            "is_active": True,
            "changed": ["first_name", "username"]
        }

        sent_before = len(mock_email)
        new_email = f"New.{uid()}@Example.COM"
        r = await client.patch(url, headers=h, json={"email": new_email})
        assert r.status_code == 200
        assert r.json()["data"]["email"] == new_email.lower()
        assert r.json()["data"]["is_verified"] is False
        assert len(mock_email) == sent_before + 1
        assert mock_email[-1]["to"] == new_email.lower()
        assert events_named(
            captured_events,
            "user.updated")[-1]["changed"] == ["email", "is_verified"]

        r = await client.patch(
            url, headers=h, json={
                "email": f"third.{uid()}@example.com",
                "is_verified": True
            })
        assert r.json()["data"]["is_verified"] is True

        r = await client.patch(
            url, headers=h, json={"email": other["email"].upper()})
        assert r.status_code == 409
        r = await client.patch(
            url, headers=h, json={"username": other["username"].upper()})
        assert r.status_code == 409
        for body in ({"role": "owner"}, {"email": None}, {"is_active": None},
                     {"username": "a b"}, {"timezone": "Mars/Base"}):
            r = await client.patch(url, headers=h, json=body)
            assert r.status_code == 422
        missing = await client.patch(
            f"{ADMIN}/users/{uuid4()}", headers=h, json={"bio": "x"})
        assert missing.status_code == 404

        await reset_rate_limits()
        captured_events.clear()
        admin = await make_user(client, mock_email, db_session, role="admin")
        target = await make_user(client, mock_email, db_session)
        url = f"{ADMIN}/users/{target['id']}"

        r = await client.patch(
            url, headers=admin["headers"], json={"role": "moderator"})
        assert r.status_code == 200
        assert r.json()["data"]["system_role"] == "moderator"
        assert r.json()["data"]["permissions"] == sorted_permissions(
            "moderator")
        assert events_named(captured_events, "user.role.changed") == [{
            "user_id": target["id"],
            "actor_id": admin["id"],
            "role": "moderator",
            "is_active": True,
            "changed": ["role"]
        }]
        assert events_named(captured_events, "user.updated") == []

        r = await client.patch(
            url, headers=admin["headers"], json={"is_active": False})
        assert r.status_code == 200
        assert r.json()["data"]["is_active"] is False
        assert events_named(
            captured_events,
            "user.deactivated")[-1]["changed"] == ["is_active"]
        refresh = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": target["refresh_token"]})
        assert refresh.status_code == 401

        r = await client.patch(
            url, headers=admin["headers"], json={"is_active": True})
        assert r.json()["data"]["is_active"] is True
        assert (
            events_named(captured_events, "user.reactivated")[-1]["is_active"]
            is True)

        from config import settings
        from shared.user_state import get_user_state

        state = await get_user_state(settings, target["id"])
        assert state is not None and state.role == "moderator"
        assert state.is_active

    async def test_last_admin_deactivation_and_two_factor_reset(
            self, client, mock_email, db_session, captured_events):
        await demote_all_admins(db_session)
        a1 = await make_user(client, mock_email, db_session, role="admin")
        a2 = await make_user(client, mock_email, db_session, role="admin")

        r = await client.patch(
            f"{ADMIN}/users/{a2['id']}", headers=a1["headers"],
            json={"role": "user"})
        assert r.status_code == 200

        url = f"{ADMIN}/users/{a1['id']}"
        stale = a2["headers"]
        for call in (client.patch(url, headers=stale,
                                  json={"role": "moderator"}),
                     client.patch(url, headers=stale,
                                  json={"is_active": False}),
                     client.post(f"{url}/deactivate",
                                 headers=stale), client.delete(url,
                                                               headers=stale)):
            r = await call
            assert r.status_code == 409, r.text
            assert "last active admin" in r.json()["error"]["message"]

        await set_role(db_session, a2["id"], "admin")
        r = await client.post(f"{url}/deactivate", headers=stale)
        assert r.status_code == 200

        await reset_rate_limits()
        mod = await make_user(client, mock_email, db_session, role="moderator")
        target = await make_user(client, mock_email, db_session)
        url = f"{ADMIN}/users/{target['id']}"
        r = await client.post(f"{url}/deactivate", headers=mod["headers"])
        assert r.status_code == 200
        r = await client.post(f"{url}/deactivate", headers=mod["headers"])
        assert r.status_code == 200

        refresh = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": target["refresh_token"]})
        assert refresh.status_code == 401
        login_r = await login_response(client, target["email"])
        assert login_r.status_code == 401
        assert login_r.json()["error"]["message"] == (
            "This account has been deactivated.")

        await client.post(f"{url}/reactivate", headers=mod["headers"])
        again = await login_response(client, target["email"])
        assert again.status_code == 200

        await reset_rate_limits()
        user = await make_user(client, mock_email, db_session, prefix="reset")
        await enable_2fa(client, user["headers"])
        assert (await login_response(client, user["email"])).status_code == 401
        admin = await make_user(
            client, mock_email, db_session, role="admin", prefix="a")
        moderator = await make_user(
            client, mock_email, db_session, role="moderator", prefix="m")
        url = f"{ADMIN}/users/{user['id']}/2fa"

        assert (
            await
            client.delete(url,
                          headers=moderator["headers"])).status_code == 403
        assert (
            await client.delete(url,
                                headers=admin["headers"])).status_code == 204
        assert (await login_response(client, user["email"])).status_code == 200
        detail = (
            await client.get(
                f"{ADMIN}/users/{user['id']}",
                headers=admin["headers"])).json()["data"]
        assert detail["two_factor_enabled"] is False
        updates = [p for n, p in captured_events if n == "user.updated"]
        assert updates[-1]["changed"] == ["two_factor_enabled"]

        assert (
            await client.delete(url,
                                headers=admin["headers"])).status_code == 204
        own = f"{ADMIN}/users/{admin['id']}/2fa"
        assert (
            await client.delete(own,
                                headers=admin["headers"])).status_code == 403
        missing = f"{ADMIN}/users/{uuid4()}/2fa"
        assert (
            await client.delete(missing,
                                headers=admin["headers"])).status_code == 404

    async def test_soft_delete_effects(
            self, client, mock_email, db_session, captured_events):
        import security
        from config import settings
        from sqlalchemy import func, select
        from domains.auth.model import (OAuthAccount, TwoFactorSecret, User,
                                        UserSession)
        from domains.organization.model import OrganizationMember
        from domains.social.model import Connection
        from shared.user_state import get_user_state

        admin = await make_user(client, mock_email, db_session, role="admin")
        target = await make_user(client, mock_email, db_session)
        friend = await make_user(client, mock_email, db_session)
        tid = UUID(target["id"])

        req = await client.post(
            "/api/v1/connections", headers=friend["headers"],
            json={"addressee_username": target["username"]})
        await client.post(
            f"/api/v1/connections/{req.json()['data']['id']}/accept",
            headers=target["headers"])
        created = await client.post(
            ORGS, json={"name": "Keep"}, headers=friend["headers"])
        org = created.json()["data"]
        db_session.add(
            OrganizationMember(
                organization_id=UUID(org["id"]), user_id=tid, role="member"))
        own_org = await client.post(
            ORGS, json={"name": "Mine"}, headers=target["headers"])
        assert own_org.status_code == 201
        db_session.add(
            OAuthAccount(
                user_id=tid, provider="google", provider_user_id=f"g-{uid()}"))
        db_session.add(
            TwoFactorSecret(
                user_id=tid, secret="JBSWY3DPEHPK3PXP", enabled=True))
        await db_session.flush()
        user_row = await db_session.get(User, tid)
        reset_token = security.create_password_reset_token(
            target["id"], user_row.password_hash)
        detail = (
            await client.get(
                f"{ADMIN}/users/{target['id']}",
                headers=admin["headers"])).json()["data"]
        assert detail["two_factor_enabled"] is True
        assert detail["oauth_providers"] == ["google"]
        assert detail["organizations_count"] == 2

        r = await client.delete(
            f"{ADMIN}/users/{target['id']}", headers=admin["headers"])
        assert r.status_code == 204, r.text

        await db_session.refresh(user_row)
        assert user_row.deleted_at is not None
        assert user_row.is_active is False
        assert user_row.email == f"deleted-{tid.hex}@deleted.alphamind.local"
        assert user_row.username == f"deleted_{tid.hex}"

        async def count(model, *where):
            return (
                await db_session.execute(
                    select(func.count()).select_from(model).where(*where)
                )).scalar_one()

        assert await count(OAuthAccount, OAuthAccount.user_id == tid) == 0
        assert (
            await count(TwoFactorSecret, TwoFactorSecret.user_id == tid) == 0)
        assert (
            await count(
                UserSession, UserSession.user_id == tid,
                UserSession.revoked_at.is_(None)) == 0)
        assert (
            await count(
                Connection, (Connection.requester_id == tid)
                | (Connection.addressee_id == tid)) == 0)
        members = ((
            await db_session.execute(
                select(OrganizationMember.role).where(
                    OrganizationMember.user_id == tid))).scalars().all())
        assert members == ["owner"]

        state = await get_user_state(settings, target["id"])
        assert state is not None and state.is_active is False
        assert events_named(captured_events, "user.deleted") == [{
            "user_id": target["id"],
            "actor_id": admin["id"],
            "role": "user",
            "is_active": False,
            "changed": ["deleted_at", "email", "is_active", "username"]
        }]

        assert (
            await login_response(client, target["email"])).status_code == 401
        assert (
            await client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": target["refresh_token"]
                      })).status_code == 401
        sent = len(mock_email)
        r = await client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": target["email"]})
        assert r.status_code == 200 and len(mock_email) == sent
        r = await client.post(
            "/api/v1/auth/password-reset/confirm", json={
                "token": reset_token,
                "new_password": "another-password-123"
            })
        assert r.status_code == 422

        listed = await client.get(
            f"{ADMIN}/users", headers=admin["headers"], params={"q": tid.hex})
        assert listed.json()["data"]["total"] == 0
        listed = await client.get(
            f"{ADMIN}/users", headers=admin["headers"], params={
                "q": tid.hex,
                "include_deleted": "true"
            })
        assert listed.json()["data"]["items"][0]["deleted_at"] is not None
        r = await client.get(
            f"{ADMIN}/users/{target['id']}", headers=admin["headers"])
        assert r.status_code == 200 and r.json()["data"]["deleted_at"]
        for call in (client.delete(f"{ADMIN}/users/{target['id']}",
                                   headers=admin["headers"]),
                     client.patch(f"{ADMIN}/users/{target['id']}",
                                  headers=admin["headers"], json={"bio": "x"}),
                     client.post(f"{ADMIN}/users/{target['id']}/reactivate",
                                 headers=admin["headers"])):
            assert (await call).status_code == 409
        r = await client.post(
            "/api/v1/auth/register", json={
                "email": target["email"],
                "username": target["username"],
                "password": PASSWORD
            })
        assert r.status_code == 201

    async def test_stats_combines_services(
            self, client, mock_email, db_session, monkeypatch):
        from config import settings
        import domains.admin.service as admin_service
        from shared.http_client import build_http_client

        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((
                request.url.host, request.url.path,
                request.headers.get("X-Internal-Token")))
            if request.url.host == httpx.URL(settings.CORE_SERVICE_URL).host:
                return httpx.Response(200, json={"portfolios": 3, "notes": 7})
            return httpx.Response(503, json={"detail": "down"})

        monkeypatch.setattr(
            admin_service, "_stats_http_client",
            lambda: build_http_client(transport=httpx.MockTransport(handler)))
        admin = await make_user(client, mock_email, db_session, role="admin")
        guest = await make_user(client, mock_email, db_session, role="guest")
        await client.post(
            f"{ADMIN}/users/{guest['id']}/deactivate",
            headers=admin["headers"])

        r = await client.get(f"{ADMIN}/stats", headers=admin["headers"])
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["core"] == {"portfolios": 3, "notes": 7}
        assert data["ai"] is None
        auth = data["auth"]
        assert set(
            auth["users_by_role"]) == {"admin", "moderator", "user", "guest"}
        assert auth["users_by_role"]["admin"] >= 1
        assert auth["users_by_role"]["guest"] >= 1
        assert auth["users_total"] >= auth["users_active"] + 1
        assert sum(auth["users_by_role"].values()) == auth["users_total"]
        assert {path for _, path, _ in seen} == {"/internal/stats"}
        assert all(
            token == settings.INTERNAL_API_TOKEN for _, _, token in seen)

        internal = await client.get("/internal/stats")
        assert internal.status_code == 404


class TestSetSystemRole:

    async def test_set_system_role_script(
            self, client, mock_email, db_session, monkeypatch, capsys):
        from scripts.set_system_role import set_system_role

        user = await make_user(client, mock_email, db_session)
        result = await set_system_role(
            db_session, user["email"].upper(), SystemRole.ADMIN)
        assert result.changed and not result.dry_run
        assert (result.old_role, result.new_role) == ("user", "admin")
        assert "'user' -> 'admin'" in result.describe()
        assert await stored_role(db_session, user["id"]) == "admin"
        assert await stored_user_state(user["id"]) == {
            "role": "admin",
            "is_active": True
        }

        again = await set_system_role(
            db_session, user["email"], SystemRole.ADMIN)
        assert not again.changed and "nothing to do" in again.describe()

        await reset_rate_limits()
        from scripts.set_system_role import RoleChangeRefused, set_system_role

        with pytest.raises(RoleChangeRefused, match="No account"):
            await set_system_role(
                db_session, "nobody@example.com", SystemRole.ADMIN)

        await demote_all_admins(db_session)
        admin = await make_user(client, mock_email, db_session, role="admin")
        with pytest.raises(RoleChangeRefused, match="last active admin"):
            await set_system_role(db_session, admin["email"], SystemRole.USER)
        other = await make_user(client, mock_email, db_session)
        await set_role(db_session, other["id"], "admin")
        result = await set_system_role(
            db_session, admin["email"], SystemRole.USER)
        assert result.changed

        victim = await make_user(client, mock_email, db_session)
        other_headers, _ = await login(client, other["email"])
        await client.delete(
            f"{ADMIN}/users/{victim['id']}", headers=other_headers)
        from domains.auth.repository import UserRepository

        row = await UserRepository(db_session).get_by_id(UUID(victim["id"]))
        with pytest.raises(RoleChangeRefused, match="deleted"):
            await set_system_role(db_session, row.email, SystemRole.ADMIN)

        await reset_rate_limits()
        import database
        import shared.redis
        from scripts import set_system_role as script

        async def _noop():
            return None

        monkeypatch.setattr(
            database, "SessionLocal", SessionFactory(db_session))
        monkeypatch.setattr(database, "engine", FakeEngine())
        monkeypatch.setattr(shared.redis, "close_redis_connections", _noop)

        user = await make_user(client, mock_email, db_session)
        code = await script._run(
            script._parse_args(
                ["--email", user["email"], "--role", "moderator",
                 "--dry-run"]))
        assert code == 0 and "[dry run]" in capsys.readouterr().out
        code = await script._run(
            script._parse_args(
                ["--email", user["email"], "--role", "moderator"]))
        assert code == 0
        assert "'user' -> 'moderator'" in capsys.readouterr().out
        assert await stored_role(db_session, user["id"]) == "moderator"

        code = await script._run(
            script._parse_args(
                ["--email", "unknown@example.com", "--role", "admin"]))
        assert code == 1 and "No account" in capsys.readouterr().err

        with pytest.raises(SystemExit):
            script._parse_args(["--email", "a@example.com", "--role", "owner"])


class TestNotificationSettings:

    async def test_internal_notification_settings_and_state_routes(
            self, client, mock_email, db_session):
        from domains.auth.repository import UserRepository

        user = await make_user(client, mock_email, db_session)
        url = f"/internal/users/{user['id']}/notification-settings"
        assert (await client.get(url)).json()["email"] == user["email"]
        row = await UserRepository(db_session).get_by_id(UUID(user["id"]))
        for field, value, restore in (("is_verified", False,
                                       True), ("is_active", False, True),
                                      ("deleted_at", datetime.now(UTC), None)):
            setattr(row, field, value)
            await db_session.flush()
            body = (await client.get(url)).json()
            assert body["email"] is None, field
            setattr(row, field, restore)
            await db_session.flush()
        assert (await client.get(url)).json()["email"] == user["email"]

        await reset_rate_limits()
        user = await make_user(
            client, mock_email, db_session, role="moderator")
        r = await client.get(f"/internal/users/{user['id']}/state")
        assert r.status_code == 200
        assert r.json() == {"role": "moderator", "is_active": True}

        from domains.auth.repository import UserRepository

        row = await UserRepository(db_session).get_by_id(UUID(user["id"]))
        row.is_active = False
        await db_session.flush()
        r = await client.get(f"/internal/users/{user['id']}/state")
        assert r.json() == {"role": "moderator", "is_active": False}

        unknown = await client.get(f"/internal/users/{uuid4()}/state")
        assert unknown.status_code == 404


class TestUserState:

    async def test_deactivation_reaches_tokens_and_failed_mirror_rolls_back(
            self, client, mock_email, db_session, state_check, monkeypatch,
            captured_events):
        mod = await make_user(client, mock_email, db_session, role="moderator")
        target = await make_user(client, mock_email, db_session)
        me = await client.get("/api/v1/auth/me", headers=target["headers"])
        assert me.status_code == 200

        r = await client.post(
            f"{ADMIN}/users/{target['id']}/deactivate", headers=mod["headers"])
        assert r.status_code == 200
        me = await client.get("/api/v1/auth/me", headers=target["headers"])
        assert me.status_code == 401
        assert me.json()["detail"] == "Account is deactivated"

        await client.post(
            f"{ADMIN}/users/{target['id']}/reactivate", headers=mod["headers"])
        me = await client.get("/api/v1/auth/me", headers=target["headers"])
        assert me.status_code == 200

        await reset_rate_limits()
        captured_events.clear()
        import user_state_sync

        admin = await make_user(client, mock_email, db_session, role="admin")
        target = await make_user(client, mock_email, db_session)
        commits = []
        real_commit = db_session.commit

        async def _commit():
            commits.append(1)
            await real_commit()

        async def _boom(*args, **kwargs):
            raise ConnectionError("redis down")

        monkeypatch.setattr(user_state_sync, "store_user_state", _boom)
        monkeypatch.setattr(db_session, "commit", _commit)
        try:
            r = await client.post(
                f"{ADMIN}/users/{target['id']}/deactivate",
                headers=admin["headers"])
            failed = r.status_code >= 500
        except ConnectionError:
            failed = True
        assert failed
        assert commits == []
        assert captured_events == []
        assert await stored_user_state(target["id"]) == {
            "role": "user",
            "is_active": True
        }

    async def test_startup_sync_writes_every_user_and_failure_is_not_fatal(
            self, client, mock_email, db_session, monkeypatch, tmp_path):
        from config import settings
        from user_state_sync import mirror_all_users

        users = [
            await make_user(client, mock_email, db_session) for _ in range(3)
        ]
        await set_role(db_session, users[1]["id"], "guest")
        from domains.auth.repository import UserRepository

        row = await UserRepository(db_session).get_by_id(UUID(users[2]["id"]))
        row.is_active = False
        await db_session.flush()
        for user in users:
            await get_events_client(settings).delete(
                user_state_key(user["id"]))

        written = await mirror_all_users(
            SessionFactory(db_session), batch_size=2)
        assert written >= 3
        assert await stored_user_state(users[0]["id"]) == {
            "role": "user",
            "is_active": True
        }
        assert await stored_user_state(users[1]["id"]) == {
            "role": "guest",
            "is_active": True
        }
        assert await stored_user_state(users[2]["id"]) == {
            "role": "user",
            "is_active": False
        }

        import main
        from user_state_sync import mirror_all_users_safely

        class _Broken:

            def __call__(self):
                raise RuntimeError("database down")

        await mirror_all_users_safely(_Broken())

        calls = []

        async def _noop(*args, **kwargs):
            return None

        async def _sync(factory):
            calls.append(factory)

        monkeypatch.setattr(main, "ensure_schema", _noop)
        monkeypatch.setattr(main, "mirror_all_users_safely", _sync)
        monkeypatch.setattr(main, "engine", FakeEngine())
        monkeypatch.setattr(main, "close_redis_connections", _noop)
        monkeypatch.setattr(main, "close_http_clients", _noop)
        monkeypatch.setattr(
            main.settings, "AVATAR_STORAGE_DIR", str(tmp_path / "av"))
        async with main.lifespan(main.app):
            pass
        assert calls == [main.SessionLocal]


class TestAuthEvents:

    async def test_organization_events(
            self, client, mock_email, db_session, captured_events):
        owner = await make_user(client, mock_email, db_session)
        alice = await make_user(client, mock_email, db_session)
        bob = await make_user(client, mock_email, db_session)

        org = (
            await
            client.post(ORGS, json={"name": "Fund"},
                        headers=owner["headers"])).json()["data"]
        oid = org["id"]

        def base(actor, user_id, role, members):
            return {
                "organization_id": oid,
                "organization_name": "Fund",
                "actor_id": actor,
                "user_id": user_id,
                "role": role,
                "member_ids": sorted(members)
            }

        assert events_named(captured_events, "organization.created") == [
            base(owner["id"], owner["id"], "owner", [owner["id"]])
        ]

        await invite_and_join(
            client, mock_email, oid, owner["headers"], alice, "member")
        invited = events_named(
            captured_events, "organization.invitation.created")
        assert invited == [
            base(owner["id"], alice["id"], "member", [owner["id"]])
        ]
        joined = events_named(captured_events, "organization.member.joined")
        assert joined == [
            base(
                alice["id"], alice["id"], "member", [owner["id"], alice["id"]])
        ]

        r = await client.post(
            f"{ORGS}/{oid}/invitations", headers=owner["headers"], json={
                "email": "nobody-yet@example.com",
                "role": "viewer"
            })
        assert r.status_code == 201
        last_invited = events_named(
            captured_events, "organization.invitation.created")[-1]
        assert last_invited == base(
            owner["id"], None, "viewer", [owner["id"], alice["id"]])

        r = await client.patch(
            f"{ORGS}/{oid}", headers=owner["headers"],
            json={"name": "Fund II"})
        assert r.status_code == 200
        updated = events_named(captured_events, "organization.updated")
        assert updated == [{
            **base(owner["id"], None, None, [owner["id"], alice["id"]]),
            "organization_name": "Fund II"
        }]

        r = await client.patch(
            f"{ORGS}/{oid}/members/{alice['id']}/role",
            headers=owner["headers"], json={"role": "viewer"})
        assert r.status_code == 200
        changed = events_named(captured_events, "organization.role.changed")
        assert changed == [
            {
                **base(
                    owner["id"], alice["id"], "viewer", [
                        owner["id"], alice["id"]
                    ]), "organization_name": "Fund II"
            }
        ]
        await client.patch(
            f"{ORGS}/{oid}/members/{alice['id']}/role",
            headers=owner["headers"], json={"role": "viewer"})
        assert (
            len(events_named(captured_events,
                             "organization.role.changed")) == 1)

        r = await client.post(f"{ORGS}/{oid}/leave", headers=alice["headers"])
        assert r.status_code == 204
        assert events_named(captured_events,
                            "organization.member.left") == [{
                                **base(
                                    alice["id"], alice["id"], "viewer", [
                                        owner["id"]
                                    ]), "organization_name": "Fund II"
                            }]

        await invite_and_join(
            client, mock_email, oid, owner["headers"], bob, "member")
        r = await client.post(
            f"{ORGS}/{oid}/members/{bob['id']}/remove",
            headers=owner["headers"])
        assert r.status_code == 204
        removed = events_named(captured_events, "organization.member.removed")
        assert removed == [{
            **base(owner["id"], bob["id"], "member", [owner["id"]]),
            "organization_name": "Fund II"
        }]

        await invite_and_join(
            client, mock_email, oid, owner["headers"], bob, "member")
        r = await client.delete(f"{ORGS}/{oid}", headers=owner["headers"])
        assert r.status_code == 204
        assert events_named(captured_events,
                            "organization.deleted") == [{
                                **base(
                                    owner["id"], None, None, [
                                        owner["id"], bob["id"]
                                    ]), "organization_name": "Fund II"
                            }]

    async def test_friend_and_self_service_update_events(
            self, client, mock_email, db_session, captured_events, monkeypatch,
            tmp_path):
        a = await make_user(client, mock_email, db_session)
        b = await make_user(client, mock_email, db_session)
        c = await make_user(client, mock_email, db_session)

        r = await client.post(
            "/api/v1/connections", headers=a["headers"],
            json={"addressee_username": b["username"]})
        conn_id = r.json()["data"]["id"]
        assert events_named(captured_events, "friend.requested") == [{
            "actor_id": a["id"],
            "user_id": b["id"],
            "actor_username": a["username"]
        }]

        await client.post(
            f"/api/v1/connections/{conn_id}/accept", headers=b["headers"])
        assert events_named(captured_events, "friend.accepted") == [{
            "actor_id": b["id"],
            "user_id": a["id"],
            "actor_username": b["username"]
        }]

        await client.delete(
            f"/api/v1/connections/{conn_id}", headers=a["headers"])
        assert events_named(captured_events, "friend.removed") == [{
            "actor_id": a["id"],
            "user_id": b["id"],
            "actor_username": a["username"]
        }]

        r = await client.post(
            "/api/v1/connections", headers=c["headers"],
            json={"addressee_username": a["username"]})
        await client.delete(
            f"/api/v1/connections/{r.json()['data']['id']}",
            headers=a["headers"])
        assert len(events_named(captured_events, "friend.removed")) == 1

        await reset_rate_limits()
        captured_events.clear()
        from config import settings

        user = await make_user(client, mock_email, db_session)
        h = user["headers"]

        def expect(changed):
            return {
                "user_id": user["id"],
                "actor_id": user["id"],
                "role": "user",
                "is_active": True,
                "changed": changed
            }

        r = await client.patch(
            "/api/v1/auth/me", headers=h, json={
                "bio": "secret bio",
                "theme": "dark",
                "first_name": None
            })
        assert r.status_code == 200
        events = events_named(captured_events, "user.updated")
        assert events == [expect(["bio", "theme"])]
        assert "secret bio" not in str(events)

        await client.patch(
            "/api/v1/auth/me", headers=h, json={"theme": "dark"})
        assert len(events_named(captured_events, "user.updated")) == 1

        monkeypatch.setattr(settings, "AVATAR_STORAGE_DIR", str(tmp_path))
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        r = await client.post(
            "/api/v1/auth/me/avatar", headers=h,
            files={"file": ("a.png", png, "image/png")})
        assert r.status_code == 200, r.text
        assert events_named(captured_events,
                            "user.updated")[-1] == expect(["avatar_url"])

        setup = await client.post("/api/v1/auth/2fa/setup", headers=h)
        secret = setup.json()["data"]["secret"]
        r = await client.post(
            "/api/v1/auth/2fa/verify", headers=h,
            json={"code": pyotp.TOTP(secret).now()})
        assert r.status_code == 204
        assert events_named(captured_events, "user.updated")[-1] == expect(
            ["two_factor_enabled"])
        count = len(events_named(captured_events, "user.updated"))
        r = await client.post(
            "/api/v1/auth/2fa/verify", headers=h,
            json={"code": pyotp.TOTP(secret).at(time.time() + 30)})
        assert r.status_code == 204
        assert len(events_named(captured_events, "user.updated")) == count

        await client.post(
            "/api/v1/auth/password-reset/request",
            json={"email": user["email"]})
        token = mock_email[-1]["html_body"].split("token=")[1]
        token = token.split('"')[0].split("<")[0].split("&")[0].strip()
        r = await client.post(
            "/api/v1/auth/password-reset/confirm", json={
                "token": token,
                "new_password": "brand-new-password-1"
            })
        assert r.status_code == 200, r.text
        assert events_named(captured_events,
                            "user.updated")[-1] == expect(["password"])
