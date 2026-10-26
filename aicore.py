"""Resolve an SAP AI Core destination via the BTP Destination service and call AI Core with it.

Credentials for the *Destination service* come from the environment:
  - On Cloud Foundry: the bound `destination` service in VCAP_SERVICES.
  - Locally: VCAP_SERVICES in default-env.json, or a destination service key in
    destination-key.json / the DESTINATION_SERVICE_KEY env var.
The AI Core credentials themselves live in the destination, so none are needed here.
"""
import json
import os
import time

import requests

TIMEOUT = 60
_cache = {}


class StepError(Exception):
    """Error tied to a specific diagnostic step."""

    def __init__(self, step, message, detail=None):
        super().__init__(message)
        self.step = step
        self.detail = detail


def _destination_service_credentials():
    vcap = os.environ.get("VCAP_SERVICES")
    if not vcap and os.path.exists("default-env.json"):
        with open("default-env.json", encoding="utf-8") as f:
            vcap = json.dumps(json.load(f).get("VCAP_SERVICES", {}))
    if vcap:
        services = json.loads(vcap).get("destination") or []
        if services:
            return services[0]["credentials"]

    fields = {
        "clientid": os.environ.get("DESTINATION_CLIENT_ID"),
        "clientsecret": os.environ.get("DESTINATION_CLIENT_SECRET"),
        "url": os.environ.get("DESTINATION_AUTH_URL"),
        "uri": os.environ.get("DESTINATION_URI"),
    }
    if any(fields.values()):
        missing = [k for k, v in fields.items() if not v]
        if missing:
            raise StepError("destination-service", f"Incomplete Destination service settings in .env, missing: {missing}")
        return fields

    key = os.environ.get("DESTINATION_SERVICE_KEY")
    if not key and os.path.exists("destination-key.json"):
        with open("destination-key.json", encoding="utf-8") as f:
            key = f.read()
    if key:
        return json.loads(key)

    raise StepError(
        "destination-service",
        "No Destination service credentials found. Fill DESTINATION_CLIENT_ID, DESTINATION_CLIENT_SECRET, "
        "DESTINATION_AUTH_URL and DESTINATION_URI in .env (or put the service key in destination-key.json). "
        "On Cloud Foundry, bind a 'destination' service instance instead.",
    )


def _short(resp):
    try:
        return resp.json()
    except ValueError:
        return resp.text[:2000]


