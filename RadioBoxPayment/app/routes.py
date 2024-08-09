import logging
import mimetypes
import os
import uuid
from datetime import datetime
from urllib.parse import urlparse

import stripe
from flask import Blueprint, jsonify, request, current_app as app
from flask_bcrypt import generate_password_hash
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token
import requests  # Standard requests library
from google.oauth2 import id_token
from google.auth.transport.requests import Request as GoogleRequest
from werkzeug.utils import secure_filename

from .extensions import db
from .models import User, MarketplaceItem, Payment, Order, SalesTransaction, File

api_bp = Blueprint('api', __name__)

UPLOAD_FOLDER = 'Uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


@api_bp.route('/payment-success', methods=['POST'])
@jwt_required()
def handle_payment_success():
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        app.logger.info(f"Received data: {data}")  # Log incoming data for debugging

        # Ensure 'items' list and 'total_amount' are in the request data
        if not data or 'items' not in data or 'total_amount' not in data:
            return jsonify({"error": "Missing required fields"}), 400

        # Extract the list of items and total amount
        items = data['items']
        total_amount = data['total_amount']

        # Check if items is a list and not empty
        if not isinstance(items, list) or not items:
            return jsonify({"error": "Items must be a non-empty list"}), 400

        # Create an Order record
        order = Order(
            user_id=current_user_id,
            total_amount=total_amount,
            status='completed'
        )
        db.session.add(order)
        db.session.flush()  # Ensure order.id is available

        # Loop through each item to validate and process
        for item in items:
            marketplace_item_id = item.get('id')
            amount = item.get('amount')

            if not marketplace_item_id or not amount:
                return jsonify({"error": "Each item must have an 'id' and 'amount'"}), 400

            # Fetch marketplace item details
            marketplace_item = MarketplaceItem.query.get(marketplace_item_id)
            if not marketplace_item:
                app.logger.error(f"MarketplaceItem ID {marketplace_item_id} does not exist.")
                return jsonify({"error": f"Invalid MarketplaceItem ID {marketplace_item_id}"}), 400

            # Calculate the revenue split
            your_share = amount * 0.10
            seller_share = amount * 0.90

            # Log the split details for debugging
            app.logger.info(f"Amount: {amount}, Your Share: {your_share}, Seller's Share: {seller_share}")

            # Create a new Payment record for each item
            payment = Payment(
                marketplace_item_id=marketplace_item.id,
                transaction_date=datetime.utcnow(),
                amount=amount,
                status='success',
                order_id=order.id
            )
            db.session.add(payment)

            # Record the transaction details in SalesTransaction
            sales_transaction = SalesTransaction(
                item_id=marketplace_item.id,
                user_id=marketplace_item.userId,
                order_id=order.id,
                your_share=your_share,
                seller_share=seller_share,
                transaction_date=datetime.utcnow()
            )
            db.session.add(sales_transaction)

            # Update seller's account balance
            seller = User.query.get(marketplace_item.userId)
            if seller:
                seller.account_balance += seller_share
                app.logger.info(f"Updated seller {seller.email} balance by {seller_share}")

        # Commit all transactions at once
        db.session.commit()

        return jsonify({"message": "Payment recorded and order created successfully"}), 200

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Failed to handle payment success: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route('/payment-sheet', methods=['POST'])
@jwt_required()
def payment_sheet():
    current_user_id = get_jwt_identity()
    publishable_key = os.getenv('PK_TEST')
    api_key = os.getenv('API_KEY')
    stripe.api_key = api_key
    user = User.query.get(current_user_id)

    if not user:
        return jsonify({"error": "User not found"}), 404

    if not user.stripe_customer_id:
        customer = stripe.Customer.create()
        user.stripe_customer_id = customer.id
        db.session.commit()
    else:
        customer = stripe.Customer.retrieve(user.stripe_customer_id)

    ephemeralKey = stripe.EphemeralKey.create(
        customer=customer.id,
        stripe_version='2020-08-27'
    )

    amount = request.json.get('amount')
    currency = request.json.get('currency')
    print(f"Received amount: {amount}, currency: {currency}")

    paymentIntent = stripe.PaymentIntent.create(
        amount=amount,
        currency=currency,
        customer=customer.id,
        automatic_payment_methods={'enabled': True}
    )

    return jsonify({
        'paymentIntent': paymentIntent.client_secret,
        'ephemeralKey': ephemeralKey.secret,
        'customer': customer.id,
        'publishableKey': publishable_key
    }), 200


