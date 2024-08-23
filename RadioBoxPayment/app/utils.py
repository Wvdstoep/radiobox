import logging
import os
from datetime import datetime

import requests
from flask import url_for, redirect
from google.oauth2 import id_token

from .extensions import db
from .models import User
from firebase_admin import messaging


def parse_date(date_str):
    for date_format in ('%Y-%m-%d', '%d-%m-%Y', '%m-%d-%Y'):
        try:
            return datetime.strptime(date_str, date_format)
        except ValueError:
            continue
    raise ValueError(f"time data {date_str} does not match any expected format")


def get_user_by_username(username):
    db.session.expire_all()  # Force refresh of session data
    return db.session.query(User).filter_by(username=username).first()


def send_fcm_message(token, title, body, notification_type):
    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data={
            'type': notification_type
        },
        token=token,
    )
    try:
        response = messaging.send(message)
        logging.info(f'Successfully sent message: {response}')
    except Exception as e:
        logging.error(f'Failed to send FCM message: {str(e)}')


def send_chat_message_notification(receiver_fcm_token, sender_name, message_text, chat_id=None):
    # Prepare extra data to include in the FCM notification
    extra_data = {}
    if chat_id:
        extra_data['chat_id'] = chat_id  # Include chat_id if provided

    # Call the send_fcm_message function with the chat-specific information
    send_fcm_message(
        token=receiver_fcm_token,
        title="New Chat Message",
        body=f"{sender_name}: {message_text}",
        notification_type="chat_message"
    )


def send_friend_request_notification(receiver_fcm_token, sender_name):
    send_fcm_message(
        token=receiver_fcm_token,
        title="New Friend Request",
        body=f"{sender_name} has sent you a friend request.",
        notification_type="friend_request"
    )


def on_item_sold(user_id, item_name):
    user = User.query.get(user_id)
    user_fcm_token = user.fcm_token  # Fetch the latest FCM token from the database

    if user_fcm_token:
        try:
            send_fcm_message(
                token=user_fcm_token,
                title="Item Sold!",
                body=f"Your item '{item_name}' has been sold!",
                notification_type="item_sold"
            )
        except Exception as e:
            print(f"Failed to send FCM notification: {str(e)}")


def get_google_auth_url():
    google_client_id = os.getenv('GOOGLE_CLIENT_ID')
    redirect_uri = url_for('signup', _external=True)  # Your redirect URI
    scope = "https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/userinfo.email"
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?response_type=code"
        f"&client_id={google_client_id}&redirect_uri={redirect_uri}&scope={scope}"
    )
    return redirect(auth_url)


def verify_google_token(google_token):
    google_request = requests.Request()

    try:
        # Verify the token with a forced refresh of the keys
        idinfo = id_token.verify_oauth2_token(
            google_token,
            google_request,
            os.getenv('GOOGLE_CLIENT_ID')
        )
        return idinfo

    except ValueError as e:
        # Log error and re-raise it
        logging.error(f"Token verification failed: {str(e)}")
        raise
