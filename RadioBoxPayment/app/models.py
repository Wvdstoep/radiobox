from flask_bcrypt import generate_password_hash
from .extensions import db
from datetime import datetime


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    google_id = db.Column(db.String(120), unique=True)
    full_name = db.Column(db.String(100), nullable=True)
    date_of_birth = db.Column(db.Date, nullable=True)
    bio = db.Column(db.String(255), nullable=True)
    password_hash = db.Column(db.String(128), nullable=False, default=generate_password_hash('default_password'))
    stripe_customer_id = db.Column(db.String(128))
    marketplace_items = db.relationship('MarketplaceItem', backref='user', lazy=True)
    orders = db.relationship('Order', backref='user', lazy=True)
    account_balance = db.Column(db.Float, default=0.0)  # New field for account balance



class MarketplaceItem(db.Model):
    __tablename__ = 'marketplace_items'
    id = db.Column(db.String, primary_key=True)
    name = db.Column(db.String, nullable=False)
    description = db.Column(db.String, nullable=True)
    url = db.Column(db.String, nullable=True)
    imageUrl = db.Column(db.String, nullable=True)
    createdAt = db.Column(db.BigInteger, nullable=False)
    releaseDate = db.Column(db.BigInteger, nullable=True)
    price = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String, nullable=False)
    genre = db.Column(db.String, nullable=True)
    duration = db.Column(db.Integer, nullable=True)
    fileSize = db.Column(db.BigInteger, nullable=True)
    audioQuality = db.Column(db.String, nullable=True)
    artist = db.Column(db.String, nullable=True)
    album = db.Column(db.String, nullable=True)
    license = db.Column(db.String, nullable=True)
    tags = db.Column(db.PickleType, nullable=True)
    popularity = db.Column(db.Integer, nullable=True)
    userId = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    userName = db.Column(db.String, nullable=False)
    userEmail = db.Column(db.String, nullable=False)
    rating = db.Column(db.Float, nullable=True)
    reviewsCount = db.Column(db.Integer, nullable=True)
    payments = db.relationship('Payment', backref='marketplace_item', lazy=True)


class Payment(db.Model):
    __tablename__ = 'payments'
    id = db.Column(db.Integer, primary_key=True)
    marketplace_item_id = db.Column(db.String, db.ForeignKey('marketplace_items.id'), nullable=False)
    transaction_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)


class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    status = db.Column(db.String(50), nullable=False, default='completed')
    payments = db.relationship('Payment', backref='order', lazy=True)


class SalesTransaction(db.Model):
    __tablename__ = 'sales_transactions'

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.String, db.ForeignKey('marketplace_items.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    your_share = db.Column(db.Float, nullable=False)
    seller_share = db.Column(db.Float, nullable=False)
    transaction_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class File(db.Model):
    __tablename__ = 'files'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String, nullable=False)
    filepath = db.Column(db.String, nullable=False)
    url = db.Column(db.String, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    marketplace_item_id = db.Column(db.String, db.ForeignKey('marketplace_items.id'), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

class Review(db.Model):
    __tablename__ = 'reviews'
    id = db.Column(db.Integer, primary_key=True)
    marketplace_item_id = db.Column(db.String, db.ForeignKey('marketplace_items.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    rating = db.Column(db.Float, nullable=False)
    comment = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)