@api_bp.route('/signup', methods=['POST'])
def signup():
    data = request.json
    google_token = data.get('google_token')

    if not google_token:
        return jsonify({"message": "Missing Google token", "success": False}), 400

    try:
        # Verify the Google token
        idinfo = id_token.verify_oauth2_token(google_token, GoogleRequest(), os.getenv('GOOGLE_CLIENT_ID'))

        if idinfo['iss'] not in ['accounts.google.com', 'https://accounts.google.com']:
            raise ValueError('Wrong issuer.')

        # Get user info from the token
        google_id = idinfo['sub']
        email = idinfo['email']
        name = idinfo.get('name', '')

        # Check if user already exists
        user = User.query.filter_by(email=email).first()
        if not user:
            # Create new user if they do not exist
            password_hash = generate_password_hash(os.urandom(24).hex())
            user = User(
                email=email,
                username=email.split('@')[0],
                google_id=google_id,
                password_hash=password_hash
            )
            db.session.add(user)
            db.session.commit()

        # Create or retrieve Stripe customer ID
        stripe.api_key = os.getenv('API_KEY')
        if not user.stripe_customer_id:
            customer = stripe.Customer.create(email=email)
            user.stripe_customer_id = customer.id
            db.session.commit()

        # Create a fresh access token
        access_token = create_access_token(identity=user.id)
        return jsonify(
            {"access_token": access_token, "stripe_customer_id": user.stripe_customer_id, "success": True}), 201

    except ValueError as e:
        logging.error(f"Invalid Google token: {str(e)}")
        return jsonify({"message": "Invalid Google token", "error": str(e), "success": False}), 400

    except Exception as e:
        logging.error(f"Failed to create or update user: {str(e)}")
        db.session.rollback()
        return jsonify({"message": "Failed to create or update user", "error": str(e), "success": False}), 500


def sanitize_filename(url, response):
    """Generate a safe and unique filename using a UUID and the correct file extension."""
    # Generate a UUID for uniqueness
    unique_id = str(uuid.uuid4())

    # Guess the MIME type from the response headers if possible
    mime_type = response.headers.get('Content-Type')
    extension = mimetypes.guess_extension(mime_type)

    # Use the correct extension, default to .bin if none found
    if not extension:
        extension = ".bin"

    # Combine the UUID and extension to create the unique filename
    unique_filename = f"{unique_id}{extension}"

    return unique_filename


@api_bp.route('/marketplace_item', methods=['POST'])
@jwt_required()
def add_marketplace_item():
    logging.info("Received request to add marketplace item")

    user_id = get_jwt_identity()
    user = User.query.get(user_id)

    if not user:
        logging.error("User not found for ID: %s", user_id)
        return jsonify({"error": "User not found"}), 404

    data = request.json

    if 'id' not in data:
        logging.error("ID is missing in the request data")
        return jsonify({"error": "ID is required"}), 400

    item_id = data.get('id')
    logging.info("Received ID from client: %s", item_id)

    existing_item = MarketplaceItem.query.get(item_id)
    if existing_item:
        logging.error("Item with ID %s already exists", item_id)
        return jsonify({"error": "Item with this ID already exists"}), 400

    file_url = data.get('url')
    if file_url:
        try:
            response = requests.get(file_url)
            response.raise_for_status()

            # Use the updated sanitize_filename function to create a unique filename
            filename = sanitize_filename(file_url, response)
            file_path = os.path.join(UPLOAD_FOLDER, filename)

            with open(file_path, 'wb') as file:
                file.write(response.content)

            new_file = File(
                filename=filename,
                filepath=file_path,
                url=file_url,
                user_id=user.id,
                marketplace_item_id=item_id
            )
            db.session.add(new_file)

        except requests.RequestException as e:
            logging.error("Failed to download file from %s: %s", file_url, str(e))
            return jsonify({"error": "Failed to download file"}), 500

    try:
        new_item = MarketplaceItem(
            id=item_id,
            name=data.get('name'),
            description=data.get('description'),
            url=file_url,
            imageUrl=data.get('imageUrl'),
            createdAt=int(datetime.now().timestamp() * 1000),
            releaseDate=data.get('releaseDate'),
            price=data.get('price'),
            currency=data.get('currency'),
            genre=data.get('genre'),
            duration=data.get('duration'),
            fileSize=len(response.content) if file_url else data.get('fileSize'),
            audioQuality=data.get('audioQuality'),
            artist=data.get('artist'),
            album=data.get('album'),
            license=data.get('license'),
            tags=data.get('tags'),
            popularity=data.get('popularity'),
            userId=user.id,
            userName=user.username,
            userEmail=user.email,
            rating=data.get('rating'),
            reviewsCount=data.get('reviewsCount')
        )
        db.session.add(new_item)
        db.session.commit()
        logging.info("Item with ID %s added successfully", item_id)
        return jsonify({"message": "Marketplace item added successfully", "item_id": item_id}), 201
    except Exception as e:
        logging.error("Error adding item with ID %s: %s", item_id, str(e))
        return jsonify({"error": "Error adding item"}), 500


