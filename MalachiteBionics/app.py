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

# Configuration with fallbacks
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback-secret-key-for-development')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///trading_bot.db')

# Fix for Railway PostgreSQL URLs
database_url = app.config['SQLALCHEMY_DATABASE_URI']
if database_url and database_url.startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
    'connect_args': {
        'options': '-csearch_path=public'
    } if 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI'] else {}
}

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configure Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')

# User model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
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

# Initialize database tables
def create_tables():
    """Create database tables if they don't exist"""
    try:
        with app.app_context():
            # Only create tables if they don't exist, don't drop existing ones
            db.create_all()
            logger.info("Database tables created/verified successfully")
            
            # Create admin user if it doesn't exist (but don't recreate if exists)
            admin = User.query.filter_by(email='admin@tradingbot.com').first()
            if not admin:
                admin = User(email='admin@tradingbot.com', display_name='Admin')
                admin.set_password('admin123')
                db.session.add(admin)
                db.session.commit()
                logger.info("Admin user created")
            else:
                logger.info("Admin user already exists")
                
    except Exception as e:
        logger.error(f"Error creating tables: {e}")

# Call table creation on app start (only creates tables if they don't exist)
create_tables()

# Routes
@app.route('/')
def index():
    return render_template('index.html', stripe_publishable_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/health')
def health_check():
    """Health check endpoint for Railway"""
    try:
        # Test database connection
        db.session.execute('SELECT 1')
        
        # Check if users exist
        user_count = User.query.count()
        
        return jsonify({
            'status': 'healthy', 
            'database': 'connected',
            'users_count': user_count,
            'database_url_type': 'postgresql' if 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI'] else 'sqlite'
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

@app.route('/debug-db')
def debug_database():
    """Debug endpoint to check database state - REMOVE IN PRODUCTION"""
    try:
        users = User.query.all()
        user_list = []
        for user in users:
            user_list.append({
                'id': user.id,
                'email': user.email,
                'display_name': user.display_name,
                'created_at': user.created_at.strftime('%Y-%m-%d %H:%M:%S')
            })
        
        subscriptions = Subscription.query.all()
        sub_list = []
        for sub in subscriptions:
            sub_list.append({
                'id': sub.id,
                'user_id': sub.user_id,
                'plan_type': sub.plan_type,
                'status': sub.status
            })
        
        return jsonify({
            'database_url': app.config['SQLALCHEMY_DATABASE_URI'][:50] + '...' if len(app.config['SQLALCHEMY_DATABASE_URI']) > 50 else app.config['SQLALCHEMY_DATABASE_URI'],
            'users': user_list,
            'subscriptions': sub_list,
            'total_users': len(user_list),
            'total_subscriptions': len(sub_list)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/pricing')
def pricing():
    return render_template('pricing.html', stripe_publishable_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        display_name = request.form.get('display_name')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        # Validation
        if not email or not display_name or not password:
            flash('Email, display name, and password are required.', 'error')
            return render_template('register.html')
        
        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return render_template('register.html')
        
        if len(password) < 8:
            flash('Password must be at least 8 characters long.', 'error')
            return render_template('register.html')
        
        if len(display_name) < 2:
            flash('Display name must be at least 2 characters long.', 'error')
            return render_template('register.html')
        
        # Check if user already exists
        try:
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                flash('Email already registered. Please log in.', 'error')
                return render_template('register.html')
        except Exception as e:
            logger.error(f"Database query error during registration: {e}")
            flash('Database error. Please try again.', 'error')
            return render_template('register.html')
        
        # Create new user
        try:
            user = User(email=email, display_name=display_name)
            user.set_password(password)
            
            db.session.add(user)
            db.session.commit()
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            logger.error(f"Registration error: {e}")
            flash('Registration failed. Please try again later.', 'error')
            return render_template('register.html')
    
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
            return jsonify({'error': 'Please select exactly 2 cryptocurrencies'}), 400
        
        if len(coins) != 2:
            return jsonify({'error': 'Please select exactly 2 cryptocurrencies'}), 400
        
        # Define pricing (in pence - GBP)
        prices = {
            'v3': 299,   # £2.99
            'v6': 499,   # £4.99
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
                        'name': f'Trading Bot {plan_type.upper()} - 2 Coins',
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

# Settings routes
@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html')

@app.route('/settings/change-password', methods=['POST'])
@login_required
def change_password():
    current_password = request.form.get('current_password')
    new_password = request.form.get('new_password')
    confirm_password = request.form.get('confirm_password')
    
    # Verify current password
    if not check_password_hash(current_user.password_hash, current_password):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('settings'))
    
    # Check if new passwords match
    if new_password != confirm_password:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('settings'))
    
    # Validate new password length
    if len(new_password) < 6:
        flash('Password must be at least 6 characters long.', 'error')
        return redirect(url_for('settings'))
    
    try:
        # Update password
        current_user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        flash('Password updated successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash('An error occurred while updating your password.', 'error')
        logger.error(f"Password change error: {e}")
    
    return redirect(url_for('settings'))

@app.route('/settings/change-email', methods=['POST'])
@login_required
def change_email():
    password_for_email = request.form.get('password_for_email')
    new_email = request.form.get('new_email')
    
    # Verify password
    if not check_password_hash(current_user.password_hash, password_for_email):
        flash('Password is incorrect.', 'error')
        return redirect(url_for('settings'))
    
    # Check if email is already in use
    existing_user = User.query.filter_by(email=new_email).first()
    if existing_user and existing_user.id != current_user.id:
        flash('This email address is already in use.', 'error')
        return redirect(url_for('settings'))
    
    try:
        # Update email
        current_user.email = new_email
        db.session.commit()
        flash('Email address updated successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash('An error occurred while updating your email address.', 'error')
        logger.error(f"Email change error: {e}")
    
    return redirect(url_for('settings'))

@app.route('/settings/delete-account', methods=['POST'])
@login_required
def delete_account():
    password_for_deletion = request.form.get('password_for_deletion')
    confirm_deletion = request.form.get('confirm_deletion')
    
    # Verify password
    if not check_password_hash(current_user.password_hash, password_for_deletion):
        flash('Password is incorrect.', 'error')
        return redirect(url_for('settings'))
    
    # Check confirmation checkbox
    if not confirm_deletion:
        flash('You must confirm that you understand this action is permanent.', 'error')
        return redirect(url_for('settings'))
    
    try:
        user_id = current_user.id
        
        # Cancel any active subscriptions first
        active_subscription = current_user.get_active_subscription()
        if active_subscription and active_subscription.stripe_subscription_id:
            try:
                stripe.Subscription.delete(active_subscription.stripe_subscription_id)
            except Exception as stripe_error:
                logger.error(f"Error cancelling Stripe subscription: {stripe_error}")
        
        # Delete all user subscriptions
        Subscription.query.filter_by(user_id=user_id).delete()
        
        # Delete the user
        db.session.delete(current_user)
        db.session.commit()
        
        # Log out the user
        logout_user()
        
        flash('Your account has been successfully deleted.', 'success')
        return redirect(url_for('index'))
        
    except Exception as e:
        db.session.rollback()
        flash('An error occurred while deleting your account. Please try again.', 'error')
        logger.error(f"Account deletion error: {e}")
        return redirect(url_for('settings'))

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template('500.html'), 500

if __name__ == '__main__':
    try:
        # Database tables are already created by create_tables() function above
        app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        print(f"Error: {e}")
        raise
