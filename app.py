import os
import re
import datetime
from flask import Flask, request, Response, render_template, redirect, url_for, flash
from apscheduler.schedulers.background import BackgroundScheduler
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from twilio.rest import Client
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Retrieve configuration from environment variables
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')
MY_PHONE_NUMBER = os.environ.get('MY_PHONE_NUMBER')
APP_URL = os.environ.get('APP_URL')
FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'default_secret_key')

# Google Calendar configuration
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']
TOKEN_FILE = 'token.json'  # Ensure token.json is generated via OAuth2 flow.
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
    """Fetch upcoming events from the primary calendar."""
    creds = get_credentials()
    service = build('calendar', 'v3', credentials=creds)
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    events_result = service.events().list(
        calendarId=CALENDAR_ID, 
        timeMin=now,
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

def schedule_call_for_event(event):
    """Schedule an outbound call for an event that contains a phone number."""
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
        scheduler.add_job(initiate_call, 'date', run_date=call_time, args=[meeting_phone])
        print(f"Scheduled call for event '{event.get('summary', 'No Title')}' at {call_time} UTC (Meeting Phone: {meeting_phone})")

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
def initiate_call(meeting_phone):
    """Use Twilio to initiate a call to your phone, passing the meeting phone number as a parameter."""
    webhook_url = f"{APP_URL}/twilio-webhook?meeting_phone={meeting_phone}"
    try:
        call = twilio_client.calls.create(
            to=MY_PHONE_NUMBER,
            from_=TWILIO_PHONE_NUMBER,
            url=webhook_url
        )
        print(f"Initiated call (SID: {call.sid}) for meeting phone: {meeting_phone}")
    except Exception as e:
        print("Error initiating call:", e)

# ----- Flask Webhook Endpoint for Twilio -----
@app.route("/twilio-webhook", methods=['POST', 'GET'])
def twilio_webhook():
    """Respond with TwiML to bridge the call to the meeting's phone number."""
    meeting_phone = request.args.get('meeting_phone')
    response_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>{meeting_phone}</Dial>
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
    print("Scheduler started. Flask app running on http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=True)
