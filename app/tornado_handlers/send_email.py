""" Methods for sending notification emails """
from __future__ import print_function

import sys
import os
import smtplib
import requests
import json
from smtplib import SMTP_SSL, SMTP

from email.mime.text import MIMEText

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'plot_app'))
from config import *


def send_notification_email(email_address, plot_url, delete_url, info):
    """ send a notification email after uploading a plot
        :param info: dictionary with additional info
    """
    print(f"send_notification_email called with email: '{email_address}'", flush=True)

    if email_address == '':
        print("Email address is empty, not sending notification.", flush=True)
        return True

    description = info['description']
    if description == '':
        description = info['airframe']
        if 'vehicle_name' in info:
            description = "{:} - {:}".format(description, info['vehicle_name'])

    subject = "Log File uploaded ({:})".format(description)
    if len(subject) > 78: # subject should not be longer than that
        subject = subject[:78]
    destination = [email_address]

    content = """\
Hi there!

Your uploaded log file is available under:
{plot_url}

Description: {description}
Feedback: {feedback}
Vehicle type: {type}
Airframe: {airframe}
Hardware: {hardware}
Vehicle UUID: {uuid}
Software git hash: {software}
Upload file name: {upload_filename}

Use the following link to delete the log:
{delete_url}
""".format(plot_url=plot_url, delete_url=delete_url, **info)

    return _send_email(destination, subject, content)


def send_flightreport_email(destination, plot_url, rating_description,
                            wind_speed, delete_url, uploader_email, info):
    """ send notification email for a flight report upload """

    if len(destination) == 0:
        return True

    content = """\
Hi

A new flight report just got uploaded:
{plot_url}

Description: {description}
Feedback: {feedback}

Vehicle type: {type}
Airframe: {airframe}
Hardware: {hardware}
Vehicle UUID: {uuid}
Software git hash: {software}

Use the following link to delete the log:
{delete_url}
""".format(plot_url=plot_url,
           rating_description=rating_description, wind_speed=wind_speed,
           delete_url=delete_url, uploader_email=uploader_email, **info)

    description = info['description']
    if description == '':
        description = info['airframe']
        if 'vehicle_name' in info:
            description = "{:} - {:}".format(description, info['vehicle_name'])

    subject = "Flight Report uploaded ({:})".format(description)
    if info['rating'] == 'crash_sw_hw':
        subject = '[CRASH] '+subject
    if len(subject) > 78: # subject should not be longer than that
        subject = subject[:78]

    return _send_email(destination, subject, content)


def send_confirmation_email(email_address, confirmation_url):
    """ send a confirmation email after registration """
    print(f"send_confirmation_email called with email: '{email_address}'", flush=True)

    if email_address == '':
        print("Email address is empty, not sending confirmation.", flush=True)
        return False

    subject = "Confirm your Flight Review Account"
    
    content = f"""\
Hi there!

Please confirm your account by clicking the following link:
{confirmation_url}

If you did not request this, please ignore this email.
"""

    msg = MIMEText(content, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = email_config['sender']
    msg['To'] = email_address

    try:
        if email_config.get('smtpserver'):
            s = SMTP(email_config['smtpserver'], int(email_config.get('smtpport', 465)))
            if email_config.get('use_tls', False): # Assuming use_tls might be added or default to False/True depending on port
                 # Standard smtplib usage: 465 is usually SSL, 587 is STARTTLS.
                 # The existing code (which I haven't seen fully) might handle this.
                 # Let's look at how existing code does it.
                 pass
            
            # Re-reading existing code logic for SMTP
            # The config says "Will use SSL, port 465"
            # So maybe I should use SMTP_SSL if port is 465?
            
            if int(email_config.get('smtpport', 465)) == 465:
                s = SMTP_SSL(email_config['smtpserver'], int(email_config.get('smtpport', 465)))
            else:
                s = SMTP(email_config['smtpserver'], int(email_config.get('smtpport', 587)))
                s.starttls()

            if email_config.get('user_name'):
                s.login(email_config['user_name'], email_config['password'])
            s.send_message(msg)
            s.quit()
        elif email_config.get('sendlayer_api_key'):
            # Use SendLayer API
            url = "https://console.sendlayer.com/api/v1/email"
            payload = json.dumps({
                "from": {
                    "name": "Flight Review",
                    "email": email_config['sender']
                },
                "to": [
                    {
                        "name": email_address,
                        "email": email_address
                    }
                ],
                "subject": subject,
                "plain_text_body": content
            })
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {email_config["sendlayer_api_key"]}'
            }
            response = requests.request("POST", url, headers=headers, data=payload)
            print(f"SendLayer API response: {response.text}", flush=True)
        else:
             print("No email configuration found (SMTP or SendLayer).", flush=True)
             return False

    except Exception as e:
        print(f"Error sending email: {e}", flush=True)
        return False

    return True


