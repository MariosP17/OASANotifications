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
HOLIDAYS_CALENDAR_ID = os.getenv("HOLIDAYS_CALENDAR_ID")
USERS_SETTINGS = os.getenv("usersettings_file_path")
TIMEOUT = (5, 15)  # (connect timeout, read timeout) in seconds

# Google Calendar read-only scope
SCOPES = [os.getenv("GOOGLE_CALENDAR_SCOPE")]

is_muted = False
mute_until = None
listener_stop_event = threading.Event()

class BusData:
    def __init__(self, bus_number="XXX", bus_name= None, route_number="YYY", vehicle_number="ZZZ", arrival_time="-1"):
        self.bus_number = bus_number
        self.bus_name = bus_name
        self.route_number = route_number
        self.vehicle_number = vehicle_number
        self.arrival_time = arrival_time

    def is_default(self):
            return (
                self.bus_number == "XXX" and
                self.bus_name == None and
                self.route_number == "YYY" and
                self.vehicle_number == "ZZZ" and
                self.arrival_time == "-1"
            )

class UserSettings:
    def __init__(self, user_settings_times_list_dict= {}, tracked_routes_codes = {}):
        self.user_settings_times_list = UserSettingsTime.createUserSettingsTimeList(user_settings_times_list_dict)
        self.tracked_routes_codes = tracked_routes_codes

class UserSettingsTime:
    def __init__(self, start = "00:00", end = "23:59",timezone = "Europe/Athens", stop_codes = []):
        self.start = start
        self.end = end
        self.timezone = timezone
        self.stop_codes = stop_codes

    def is_in_time_window(self, now : datetime):
        start_user_dt = datetime.now(ZoneInfo(self.timezone)).replace(
            hour=int(self.start.split(":")[0]),
            minute=int(self.start.split(":")[1])
        )
        start_time = start_user_dt.astimezone(now.astimezone().tzinfo).time()
        end_user_dt = datetime.now(ZoneInfo(self.timezone)).replace(
            hour=int(self.end.split(":")[0]),
            minute=int(self.end.split(":")[1])
        )
        end_time = end_user_dt.astimezone(now.astimezone().tzinfo).time()
        return start_time <= now.time() <= end_time
    
    @staticmethod
    def createUserSettingsTimeList(user_settings_list_dict):
        user_settings_list = []
        for item in user_settings_list_dict:
            user_settings = UserSettingsTime(
                stop_codes=item.get("codes", []),
                start=item.get("start_time", "00:00"),
                timezone=item.get("timezone","Europe/Athens"),
                end=item.get("end_time", "23:59")
            )
            user_settings_list.append(user_settings)
        return user_settings_list

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

def createBus(route_names,arrivals):
    bus_data = BusData()
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
            try:
                print(f"Fetching route name for route {bus['route_code']}...")
                response2 = requests.get(f"https://telematics.oasa.gr/api/?act=getRouteName&p1={bus['route_code']}",timeout=TIMEOUT)
                if response2.status_code == 200:
                    data = response2.json()
                    bus_data.bus_name = data[0].get("route_descr", None)
                    route_names[bus["route_code"]] = bus_data.bus_name
                else:
                    bus_data.bus_name = None
                    route_names[bus["route_code"]] = bus_data.bus_name
            except requests.RequestException as e:
                print(f"Error fetching route name for route {bus['route_code']}: {e}")

    return bus_data 

def buildMuteList(bus: BusData):
    if bus.is_default():
        return
    if routes_muted.get(bus.route_number, {}).get(bus.vehicle_number) == None:
                print(f"Adding vehicle {bus.vehicle_number} on route {bus.route_number} on tracking list.")
                inner = routes_muted.setdefault(bus.route_number, {})
                # Set ALL existing vehicles to False
                for v in inner:
                    inner[v] = False

                # Add/update the new vehicle as False
                inner[bus.vehicle_number] = False

def checkSendNotification(bus: BusData, current_bus_data_list: dict, second_arrival_data: dict, arrivals: list, stop_code: str,empty_message: bool):
    if not empty_message:
        if routes_muted.get(bus.route_number, {}).get(bus.vehicle_number, False):
            print(f"Vehicle {bus.vehicle_number} on route {bus.route_number} is muted. Skipping notification.")
            return False
        old_time = int(current_bus_data_list[stop_code].arrival_time)
        new_time = int(bus.arrival_time)

        #Skip if arrival time hasn't changed at all
        if new_time == old_time:
            print(f"No change in arrival time ({old_time}). Skipping notification for route {bus.route_number}.")
            return False

        #Skip if change is small (<5 min) and remaining time is above 5 minutes
        if current_bus_data_list[stop_code].arrival_time != "-1" and 0 < (old_time - new_time) < 5 and new_time > 4:
            print(f"Arrival time hasn't changed significantly ({old_time} -> {new_time}). Skipping notification for route {bus.route_number}.")
            return False
        if (len(arrivals) > 1 and any(arrival.get("route_code") == bus.route_number and arrival.get("veh_code") != bus.vehicle_number for arrival in arrivals) and int(bus.arrival_time) <= 4):
            print(f"First bus is too close. Adding second bus notification data.")
            chosen_arrival = [arrival for arrival in arrivals if arrival.get("route_code") == bus.route_number and arrival.get("veh_code") != bus.vehicle_number][0]
            second_arrival_data[stop_code] = f"\nNext in **{chosen_arrival['btime2']}\'**" if int(chosen_arrival['btime2']) > 0 else "\nNext arriving **now**!"
        current_bus_data_list[stop_code] = bus
    return True

