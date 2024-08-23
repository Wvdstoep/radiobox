import os
from dotenv import load_dotenv
from flask import Flask

from .config import Config
from .extensions import db, bcrypt, jwt
from app.firebase import init_firebase


def create_app():
    load_dotenv()
    app = Flask(__name__)
    app.config.from_object(Config)
    os.makedirs(app.config['AUDIO_FILES_DIRECTORY'], exist_ok=True)
    init_firebase()

    # Initialize extensions
    db.init_app(app)
    bcrypt.init_app(app)
    jwt.init_app(app)

    # Import blueprints
    from .routes import api_bp
    app.register_blueprint(api_bp)

    # Setup database within app context
    with app.app_context():
        db.create_all()

    return app