@api_bp.route('/orders', methods=['GET'])
@jwt_required()
def get_user_orders():
    try:
        # Get the current user's ID from the JWT token
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)

        # Check if the user exists
        if not user:
            return jsonify({"error": "User not found"}), 404

        # Get all orders for the user
        orders = Order.query.filter_by(user_id=current_user_id).all()

        # Prepare a list to hold the orders data
        orders_data = []

        # Use a single query to get all the relevant payments
        order_ids = [order.id for order in orders]
        payments = Payment.query.filter(Payment.order_id.in_(order_ids)).all()

        # Create a dictionary to map marketplace item ids to their data
        item_dict = {}
        item_ids = {payment.marketplace_item_id for payment in payments}
        items = MarketplaceItem.query.filter(MarketplaceItem.id.in_(item_ids)).all()

        for item in items:
            item_dict[item.id] = {
                "item_id": item.id,
                "name": item.name,
                "description": item.description,
                "url": item.url,
                "imageUrl": item.imageUrl,
                "createdAt": item.createdAt,
                "releaseDate": item.releaseDate,
                "price": item.price,
                "currency": item.currency,
                "genre": item.genre,
                "duration": item.duration,
                "fileSize": item.fileSize,
                "audioQuality": item.audioQuality,
                "artist": item.artist,
                "album": item.album,
                "license": item.license,
                "tags": item.tags,
                "popularity": item.popularity,
                "userId": item.userId,
                "userName": item.userName,
                "userEmail": item.userEmail,
                "rating": item.rating,
                "reviewsCount": item.reviewsCount
            }

        # Iterate through orders and payments to build the response
        for order in orders:
            order_data = {
                "order_id": order.id,
                "total_amount": order.total_amount,
                "created_at": order.created_at,
                "status": order.status,
                "items": []
            }

            for payment in payments:
                if payment.order_id == order.id:
                    item_data = item_dict.get(payment.marketplace_item_id, {})
                    order_data["items"].append({
                        **item_data,
                        "amount_paid": payment.amount,
                        "payment_status": payment.status
                    })

            orders_data.append(order_data)

        return jsonify(orders_data), 200

    except Exception as e:
        app.logger.error(f"Failed to fetch orders: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@api_bp.route('/sales-transactions', methods=['GET'])
@jwt_required()
def get_sales_transactions():
    try:
        current_user_id = get_jwt_identity()
        transactions = SalesTransaction.query.filter_by(user_id=current_user_id).all()

        transactions_data = []

        for transaction in transactions:
            item = MarketplaceItem.query.get(transaction.item_id)
            order = Order.query.get(transaction.order_id)

            transactions_data.append({
                "transaction_id": transaction.id,
                "item_id": transaction.item_id,
                "item_name": item.name if item else "Unknown",
                "order_id": transaction.order_id,
                "order_total_amount": order.total_amount if order else 0,
                "your_share": transaction.your_share,
                "seller_share": transaction.seller_share,
                "transaction_date": transaction.transaction_date.isoformat()
            })

        return jsonify(transactions_data), 200

    except Exception as e:
        app.logger.error(f"Failed to fetch sales transactions: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
