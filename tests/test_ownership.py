from .conftest import basic, make_ics

A = basic("alice", "alicepw")
B = basic("bob", "bobpw")
D = basic("dave", "davepw")


def test_non_owner_cannot_modify(client, seed):
    assert client.put("/dav/u/bob/team/e1.ics", headers=B, content=make_ics("e1")).status_code == 201
    # dave is not a member at all
    assert client.put("/dav/u/dave/team/e1.ics", headers=D, content=make_ics("e1")).status_code == 404

    # carol is a read-only member (signs in with her token in the URL)
    carol = f"/dav/t/{seed.carol.token}/team"
    assert client.get(f"{carol}/e1.ics").status_code == 200
    assert client.put(f"{carol}/e1.ics", content=make_ics("e1", summary="Hijack")).status_code == 403
    assert client.put(f"{carol}/new.ics", content=make_ics("new")).status_code == 403
    assert client.delete(f"{carol}/e1.ics").status_code == 403


def test_other_writer_cannot_modify_but_admin_can(client, seed):
    client.put("/dav/u/bob/team/e1.ics", headers=B, content=make_ics("e1"))
    # make alice a non-admin writer first
    with client.app.state.sessionmaker() as db:
        from sharedcal.models import User
        db.get(User, seed.alice.id).is_admin = False
        db.commit()
    assert client.put("/dav/u/alice/team/e1.ics", headers=A, content=make_ics("e1", summary="X")).status_code == 403
    assert client.delete("/dav/u/alice/team/e1.ics", headers=A).status_code == 403

    with client.app.state.sessionmaker() as db:
        db.get(User, seed.alice.id).is_admin = True
        db.commit()
    assert client.put("/dav/u/alice/team/e1.ics", headers=A, content=make_ics("e1", summary="X")).status_code == 204
    # the event still belongs to bob after the admin edit
    assert b"X-SHAREDCAL-OWNER:bob" in client.get("/dav/u/alice/team/e1.ics", headers=A).content
    assert client.delete("/dav/u/alice/team/e1.ics", headers=A).status_code == 204


def test_token_write_assigns_owner(client, seed):
    r = client.put(f"/dav/t/{seed.bob.token}/team/anon.ics", content=make_ics("anon"))
    assert r.status_code == 201
    assert b"Created by Bob" in client.get("/dav/u/alice/team/anon.ics", headers=A).content
