from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import stripe
import os
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///trading_bot.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

# Configure Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# User model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    discord_user_id = db.Column(db.String(50), unique=True)
    discord_server_id = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    
    # Subscription relationship
    subscriptions = db.relationship('Subscription', backref='user', lazy=True)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def get_active_subscription(self):
        return Subscription.query.filter_by(
            user_id=self.id,
            status='active'
        ).first()

# Subscription model
class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    stripe_subscription_id = db.Column(db.String(100), unique=True)
    stripe_customer_id = db.Column(db.String(100))
    plan_type = db.Column(db.String(20), nullable=False)  # v3, v6, v9
    coins = db.Column(db.Text)  # JSON string of selected coins
    status = db.Column(db.String(20), default='inactive')  # active, inactive, cancelled
    current_period_start = db.Column(db.DateTime)
    current_period_end = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Routes
@app.route('/')
def index():
    return render_template('index.html', stripe_publishable_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/pricing')
def pricing():
    return render_template('pricing.html', stripe_publishable_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        # Validation
        if not email or not password:
            flash('Email and password are required.', 'error')
            return render_template('register.html')
        
        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return render_template('register.html')
        
        if len(password) < 8:
            flash('Password must be at least 8 characters long.', 'error')
            return render_template('register.html')
        
        # Check if user already exists
        if User.query.filter_by(email=email).first():
            flash('Email already registered. Please log in.', 'error')
            return render_template('register.html')
        
        # Create new user
        user = User(email=email)
        user.set_password(password)
        
        try:
            db.session.add(user)
            db.session.commit()
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            logger.error(f"Registration error: {e}")
            flash('Registration failed. Please try again.', 'error')
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        if not email or not password:
            flash('Email and password are required.', 'error')
            return render_template('login.html')
        
        user = User.query.filter_by(email=email).first()
        
        if user and user.check_password(password):
            login_user(user, remember=True)
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'error')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    subscription = current_user.get_active_subscription()
    return render_template('dashboard.html', user=current_user, subscription=subscription)

@app.route('/subscribe/<plan_type>')
@login_required
def subscribe(plan_type):
    if plan_type not in ['v3', 'v6', 'v9']:
        flash('Invalid plan type.', 'error')
        return redirect(url_for('pricing'))
    
    # Check if user already has active subscription
    active_sub = current_user.get_active_subscription()
    if active_sub:
        flash('You already have an active subscription.', 'warning')
        return redirect(url_for('dashboard'))
    
    return render_template('subscribe.html', plan_type=plan_type, stripe_publishable_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        plan_type = request.form.get('plan_type')
        coins = request.form.getlist('coins')
        
        if not plan_type or plan_type not in ['v3', 'v6', 'v9']:
            return jsonify({'error': 'Invalid plan type'}), 400
        
        if not coins:
            return jsonify({'error': 'Please select at least one coin'}), 400
        
        # Define pricing (in pence - GBP)
        prices = {
            'v3': 300,   # £3.00
            'v6': 500,   # £5.00
            'v9': 799    # £7.99
        }
        
        # Create or get Stripe customer
        customer = None
        try:
            if current_user.get_active_subscription() and current_user.get_active_subscription().stripe_customer_id:
                customer_id = current_user.get_active_subscription().stripe_customer_id
                customer = stripe.Customer.retrieve(customer_id)
            else:
                customer = stripe.Customer.create(
                    email=current_user.email,
                    metadata={'user_id': current_user.id}
                )
        except Exception as e:
            logger.error(f"Stripe customer error: {e}")
            return jsonify({'error': 'Payment system error'}), 500
        
        # Create checkout session
        checkout_session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'gbp',
                    'product_data': {
                        'name': f'Trading Bot {plan_type.upper()} - {len(coins)} Coins',
                        'description': f'Monthly subscription for {plan_type.upper()} trading algorithm with {", ".join(coins)} monitoring'
                    },
                    'unit_amount': prices[plan_type],
                    'recurring': {
                        'interval': 'month',
                    },
                },
                'quantity': 1,
            }],
            mode='subscription',
            success_url=url_for('payment_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('subscribe', plan_type=plan_type, _external=True),
            metadata={
                'user_id': current_user.id,
                'plan_type': plan_type,
                'coins': ','.join(coins)
            }
        )
        
        return jsonify({'checkout_url': checkout_session.url})
        
    except Exception as e:
        logger.error(f"Checkout session error: {e}")
        return jsonify({'error': 'Payment system error'}), 500