def _send_email(destination, subject, content):
    """ common method for sending an email to one or more destinations """

    # Check if SendLayer API Key is configured
    if email_config.get('sendlayer_api_key'):
        return _send_email_via_api(destination, subject, content)

    # typical values for text_subtype are plain, html, xml
    text_subtype = 'plain'

    try:
        msg = MIMEText(content, text_subtype)
        msg['Subject'] = subject
        sender = email_config['sender']
        msg['From'] = sender # some SMTP servers will do this automatically

        print(f"Attempting to send email to {destination} via {email_config['smtpserver']}...", flush=True)

        server = email_config['smtpserver']
        port = int(email_config.get('smtpport', 465))

        print(f"Connecting to {server}:{port}...", flush=True)
        if port == 587:
            conn = SMTP(server, port, timeout=30)
            print("Connected. Setting debug level...", flush=True)
            conn.set_debuglevel(True)
            print("Starting TLS...", flush=True)
            conn.starttls()
            print("TLS started.", flush=True)
        else:
            conn = SMTP_SSL(server, port, timeout=30)
            print("Connected (SSL). Setting debug level...", flush=True)
            conn.set_debuglevel(True)

        print("Logging in...", flush=True)
        conn.login(email_config['user_name'], email_config['password'])
        try:
            print("Sending mail...", flush=True)
            conn.sendmail(sender, destination, msg.as_string())
            print(f"Email sent successfully to {destination}", flush=True)
        finally:
            conn.quit()

    except Exception as exc:
        print(f"Mail failed to send to {destination}. Error: {str(exc)}", flush=True)
        print(f"SMTP Config: Server={email_config.get('smtpserver')}, Port={email_config.get('smtpport', 465)}, User={email_config.get('user_name')}, Sender={email_config.get('sender')}", flush=True)
        return False
    return True


def _send_email_via_api(destination, subject, content):
    """ Send email using SendLayer REST API """
    print(f"Attempting to send email to {destination} via SendLayer API...", flush=True)
    
    url = "https://console.sendlayer.com/api/v1/email"
    api_key = email_config['sendlayer_api_key']
    sender_email = email_config['sender']
    
    # Format recipients
    to_list = [{"email": email, "name": email} for email in destination]
    
    payload = {
        "From": {
            "name": "Flight Review",
            "email": sender_email
        },
        "To": to_list,
        "Subject": subject,
        "ContentType": "HTML", # Using HTML to support newlines properly if needed, but content is plain text
        "HTMLContent": f"<html><body><pre>{content}</pre></body></html>",
        "PlainContent": content
    }
    
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    
    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        
        if response.status_code == 200:
            print(f"Email sent successfully via API. Response: {response.text}", flush=True)
            return True
        else:
            print(f"Failed to send email via API. Status: {response.status_code}, Response: {response.text}", flush=True)
            return False
            
    except Exception as e:
        print(f"Exception when sending email via API: {str(e)}", flush=True)
        return False

