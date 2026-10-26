import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()  # read .env before anything looks at os.environ

import aicore  # noqa: E402

DESTINATION_NAME = os.environ.get("AICORE_DESTINATION", "")

app = Flask(__name__, static_folder="static")


def _error(e, status=502):
    if isinstance(e, aicore.StepError):
        return jsonify({"ok": False, "step": e.step, "error": str(e), "detail": e.detail}), status
    return jsonify({"ok": False, "step": "app", "error": f"{type(e).__name__}: {e}"}), 500


def _dest_name(payload):
    name = (payload.get("destination") or DESTINATION_NAME).strip()
    if not name:
        raise aicore.StepError("config", "No destination name given (set AICORE_DESTINATION or enter it in the UI)")
    return name


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify({"destination": DESTINATION_NAME, "resourceGroup": os.environ.get("AI_RESOURCE_GROUP", "")})


@app.post("/api/check")
def check():
    """Resolve the destination and list running deployments - proves the destination works end to end."""
    payload = request.get_json(silent=True) or {}
    try:
        name = _dest_name(payload)
        dest = aicore.get_destination(name, force=True)
        deployments = aicore.list_deployments(dest, payload.get("resourceGroup") or None)
    except Exception as e:  # noqa: BLE001 - report every failure to the UI
        return _error(e)
    return jsonify({
        "ok": True,
        "destination": {
            "name": name,
            "url": dest["url"],
            "authentication": dest["auth_type"],
            "resourceGroup": payload.get("resourceGroup") or dest["resource_group"],
            "properties": dest["raw"],
        },
        "deployments": deployments,
    })


@app.post("/api/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    try:
        name = _dest_name(payload)
        dest = aicore.get_destination(name)
        rg = payload.get("resourceGroup") or None
        deployment = next((d for d in aicore.list_deployments(dest, rg) if d["id"] == payload.get("deploymentId")), None)
        if not deployment:
            raise aicore.StepError("config", "Deployment not found or not RUNNING - run the destination check again")
        messages = [{"role": "system", "content": "You are a helpful, concise general-purpose assistant."}]
        messages += [{"role": m["role"], "content": m["content"]} for m in payload.get("messages", [])]
        reply = aicore.chat(dest, deployment, messages, payload.get("model") or deployment["model"], rg)
    except Exception as e:  # noqa: BLE001
        return _error(e)
    return jsonify({"ok": True, "reply": reply})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
