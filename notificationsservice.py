from zoneinfo import ZoneInfo

import requests
import time
import threading
import json
import os
from datetime import datetime
from datetime import timedelta
from dotenv import load_dotenv

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

load_dotenv()

TOPIC = os.getenv("TOPIC_NAME")
WORK_CALENDAR_ID = os.getenv("WORK_CALENDAR_ID")

# Google Calendar read-only scope
SCOPES = [os.getenv("GOOGLE_CALENDAR_SCOPE")]

is_muted = False
mute_until = None
listener_stop_event = threading.Event()

class BusData:
    def __init__(self, bus_number="XXX", bus_name="Bus Name", route_number="YYY", vehicle_number="ZZZ", arrival_time="-1"):
        self.bus_number = bus_number
        self.bus_name = bus_name
        self.route_number = route_number
        self.vehicle_number = vehicle_number
        self.arrival_time = arrival_time

    def is_default(self):
            return (
                self.bus_number == "XXX" and
                self.bus_name == "Bus Name" and
                self.route_number == "YYY" and
                self.vehicle_number == "ZZZ" and
                self.arrival_time == "-1"
            )

def createBusDataList(stop_codes,current_list):
    for stop_code in stop_codes:
            if current_list.get(stop_code) is None:
                current_list[stop_code] = BusData()
    return current_list

def createsecondBusList(stop_codes):
    list = {}
    for stop_code in stop_codes:
        list[stop_code] = None
    return list

def createBus(stop_code,route_names):
    bus_data = BusData()
    response = requests.get(f"https://telematics.oasa.gr/api/?act=getStopArrivals&p1={stop_code}")
    if response.status_code == 200:
        data = response.json()
        arrivals = data if isinstance(data, list) else []
        arrivals = [x for x in arrivals if x.get("route_code") in tracked_routes_codes]
        if (len(arrivals)) == 0:
            return bus_data
        bus = arrivals[0]
        if bus["route_code"] in tracked_routes_codes:
            bus_data.bus_number = tracked_routes_codes[bus["route_code"]]
            bus_data.route_number = bus["route_code"]
            bus_data.arrival_time = bus["btime2"]
            bus_data.vehicle_number = bus["veh_code"]
            if route_names.get(bus["route_code"]) is not None:
                bus_data.bus_name = route_names[bus["route_code"]]
            else:
                response2 = requests.get(f"https://telematics.oasa.gr/api/?act=getRouteName&p1={bus['route_code']}")
                if response2.status_code == 200:
                    data = response2.json()
                    bus_data.bus_name = data[0].get("route_descr", "Unknown Route")
                    route_names[bus["route_code"]] = bus_data.bus_name
                else:
                    bus_data.bus_name = "Unknown Route"
                    route_names[bus["route_code"]] = bus_data.bus_name

        return bus_data 

def buildMuteList(bus: BusData):
    if routes_muted.get(bus.route_number, {}).get(bus.vehicle_number) == None:
                print(f"Adding vehicle {bus.vehicle_number} on route {bus.route_number} on tracking list.")
                inner = routes_muted.setdefault(bus.route_number, {})
                # Set ALL existing vehicles to False
                for v in inner:
                    inner[v] = False

                # Add/update the new vehicle as False
                inner[bus.vehicle_number] = False

def checkSendNotification(bus: BusData, current_bus_data_list: dict, second_arrival_data: dict, arrivals: list, stop_code: str):
    if routes_muted.get(bus.route_number, {}).get(bus.vehicle_number, False):
        print(f"Vehicle {bus.vehicle_number} on route {bus.route_number} is muted. Skipping notification.")
        return False
    if current_bus_data_list[stop_code].arrival_time != "-1" and (0 <= (int(current_bus_data_list[stop_code].arrival_time) - int(bus.arrival_time)) < 5 and int(bus.arrival_time) > 4):
        print(f"Arrival time hasn't changed significantly ({current_bus_data_list[stop_code].arrival_time} -> {bus.arrival_time}). Skipping notification for route {bus.route_number}.")
        return False
    if (len(arrivals) > 1 and bus.route_number == arrivals[1]["route_code"] and int(bus.arrival_time) <= 4):
        print(f"First bus is too close. Adding second bus notification data.")
        second_arrival_data[stop_code] = f"\nNext in **{arrivals[1]['btime2']}\'**" if int(arrivals[1]['btime2']) > 0 else "\nNext arriving **now**!"
    current_bus_data_list[stop_code] = bus
    return True

