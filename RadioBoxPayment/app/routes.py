import logging
import mimetypes
import os
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

import stripe
from firebase_admin import messaging
from flask import Blueprint, jsonify, request, current_app as app, Response, redirect, url_for
from flask_bcrypt import generate_password_hash
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token
import requests  # Standard requests library
from google.oauth2 import id_token
from google.auth.transport.requests import Request as GoogleRequest
from werkzeug.utils import secure_filename

from . import db
from .models import User, MarketplaceItem, Payment, Order, SalesTransaction, File, Review
from .utils import on_item_sold, send_fcm_message, send_chat_message_notification

api_bp = Blueprint('api', __name__)

UPLOAD_FOLDER = 'Uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


@api_bp.route('/delete-account', methods=['DELETE'])
@jwt_required()
def delete_account():
    """Delete a connected Stripe account."""
    try:
        # Get the current user's ID from the JWT token
        user_id = get_jwt_identity()
        logging.info(f"User ID from JWT: {user_id}")

        # Fetch the user from the database
        user = User.query.get(user_id)
        if not user:
            logging.error("User not found")
            return jsonify({"error": "User not found"}), 404

        # Ensure the user has a Stripe account ID
        if not user.stripe_account_id:
            logging.error("User does not have a Stripe account")
            return jsonify({"error": "User does not have a Stripe account"}), 400

        # Delete the Stripe account
        deleted_account = stripe.Account.delete(user.stripe_account_id)
        logging.info(f"Stripe account deleted: {deleted_account.id}")

        # Remove the Stripe account ID from the user record
        user.stripe_account_id = None
        db.session.commit()

        return jsonify(
            {"message": "Stripe account deleted successfully", "deleted_account_id": deleted_account.id}), 200

    except Exception as e:
        logging.error('An error occurred when deleting the Stripe account: %s', e, exc_info=True)
        return jsonify(error=str(e)), 500


@api_bp.route('/transfer', methods=['POST'])
@jwt_required()
def create_transfer():
    """Initiate a transfer to a connected account using the user's account balance."""
    try:
        data = request.json
        account_id = data.get('account_id')
        currency = data.get('currency', 'usd')

        if not account_id:
            return jsonify({"error": "Account ID is required"}), 400

        # Ensure the user has an account balance for the transfer
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        if user.account_balance <= 0:
            return jsonify({"error": "Insufficient balance"}), 400

        # Create a transfer to the connected account using the user's account balance
        transfer = stripe.Transfer.create(
            amount=int(user.account_balance * 100),  # Convert to the smallest currency unit (e.g., cents)
            currency=currency,
            destination=account_id,
            transfer_group='ORDER123',  # Optional: A unique string to group the transfer with a payment or other object
        )

        # Deduct the amount from the user's balance
        user.account_balance = 0  # Optionally, you could set this to a smaller value if partial balance transfers are allowed
        db.session.commit()

        return jsonify({'transfer': transfer.id})
    except Exception as e:
        logging.error('An error occurred when creating the transfer: ', exc_info=True)
        return jsonify(error=str(e)), 500


@api_bp.route('/refresh/<account_id>', methods=['GET'])
def refresh_account_link(account_id):
    """Refresh the Stripe account link."""
    try:
        # Create a new account link
        account_link = stripe.AccountLink.create(
            account=account_id,
            return_url=f"http://localhost:4242/return/{account_id}",
            refresh_url=f"http://localhost:5001/refresh/{account_id}",
            type="account_onboarding",
        )

        # Redirect the user to the new account link URL
        return redirect(account_link.url)

    except Exception as e:
        logging.error('An error occurred when refreshing the Stripe account link: ', exc_info=True)
        return Response(f"Error: {str(e)}", content_type='text/plain', status=500)


