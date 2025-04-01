import os
import re
import datetime
import urllib.parse
from flask import Flask, request, Response, render_template, redirect, url_for, flash
from apscheduler.schedulers.background import BackgroundScheduler
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from twilio.rest import Client
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

import os
import json

token_data = os.environ.get('GOOGLE_OAUTH_TOKEN')
if token_data:
    # Optionally verify it's valid JSON
    try:
        token_json = json.loads(token_data)
        # Write the token to a file, if necessary
        with open('token.json', 'w') as f:
            json.dump(token_json, f)
        print("token.json created from environment variable.")
    except json.JSONDecodeError:
        print("Invalid JSON for GOOGLE_OAUTH_TOKEN")
else:
    print("GOOGLE_OAUTH_TOKEN not set!")


# Retrieve configuration from environment variables
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')
MY_PHONE_NUMBER = os.environ.get('MY_PHONE_NUMBER')
APP_URL = os.environ.get('APP_URL')  # e.g., https://your-cloudflared-url.trycloudflare.com
FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'default_secret_key')

# Google Calendar configuration
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
TOKEN_FILE = 'token.json'  # Ensure this file is generated via OAuth2 flow
CALENDAR_ID = 'primary'

# Initialize Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

# ----- Google Calendar Functions -----
def get_credentials():
    """Load Google Calendar API credentials from token.json."""
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        return creds
    else:
        raise Exception("Google Calendar credentials not found. Please complete the OAuth2 flow to generate token.json.")

def get_upcoming_events():
    """Fetch upcoming events from the primary calendar for the next hour."""
    creds = get_credentials()
    service = build('calendar', 'v3', credentials=creds)
    now = datetime.datetime.utcnow()
    time_min = now.isoformat() + 'Z'
    time_max = (now + datetime.timedelta(hours=1)).isoformat() + 'Z'
    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    return events_result.get('items', [])


def extract_phone_number(text):
    """Extract a US phone number from text using regex."""
    if not text:
        return None
    match = re.search(r'(\+?1?\s*\-?\(?\d{3}\)?\s*\-?\s*\d{3}\s*\-?\s*\d{4})', text)
    return match.group(0) if match else None

# Global set to track events that have already been scheduled
scheduled_event_ids = set()

def schedule_call_for_event(event):
    """Schedule an outbound call for an event that contains a phone number,
       ensuring each event is scheduled only once."""
    event_id = event.get('id')
    if event_id in scheduled_event_ids:
        # Already scheduled this event, so skip it
        return

    description = event.get('description', '')
    meeting_phone = extract_phone_number(description)
    if meeting_phone:
        start_time_str = event['start'].get('dateTime')
        if not start_time_str:
            return
        start_time = datetime.datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
        call_time = start_time - datetime.timedelta(minutes=1)
        # Only schedule if the call time is in the future
        if call_time < datetime.datetime.now(datetime.timezone.utc):
            return
        event_name = event.get('summary', 'Upcoming Event')
        # Mark this event as scheduled so we don't schedule it again
        scheduled_event_ids.add(event_id)
        scheduler.add_job(initiate_call, 'date', run_date=call_time, args=[meeting_phone, event_name])
        print(f"Scheduled call for event '{event_name}' at {call_time} UTC (Meeting Phone: {meeting_phone})")


def check_calendar_events():
    """Periodically check Google Calendar for events and schedule calls."""
    print(f"[{datetime.datetime.utcnow().isoformat()}] Checking calendar for upcoming events...")
    try:
        events = get_upcoming_events()
        for event in events:
            schedule_call_for_event(event)
    except Exception as e:
        print("Error checking calendar events:", e)

# ----- Twilio Call Functions -----
def initiate_call(meeting_phone, event_name):
    """Initiate a call using Twilio and pass along the event name."""
    # URL-encode the event name for safe transmission
    encoded_event_name = urllib.parse.quote(event_name)
    webhook_url = f"{APP_URL}/twilio-webhook?meeting_phone={meeting_phone}&event_name={encoded_event_name}"
    try:
        call = twilio_client.calls.create(
            to=MY_PHONE_NUMBER,  # Call your phone first
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url
        )
        print(f"Initiated call (SID: {call.sid}) for event '{event_name}' (Meeting Phone: {meeting_phone})")
    except Exception as e:
        print("Error initiating call:", e)

# ----- Flask Webhook Endpoint for Twilio -----
@app.route("/twilio-webhook", methods=['POST', 'GET'])
def twilio_webhook():
    meeting_phone = request.args.get('meeting_phone')
    event_name = request.args.get('event_name', 'Upcoming Event')
    response_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">Hello, you have an upcoming event: {event_name}. Please wait while we connect you.</Say>
    <Pause length="3"/>
    <Dial callerId="{MY_PHONE_NUMBER}" timeout="20">{meeting_phone}</Dial>
</Response>"""
    return Response(response_xml, mimetype='text/xml')

# ----- Local Webpage Routes -----
@app.route("/")
def index():
    """Display upcoming events and control options."""
    try:
        events = get_upcoming_events()
    except Exception as e:
        flash(str(e))
        events = []
    return render_template("index.html", events=events)

@app.route("/force-check", methods=["GET"])
def force_check():
    """Force a calendar check to schedule calls immediately."""
    try:
        check_calendar_events()
        flash("Calendar checked successfully!")
    except Exception as e:
        flash(f"Error checking calendar: {e}")
    return redirect(url_for('index'))

# ----- Main Application Setup -----
if __name__ == "__main__":
    # Set up the scheduler to check the calendar every 5 minutes.
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(check_calendar_events, 'interval', minutes=5)
    scheduler.start()
    print(f"Scheduler started. Flask app running on {APP_URL}")
    app.run(host='0.0.0.0', port=5000, debug=True)
