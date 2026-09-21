from datetime import datetime
import json
import subprocess
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from crontab import CronTab

# Access the current user's crontab. 
# Use user='username' for a specific user (requires root privileges).
cron = CronTab(user=True)

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

@app.get("/UUID", response_class=HTMLResponse)
def generate_uuid():
    return str(uuid4())

@app.post("/settings/save", response_class=HTMLResponse)
def save_settings(raw_json: str = Form(...)):
    try:
        parsed = json.loads(raw_json)
        removeDeletedCronJobs(parsed.get("times", []))
        checkUidsAndGenerate(parsed.get("times", []))
        editCron(parsed.get("times", []))
        CONFIG_FILE.write_text(json.dumps(parsed, indent=2))
        return '<span style="color: green;">Saved successfully!</span>'
    except json.JSONDecodeError as e:
        return f'<span style="color: red;">Invalid JSON: {e}</span>'

def checkUidsAndGenerate(times):
    if not times or len(times) == 0:
        return
    for time in times:
        if not time.get("id"):
            time["id"] = str(uuid4())

def editCron(times):
    if not times or len(times) == 0:
        return
    for time in times:
        if time.get("active", False):
            if time.get("id"):
                if time.get("start_time"):
                    if len(list(cron.find_comment(time.get("id")+"-start"))) == 1:
                        job = list(cron.find_comment(time.get("id")+"-start"))[0]
                        hour, minute = convert_time_to_server_tz(
                            int(time.get("start_time").split(":")[0]),
                            int(time.get("start_time").split(":")[1]),
                            time.get("timezone", "Europe/Athens")
                        )
                        job.setall(minute, hour , "*", "*", "1-5")
                        job.set_command("sudo systemctl start oasa-notifications.service")
                        job.enable(True)
                    elif len(list(cron.find_comment(time.get("id")+"-start"))) == 0:
                        job = cron.new(command="sudo systemctl start oasa-notifications.service", comment=time.get("id")+"-start")
                        hour, minute = convert_time_to_server_tz(
                            int(time.get("start_time").split(":")[0]),
                            int(time.get("start_time").split(":")[1]),
                            time.get("timezone", "Europe/Athens")
                        )
                        job.setall(minute, hour , "*", "*", "1-5")
                        job.enable(True)
                if time.get("end_time"):
                    if len(list(cron.find_comment(time.get("id")+"-stop"))) == 1:
                        job = list(cron.find_comment(time.get("id")+"-stop"))[0]
                        hour, minute = convert_time_to_server_tz(
                            int(time.get("end_time").split(":")[0]),
                            int(time.get("end_time").split(":")[1]),
                            time.get("timezone", "Europe/Athens")
                        )
                        job.setall(minute, hour , "*", "*", "1-5")
                        job.set_command("sudo systemctl stop oasa-notifications.service")
                        job.enable(True)
                    elif len(list(cron.find_comment(time.get("id")+"-stop"))) == 0:
                        job = cron.new(command="sudo systemctl stop oasa-notifications.service", comment=time.get("id")+"-stop")
                        hour, minute = convert_time_to_server_tz(
                            int(time.get("end_time").split(":")[0]),
                            int(time.get("end_time").split(":")[1]),
                            time.get("timezone", "Europe/Athens")
                        )
                        job.setall(minute, hour , "*", "*", "1-5")
                        job.enable(True)
        else:
            if time.get("id"):
                if len(list(cron.find_comment(time.get("id")+"-start"))) == 1:
                    for job in cron.find_comment(time.get("id")+"-start"):
                        job.enable(False)
                if len(list(cron.find_comment(time.get("id")+"-stop"))) == 1:
                    for job in cron.find_comment(time.get("id")+"-stop"):
                        job.enable(False)
    cron.write()  # Save the changes to the crontab

def removeDeletedCronJobs(times):
    initial_times = json.loads(CONFIG_FILE.read_text()).get("times", [])
    deleted_times = [time.get("id") for time in initial_times if time.get("id") not in [t.get("id") for t in times]]
    print("Deleted times:", deleted_times)
    if deleted_times and len(deleted_times) > 0:
        for deleted_id in deleted_times:
            for job in cron.find_comment(deleted_id+"-start"):
                cron.remove(job)
            for job in cron.find_comment(deleted_id+"-stop"):
                cron.remove(job)
    cron.write()

def convert_time_to_server_tz(hour, minute, source_tz_name):
    # We grab today's date just to resolve the Daylight Saving Time rules
    today = datetime.now()
    
    # Create the time in the source timezone
    source_dt = datetime(
        year=today.year, 
        month=today.month, 
        day=today.day, 
        hour=hour, 
        minute=minute, 
        tzinfo=ZoneInfo(source_tz_name)
    )
    
    # Convert to the server's local timezone and extract only the time
    server_time = source_dt.astimezone().time()
    
    return str(server_time.hour).zfill(2), str(server_time.minute).zfill(2)