@app.route('/payment-success')
@login_required
def payment_success():
    session_id = request.args.get('session_id')
    if not session_id:
        flash('Invalid payment session.', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        # Retrieve the checkout session
        session = stripe.checkout.Session.retrieve(session_id)
        
        if session.payment_status == 'paid':
            flash('Payment successful! Your subscription is now active.', 'success')
        else:
            flash('Payment processing. Please check your dashboard shortly.', 'info')
            
    except Exception as e:
        logger.error(f"Payment success error: {e}")
        flash('Payment completed, but there was an issue updating your account. Please contact support.', 'warning')
    
    return redirect(url_for('dashboard'))

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        logger.error("Invalid payload in webhook")
        return '', 400
    except stripe.error.SignatureVerificationError:
        logger.error("Invalid signature in webhook")
        return '', 400
    
    # Handle the event
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        handle_successful_payment(session)
    elif event['type'] == 'invoice.payment_succeeded':
        invoice = event['data']['object']
        handle_subscription_renewal(invoice)
    elif event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        handle_subscription_cancelled(subscription)
    
    return '', 200

def handle_successful_payment(session):
    try:
        user_id = session['metadata']['user_id']
        plan_type = session['metadata']['plan_type']
        coins = session['metadata']['coins']
        
        user = User.query.get(user_id)
        if not user:
            logger.error(f"User {user_id} not found for payment")
            return
        
        # Get the subscription from Stripe
        stripe_subscription = stripe.Subscription.retrieve(session['subscription'])
        
        # Create or update subscription record
        subscription = Subscription(
            user_id=user.id,
            stripe_subscription_id=stripe_subscription['id'],
            stripe_customer_id=session['customer'],
            plan_type=plan_type,
            coins=coins,
            status='active',
            current_period_start=datetime.fromtimestamp(stripe_subscription['current_period_start']),
            current_period_end=datetime.fromtimestamp(stripe_subscription['current_period_end'])
        )
        
        db.session.add(subscription)
        db.session.commit()
        
        logger.info(f"Subscription created for user {user_id}, plan {plan_type}")
        
    except Exception as e:
        logger.error(f"Error handling successful payment: {e}")
        db.session.rollback()

def handle_subscription_renewal(invoice):
    try:
        subscription_id = invoice['subscription']
        subscription = Subscription.query.filter_by(stripe_subscription_id=subscription_id).first()
        
        if subscription:
            # Update subscription period
            stripe_subscription = stripe.Subscription.retrieve(subscription_id)
            subscription.current_period_start = datetime.fromtimestamp(stripe_subscription['current_period_start'])
            subscription.current_period_end = datetime.fromtimestamp(stripe_subscription['current_period_end'])
            subscription.status = 'active'
            subscription.updated_at = datetime.utcnow()
            
            db.session.commit()
            logger.info(f"Subscription renewed: {subscription_id}")
            
    except Exception as e:
        logger.error(f"Error handling subscription renewal: {e}")
        db.session.rollback()

def handle_subscription_cancelled(stripe_subscription):
    try:
        subscription = Subscription.query.filter_by(
            stripe_subscription_id=stripe_subscription['id']
        ).first()
        
        if subscription:
            subscription.status = 'cancelled'
            subscription.updated_at = datetime.utcnow()
            db.session.commit()
            logger.info(f"Subscription cancelled: {stripe_subscription['id']}")
            
    except Exception as e:
        logger.error(f"Error handling subscription cancellation: {e}")
        db.session.rollback()

@app.route('/cancel-subscription', methods=['POST'])
@login_required
def cancel_subscription():
    try:
        subscription = current_user.get_active_subscription()
        
        if not subscription:
            flash('No active subscription found.', 'error')
            return redirect(url_for('dashboard'))
        
        # Cancel the subscription in Stripe
        stripe.Subscription.delete(subscription.stripe_subscription_id)
        
        # Update local subscription status
        subscription.status = 'cancelled'
        subscription.updated_at = datetime.utcnow()
        db.session.commit()
        
        flash('Subscription cancelled successfully. Access will continue until the end of your billing period.', 'info')
        
    except Exception as e:
        logger.error(f"Error cancelling subscription: {e}")
        flash('Error cancelling subscription. Please contact support.', 'error')
    
    return redirect(url_for('dashboard'))

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template('500.html'), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, host='0.0.0.0', port=5000)