@api_bp.route('/account', methods=['GET'])
@jwt_required()
def get_stripe_account():
    try:
        # Get the current user from the database
        user_id = get_jwt_identity()
        user = User.query.get(user_id)

        if not user or not user.stripe_account_id:
            return jsonify({"error": "User not found or no associated Stripe account"}), 404

        # Retrieve the Stripe account using the stored account ID
        account = stripe.Account.retrieve(user.stripe_account_id)

        # Extract the necessary fields to check onboarding status
        charges_enabled = account.get('charges_enabled', False)
        details_submitted = account.get('details_submitted', False)

        # Send the relevant account information to the frontend
        return jsonify({
            "stripe_account_id": account.id,
            "charges_enabled": charges_enabled,
            "details_submitted": details_submitted
        }), 200

    except stripe.error.StripeError as e:
        # Handle Stripe-specific errors
        return jsonify({"error": str(e)}), 500

    except Exception as e:
        # Handle other errors
        return jsonify({"error": str(e)}), 500


@api_bp.route('/account', methods=['POST'])
@jwt_required()
def create_account():
    try:
        logging.debug("Request Headers: %s", request.headers)
        logging.debug("Request Data: %s", request.get_json())

        # Extract the necessary data from the request body
        data = request.get_json()

        # Extracting data with default values if not provided
        country = data.get('country', 'US')
        email = data.get('email')
        business_type = data.get('business_type')
        company = data.get('company', {})
        individual = data.get('individual', {})

        # Prepare the individual dictionary if business_type is 'individual'
        if business_type == "individual":
            individual_data = {
                "address": {
                    "city": individual.get("address", {}).get("city"),
                    "country": individual.get("address", {}).get("country"),
                    "line1": individual.get("address", {}).get("line1"),
                    "line2": individual.get("address", {}).get("line2"),
                    "postal_code": individual.get("address", {}).get("postal_code"),
                    "state": individual.get("address", {}).get("state"),
                },
                "dob": {
                    "day": individual.get("dob", {}).get("day"),
                    "month": individual.get("dob", {}).get("month"),
                    "year": individual.get("dob", {}).get("year"),
                },
                "email": individual.get("email"),
                "first_name": individual.get("first_name"),
                "last_name": individual.get("last_name"),
                "phone": individual.get("phone"),
                # Add any other fields as necessary
            }

        else:
            individual_data = {}

        # Create a Stripe account with the selected country and other prefilled information
        account = stripe.Account.create(
            controller={
                "fees": {"payer": "application"},
                "losses": {"payments": "application"},
                "stripe_dashboard": {"type": "express"},
            },
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True}
            },
            country=country,
            email=email,
            business_type=business_type,
            company=company,
            individual=individual_data,  # Pass the individual data

        )

        logging.info("Stripe account created: %s", account.id)

        # Get the current user from the database
        user_id = get_jwt_identity()
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        # Save the Stripe account ID to the user record
        user.stripe_account_id = account.id
        db.session.commit()

        return jsonify({
            'stripe_account_id': account.id,
            'email': account.email,
            'country': account.country
        })

    except Exception as e:
        logging.error('An error occurred when calling the Stripe API to create an account: %s', e, exc_info=True)
        return jsonify(error=str(e)), 500


@api_bp.route('/account/<account>', methods=['POST'])
def update_account(account):
    try:
        connected_account = stripe.Account.modify(
            account,
            business_type="individual",
        )

        return jsonify({
            'account': connected_account.id,
        })
    except Exception as e:
        print('An error occurred when calling the Stripe API to update an account: ', e)
        return jsonify(error=str(e)), 500


@api_bp.route('/account_link', methods=['POST'])
@jwt_required()
def create_account_link():
    """Create a Stripe account link for onboarding."""
    try:
        data = request.get_json()
        connected_account_id = data.get('account')

        if not connected_account_id:
            return jsonify({"error": "Account ID is required"}), 400

        return_url = f"http://localhost:4242/return/{connected_account_id}"
        refresh_url = f"http://192.168.0.22:5001/refresh/{connected_account_id}"

        account_link = stripe.AccountLink.create(
            account=connected_account_id,
            return_url=return_url,
            refresh_url=refresh_url,
            type="account_onboarding",
        )

        if account_link.url == refresh_url:
            # Perform different action if refresh URL is returned
            logging.warning('Stripe returned the refresh URL, indicating the account link process was incomplete.')
            return jsonify({
                'message': 'Account link process was incomplete. Please try again.',
                'refresh_url': refresh_url,
            }), 202

        # Otherwise, return the normal account link URL
        return jsonify({
            'url': account_link.url,
        })
    except Exception as e:
        logging.error('An error occurred when calling the Stripe API to create an account link: ', exc_info=True)
        return jsonify(error=str(e)), 500


