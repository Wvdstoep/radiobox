import os

import firebase_admin
from firebase_admin import credentials


def init_firebase():
    # Construct the correct path to the google-services.json file
    project_root = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(project_root, '..', 'instance', 'google-services.json')

    # Initialize Firebase
    cred = credentials.Certificate(json_path)
    firebase_admin.initialize_app(cred)
