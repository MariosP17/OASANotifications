import json
import subprocess
from pathlib import Path
from fastapi import FastAPI, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

SERVICE_NAME = "oasa-notifications.service"
CONFIG_FILE = Path("../user_settings.json")

app.mount("/static", StaticFiles(directory="static"), name="static")

def load_settings():
    if not CONFIG_FILE.exists():
        return {"times": [], "tracked_routes_codes": {}}

    try:
        return json.loads(CONFIG_FILE.read_text())
    except json.JSONDecodeError:
        return {"times": [], "tracked_routes_codes": {}}


def get_service_status():
    result = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "settings": load_settings(),
        },
    )


@app.get("/service/status", response_class=HTMLResponse)
def status():
    st = get_service_status()
    color = "green" if st == "active" else "red"
    return f'<span style="color: {color}; font-weight: bold;">{st}</span>'


@app.post("/service/{action}", response_class=HTMLResponse)
def control_service(action: str):
    if action in ["start", "stop", "restart"]:
        subprocess.run(["sudo", "systemctl", action, SERVICE_NAME], check=True)
    return status()


@app.get("/service/logs", response_class=HTMLResponse)
def logs():
    result = subprocess.run(
        ["journalctl", "-u", SERVICE_NAME, "-n", "30", "--no-pager"],
        stdout=subprocess.PIPE,
        text=True,
    )
    return f"<pre><code>{result.stdout}</code></pre>"


@app.post("/settings/save", response_class=HTMLResponse)
def save_settings(raw_json: str = Form(...)):
    try:
        parsed = json.loads(raw_json)
        CONFIG_FILE.write_text(json.dumps(parsed, indent=2))
        return '<span style="color: green;">Saved successfully!</span>'
    except json.JSONDecodeError as e:
        return f'<span style="color: red;">Invalid JSON: {e}</span>'