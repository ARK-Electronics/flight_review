""" Methods for sending notification emails """
from __future__ import print_function

import sys
import os
import smtplib
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


def _send_email(destination, subject, content):
    """ common method for sending an email to one or more destinations """

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