def sendNotification(current_bus_data_list: dict, stop_code: str, sendEmpty :bool,success: bool):
    try:
        if (sendEmpty):
            if success:
                requests.post(f"https://ntfy.sh/{TOPIC}",
                                data= "No bus is arriving at this stop right now".encode('utf-8'),
                                headers={
                                    "Title": f"No Data - {stops_names.get(stop_code, 'Unknown Stop')}".encode('utf-8'),
                                    "Priority": "high",
                                    "Click": f"https://telematics.oasa.gr/content.php#stationInfo_{stop_code}",
                                    "Markdown": "yes",
                                    "Tags": "warning,bus",
                                    "Actions": f"http, Mute, https://ntfy.sh/{TOPIC}, body=Mute 10m, headers.X-Priority=1, clear=true; http, Stop, https://ntfy.sh/{TOPIC}, body=mute, headers.X-Priority=1, clear=true;"
                                },timeout=TIMEOUT)
                print(f"Empty notification sent for stop {stops_names.get(stop_code, 'Unknown Stop')}.")
            else:
                requests.post(f"https://ntfy.sh/{TOPIC}",
                                data= "Couldn't load data for this stop".encode('utf-8'),
                                headers={
                                    "Title": f"No Data - {stops_names.get(stop_code, 'Unknown Stop')}".encode('utf-8'),
                                    "Priority": "high",
                                    "Click": f"https://telematics.oasa.gr/content.php#stationInfo_{stop_code}",
                                    "Markdown": "yes",
                                    "Tags": "warning,bus",
                                    "Actions": f"http, Mute, https://ntfy.sh/{TOPIC}, body=Mute 10m, headers.X-Priority=1, clear=true; http, Stop, https://ntfy.sh/{TOPIC}, body=mute, headers.X-Priority=1, clear=true;"
                                },timeout=TIMEOUT)
                print(f"Empty notification sent for stop {stops_names.get(stop_code, 'Unknown Stop')}.")
        else:
            requests.post(f"https://ntfy.sh/{TOPIC}",
                            data= f"{f'Arriving in **{current_bus_data_list[stop_code].arrival_time}\'** *({current_bus_data_list[stop_code].bus_name if current_bus_data_list[stop_code].bus_name else 'Unknown Route'})*' if int(current_bus_data_list[stop_code].arrival_time) > 0 else f'Arriving **now** *({current_bus_data_list[stop_code].bus_name if current_bus_data_list[stop_code].bus_name else 'Unknown Route'})*'}{second_arrival_data[stop_code] if second_arrival_data[stop_code] else ''}".encode('utf-8'),
                            headers={
                                "Title": f"{current_bus_data_list[stop_code].bus_number} - {stops_names.get(stop_code, 'Unknown Stop')}".encode('utf-8'),
                                "Priority": "high",
                                "Click": f"https://telematics.oasa.gr/content.php#stationInfo_{stop_code}",
                                "Markdown": "yes",
                                "Tags": "warning,bus",
                                "Actions": f"http, Mute, https://ntfy.sh/{TOPIC}, body=Mute 10m, headers.X-Priority=1, clear=true; http, Mute route, https://ntfy.sh/{TOPIC}, body=mute {current_bus_data_list[stop_code].route_number}_{current_bus_data_list[stop_code].vehicle_number}, headers.X-Priority=1, clear=true; http, Stop, https://ntfy.sh/{TOPIC}, body=mute, headers.X-Priority=1, clear=true;"
                            },timeout=TIMEOUT)
            print(f"Notification sent for route {current_bus_data_list[stop_code].route_number} with vehicle {current_bus_data_list[stop_code].vehicle_number} at stop {stops_names.get(stop_code, 'Unknown Stop')}.")
    except requests.RequestException as e:
        print(f"Error sending notification for route {current_bus_data_list[stop_code].route_number}: {e}")

def listen_for_mute(stop_event):
    """Background thread that listens for the 'mute' command on the topic."""
    global is_muted
    global mute_until
    print(f"Listening for mute commands on: {TOPIC}\n")

    subscribe_url = f"https://ntfy.sh/{TOPIC}/json"

    while not stop_event.is_set():
        try:
            # Timeout lets the loop periodically check stop_event and exit quickly.
            with requests.get(subscribe_url, stream=True) as response:
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
    print("Mute listener thread exiting.")
            
