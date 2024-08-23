import os
from datetime import timedelta


class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    PARENT_DIR = os.path.abspath(os.path.join(BASE_DIR, os.pardir))  # Go up to the project directory
    SQLALCHEMY_DATABASE_URI = 'sqlite:///tasks.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = 'JWT_SECRET_KEY'
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=4)  # Access token expiration time
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)
    AUDIO_FILES_DIRECTORY = os.path.join(PARENT_DIR, 'audio')  # Path to the audio directory