@api_bp.route('/payout', methods=['POST'])
@jwt_required()
def create_payout():
    """Initiate a payout to a connected account."""
    try:
        data = request.json
        account_id = data.get('account_id')
        amount = data.get('amount')
        currency = data.get('currency', 'usd')

        # Ensure the user has enough balance for the payout
        current_user_id = get_jwt_identity()
        user = User.query.get(current_user_id)
        if user.account_balance < amount:
            return jsonify({"error": "Insufficient balance"}), 400

        # Create a payout to the connected account
        payout = stripe.Payout.create(
            amount=int(amount),
            currency=currency,
            stripe_account=account_id,
        )

        # Deduct the amount from the user's balance
        user.account_balance -= amount
        db.session.commit()

        return jsonify({'payout': payout.id})
    except Exception as e:
        logging.error('An error occurred when creating the payout: ', exc_info=True)
        return jsonify(error=str(e)), 500


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
        fcm_token = data['fcm_token']  # Extract FCM token

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

                if fcm_token:
                    on_item_sold(marketplace_item.userId, marketplace_item.name)

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

    # Retrieve amount, currency, and item ID from the request
    amount = request.json.get('amount')
    currency = request.json.get('currency')
    item_id = request.json.get('item_id')
    print(f"Received amount: {amount}, currency: {currency}, item_id: {item_id}")

    print(f"Received item_id: {item_id}")
    marketplace_item = MarketplaceItem.query.get(item_id)
    if marketplace_item:
        print(f"Found marketplace item: {marketplace_item.name}")
    else:
        print(f"No marketplace item found with id: {item_id}")

    seller = User.query.get(marketplace_item.userId)
    if not seller or not seller.stripe_account_id:
        return jsonify({"error": "Seller not found or not connected to Stripe"}), 404

    # Calculate platform fee (e.g., 10% of the amount)
    platform_fee_percentage = 10
    platform_fee_amount = int(amount * platform_fee_percentage / 100)

    # Calculate the amount to be transferred to the seller
    transfer_amount = amount - platform_fee_amount

    paymentIntent = stripe.PaymentIntent.create(
        amount=amount,  # The total amount to be charged to the customer
        currency=currency,
        customer=customer.id,
        automatic_payment_methods={'enabled': True},
        transfer_data={
            'destination': seller.stripe_account_id
        },
        application_fee_amount=platform_fee_amount,  # Specify the platform fee
        on_behalf_of=seller.stripe_account_id  # Optional: for showing seller's details on the statement
    )

    # Create the ephemeral key
    ephemeralKey = stripe.EphemeralKey.create(
        customer=customer.id,
        stripe_version='2022-11-15'  # Use a more recent version if necessary
    )

    return jsonify({
        'paymentIntent': paymentIntent.client_secret,
        'ephemeralKey': ephemeralKey.secret,
        'customer': customer.id,
        'publishableKey': publishable_key
    }), 200


@api_bp.route('/get_fcm_token', methods=['GET'])
@jwt_required()
def get_fcm_token():
    try:
        # Get the current user's ID from the JWT token
        user_id = get_jwt_identity()
        logging.info(f"User ID from JWT: {user_id}")

        # Fetch the user from the database
        user = User.query.get(user_id)
        if not user:
            logging.error("User not found")
            return jsonify({"error": "User not found"}), 404

        # Ensure the user has an FCM token
        if not user.fcm_token:
            logging.error("User does not have an FCM token")
            return jsonify({"error": "User does not have an FCM token"}), 400

        return jsonify({"fcm_token": user.fcm_token}), 200

    except Exception as e:
        logging.error('An error occurred when retrieving the FCM token: %s', e, exc_info=True)
        return jsonify(error=str(e)), 500