def getStopNameFromCode(stop_codes,stops_names):
    for code in stop_codes:
        if stops_names.get(code) == f"Unknown Stop {code}" or stops_names.get(code) is None:
            try:
                response = requests.get(f"https://telematics.oasa.gr/api/?act=getStopNameAndXY&p1={code}", timeout=TIMEOUT)
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
            calendarId=WORK_CALENDAR_ID, timeMin=start, timeMax=end,
            singleEvents=True, orderBy='startTime'
        ).execute()
        holidays_result = service.events().list(
            calendarId=HOLIDAYS_CALENDAR_ID, timeMin=start, timeMax=end,
            singleEvents=True, orderBy='startTime'
        ).execute()
        items = events_result.get('items', []) + holidays_result.get('items', [])
        for ev in items:
            summary = ev.get('summary', '')
            description = ev.get('description', '')
            if 'remote' in summary.lower():
                print("Found 'Remote' event in calendar today.")
                return True
            if 'άδεια'.lower() in summary.lower() or 'αδεια'.lower() in summary.lower():
                print("Found Greek leave event in calendar today.")
                return True
            if 'Επίσημη αργία'.lower() in description.lower():
                print("Found Greek official holiday in calendar today.")
                return True
        return False
    except Exception as exc:
        print(f"Error querying Google Calendar: {exc}")
        return False

def getCurrentStopCodesWithNames(user_settings_times_list, stop_codes, stop_names):
    now = datetime.now()
    print(f"Current time: {now.time()}. Checking for bus arrivals...")
    # If the user's calendar indicates a remote day, skip notifications
    try:
        if is_remote_or_holiday_today():
            print("Remote day, leave or Greek official holiday detected in calendar — skipping notifications for today.")
            return [],[]
    except Exception as exc:
        print(f"Calendar check failed: {exc}")
    if any(times.is_in_time_window(now) for times in user_settings_times_list):
        times = [t for t in user_settings_times_list if t.is_in_time_window(now)][0]  # Get the first matching settings
        if set(times.stop_codes) == set(stop_codes):
            print("Stop codes haven't changed since last check. Skipping API call.")
            none_codes = [code for code in times.stop_codes if stop_names.get(code) is None]
            if none_codes:
                for code in none_codes:
                    stop_names[code] = getStopNameFromCode([code], stop_names).get(code, f"Unknown Stop {code}")
            return stop_codes, stop_names
        stop_names = getStopNameFromCode(times.stop_codes, stop_names)
        return times.stop_codes, stop_names
    else:
        print("Outside of specified time windows. No notifications will be sent.")
        return [],[]  # Return an empty list if it's outside the specified time windows

def buildarrivals(stop_code):
    try:
        print(f"Fetching arrivals for stop {stop_code}...")
        arrivals = requests.get(f"https://telematics.oasa.gr/api/?act=getStopArrivals&p1={stop_code}", timeout=TIMEOUT).json()
        arrivals = arrivals if isinstance(arrivals, list) else []
        arrivals = [x for x in arrivals if x.get("route_code") in tracked_routes_codes]
        return arrivals,True
    except requests.RequestException as e:
        print(f"Error fetching arrivals for stop {stop_code}: {e}")
        return [],False

def buildUserSettings(path):
    user_settings = UserSettings()
    try:
        with open(path, 'r') as f:
            data = json.load(f)
            times = data.get("times", [])
            tracked_routes_codes = data.get("tracked_routes_codes", {})
            user_settings = UserSettings(user_settings_times_list_dict=times, tracked_routes_codes=tracked_routes_codes)
    except Exception as exc:
        print(f"Error loading user settings: {exc}")
    return user_settings
listener_thread = threading.Thread(target=listen_for_mute, args=(listener_stop_event,), daemon=True)
listener_thread.start()

def initEmpty(stop_codes,empty_messages):
    for stop_code in stop_codes:
        if empty_messages.get(stop_code) == None:
            empty_messages[stop_code] = None
    return empty_messages



if __name__ == "__main__":
    try:
        user_settings = buildUserSettings(USERS_SETTINGS)
        tracked_routes_codes = user_settings.tracked_routes_codes
        stop_codes = []
        stops_names = {}
        route_names = {}
        routes_muted = {}
        bus_data_list = {}  
        empty_messages = {}
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
            stop_codes,stops_names = getCurrentStopCodesWithNames(user_settings.user_settings_times_list, stop_codes, stops_names)
            if stop_codes == []:
                break 
            bus_data_list = createBusDataList(stop_codes,bus_data_list)
            second_arrival_data = createsecondBusList(stop_codes)
            empty_messages = initEmpty(stop_codes,empty_messages)
            for stop_code in stop_codes:
                arrivals,success = buildarrivals(stop_code)
                bus = createBus(route_names,arrivals)
                if bus.is_default():
                    if bus_data_list[stop_code].is_default() and empty_messages[stop_code] == None:
                        print("First call returned no data. Sending empty message")
                        empty_messages[stop_code] = True
                    else:
                        print(f"No bus data available for stop {stop_code}. Skipping notification.")
                        empty_messages[stop_code] = False
                        continue
                buildMuteList(bus)
                if checkSendNotification(bus,bus_data_list,second_arrival_data,arrivals,stop_code,empty_messages[stop_code]):
                    sendNotification(bus_data_list,stop_code,empty_messages[stop_code],success)

            time.sleep(60) # Wait 1 minute before checking again
    finally:
        listener_stop_event.set()
        listener_thread.join(timeout=6)
