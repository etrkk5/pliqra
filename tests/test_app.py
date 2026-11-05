import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from pliqra.main import create_app


class Fragments(httpx.AsyncByteStream):
    def __init__(self, value):
        self.value = value.encode()

    async def __aiter__(self):
        for index in range(0, len(self.value), 11):
            yield self.value[index:index+11]


@pytest.fixture
def workspace(tmp_path):
    captured = []

    def upstream(request):
        captured.append(request)
        path = request.url.path
        if request.url.host == "offline.test":
            raise httpx.ConnectError("unavailable", request=request)
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "quartz-mini"}]})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "local-quartz"}]})
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": "https://identity.test", "authorization_endpoint": "https://identity.test/authorize", "token_endpoint": "https://identity.test/token", "jwks_uri": "https://identity.test/jwks"})
        if path == "/v1/chat/completions":
            body = json.loads(request.content)
            if body["model"] == "unavailable":
                return httpx.Response(503)
            if body.get("stream"):
                chunk = {"id": "upstream", "choices": [{"index": 0, "delta": {"content": "Hello, dünya!"}, "finish_reason": None}]}
                output = "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                if body["messages"][-1]["content"] != "truncate":
                    output += 'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7}}\n\ndata: [DONE]\n\n'
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Fragments(output))
            return httpx.Response(200, json={"id": "upstream", "model": body["model"], "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 7}})
        if path == "/api/chat":
            body = json.loads(request.content)
            value = {"model": body["model"], "message": {"role": "assistant", "content": "Local response"}, "done": True, "prompt_eval_count": 10, "eval_count": 5}
            if body.get("stream"):
                return httpx.Response(200, stream=Fragments(json.dumps(value) + "\n"))
            return httpx.Response(200, json=value)
        return httpx.Response(404)

    app = create_app(tmp_path)
    with TestClient(app) as client:
        mock = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app.state.client = mock
        app.state.gateway.client = mock
        setup = client.post("/api/v1/setup", json={"name": "Test Owner", "email": "owner@example.test", "password": "long-local-password"})
        assert setup.status_code == 200
        client.headers["X-Pliqra-CSRF"] = setup.json()["csrf"]
        yield client, app, captured
        asyncio.run(mock.aclose())


def connect(client, endpoint="https://models.test", **values):
    response = client.post("/api/v1/connections", json={"name": "Inference server", "endpoint": endpoint, "api_key": "provider-secret-value", **values})
    assert response.status_code == 201, response.text
    return response.json()


def call(client, service, **values):
    return client.post("/v1/chat/completions", json={"model": service, "messages": [{"role": "user", "content": "Private prompt that must not be stored"}], **values})


def price(client):
    deployment = client.get("/api/v1/connections").json()[0]["models"][0]
    response = client.patch("/api/v1/deployments/" + deployment["id"], json={"capabilities": ["chat", "streaming"], "input_price": 2, "output_price": 8})
    assert response.status_code == 200
    return deployment


def key(client, **values):
    response = client.post("/api/v1/keys", json={"name": "Application", **values})
    assert response.status_code == 201, response.text
    return response.json()


def test_endpoint_discovery_encryption_and_private_content(workspace):
    client, app, captured = workspace
    result = connect(client)
    service = result["services"][0]["name"]
    assert captured[0].url.path == "/models"
    assert captured[1].url.path == "/v1/models"
    connections = client.get("/api/v1/connections").json()
    assert "secret" not in connections[0]
    assert connections[0]["has_secret"] is True
    stored = app.state.store.one("SELECT secret FROM connections")
    assert stored["secret"] != "provider-secret-value"
    assert app.state.vault.decrypt(stored["secret"]) == "provider-secret-value"
    response = call(client, service)
    assert response.status_code == 200
    assert captured[-1].headers["authorization"] == "Bearer provider-secret-value"
    event = client.get("/api/v1/events").json()["items"][0]
    assert event["status"] == "success"
    assert event["input_tokens"] == 12
    assert event["cost_micros"] is None
    assert "Private prompt" not in json.dumps(event)
    assert "Hello!" not in json.dumps(event)
    assert any(item.get("selected") for item in event["decisions"])
    assert client.get("/api/v1/connections").json()[0]["models"][0]["verified"] == ["chat"]


def test_stream_fragmentation_usage_and_incomplete_stream(workspace):
    client, _, _ = workspace
    connected = connect(client)
    price(client)
    service = connected["services"][0]["name"]
    response = call(client, service, stream=True)
    assert response.status_code == 200
    assert "Hello, dünya!" in response.text
    assert "[DONE]" in response.text
    event = client.get("/api/v1/events/" + response.headers["x-request-id"]).json()
    assert event["status"] == "success"
    assert event["ttft_ms"] is not None
    assert event["cost_micros"] == 80
    response = call(client, service, stream=True, messages=[{"role": "user", "content": "truncate"}])
    assert "incomplete_stream" in response.text
    event = client.get("/api/v1/events/" + response.headers["x-request-id"]).json()
    assert event["status"] == "incomplete"
    assert event["usage_source"] == "unknown"
    assert event["cost_micros"] is None


def test_protocol_binding_identity_capability_and_change_detection(workspace):
    client, _, _ = workspace
    result = connect(client)
    service = result["services"][0]["name"]
    rejected = client.post("/protocol/v1/bindings", json={"service": service, "requirements": ["vision"]})
    assert rejected.status_code == 422
    token = key(client)["token"]
    bearer = {"Authorization": "Bearer " + token}
    binding = client.post("/protocol/v1/bindings", headers=bearer, json={"service": service, "requirements": ["streaming"]}).json()
    invocation = {"model": service, "messages": [{"role": "user", "content": "hello"}]}
    assert client.post(binding["invoke"], json=invocation).status_code == 403
    assert client.post(binding["invoke"], headers=bearer, json=invocation).status_code == 200
    price(client)  # Model metadata changes must invalidate existing contracts even if the service version is unchanged.
    changed = client.post(binding["invoke"], headers=bearer, json=invocation)
    assert changed.status_code == 409
    assert "changed" in changed.text


def test_api_key_scopes_rate_limit_and_revocation(workspace):
    client, _, _ = workspace
    a = connect(client)
    b = connect(client)
    created = key(client, allowed_ports=[a["services"][0]["id"]], rpm=1)
    headers = {"Authorization": "Bearer " + created["token"]}
    models = client.get("/v1/models", headers=headers).json()["data"]
    assert [m["id"] for m in models] == [a["services"][0]["name"]]
    assert client.post("/v1/chat/completions", headers=headers, json={"model": b["services"][0]["name"], "messages": [{"role": "user", "content": "hello"}]}).status_code == 403
    body = {"model": a["services"][0]["name"], "messages": [{"role": "user", "content": "hello"}]}
    assert client.post("/v1/chat/completions", headers=headers, json=body).status_code == 200
    assert client.post("/v1/chat/completions", headers=headers, json=body).status_code == 429
    assert client.get("/api/v1/connections", headers=headers).status_code == 403
    client.delete("/api/v1/keys/" + created["id"])
    assert client.get("/v1/models", headers=headers).status_code == 401


def test_csrf_last_admin_validation_and_oidc_metadata(workspace):
    client, _, _ = workspace
    csrf = client.headers.pop("X-Pliqra-CSRF")
    assert client.post("/api/v1/projects", json={"name": "Blocked"}).status_code == 403
    client.headers["X-Pliqra-CSRF"] = csrf
    assert client.post("/api/v1/projects", json={"name": "Blocked"}, headers={"Origin": "https://evil.test"}).status_code == 403
    user = client.get("/api/v1/users").json()[0]
    assert client.patch("/api/v1/users/" + user["id"], json={"role": "viewer", "active": True}).status_code == 409
    secret = "do-not-echo-this-secret"
    response = client.post("/api/v1/users", json={"name": "X", "email": "test@example.test", "password": secret, "role": "superadmin"})
    assert response.status_code == 422
    assert secret not in response.text
    provider = client.post("/api/v1/identity-providers", json={"name": "Company", "issuer": "https://identity.test", "client_id": "client", "client_secret": secret})
    assert provider.status_code == 201
    assert secret not in client.get("/api/v1/identity-providers").text
    assert client.post("/api/v1/setup", json={"name": "Other", "email": "other@example.test", "password": "another-long-password"}).status_code == 409


def test_ollama_adapter_and_unknown_provider(workspace):
    client, _, _ = workspace
    result = connect(client, endpoint="https://ollama.test", provider="ollama")
    service = result["services"][0]["name"]
    assert result["provider"] == "ollama"
    response = call(client, service)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Local response"
    streamed = call(client, service, stream=True)
    assert "[DONE]" in streamed.text
    assert client.get("/api/v1/events").json()["items"][0]["input_tokens"] == 10
    response = client.post("/api/v1/connections/discover", json={"name": "Bad", "endpoint": "file:///etc/passwd"})
    assert response.status_code == 422


def test_fallback_and_preview_without_sending_inference(workspace):
    client, _, captured = workspace
    good = connect(client)
    bad = connect(client, model="unavailable", endpoint="https://models.test/v1")
    deployments = client.get("/api/v1/connections").json()
    bad_id = next(c["models"][0]["id"] for c in deployments if c["id"] == bad["id"])
    good_id = next(c["models"][0]["id"] for c in deployments if c["id"] == good["id"])
    created = client.post("/api/v1/ports", json={"name": "resilient", "deployments": [bad_id, good_id]})
    assert created.status_code == 201
    response = call(client, "resilient", stream=True)
    assert response.status_code == 200
    event = client.get("/api/v1/events/" + response.headers["x-request-id"]).json()
    assert event["model"] == "quartz-mini"
    assert any("503" in decision.get("outcome", "") for decision in event["decisions"])
    count = len(captured)
    preview = client.post("/api/v1/ports/" + created.json()["id"] + "/preview", json={"name": "resilient", "deployments": [good_id], "requirements": ["tools"]})
    assert preview.status_code == 200
    assert preview.json()["ready"] is False
    assert len(captured) == count


def test_budget_admission_usage_export_and_role_boundary(workspace):
    client, app, _ = workspace
    result = connect(client)
    service = result["services"][0]["name"]
    project = client.post("/api/v1/projects", json={"name": "Budgeted", "monthly_budget": 0}).json()
    token = key(client, project_id=project["id"])["token"]
    headers = {"Authorization": "Bearer " + token}
    body = {"model": service, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 5}
    assert client.post("/v1/chat/completions", headers=headers, json=body).status_code == 422
    price(client)
    assert client.post("/v1/chat/completions", headers=headers, json=body).status_code == 402
    client.patch("/api/v1/projects/" + project["id"], json={"name": "Budgeted", "monthly_budget": 1})
    assert client.post("/v1/chat/completions", headers=headers, json=body).status_code == 200
    assert client.get("/api/v1/usage").json()["summary"]["cost_micros"] == 80
    exported = client.get("/api/v1/usage.csv")
    assert exported.status_code == 200
    assert "quartz-mini" in exported.text
    client.post("/api/v1/users", json={"name": "Viewer", "email": "viewer@example.test", "password": "viewer-long-password", "role": "viewer"})
    client.post("/api/v1/logout", json={})
    login = client.post("/api/v1/login", json={"email": "viewer@example.test", "password": "viewer-long-password"})
    client.headers["X-Pliqra-CSRF"] = login.json()["csrf"]
    assert client.get("/api/v1/usage").status_code == 200
    assert client.post("/api/v1/projects", json={"name": "Forbidden"}).status_code == 403
    assert client.get("/api/v1/keys").status_code == 403


def test_persistence_cookie_and_secret_key_survive_restart(tmp_path):
    with TestClient(create_app(tmp_path)) as first:
        initialized = first.post("/api/v1/setup", json={"name": "Owner", "email": "owner@example.test", "password": "long-local-password"})
        cookie = first.cookies.get("plq_session")
        assert initialized.status_code == 200
    with TestClient(create_app(tmp_path)) as second:
        second.cookies.set("plq_session", cookie)
        assert second.get("/api/v1/session").json()["user"]["name"] == "Owner"
        assert second.get("/api/v1/session").json()["setup_required"] is False


def test_oidc_round_trip_pkce_nonce_and_safe_default_role(workspace):
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    client, app, _ = workspace
    result = client.post("/api/v1/identity-providers", json={"name": "Company", "issuer": "https://identity.test", "client_id": "pliqra-client", "client_secret": "test-client-secret"})
    provider_id = result.json()["id"]
    signing_key = RSAKey.generate_key(parameters={"kid": "test-signing-key"})
    flow = {}
    metadata = {"issuer": "https://identity.test", "authorization_endpoint": "https://identity.test/authorize", "token_endpoint": "https://identity.test/token", "jwks_uri": "https://identity.test/jwks", "id_token_signing_alg_values_supported": ["RS256"]}

    def identity_server(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=metadata)
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [signing_key.as_dict()]})
        if request.url.path == "/token":
            values = parse_qs(request.content.decode())
            assert values["code_verifier"][0]
            assert values["code"][0] == "test-code"
            claims = {"iss": "https://identity.test", "sub": "employee-stable-id", "aud": "pliqra-client", "exp": int(time.time()) + 300, "iat": int(time.time()), "nonce": flow["nonce"], "email": "employee@example.test", "name": "SSO Employee", "role": "admin"}
            signed = jwt.encode({"alg": "RS256", "kid": "test-signing-key"}, claims, signing_key)
            return httpx.Response(200, json={"access_token": "test-upstream-token", "token_type": "Bearer", "expires_in": 300, "id_token": signed})
        return httpx.Response(404)

    app.state.oauth.register("oidc_" + provider_id, client_id="pliqra-client", client_secret="test-client-secret", server_metadata_url="https://identity.test/.well-known/openid-configuration", client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256", "transport": httpx.MockTransport(identity_server)})
    redirect = client.get(f"/auth/oidc/{provider_id}/login", follow_redirects=False)
    assert redirect.status_code == 302
    params = parse_qs(urlsplit(redirect.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    flow["nonce"] = params["nonce"][0]
    callback = client.get(f"/auth/oidc/{provider_id}/callback?code=test-code&state={params['state'][0]}", follow_redirects=False)
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"
    session = client.get("/api/v1/session").json()
    assert session["user"]["name"] == "SSO Employee"
    assert session["user"]["role"] == "viewer"  # Never trust arbitrary role claims from the IdP.
    assert session["user"]["issuer"] == provider_id