@api_bp.route('/signup', methods=['POST'])
def signup_with_google_token():
    data = request.json
    logging.debug(f"Received data: {data}")
    google_token = data.get('google_token')
    fcm_token = data.get('fcm_token')
    firebase_user_id = data.get('firebase_user_id')  # Extract the Firebase user ID

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
                password_hash=password_hash,
                fcm_token=fcm_token  # Save the FCM token
            )
            db.session.add(user)
        else:
            # Update FCM token for existing user
            user.fcm_token = fcm_token

        # Optionally update Firebase user ID if needed
        if firebase_user_id:
            user.firebase_user_id = firebase_user_id  # Assuming you have this field

        # Create or retrieve Stripe customer ID
        stripe.api_key = os.getenv('API_KEY')
        if not user.stripe_customer_id:
            customer = stripe.Customer.create(email=email)
            user.stripe_customer_id = customer.id

        # Commit the transaction
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


@api_bp.route('/login')
def get_google_auth_url():
    google_client_id = os.getenv('GOOGLE_CLIENT_ID')
    redirect_uri = url_for('google_auth_callback', _external=True)  # Callback URI
    scope = "https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/userinfo.email"
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?response_type=code"
        f"&client_id={google_client_id}&redirect_uri={redirect_uri}&scope={scope}"
    )
    return redirect(auth_url)


@api_bp.route('/auth/callback')
def google_auth_callback():
    google_client_id = os.getenv('GOOGLE_CLIENT_ID')
    google_client_secret = os.getenv('GOOGLE_CLIENT_SECRET')
    redirect_uri = url_for('google_auth_callback', _external=True)
    code = request.args.get('code')

    if not code:
        return jsonify({"message": "Missing authorization code", "success": False}), 400

    # Exchange authorization code for access token
    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "code": code,
        "client_id": google_client_id,
        "client_secret": google_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }

    try:
        token_response = requests.post(token_url, data=token_data)
        token_response_data = token_response.json()

        if "error" in token_response_data:
            return jsonify({"message": "Error obtaining access token", "error": token_response_data['error'],
                            "success": False}), 400

        access_token = token_response_data.get('access_token')
        google_token = token_response_data.get('id_token')  # ID token if needed

        # Now, use the access token or ID token for further operations (e.g., fetching user profile)
        # For example, call the signup function or return tokens
        return signup_with_google_token(google_token=google_token)

    except Exception as e:
        logging.error(f"Failed to exchange authorization code: {str(e)}")
        return jsonify({"message": "Failed to exchange authorization code", "error": str(e), "success": False}), 500


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
            reviewsCount=data.get('reviewsCount'),
            language=data.get('language')
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
                "id": item.id,  # Ensure 'id' is included
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
                "reviewsCount": item.reviewsCount,
                "language": item.language,  # Added the language field here
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


@api_bp.route('/item_sold/<user_fcm_token>/<item_name>', methods=['GET'])
def item_sold(user_fcm_token, item_name):
    try:
        on_item_sold(user_fcm_token, item_name)
        return jsonify({"message": "Notification sent!"}), 200
    except Exception as e:
        logging.error(f"Failed to send notification: {e}", exc_info=True)
        return jsonify({"error": "Failed to send notification", "details": str(e)}), 500


@api_bp.route('/update_fcm_token', methods=['POST'])
@jwt_required()
def update_fcm_token():
    try:
        # Get the current user's ID from the JWT token
        user_id = get_jwt_identity()
        logging.info(f"User ID from JWT: {user_id}")

        # Fetch the user from the database
        user = User.query.get(user_id)
        if not user:
            logging.error("User not found")
            return jsonify({"error": "User not found"}), 404

        # Get the FCM token from the request body
        data = request.get_json()
        fcm_token = data.get("fcm_token")

        if not fcm_token:
            logging.error("No FCM token provided")
            return jsonify({"error": "No FCM token provided"}), 400

        # Update the user's FCM token in the database
        user.fcm_token = fcm_token
        db.session.commit()

        logging.info(f"FCM token updated for user {user_id}")
        return jsonify({"message": "FCM token updated successfully"}), 200

    except Exception as e:
        logging.error('An error occurred when updating the FCM token: %s', e, exc_info=True)
        return jsonify(error=str(e)), 500