def get_destination(name, force=False):
    """Return {'url', 'headers', 'resource_group', 'auth_type', 'raw'} for the destination."""
    cached = _cache.get(name)
    if cached and not force and cached["expires"] > time.time():
        return cached["value"]

    creds = _destination_service_credentials()

    # 1. Token for the Destination service itself
    try:
        tok = requests.post(
            creds["url"].rstrip("/") + "/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(creds["clientid"], creds["clientsecret"]),
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise StepError("destination-service", f"Cannot reach XSUAA: {e}")
    if tok.status_code != 200:
        raise StepError("destination-service", f"XSUAA token request failed ({tok.status_code})", _short(tok))

    # 2. "Find destination" - also returns ready-to-use auth tokens for OAuth destinations
    try:
        resp = requests.get(
            f"{creds['uri'].rstrip('/')}/destination-configuration/v1/destinations/{name}",
            headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise StepError("destination-lookup", f"Cannot reach Destination service: {e}")
    if resp.status_code == 404:
        raise StepError("destination-lookup", f"Destination '{name}' not found in this subaccount/instance", _short(resp))
    if resp.status_code != 200:
        raise StepError("destination-lookup", f"Destination lookup failed ({resp.status_code})", _short(resp))

    body = resp.json()
    config = body.get("destinationConfiguration", {})
    url = config.get("URL", "").rstrip("/")
    if url.endswith("/v2"):
        url = url[:-3]
    if not url:
        raise StepError("destination-lookup", "Destination has no URL", config)

    headers = {}
    for t in body.get("authTokens") or []:
        if t.get("error"):
            raise StepError(
                "destination-auth",
                f"Destination service could not fetch a token for '{name}': {t['error']}",
                {k: v for k, v in t.items() if k != "value"},
            )
        header = t.get("http_header") or {"key": "Authorization", "value": f"{t['type']} {t['value']}"}
        headers[header["key"]] = header["value"]
    if "Authorization" not in headers and config.get("Authentication") != "NoAuthentication":
        raise StepError("destination-auth", "Destination returned no auth token", {"Authentication": config.get("Authentication")})

    # Honour "URL.headers.<name>" additional properties (e.g. URL.headers.AI-Resource-Group)
    for k, v in config.items():
        if k.startswith("URL.headers."):
            headers[k[len("URL.headers."):]] = v
    resource_group = headers.pop("AI-Resource-Group", None) or os.environ.get("AI_RESOURCE_GROUP", "default")

    value = {
        "url": url,
        "headers": headers,
        "resource_group": resource_group,
        "auth_type": config.get("Authentication"),
        "raw": {k: v for k, v in config.items() if "secret" not in k.lower() and "password" not in k.lower()},
    }
    expires_in = min([int(t.get("expires_in", 300)) for t in body.get("authTokens") or []] or [300])
    _cache[name] = {"value": value, "expires": time.time() + max(expires_in - 60, 30)}
    return value


def _ai_headers(dest, resource_group=None):
    return {
        **dest["headers"],
        "AI-Resource-Group": resource_group or dest["resource_group"],
        "Content-Type": "application/json",
    }


def list_deployments(dest, resource_group=None):
    try:
        resp = requests.get(
            dest["url"] + "/v2/lm/deployments",
            params={"status": "RUNNING"},
            headers=_ai_headers(dest, resource_group),
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise StepError("ai-core", f"Cannot reach AI Core at {dest['url']}: {e}")
    if resp.status_code != 200:
        raise StepError("ai-core", f"AI Core rejected the request ({resp.status_code})", _short(resp))

    out = []
    for d in resp.json().get("resources", []):
        model = (d.get("details") or {}).get("resources", {}).get("backend_details", {}).get("model", {})
        out.append({
            "id": d["id"],
            "scenario": d.get("scenarioId"),
            "executable": d.get("executableId"),
            "model": model.get("name"),
            "url": d.get("deploymentUrl"),
            "status": d.get("status"),
        })
    return out


def chat(dest, deployment, messages, model=None, resource_group=None):
    """Send the conversation to a deployment. Returns the assistant reply text."""
    headers = _ai_headers(dest, resource_group)
    base = deployment["url"].rstrip("/")
    system = [m for m in messages if m["role"] == "system"]
    convo = [m for m in messages if m["role"] != "system"]

    if deployment["scenario"] == "orchestration":
        last = convo[-1]["content"]
        body = {
            "config": {"modules": {"prompt_templating": {
                "prompt": {"template": system + [{"role": "user", "content": "{{?user_input}}"}]},
                "model": {"name": model or "gpt-4o"},
            }}},
            "placeholder_values": {"user_input": last},
            "messages_history": convo[:-1],
        }
        resp = requests.post(base + "/v2/completion", json=body, headers=headers, timeout=TIMEOUT)
        _raise(resp)
        data = resp.json()
        result = data.get("final_result") or data.get("orchestration_result") or {}
        return result["choices"][0]["message"]["content"]

    if deployment["executable"] == "aws-bedrock":
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": convo,
        }
        if system:
            body["system"] = "\n".join(m["content"] for m in system)
        resp = requests.post(base + "/invoke", json=body, headers=headers, timeout=TIMEOUT)
        _raise(resp)
        return "".join(c.get("text", "") for c in resp.json().get("content", []))

    # Azure OpenAI and other OpenAI-compatible foundation-model deployments
    params = {"api-version": "2024-10-21"} if deployment["executable"] == "azure-openai" else None
    resp = requests.post(base + "/chat/completions", params=params, json={"messages": messages}, headers=headers, timeout=TIMEOUT)
    _raise(resp)
    return resp.json()["choices"][0]["message"]["content"]


def _raise(resp):
    if resp.status_code >= 400:
        raise StepError("llm", f"LLM call failed ({resp.status_code})", _short(resp))
