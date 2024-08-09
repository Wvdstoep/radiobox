from datetime import datetime

from .extensions import db
from .models import User


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