@api_bp.route('/api/sendFriendRequestNotification', methods=['POST'])
@jwt_required()
def send_friend_request_notification():
    # Get the identity of the current user (from the JWT token)
    current_user = get_jwt_identity()

    data = request.get_json()
    receiver_id = data.get('receiver_id')
    sender_name = data.get('sender_name')

    # Retrieve receiver's user record from the database based on firebase_user_id
    user = User.query.filter_by(firebase_user_id=receiver_id).first()

    if user is None:
        logging.error(f"User with Firebase User ID {receiver_id} not found.")
        return jsonify({"status": "error", "message": "User not found"}), 404

    receiver_fcm_token = user.fcm_token

    if receiver_fcm_token:
        try:
            # Use the send_fcm_message function
            send_fcm_message(
                token=receiver_fcm_token,
                title="New Friend Request",
                body=f"{sender_name} has sent you a friend request.",
                notification_type="friend_request"
            )
            return jsonify({"status": "success", "message": "Notification sent successfully"}), 200
        except Exception as e:
            logging.error(f"Error sending FCM notification: {str(e)}")
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        logging.error(f"FCM token not found for user with Firebase User ID {receiver_id}.")
        return jsonify({"status": "error", "message": "FCM token not found"}), 404


@api_bp.route('/api/sendChatMessageNotification', methods=['POST'])
@jwt_required()
def send_chat_message_notification_endpoint():
    # Get the identity of the current user (from the JWT token)
    current_user = get_jwt_identity()

    data = request.get_json()
    receiver_id = data.get('receiver_id')
    sender_name = data.get('sender_name')
    message = data.get('message')
    chat_id = data.get('chat_id')  # Optional field

    # Retrieve receiver's user record from the database based on firebase_user_id
    user = User.query.filter_by(firebase_user_id=receiver_id).first()

    if user is None:
        logging.error(f"User with Firebase User ID {receiver_id} not found.")
        return jsonify({"status": "error", "message": "User not found"}), 404

    receiver_fcm_token = user.fcm_token

    if receiver_fcm_token:
        try:
            # Use the send_chat_message_notification function
            send_chat_message_notification(
                receiver_fcm_token=receiver_fcm_token,
                sender_name=sender_name,
                message_text=message,
                chat_id=chat_id  # Pass the chat_id if available
            )
            return jsonify({"status": "success", "message": "Notification sent successfully"}), 200
        except Exception as e:
            logging.error(f"Error sending FCM notification: {str(e)}")
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        logging.error(f"FCM token not found for user with Firebase User ID {receiver_id}.")
        return jsonify({"status": "error", "message": "FCM token not found"}), 404


@api_bp.route('/api/submitReview', methods=['POST'])
@jwt_required()
def submit_review():
    # Get the identity of the current user from the JWT token
    current_user_id = get_jwt_identity()

    # Parse JSON data from the request body
    data = request.get_json()
    marketplace_item_id = data.get('marketplace_item_id')
    rating = data.get('rating')
    comment = data.get('comment')

    # Validate the input
    if not marketplace_item_id or rating is None:
        return jsonify({"status": "error", "message": "Missing required fields"}), 400

    # Ensure the rating is within the valid range
    if rating < 0 or rating > 5:
        return jsonify({"status": "error", "message": "Invalid rating"}), 400

    # Retrieve the current user
    user = User.query.filter_by(id=current_user_id).first()
    if user is None:
        return jsonify({"status": "error", "message": "User not found"}), 404

    # Retrieve the marketplace item
    item = MarketplaceItem.query.get(marketplace_item_id)
    if item is None:
        return jsonify({"status": "error", "message": "Item not found"}), 404

    # Create and save the review
    review = Review(
        marketplace_item_id=marketplace_item_id,
        user_id=current_user_id,
        rating=rating,
        comment=comment
    )

    db.session.add(review)
    db.session.commit()

    return jsonify({"status": "success", "message": "Review submitted successfully"}), 201


@api_bp.route('/refresh-token', methods=['POST'])
@jwt_required(refresh=True)
def refresh_token():
    try:
        current_user = get_jwt_identity()
        new_token = create_access_token(identity=current_user, fresh=False)  # Or your preferred way to create a new token
        return jsonify({"token": new_token}), 200
    except Exception as e:
        app.logger.error(f"Failed to refresh token: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
