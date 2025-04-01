import os
import re
import datetime
import urllib.parse
import logging

from flask import Flask, request, Response, render_template, redirect, url_for, flash
from apscheduler.schedulers.background import BackgroundScheduler
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from twilio.rest import Client

# Optionally load local .env for local development; on Render, env vars are injected via the dashboard.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Retrieve configuration from environment variables
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')
MY_PHONE_NUMBER = os.environ.get('MY_PHONE_NUMBER')
APP_URL = os.environ.get('APP_URL')  # e.g. https://your-service.onrender.com
FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'default_secret_key')

# Google Calendar configuration
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
TOKEN_FILE = 'token.json'  # Ensure this file is generated via the OAuth2 flow
CALENDAR_ID = 'primary'

# Initialize Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

# Create the scheduler globally so all functions can access it.
scheduler = BackgroundScheduler(timezone="UTC")

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

# Global set to avoid scheduling the same event more than once.
scheduled_event_ids = set()

def schedule_call_for_event(event):
    """Schedule an outbound call for an event that contains a phone number."""
    event_id = event.get('id')
    if event_id in scheduled_event_ids:
        logger.info(f"Event {event_id} already scheduled, skipping.")
        return

    description = event.get('description', '')
    meeting_phone = extract_phone_number(description)
    if meeting_phone:
        start_time_str = event['start'].get('dateTime')
        if not start_time_str:
            return
        start_time = datetime.datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
        call_time = start_time - datetime.timedelta(minutes=1)
        if call_time < datetime.datetime.now(datetime.timezone.utc):
            logger.info(f"Call time {call_time} is in the past. Skipping event.")
            return
        event_name = event.get('summary', 'Upcoming Event')
        scheduled_event_ids.add(event_id)
        scheduler.add_job(initiate_call, 'date', run_date=call_time, args=[meeting_phone, event_name])
        logger.info(f"Scheduled call for event '{event_name}' at {call_time} (Meeting Phone: {meeting_phone})")
    else:
        logger.info("No valid phone number found in event description.")

def check_calendar_events():
    """Periodically check Google Calendar for events and schedule calls."""
    logger.info(f"[{datetime.datetime.utcnow().isoformat()}] Checking calendar for upcoming events...")
    try:
        events = get_upcoming_events()
        for event in events:
            schedule_call_for_event(event)
    except Exception as e:
        logger.error(f"Error checking calendar events: {e}")

# ----- Twilio Call Functions -----
def initiate_call(meeting_phone, event_name):
    """Initiate a call using Twilio and pass along the event name."""
    logger.info(f"Initiate_call invoked for event '{event_name}' to meeting phone: {meeting_phone}")
    encoded_event_name = urllib.parse.quote(event_name)
    webhook_url = f"{APP_URL}/twilio-webhook?meeting_phone={meeting_phone}&event_name={encoded_event_name}"
    try:
        call = twilio_client.calls.create(
            to=MY_PHONE_NUMBER,
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url
        )
        logger.info(f"Twilio call initiated, SID: {call.sid}")
    except Exception as e:
        logger.error(f"Error initiating call: {e}")

# ----- Flask Endpoints -----
@app.route("/twilio-webhook", methods=['POST', 'GET'])
def twilio_webhook():
    """
    When you answer the call, Twilio will request this endpoint.
    It will read out the event name with text-to-speech, pause, and then dial the meeting phone number.
    """
    meeting_phone = request.args.get('meeting_phone')
    event_name = request.args.get('event_name', 'Upcoming Event')
    response_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">Hello, you have an upcoming event: {event_name}. Please wait while we connect you.</Say>
    <Pause length="3"/>
    <Dial callerId="{MY_PHONE_NUMBER}">{meeting_phone}</Dial>
</Response>"""
    logger.info("Returning TwiML response for Twilio webhook.")
    return Response(response_xml, mimetype='text/xml')

@app.route("/")
def index():
    """Display upcoming events and provide a way to force a calendar check."""
    try:
        events = get_upcoming_events()
    except Exception as e:
        flash(str(e))
        events = []
    return render_template("index.html", events=events)

@app.route("/force-check", methods=["GET"])
def force_check():
    """Manually trigger a calendar check to schedule calls."""
    try:
        check_calendar_events()
        flash("Calendar checked successfully!")
    except Exception as e:
        flash(f"Error checking calendar: {e}")
    return redirect(url_for('index'))

# ----- Main Application Entry Point -----
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    scheduler.add_job(check_calendar_events, 'interval', minutes=5)
    scheduler.start()
    logger.info(f"Scheduler started. Flask app running on http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=True)