def sendNotification(current_bus_data_list: dict, stop_code: str):
    requests.post(f"https://ntfy.sh/{TOPIC}",
                    data= f"{f'Arriving in **{current_bus_data_list[stop_code].arrival_time}\'** *({current_bus_data_list[stop_code].bus_name})*' if int(current_bus_data_list[stop_code].arrival_time) > 0 else f'Arriving **now** *({current_bus_data_list[stop_code].bus_name})*'}{second_arrival_data[stop_code] if second_arrival_data[stop_code] else ''}".encode('utf-8'),
                    headers={
                        "Title": f"{current_bus_data_list[stop_code].bus_number} - {stops_names.get(stop_code, 'Unknown Stop')}".encode('utf-8'),
                        "Priority": "high",
                        "Click": "https://www.oasa.gr/en/telematics/",
                        "Markdown": "yes",
                        "Tags": "warning,bus",
                        "Actions": f"http, Mute, https://ntfy.sh/{TOPIC}, body=Mute 10m, headers.X-Priority=1, clear=true; http, Mute route, https://ntfy.sh/{TOPIC}, body=mute {current_bus_data_list[stop_code].route_number}_{current_bus_data_list[stop_code].vehicle_number}, headers.X-Priority=1, clear=true; http, Stop, https://ntfy.sh/{TOPIC}, body=mute, headers.X-Priority=1, clear=true;".encode('utf-8')
                    })

def listen_for_mute(stop_event):
    """Background thread that listens for the 'mute' command on the topic."""
    global is_muted
    global mute_until
    print(f"Listening for mute commands on: {TOPIC}")

    subscribe_url = f"https://ntfy.sh/{TOPIC}/json"

    while not stop_event.is_set():
        try:
            # Timeout lets the loop periodically check stop_event and exit quickly.
            with requests.get(subscribe_url, stream=True, timeout=(5, 5)) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if stop_event.is_set():
                        return
                    if not line:
                        continue
                    data = json.loads(line)
                    message = data.get("message", "").strip()
                    message_lower = message.lower()
                    if message_lower == "mute":
                        print("Mute command received! Silencing on next loop...")
                        is_muted = True
                        return
                    if message_lower == "mute 10m":
                        if mute_until and datetime.now() < mute_until:
                            print(f"Already temporarily muted until {mute_until.strftime('%H:%M:%S')}. Ignoring new mute command.")
                            continue
                        mute_until = datetime.now() + timedelta(minutes=10)
                        print(f"Temporary mute enabled until {mute_until.strftime('%H:%M:%S')}")
                        continue
                    if message_lower.startswith("mute ") and message_lower not in ("mute", "mute 10m"):
                        route_code = message.split(" ",1)[1].split("_",1)[0].strip()
                        veh_code = message.split(" ",1)[1].split("_",1)[1].strip()
                        print(f"Received specific mute command: {message}")
                        routes_muted[route_code][veh_code] = True
        except (requests.exceptions.ReadTimeout, requests.exceptions.Timeout):
            continue
        except requests.RequestException as exc:
            if "Read timed out" in str(exc):
                continue
            print(f"Listener connection error: {exc}. Retrying...")
            time.sleep(2)
        except json.JSONDecodeError:
            continue
            
def getStopNameFromCode(stop_codes,stops_names):
    for code in stop_codes:
        if stops_names.get(code) == f"Unknown Stop {code}" or stops_names.get(code) is None:
            try:
                response = requests.get(f"https://telematics.oasa.gr/api/?act=getStopNameAndXY&p1={code}", timeout=10)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, list) and data:
                    stops_names[code] = data[0].get("stop_descr", f"Unknown Stop {code}")
                else:
                    stops_names[code] = f"Unknown Stop {code}"
            except requests.RequestException as exc:
                print(f"Failed to fetch name for stop {code}: {exc}")
                stops_names[code] = f"Unknown Stop {code}"
            except ValueError:
                print(f"Invalid name data for stop {code}")
                stops_names[code] = f"Unknown Stop {code}"
    return stops_names


def is_remote_or_holiday_today():
    """Return True if user's primary Google Calendar has an event with 'Remote' in the summary for today.

    This function uses `credentials.json` (OAuth client) and stores/uses `token.json` for cached credentials.
    If Google API libraries or credentials are not available it will return False.
    """

    token_path = 'token.json'
    creds_path = 'credentials.json'
    creds = None
    try:
        if os.path.exists(token_path):
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception:
        creds = None

    try:
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(creds_path):
                    print('credentials.json not found; cannot check Google Calendar.')
                    return False
                flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
                creds = flow.run_local_server(port=0)
                with open(token_path, 'w') as token:
                    token.write(creds.to_json())
     
        service = build('calendar', 'v3', credentials=creds)
        now = datetime.now().astimezone()
        start = datetime(now.year, now.month, now.day, tzinfo=ZoneInfo("Europe/Athens")).isoformat()
        end = datetime(now.year, now.month, now.day, hour=23, minute=59, second=59, tzinfo=ZoneInfo("Europe/Athens")).isoformat()

        events_result = service.events().list(
            calendarId=os.getenv('WORK_CALENDAR_ID'), timeMin=start, timeMax=end,
            singleEvents=True, orderBy='startTime', q='Remote'
        ).execute()
        items = events_result.get('items', [])
        for ev in items:
            summary = ev.get('summary', '')
            if 'remote' in summary.lower():
                print("Found 'Remote' event in calendar today.")
                return True
        holidays_result = service.events().list(
            calendarId=os.getenv('HOLIDAYS_CALENDAR_ID'), timeMin=start, timeMax=end,
            singleEvents=True, orderBy='startTime'
        ).execute()
        holidays = holidays_result.get('items', [])
        for holiday in holidays:
            description = holiday.get('description', '')
            if 'Επίσημη αργία'.lower() in description.lower():
                print("Found Greek official holiday in calendar today.")
                return True
        return False
    except Exception as exc:
        print(f"Error querying Google Calendar: {exc}")
        return False

def getCurrentStopCodes():
    now = datetime.now().time()
    print(f"Current time: {now}. Checking for bus arrivals...")
    # If the user's calendar indicates a remote day, skip notifications
    try:
        if is_remote_or_holiday_today():
            print("Remote day or Greek official holiday detected in calendar — skipping notifications for today.")
            return []
    except Exception as exc:
        print(f"Calendar check failed: {exc}")
    if datetime.strptime("14:40", "%H:%M").time() <= now <= datetime.strptime("15:30", "%H:%M").time():
        return ["320013", "320009"]
    elif datetime.strptime("06:50", "%H:%M").time() < now <= datetime.strptime("7:25", "%H:%M").time():
        return ["320005","240071"]
    else:
        print("Outside of specified time windows. No notifications will be sent.")
        return []  # Return an empty list if it's outside the specified time windows

def buildarrivals(stop_code):
    arrivals = requests.get(f"https://telematics.oasa.gr/api/?act=getStopArrivals&p1={stop_code}").json()
    arrivals = arrivals if isinstance(arrivals, list) else []
    arrivals = [x for x in arrivals if x.get("route_code") in tracked_routes_codes]
    return arrivals

listener_thread = threading.Thread(target=listen_for_mute, args=(listener_stop_event,), daemon=True)
listener_thread.start()

if __name__ == "__main__":
    try:
        tracked_routes_codes = {
            "2810" : "218",
            "2034" : "218",
            "2899" : "218"
        }
        stops_names = {}
        route_names = {}
        routes_muted = {}
        bus_data_list = {}
        while True:
            if is_muted:
                print("System is muted. Notification blocked.")
                break
            if mute_until and datetime.now() < mute_until:
                print(f"System temporarily muted until {mute_until.strftime('%H:%M:%S')}. Skipping this cycle.")
                time.sleep(30)
                continue
            if mute_until and datetime.now() >= mute_until:
                print("Temporary mute expired. Resuming notifications.")
                mute_until = None
            stop_codes = getCurrentStopCodes()
            if stop_codes == []:
                break 
            bus_data_list = createBusDataList(stop_codes,bus_data_list)
            second_arrival_data = createsecondBusList(stop_codes)
            stops_names = getStopNameFromCode(stop_codes,stops_names)
            for stop_code in stop_codes:
                arrivals = buildarrivals(stop_code)
                bus = createBus(stop_code,route_names)
                if bus.is_default():
                    print(f"No bus data available for stop {stop_code}. Skipping notification.")
                    continue
                buildMuteList(bus)
                if checkSendNotification(bus,bus_data_list,second_arrival_data,arrivals,stop_code):
                    sendNotification(bus_data_list,stop_code)
                    print(f"Notification sent for route {bus_data_list[stop_code].route_number} {bus_data_list[stop_code].vehicle_number}!")

            time.sleep(60) # Wait 1 minute before checking again
    finally:
        listener_stop_event.set()
        listener_thread.join(timeout=6)