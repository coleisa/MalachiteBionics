from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from sqlalchemy import inspect, text
import stripe
import os
import logging
import secrets
import json
import uuid
from dotenv import load_dotenv
from push_notifications import PushNotificationService

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Configure logging first
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration with fallbacks
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback-secret-key-for-development')

# Email configuration
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')  # Your business Gmail
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')  # Your Gmail app password
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME')

# Railway database configuration with improvements
database_url = os.environ.get('DATABASE_URL')
if database_url:
    # Fix Railway PostgreSQL URL format
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    logger.info(f"Using Railway PostgreSQL database")
else:
    # Fallback to SQLite for local development
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///trading_bot.db'
    logger.info("Using local SQLite database")

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
    'pool_timeout': 20,
    'pool_size': 10,
    'max_overflow': 20,
}

# Add PostgreSQL specific settings if using PostgreSQL
if 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI']:
    app.config['SQLALCHEMY_ENGINE_OPTIONS']['connect_args'] = {
        'options': '-csearch_path=public',
        'sslmode': 'require',
        'connect_timeout': 10,
    }

# Initialize extensions
db = SQLAlchemy(app)
mail = Mail(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'
login_manager.session_protection = 'basic'  # Changed from 'strong' to 'basic' to prevent logouts on refresh

# Initialize push notification service
push_service = PushNotificationService()
login_manager.remember_cookie_duration = timedelta(days=30)

# Configure Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET')

# User model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Add UUID for better session tracking
    uuid = db.Column(db.String(36), unique=True, nullable=True, default=None)
    email = db.Column(db.String(120), unique=True, nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    
    # Email verification fields
    email_verified = db.Column(db.Boolean, default=False, nullable=False)
    email_verification_token = db.Column(db.String(100), unique=True)
    email_verification_sent_at = db.Column(db.DateTime)
    
    # Phone number for SMS alerts (optional)
    phone = db.Column(db.String(20), nullable=True)
    
    # Session tracking for better persistence
    last_login = db.Column(db.DateTime)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    login_count = db.Column(db.Integer, default=0)
    
    # Admin role support
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    
    discord_user_id = db.Column(db.String(50), unique=True)
    discord_server_id = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    
    # Bot status fields for individual user bots
    bot_status = db.Column(db.String(20), default='offline')  # 'online', 'offline'
    bot_last_active = db.Column(db.DateTime)
    bot_activated_at = db.Column(db.DateTime)
    
    # Push notification subscription fields
    push_subscription_endpoint = db.Column(db.Text)
    push_subscription_p256dh = db.Column(db.String(200))
    push_subscription_auth = db.Column(db.String(100))
    push_notifications_enabled = db.Column(db.Boolean, default=False)
    
    # Subscription relationship
    subscriptions = db.relationship('Subscription', backref='user', lazy=True)
    
    def __init__(self, **kwargs):
        super(User, self).__init__(**kwargs)
        # Generate UUID if not provided
        if not self.uuid:
            self.uuid = str(uuid.uuid4())
    
    def get_id(self):
        """Override get_id to use UUID for better session persistence"""
        try:
            # Use UUID if available, otherwise fall back to regular ID
            if hasattr(self, 'uuid') and self.uuid:
                return str(self.uuid)
            # Generate UUID if missing
            if not hasattr(self, 'uuid') or not self.uuid:
                self.uuid = str(uuid.uuid4())
                try:
                    db.session.commit()
                except Exception as uuid_error:
                    logger.warning(f"Failed to save UUID for user {self.id}: {uuid_error}")
                    db.session.rollback()
                return str(self.uuid)
            return str(self.id)
        except Exception as e:
            logger.warning(f"Error in get_id for user {getattr(self, 'id', 'unknown')}: {e}")
            # Fallback to ID-based session
            return str(getattr(self, 'id', 'unknown'))
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def generate_verification_token(self):
        """Generate email verification token"""
        self.email_verification_token = secrets.token_urlsafe(32)
        self.email_verification_sent_at = datetime.utcnow()
        return self.email_verification_token
    
    def verify_email_token(self, token):
        """Verify email verification token"""
        if self.email_verification_token == token:
            # Check if token is not too old (24 hours)
            if self.email_verification_sent_at and \
               datetime.utcnow() - self.email_verification_sent_at < timedelta(hours=24):
                self.email_verified = True
                self.email_verification_token = None
                self.email_verification_sent_at = None
                return True
        return False
    
    def update_last_seen(self):
        """Update last seen timestamp"""
        self.last_seen = datetime.utcnow()
        # Don't commit here - let the calling function handle the commit
    
    def get_active_subscription(self):
        return Subscription.query.filter_by(
            user_id=self.id,
            status='active'
        ).first()
    
    def activate_bot(self):
        """Activate user's trading bot"""
        self.bot_status = 'online'
        self.bot_activated_at = datetime.utcnow()
        self.bot_last_active = datetime.utcnow()
        db.session.commit()
        return True
    
    def deactivate_bot(self):
        """Deactivate user's trading bot"""
        self.bot_status = 'offline'
        db.session.commit()
        return True
    
    def update_bot_activity(self):
        """Update bot last active timestamp"""
        if self.bot_status == 'online':
            self.bot_last_active = datetime.utcnow()
            db.session.commit()
    
    def get_bot_algorithm(self):
        """Get the algorithm type for this user"""
        if self.is_admin:
            return 'free'  # Admin gets free version
        
        subscription = self.get_active_subscription()
        if subscription and subscription.status == 'active':
            return subscription.plan_type
        
        return None  # No active subscription

# Subscription model
class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    stripe_subscription_id = db.Column(db.String(100), unique=True)
    stripe_customer_id = db.Column(db.String(100))
    plan_type = db.Column(db.String(20), nullable=False)  # v3, v6, v9, elite/v12
    coins = db.Column(db.Text)  # JSON string of selected coins
    status = db.Column(db.String(20), default='inactive')  # active, inactive, cancelled
    current_period_start = db.Column(db.DateTime)
    current_period_end = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# Trading Alert model for website-based alerts
class TradingAlert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    coin_pair = db.Column(db.String(20), nullable=False)  # e.g., BTC/USD, ETH/USD
    alert_type = db.Column(db.String(20), nullable=False)  # buy, sell, hold
    price = db.Column(db.Float, nullable=False)
    confidence = db.Column(db.Integer, default=85)  # Confidence percentage
    algorithm = db.Column(db.String(20), nullable=False)  # v3, v6, v9, elite/v12
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)  # When the alert expires
    
    # Relationship
    user = db.relationship('User', backref=db.backref('alerts', lazy=True))

@login_manager.user_loader
def load_user(user_uuid):
    """Load user by UUID instead of ID for better session persistence"""
    try:
        # First try by UUID (new method)
        user = User.query.filter_by(uuid=user_uuid).first()
        if user:
            return user
        
        # Fallback: try by ID in case UUID doesn't exist yet
        try:
            user_id = int(user_uuid)
            return User.query.get(user_id)
        except (ValueError, TypeError):
            pass
            
        return None
    except Exception as e:
        logger.error(f"Error loading user {user_uuid}: {e}")
        return None

# Initialize database tables
def create_tables():
    """Create database tables if they don't exist and handle schema migrations"""
    try:
        with app.app_context():
            logger.info("Starting database table creation/verification...")
            
            # Test database connection first
            try:
                db.session.execute(text('SELECT 1'))
                logger.info("Database connection successful")
            except Exception as conn_error:
                logger.error(f"Database connection failed: {conn_error}")
                # Create database file if it doesn't exist
                try:
                    db.create_all()
                    logger.info("Database created from scratch")
                except Exception as create_error:
                    logger.error(f"Failed to create database: {create_error}")
                    return False
            
            # Create all tables based on current models
            try:
                db.create_all()
                logger.info("Database tables created/updated successfully")
            except Exception as table_error:
                logger.error(f"Error creating tables: {table_error}")
                return False
            
            # Handle specific column migrations
            migrations_needed = []
            
            # Check for phone column in user table
            try:
                result = db.session.execute(text("SELECT phone FROM user LIMIT 1"))
                logger.info("Phone column exists in user table")
            except Exception as e:
                if "no such column" in str(e).lower() or "unknown column" in str(e).lower():
                    migrations_needed.append(("user", "phone", "VARCHAR(20)"))
                    logger.info("Phone column missing - will be added")
                else:
                    logger.warning(f"Error checking phone column: {e}")
            
            # Check for uuid column in user table
            try:
                result = db.session.execute(text("SELECT uuid FROM user LIMIT 1"))
                logger.info("UUID column exists in user table")
            except Exception as e:
                if "no such column" in str(e).lower() or "unknown column" in str(e).lower():
                    migrations_needed.append(("user", "uuid", "VARCHAR(36)"))
                    logger.info("UUID column missing - will be added")
                else:
                    logger.warning(f"Error checking uuid column: {e}")
            
            # Apply migrations
            for table, column, column_type in migrations_needed:
                try:
                    alter_sql = f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                    db.session.execute(text(alter_sql))
                    db.session.commit()
                    logger.info(f"Successfully added {column} column to {table} table")
                except Exception as alter_error:
                    logger.error(f"Failed to add {column} column to {table}: {alter_error}")
                    db.session.rollback()
                    # Don't fail completely, just log the error
            
            # Verify critical tables exist
            from sqlalchemy import inspect
            try:
                inspector = inspect(db.engine)
                tables = inspector.get_table_names()
                logger.info(f"Available database tables: {tables}")
                
                required_tables = ['user', 'subscription', 'trading_alert']
                missing_tables = [t for t in required_tables if t not in tables]
                
                if missing_tables:
                    logger.error(f"Missing required tables: {missing_tables}")
                    # Force recreation of all tables
                    logger.info("Recreating all database tables...")
                    db.drop_all()
                    db.create_all()
                    logger.info("Database tables recreated successfully")
                else:
                    logger.info("All required tables are present")
                    
            except Exception as inspect_error:
                logger.error(f"Error inspecting database: {inspect_error}")
                # Try to recreate tables as fallback
                try:
                    db.create_all()
                    logger.info("Fallback table creation completed")
                except Exception as fallback_error:
                    logger.error(f"Fallback table creation failed: {fallback_error}")
                    return False
            
            logger.info("Database initialization completed successfully")
            return True
            
    except Exception as e:
        logger.error(f"Critical error in create_tables: {e}")
        logger.error(f"Error type: {type(e).__name__}")
        # Log the full traceback for debugging
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        return False

# Create tables immediately but safely
try:
    create_tables()
except Exception as e:
    logger.error(f"Initial table creation failed: {e}")

# Email functions
def send_verification_email(user):
    """Send email verification email"""
    try:
        token = user.generate_verification_token()
        db.session.commit()
        
        verification_url = url_for('verify_email', token=token, _external=True)
        
        msg = Message(
            'Verify Your Email - Trading Bot',
            recipients=[user.email]
        )
        
        msg.html = f"""
        <h2>Welcome to Trading Bot!</h2>
        <p>Hi {user.display_name},</p>
        <p>Thank you for signing up! Please verify your email address by clicking the link below:</p>
        <p><a href="{verification_url}" style="background-color: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Verify Email</a></p>
        <p>If the button doesn't work, copy and paste this link into your browser:</p>
        <p>{verification_url}</p>
        <p>This link will expire in 24 hours.</p>
        <br>
        <p>Best regards,<br>Trading Bot Team</p>
        """
        
        mail.send(msg)
        logger.info(f"Verification email sent to {user.email}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send verification email to {user.email}: {e}")
        return False

def send_welcome_email(user):
    """Send welcome email after user verifies their account"""
    try:
        # Skip welcome email for admin account
        if user.email.lower() == 'malachitebionics@gmail.com':
            logger.info(f"Skipping welcome email for admin account: {user.email}")
            return True
        
        msg = Message(
            'Welcome to MalachiteBionics - Your Trading Journey Begins!',
            recipients=[user.email]
        )
        
        msg.html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f8f9fa;">
            <div style="background: white; border-radius: 15px; padding: 30px; box-shadow: 0 10px 30px rgba(0,0,0,0.1);">
                <div style="text-align: center; margin-bottom: 30px;">
                    <h1 style="color: #000; font-size: 28px; margin-bottom: 10px;">🎉 Welcome to MalachiteBionics!</h1>
                    <p style="color: #666; font-size: 16px; margin: 0;">Your professional crypto trading journey starts now</p>
                </div>
                
                <div style="background: linear-gradient(135deg, #f8f9fa, #e9ecef); border-radius: 10px; padding: 20px; margin-bottom: 25px;">
                    <h2 style="color: #000; font-size: 20px; margin-bottom: 15px;">Hi {user.display_name}!</h2>
                    <p style="color: #555; line-height: 1.6; margin-bottom: 15px;">
                        Congratulations on joining thousands of successful traders who trust MalachiteBionics for professional cryptocurrency trading signals!
                    </p>
                    <p style="color: #555; line-height: 1.6; margin: 0;">
                        Your email has been verified and your account is ready to use. 🚀
                    </p>
                </div>
                
                <div style="margin-bottom: 25px;">
                    <h3 style="color: #000; font-size: 18px; margin-bottom: 15px;">🎯 What's Next?</h3>
                    <div style="background: white; border-left: 4px solid #22c55e; padding: 15px; margin-bottom: 15px;">
                        <h4 style="color: #22c55e; font-size: 16px; margin-bottom: 8px;">1. Choose Your Algorithm</h4>
                        <p style="color: #666; margin: 0; font-size: 14px;">Select from Basic, Classic, Advanced, or Elite algorithm packages</p>
                    </div>
                    <div style="background: white; border-left: 4px solid #3b82f6; padding: 15px; margin-bottom: 15px;">
                        <h4 style="color: #3b82f6; font-size: 16px; margin-bottom: 8px;">2. Pick Your Cryptocurrencies</h4>
                        <p style="color: #666; margin: 0; font-size: 14px;">Choose 2 cryptocurrency pairs for monitoring and signals</p>
                    </div>
                    <div style="background: white; border-left: 4px solid #8b5cf6; padding: 15px;">
                        <h4 style="color: #8b5cf6; font-size: 16px; margin-bottom: 8px;">3. Start Trading</h4>
                        <p style="color: #666; margin: 0; font-size: 14px;">Receive real-time signals via Discord and maximize your profits</p>
                    </div>
                </div>
                
                <div style="text-align: center; margin-bottom: 25px;">
                    <a href="{url_for('pricing', _external=True)}" style="background: linear-gradient(135deg, #000, #333); color: white; padding: 15px 30px; text-decoration: none; border-radius: 25px; font-weight: bold; font-size: 16px; display: inline-block;">
                        🚀 Choose Your Plan
                    </a>
                </div>
                
                <div style="background: #f8f9fa; border-radius: 10px; padding: 20px; margin-bottom: 20px;">
                    <h3 style="color: #000; font-size: 16px; margin-bottom: 10px;">📊 Our Track Record</h3>
                    <ul style="color: #666; padding-left: 20px; margin: 0;">
                        <li>85%+ accuracy on trading signals</li>
                        <li>24/7 automated market monitoring</li>
                        <li>Real-time Discord integration</li>
                        <li>Professional risk management</li>
                    </ul>
                </div>
                
                <div style="text-align: center; border-top: 1px solid #e9ecef; padding-top: 20px;">
                    <p style="color: #666; font-size: 14px; margin-bottom: 10px;">Need help getting started?</p>
                    <a href="{url_for('help_page', _external=True)}" style="color: #3b82f6; text-decoration: none;">Visit our Help Center</a>
                    <p style="color: #999; font-size: 12px; margin-top: 15px;">
                        Best regards,<br>
                        The MalachiteBionics Team
                    </p>
                </div>
            </div>
        </div>
        """
        
        mail.send(msg)
        logger.info(f"Welcome email sent to {user.email}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send welcome email to {user.email}: {e}")
        return False

# Admin decorator and utilities
from functools import wraps

def admin_required(f):
    """Decorator to require admin access"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        if not current_user.is_admin:
            flash('Access denied. Admin privileges required.', 'error')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# Custom Jinja filters
@app.template_filter('from_json')
def from_json_filter(json_str):
    """Convert JSON string to Python object"""
    try:
        return json.loads(json_str) if json_str else []
    except (json.JSONDecodeError, TypeError):
        return []

# Routes
@app.route('/')
def index():
    return render_template('index.html', stripe_publishable_key=STRIPE_PUBLISHABLE_KEY)

@app.route('/health')
def health_check():
    """Health check endpoint for Railway"""
    try:
        # Test database connection
        db.session.execute(text('SELECT 1'))
        
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

@app.route('/test-db')
def test_database():
    """Simple database test endpoint"""
    try:
        # Test basic database connection
        db.session.execute(text('SELECT 1'))
        
        # Test table existence
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        
        # Test user table structure
        user_columns = []
        if 'user' in tables:
            user_columns = [col['name'] for col in inspector.get_columns('user')]
        
        # Test creating a simple query
        user_count = User.query.count()
        
        return jsonify({
            'status': 'Database working',
            'connection': 'OK',
            'tables': tables,
            'user_table_exists': 'user' in tables,
            'user_columns': user_columns,
            'user_count': user_count,
            'database_type': 'postgresql' if 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI'] else 'sqlite',
            'has_database_url': 'DATABASE_URL' in os.environ
        })
        
    except Exception as e:
        logger.error(f"Database test failed: {e}")
        return jsonify({
            'status': 'Database error',
            'error': str(e),
            'database_type': 'postgresql' if 'postgresql' in app.config['SQLALCHEMY_DATABASE_URI'] else 'sqlite',
            'has_database_url': 'DATABASE_URL' in os.environ
        }), 500

@app.route('/debug-db')
def debug_database():
    """Debug endpoint to check database state - REMOVE IN PRODUCTION"""
    try:
        # Simple database check
        total_users = User.query.count()
        verified_users = User.query.filter_by(email_verified=True).count()
        unverified_users = total_users - verified_users
        
        # Check database type
        db_url = app.config['SQLALCHEMY_DATABASE_URI']
        db_type = 'postgresql' if 'postgresql' in db_url else 'sqlite'
        
        # Get sample user data (first 5 users for debugging)
        sample_users = User.query.limit(5).all()
        user_samples = []
        for user in sample_users:
            user_samples.append({
                'id': user.id,
                'uuid': user.uuid,
                'email': user.email,
                'display_name': user.display_name,
                'email_verified': user.email_verified,
                'last_login': user.last_login.strftime('%Y-%m-%d %H:%M:%S') if user.last_login else 'Never',
                'login_count': user.login_count,
                'created_at': user.created_at.strftime('%Y-%m-%d %H:%M:%S')
            })
        
        return jsonify({
            'database_type': db_type,
            'total_users': total_users,
            'verified_users': verified_users,
            'unverified_users': unverified_users,
            'railway_database_url_env': 'DATABASE_URL' in os.environ,
            'mail_configured': bool(app.config.get('MAIL_USERNAME')),
            'sample_users': user_samples,
            'status': 'ok'
        })
        
    except Exception as e:
        return jsonify({'error': str(e), 'database_url_present': 'DATABASE_URL' in os.environ}), 500

@app.route('/setup-database')
def setup_database():
    """Simple database setup endpoint - creates tables and admin account"""
    try:
        with app.app_context():
            # Drop all tables first (fresh start)
            db.drop_all()
            
            # Create all tables
            db.create_all()
            
            # Verify tables were created
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            tables = inspector.get_table_names()
            
            return jsonify({
                'success': True,
                'message': 'Database setup complete! You can now register accounts.',
                'tables_created': tables,
                'next_step': 'Go to /register and create your admin account with malachitebionics@gmail.com'
            })
            
    except Exception as e:
        logger.error(f"Database setup error: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'message': 'Database setup failed'
        }), 500

@app.route('/system-status')
def system_status():
    """Comprehensive system status check for login troubleshooting"""
    status_checks = []
    overall_status = "healthy"
    
    try:
        # Database connection test
        try:
            db.session.execute(text('SELECT 1'))
            status_checks.append({"component": "Database Connection", "status": "✅ OK", "details": "Connection successful"})
        except Exception as db_error:
            status_checks.append({"component": "Database Connection", "status": "❌ FAILED", "details": str(db_error)})
            overall_status = "critical"
        
        # User table structure test
        try:
            result = db.session.execute(text("SELECT id, email, password_hash FROM user LIMIT 1"))
            status_checks.append({"component": "User Table Structure", "status": "✅ OK", "details": "Core columns accessible"})
        except Exception as table_error:
            status_checks.append({"component": "User Table Structure", "status": "❌ FAILED", "details": str(table_error)})
            overall_status = "critical"
        
        # Phone column test
        try:
            db.session.execute(text("SELECT phone FROM user LIMIT 1"))
            status_checks.append({"component": "Phone Column", "status": "✅ OK", "details": "Column exists"})
        except Exception as phone_error:
            if "no such column" in str(phone_error).lower():
                status_checks.append({"component": "Phone Column", "status": "⚠️ MISSING", "details": "Phone column needs migration"})
                overall_status = "warning" if overall_status == "healthy" else overall_status
            else:
                status_checks.append({"component": "Phone Column", "status": "❌ ERROR", "details": str(phone_error)})
        
        # UUID column test
        try:
            db.session.execute(text("SELECT uuid FROM user LIMIT 1"))
            status_checks.append({"component": "UUID Column", "status": "✅ OK", "details": "Column exists"})
        except Exception as uuid_error:
            if "no such column" in str(uuid_error).lower():
                status_checks.append({"component": "UUID Column", "status": "⚠️ MISSING", "details": "UUID column needs migration"})
                overall_status = "warning" if overall_status == "healthy" else overall_status
            else:
                status_checks.append({"component": "UUID Column", "status": "❌ ERROR", "details": str(uuid_error)})
        
        # User count test
        try:
            user_count = User.query.count()
            status_checks.append({"component": "User Data", "status": "✅ OK", "details": f"{user_count} users in database"})
        except Exception as count_error:
            status_checks.append({"component": "User Data", "status": "❌ FAILED", "details": str(count_error)})
            overall_status = "critical"
        
        # Flask-Login configuration
        login_manager_configured = hasattr(app, 'login_manager') and app.login_manager is not None
        if login_manager_configured:
            status_checks.append({"component": "Flask-Login", "status": "✅ OK", "details": "Login manager configured"})
        else:
            status_checks.append({"component": "Flask-Login", "status": "❌ FAILED", "details": "Login manager not configured"})
            overall_status = "critical"
        
        # Generate HTML report
        status_color = {
            "healthy": "#28a745",
            "warning": "#ffc107", 
            "critical": "#dc3545"
        }
        
        html_report = f"""
        <html>
        <head>
            <title>System Status Report</title>
            <meta http-equiv="refresh" content="30">
        </head>
        <body style="font-family: Arial, sans-serif; margin: 40px; background-color: #f8f9fa;">
            <div style="max-width: 800px; margin: 0 auto;">
                <h1 style="color: {status_color[overall_status]};">🔍 System Status Report</h1>
                <div style="background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px;">
                    <h2 style="margin-top: 0;">Overall Status: <span style="color: {status_color[overall_status]};">{overall_status.upper()}</span></h2>
                    <p style="color: #666;">Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</p>
                </div>
                
                <div style="background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                    <h3>Component Status:</h3>
                    <table style="width: 100%; border-collapse: collapse;">
                        <thead>
                            <tr style="background-color: #f8f9fa;">
                                <th style="text-align: left; padding: 12px; border-bottom: 1px solid #dee2e6;">Component</th>
                                <th style="text-align: left; padding: 12px; border-bottom: 1px solid #dee2e6;">Status</th>
                                <th style="text-align: left; padding: 12px; border-bottom: 1px solid #dee2e6;">Details</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(f'''
                            <tr>
                                <td style="padding: 12px; border-bottom: 1px solid #dee2e6;">{check["component"]}</td>
                                <td style="padding: 12px; border-bottom: 1px solid #dee2e6;">{check["status"]}</td>
                                <td style="padding: 12px; border-bottom: 1px solid #dee2e6; font-family: monospace; font-size: 12px;">{check["details"]}</td>
                            </tr>
                            ''' for check in status_checks)}
                        </tbody>
                    </table>
                </div>
                
                <div style="background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-top: 20px;">
                    <h3>Quick Actions:</h3>
                    <p>
                        <a href="/admin/fix-database" style="background: #007bff; color: white; padding: 10px 15px; text-decoration: none; border-radius: 4px; margin-right: 10px;">🔧 Fix Database</a>
                        <a href="/login" style="background: #28a745; color: white; padding: 10px 15px; text-decoration: none; border-radius: 4px; margin-right: 10px;">🔐 Try Login</a>
                        <a href="/emergency-db-reset" style="background: #dc3545; color: white; padding: 10px 15px; text-decoration: none; border-radius: 4px;" onclick="return confirm('This will delete ALL data! Are you sure?')">⚠️ Emergency Reset</a>
                    </p>
                </div>
                
                <div style="background: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 4px; margin-top: 20px;">
                    <h4 style="margin-top: 0; color: #856404;">💡 Troubleshooting Tips:</h4>
                    <ul style="color: #856404;">
                        <li>If you see missing column errors, use the "Fix Database" button</li>
                        <li>For persistent login issues, check the system logs</li>
                        <li>Emergency reset should only be used as a last resort</li>
                        <li>This page auto-refreshes every 30 seconds</li>
                    </ul>
                </div>
            </div>
        </body>
        </html>
        """
        
        return html_report
        
    except Exception as e:
        logger.error(f"System status check failed: {e}")
        return f"""
        <html>
        <body style="font-family: Arial, sans-serif; margin: 40px;">
            <h1 style="color: red;">❌ System Status Check Failed</h1>
            <p><strong>Error:</strong> {e}</p>
            <p><a href="/login">Return to Login</a></p>
        </body>
        </html>
        """@app.route('/init-db')
def init_database():
    """Manual database initialization endpoint"""
    try:
        with app.app_context():
            # Force recreate tables
            db.create_all()
            
            info = []
            
            # Make malachitebionics@gmail.com the admin user
            admin_user = User.query.filter_by(email='malachitebionics@gmail.com').first()
            if admin_user:
                admin_user.is_admin = True
                admin_user.email_verified = True  # Force verify admin
                admin_user.email_verification_token = None  # Clear any pending verification
                info.append(f"Made {admin_user.email} an admin user (auto-verified)")
            else:
                info.append("malachitebionics@gmail.com not found - please register this account first")
            
            # Also make any existing admin accounts verified
            existing_admins = User.query.filter_by(is_admin=True).all()
            for admin in existing_admins:
                admin.email_verified = True
                admin.email_verification_token = None
                if admin.email != 'malachitebionics@gmail.com':
                    info.append(f"Auto-verified admin: {admin.email}")
            
            db.session.commit()
            
            user_count = User.query.count()
            admin_count = User.query.filter_by(is_admin=True).count()
            verified_count = User.query.filter_by(email_verified=True).count()
            
            return jsonify({
                'success': True, 
                'message': 'Database initialized successfully',
                'total_users': user_count,
                'admin_users': admin_count,
                'verified_users': verified_count,
                'info': info
            })
            
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        return jsonify({
            'success': False, 
            'error': str(e),
            'message': 'Database initialization failed - check logs for details'
        }), 500

@app.route('/make-admin')
def make_admin():
    """Emergency route to make malachitebionics@gmail.com admin without email verification"""
    try:
        admin_user = User.query.filter_by(email='malachitebionics@gmail.com').first()
        if admin_user:
            admin_user.is_admin = True
            admin_user.email_verified = True
            admin_user.email_verification_token = None
            db.session.commit()
            return jsonify({
                'success': True,
                'message': f'{admin_user.email} is now admin and verified'
            })
        else:
            # Create the admin user if it doesn't exist
            try:
                admin = User(
                    email='malachitebionics@gmail.com',
                    display_name='MalachiteBionics Admin',
                    is_admin=True,
                    email_verified=True,
                    is_active=True,
                    bot_status='offline'
                )
                admin.set_password('admin123')  # Default password
                db.session.add(admin)
                db.session.commit()
                return jsonify({
                    'success': True,
                    'message': 'Admin account created: malachitebionics@gmail.com (password: admin123)',
                    'note': 'Please change password after first login'
                })
            except Exception as create_error:
                logger.error(f"Error creating admin user: {create_error}")
                return jsonify({
                    'success': False,
                    'message': f'Failed to create admin user: {str(create_error)}'
                })
    except Exception as e:
        logger.error(f"Make admin error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/pricing')
def pricing():
    try:
        stripe_key = STRIPE_PUBLISHABLE_KEY or 'pk_test_fallback'
        return render_template('pricing.html', stripe_publishable_key=stripe_key)
    except Exception as e:
        logger.error(f"Pricing page error: {e}")
        return render_template('pricing.html', stripe_publishable_key='pk_test_fallback')

@app.route('/help')
def help_page():
    """Help page with contact information"""
    business_email = app.config['MAIL_USERNAME'] or 'support@tradingbot.com'
    return render_template('help.html', business_email=business_email)

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
            # Test database connection first
            db.session.execute(text('SELECT 1'))
            
            existing_user = User.query.filter_by(email=email).first()
            if existing_user:
                if existing_user.email_verified:
                    flash('Email already registered. Please log in.', 'error')
                else:
                    flash('Email already registered but not verified. Please check your email or request a new verification.', 'warning')
                return render_template('register.html')
        except Exception as e:
            logger.error(f"Database connection error during registration check: {e}")
            logger.error(f"Error type: {type(e).__name__}")
            logger.error(f"Full error details: {repr(e)}")
            
            # Provide more helpful error message based on the error type
            error_str = str(e).lower()
            if 'connection' in error_str or 'timeout' in error_str or 'connect' in error_str:
                flash('Database connection issue. Please try again in a few moments.', 'error')
            elif 'table' in error_str or 'column' in error_str or 'relation' in error_str:
                flash('Database needs initialization. Please contact support.', 'error')
            elif 'ssl' in error_str or 'certificate' in error_str:
                flash('Database SSL connection issue. Please contact support.', 'error')
            elif 'password' in error_str or 'authentication' in error_str or 'auth' in error_str:
                flash('Database authentication issue. Please contact support.', 'error')
            elif type(e).__name__ == 'ArgumentError':
                flash('Database schema issue. Please visit /init-db to initialize the database.', 'error')
            else:
                # Show the actual error type for debugging
                flash(f'Database error ({type(e).__name__}). Please contact support if this persists.', 'error')
            return render_template('register.html')
        
        # Create new user
        try:
            # Test database connection again before creating user
            db.session.execute(text('SELECT 1'))
            
            user = User(email=email, display_name=display_name)
            user.set_password(password)
            
            # Special handling for admin email - auto-verify and make admin
            if email.lower() == 'malachitebionics@gmail.com':
                user.email_verified = True
                user.is_admin = True
                logger.info(f"Auto-verified and granted admin privileges to {email}")
            
            db.session.add(user)
            db.session.flush()  # Get the user ID without committing yet
            
            # Send verification email (skip for admin email to avoid issues)
            if email.lower() == 'malachitebionics@gmail.com':
                db.session.commit()
                flash('Registration successful! Admin account created and verified.', 'success')
            else:
                # Try to send verification email before committing
                email_sent = send_verification_email(user)
                db.session.commit()
                
                if email_sent:
                    flash('Registration successful! Please check your email and click the verification link before logging in.', 'success')
                else:
                    flash('Registration successful, but we could not send the verification email. Please contact support.', 'warning')
            
            return redirect(url_for('login'))
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Registration error for {email}: {e}")
            logger.error(f"Error type: {type(e).__name__}")
            logger.error(f"Full error details: {repr(e)}")
            
            # Provide more specific error messages based on error type
            error_str = str(e).lower()
            if 'unique constraint' in error_str or 'duplicate key' in error_str:
                flash('This email address is already registered. Please try logging in instead.', 'error')
            elif 'connection' in error_str or 'timeout' in error_str or 'connect' in error_str:
                flash('Database connection issue. Please try again in a few moments.', 'error')
            elif 'table' in error_str or 'column' in error_str or 'relation' in error_str:
                flash('Database needs initialization. Please contact support.', 'error')
            elif 'ssl' in error_str or 'certificate' in error_str:
                flash('Database SSL connection issue. Please contact support.', 'error')
            elif 'password' in error_str or 'authentication' in error_str or 'auth' in error_str:
                flash('Database authentication issue. Please contact support.', 'error')
            elif type(e).__name__ == 'ArgumentError':
                flash('Database schema issue. Please visit /init-db to initialize the database.', 'error')
            elif 'mail' in error_str or 'smtp' in error_str:
                flash('Registration successful, but email verification could not be sent. Please contact support.', 'warning')
                # Still redirect to login since user was created
                return redirect(url_for('login'))
            else:
                # Show the actual error type for debugging
                flash(f'Registration failed ({type(e).__name__}). Please contact support if this persists.', 'error')
            return render_template('register.html')
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        remember = request.form.get('remember')  # Get remember checkbox value
        
        if not email or not password:
            flash('Email and password are required.', 'error')
            return render_template('login.html')
        
        try:
            # First, test database connection and schema
            try:
                db.session.execute(text('SELECT 1'))
                # Test if User table has all required columns
                db.session.execute(text('SELECT id, email, password_hash FROM user LIMIT 1'))
            except Exception as db_error:
                logger.error(f"Database connection/schema error: {db_error}")
                # Try to fix schema issues automatically
                try:
                    create_tables()  # This will handle missing columns
                    logger.info("Database schema updated during login")
                except Exception as fix_error:
                    logger.error(f"Failed to fix database schema: {fix_error}")
                    flash('Database is being updated. Please try again in a moment.', 'warning')
                    return render_template('login.html')
            
            user = User.query.filter_by(email=email).first()
            
            if user and user.check_password(password):
                # Check email verification (skip for admin to avoid issues)
                if not user.is_admin and hasattr(user, 'email_verified') and not user.email_verified:
                    flash('Please verify your email before logging in. Check your inbox for the verification link.', 'warning')
                    return render_template('login.html')
                
                # Update user login tracking
                try:
                    user.last_login = datetime.utcnow()
                    user.login_count = (user.login_count or 0) + 1
                    user.update_last_seen()
                    db.session.commit()
                except Exception as update_error:
                    logger.warning(f"Failed to update login tracking: {update_error}")
                    # Don't fail login for tracking issues
                    db.session.rollback()
                
                # Use remember checkbox value, default to extended session
                remember_user = bool(remember) if remember else True
                login_user(user, remember=remember_user, duration=timedelta(days=30))
                logger.info(f"User {user.email} logged in successfully")
                
                next_page = request.args.get('next')
                return redirect(next_page) if next_page else redirect(url_for('dashboard'))
            else:
                flash('Invalid email or password.', 'error')
                logger.warning(f"Failed login attempt for email: {email}")
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Login error details: {error_msg}")
            logger.error(f"Login error type: {type(e).__name__}")
            
            # Comprehensive error handling with automatic recovery
            if "no such column" in error_msg.lower() or "unknown column" in error_msg.lower():
                logger.error("Database schema issue detected - attempting automatic fix")
                try:
                    # Force database schema update
                    create_tables()
                    flash('Database schema updated. Please try logging in again.', 'info')
                except Exception as schema_fix_error:
                    logger.error(f"Schema fix failed: {schema_fix_error}")
                    flash('Database schema issue detected. Please contact support.', 'error')
            elif "database is locked" in error_msg.lower():
                flash('Database is temporarily busy. Please try again in a few seconds.', 'warning')
            elif "connection" in error_msg.lower() or "operational error" in error_msg.lower():
                flash('Database connection issue. Please try again.', 'warning')
            elif "integrity error" in error_msg.lower():
                flash('Data integrity issue. Please try again or contact support.', 'error')
            else:
                # Generic error with more helpful message
                flash('Login system is experiencing issues. Please try again or contact support if the problem persists.', 'error')
                logger.error(f"Unhandled login error: {error_msg}")
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/verify-email/<token>')
def verify_email(token):
    """Email verification endpoint"""
    user = User.query.filter_by(email_verification_token=token).first()
    
    if not user:
        flash('Invalid or expired verification link.', 'error')
        return redirect(url_for('login'))
    
    if user.verify_email_token(token):
        db.session.commit()
        
        # Send welcome email after successful verification (except for admin)
        welcome_sent = send_welcome_email(user)
        
        if user.email.lower() == 'malachitebionics@gmail.com':
            flash('Email verified successfully! Admin account is ready.', 'success')
        else:
            if welcome_sent:
                flash('Email verified successfully! Check your inbox for a welcome message with next steps.', 'success')
            else:
                flash('Email verified successfully! You can now log in and start trading.', 'success')
        
        logger.info(f"Email verified for user: {user.email}")
    else:
        flash('Invalid or expired verification link.', 'error')
    
    return redirect(url_for('login'))

@app.route('/resend-verification', methods=['GET', 'POST'])
def resend_verification():
    """Resend email verification"""
    if request.method == 'POST':
        email = request.form.get('email')
        
        if not email:
            flash('Email is required.', 'error')
            return render_template('resend_verification.html')
        
        user = User.query.filter_by(email=email).first()
        
        if not user:
            flash('No account found with that email address.', 'error')
            return render_template('resend_verification.html')
        
        if user.email_verified:
            flash('Email is already verified. You can log in.', 'info')
            return redirect(url_for('login'))
        
        # Check if we recently sent a verification email (rate limiting)
        if user.email_verification_sent_at and \
           datetime.utcnow() - user.email_verification_sent_at < timedelta(minutes=5):
            flash('Verification email was sent recently. Please wait 5 minutes before requesting another.', 'warning')
            return render_template('resend_verification.html')
        
        if send_verification_email(user):
            flash('Verification email sent! Please check your inbox.', 'success')
        else:
            flash('Failed to send verification email. Please try again later.', 'error')
        
        return render_template('resend_verification.html')
    
    return render_template('resend_verification.html')

@app.route('/dashboard')
@login_required
def dashboard():
    subscription = current_user.get_active_subscription()
    
    # Get recent alerts for the user (last 24 hours)
    recent_alerts = TradingAlert.query.filter_by(user_id=current_user.id)\
        .filter(TradingAlert.created_at >= datetime.utcnow() - timedelta(hours=24))\
        .order_by(TradingAlert.created_at.desc())\
        .limit(10).all()
    
    # Get unread alert count
    unread_count = TradingAlert.query.filter_by(user_id=current_user.id, is_read=False).count()
    
    return render_template('dashboard.html', 
                         user=current_user, 
                         subscription=subscription,
                         recent_alerts=recent_alerts,
                         unread_count=unread_count)

@app.route('/bot/activate', methods=['POST'])
@login_required
def activate_bot():
    """Activate user's trading bot (Admin only)"""
    try:
        # Only admins can control bots
        if not current_user.is_admin:
            flash('Access denied. Only administrators can control trading bots.', 'error')
            return redirect(url_for('dashboard'))
        
        if current_user.activate_bot():
            flash('Admin trading bot activated successfully! 🤖', 'success')
        else:
            flash('Failed to activate trading bot.', 'error')
            
    except Exception as e:
        logger.error(f"Bot activation error for user {current_user.id}: {e}")
        flash('Error activating trading bot.', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/bot/deactivate', methods=['POST'])
@login_required  
def deactivate_bot():
    """Deactivate user's trading bot (Admin only)"""
    try:
        # Only admins can control bots
        if not current_user.is_admin:
            flash('Access denied. Only administrators can control trading bots.', 'error')
            return redirect(url_for('dashboard'))
        
        if current_user.deactivate_bot():
            flash('Admin trading bot deactivated successfully.', 'info')
        else:
            flash('Failed to deactivate trading bot.', 'error')
            
    except Exception as e:
        logger.error(f"Bot deactivation error for user {current_user.id}: {e}")
        flash('Error deactivating trading bot.', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/admin/bot/activate/<int:user_id>', methods=['POST'])
@admin_required
def admin_activate_user_bot(user_id):
    """Admin activates a customer's trading bot"""
    try:
        user = User.query.get_or_404(user_id)
        
        # Check if user has active subscription or is admin
        if not user.is_admin and not user.get_active_subscription():
            flash(f'User {user.email} needs an active subscription to activate their bot.', 'error')
            return redirect(url_for('admin_bot_status'))
        
        if user.activate_bot():
            flash(f'Successfully activated trading bot for {user.email}! 🤖', 'success')
        else:
            flash(f'Failed to activate trading bot for {user.email}.', 'error')
            
    except Exception as e:
        logger.error(f"Admin bot activation error for user {user_id}: {e}")
        flash('Error activating user trading bot.', 'error')
    
    return redirect(url_for('admin_bot_status'))

@app.route('/admin/bot/deactivate/<int:user_id>', methods=['POST'])
@admin_required
def admin_deactivate_user_bot(user_id):
    """Admin deactivates a customer's trading bot"""
    try:
        user = User.query.get_or_404(user_id)
        
        if user.deactivate_bot():
            flash(f'Successfully deactivated trading bot for {user.email}.', 'info')
        else:
            flash(f'Failed to deactivate trading bot for {user.email}.', 'error')
            
    except Exception as e:
        logger.error(f"Admin bot deactivation error for user {user_id}: {e}")
        flash('Error deactivating user trading bot.', 'error')
    
    return redirect(url_for('admin_bot_status'))

@app.route('/alerts')
@login_required
def view_alerts():
    """View all trading alerts for the current user"""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    alerts = TradingAlert.query.filter_by(user_id=current_user.id)\
        .order_by(TradingAlert.created_at.desc())\
        .paginate(page=page, per_page=per_page, error_out=False)
    
    return render_template('alerts.html', alerts=alerts)

@app.route('/alerts/mark-read/<int:alert_id>', methods=['POST'])
@login_required
def mark_alert_read(alert_id):
    """Mark a specific alert as read"""
    alert = TradingAlert.query.filter_by(id=alert_id, user_id=current_user.id).first_or_404()
    alert.is_read = True
    db.session.commit()
    return jsonify({'success': True})

@app.route('/alerts/mark-all-read', methods=['POST'])
@login_required
def mark_all_alerts_read():
    """Mark all alerts as read for the current user"""
    TradingAlert.query.filter_by(user_id=current_user.id, is_read=False)\
        .update({'is_read': True})
    db.session.commit()
    flash('All alerts marked as read.', 'success')
    return redirect(url_for('view_alerts'))

@app.route('/api/alerts/recent')
@login_required
def get_recent_alerts():
    """API endpoint to get recent alerts (for live updates)"""
    alerts = TradingAlert.query.filter_by(user_id=current_user.id)\
        .filter(TradingAlert.created_at >= datetime.utcnow() - timedelta(hours=1))\
        .order_by(TradingAlert.created_at.desc())\
        .limit(5).all()
    
    alert_data = []
    for alert in alerts:
        alert_data.append({
            'id': alert.id,
            'coin_pair': alert.coin_pair,
            'alert_type': alert.alert_type,
            'price': alert.price,
            'confidence': alert.confidence,
            'algorithm': alert.algorithm,
            'message': alert.message,
            'is_read': alert.is_read,
            'created_at': alert.created_at.isoformat(),
            'time_ago': get_time_ago(alert.created_at)
        })
    
    unread_count = TradingAlert.query.filter_by(user_id=current_user.id, is_read=False).count()
    
    return jsonify({
        'alerts': alert_data,
        'unread_count': unread_count
    })

def get_time_ago(timestamp):
    """Helper function to get human-readable time difference"""
    now = datetime.utcnow()
    diff = now - timestamp
    
    if diff.seconds < 60:
        return "Just now"
    elif diff.seconds < 3600:
        minutes = diff.seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif diff.seconds < 86400:
        hours = diff.seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        days = diff.days
        return f"{days} day{'s' if days != 1 else ''} ago"

@app.route('/subscribe/<plan_type>')
@login_required
def subscribe(plan_type):
    if plan_type not in ['v3', 'v6', 'v9', 'elite']:
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
        
        if not plan_type or plan_type not in ['v3', 'v6', 'v9', 'elite']:
            return jsonify({'error': 'Invalid plan type'}), 400
        
        if not coins:
            return jsonify({'error': 'Please select exactly 2 cryptocurrencies'}), 400
        
        if len(coins) != 2:
            return jsonify({'error': 'Please select exactly 2 cryptocurrencies'}), 400
        
        # Define pricing (in pence - GBP)
        prices = {
            'v3': 299,    # £2.99
            'v6': 499,    # £4.99
            'v9': 799,    # £7.99
            'elite': 1499 # £14.99
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

@app.route('/settings/change-display-name', methods=['POST'])
@login_required
def change_display_name():
    """Change user's display name"""
    new_display_name = request.form.get('new_display_name')
    
    # Validation
    if not new_display_name:
        flash('Display name is required.', 'error')
        return redirect(url_for('settings'))
    
    if len(new_display_name) < 2:
        flash('Display name must be at least 2 characters long.', 'error')
        return redirect(url_for('settings'))
    
    if len(new_display_name) > 50:
        flash('Display name must be less than 50 characters.', 'error')
        return redirect(url_for('settings'))
    
    try:
        # Update display name
        current_user.display_name = new_display_name.strip()
        db.session.commit()
        flash('Display name updated successfully!', 'success')
        logger.info(f"Display name changed for user {current_user.email} to {new_display_name}")
    except Exception as e:
        db.session.rollback()
        flash('An error occurred while updating your display name.', 'error')
        logger.error(f"Display name change error: {e}")
    
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

# Admin Routes
@app.route('/admin')
@admin_required
def admin_dashboard():
    """Admin dashboard with user management"""
    try:
        # Get all users with their subscription information
        users = db.session.query(User).outerjoin(Subscription).all()
        
        # Get statistics
        total_users = User.query.count()
        active_users = User.query.filter_by(is_active=True).count()
        verified_users = User.query.filter_by(email_verified=True).count()
        admin_users = User.query.filter_by(is_admin=True).count()
        
        # Get subscription statistics
        active_subscriptions = Subscription.query.filter_by(status='active').count()
        total_subscriptions = Subscription.query.count()
        
        # Get recent users (last 30 days)
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        recent_users = User.query.filter(User.created_at >= thirty_days_ago).count()
        
        # Get alert statistics
        total_alerts = TradingAlert.query.count()
        alerts_today = TradingAlert.query.filter(
            TradingAlert.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0)
        ).count()
        unread_alerts = TradingAlert.query.filter_by(is_read=False).count()
        
        stats = {
            'total_users': total_users,
            'active_users': active_users,
            'verified_users': verified_users,
            'admin_users': admin_users,
            'active_subscriptions': active_subscriptions,
            'total_subscriptions': total_subscriptions,
            'recent_users': recent_users,
            'total_alerts': total_alerts,
            'alerts_today': alerts_today,
            'unread_alerts': unread_alerts
        }
        
        return render_template('admin/dashboard.html', users=users, stats=stats)
        
    except Exception as e:
        logger.error(f"Admin dashboard error: {e}")
        flash('Error loading admin dashboard.', 'error')
        return redirect(url_for('dashboard'))

@app.route('/admin/user/<int:user_id>')
@admin_required
def admin_user_detail(user_id):
    """Detailed view of a specific user"""
    try:
        user = User.query.get_or_404(user_id)
        subscriptions = Subscription.query.filter_by(user_id=user_id).order_by(Subscription.created_at.desc()).all()
        
        return render_template('admin/user_detail.html', user=user, subscriptions=subscriptions)
        
    except Exception as e:
        logger.error(f"Admin user detail error: {e}")
        flash('Error loading user details.', 'error')
        return redirect(url_for('admin_dashboard'))

@app.route('/admin/user/<int:user_id>/toggle-status', methods=['POST'])
@admin_required
def admin_toggle_user_status(user_id):
    """Toggle user active/inactive status"""
    try:
        user = User.query.get_or_404(user_id)
        
        # Prevent admin from deactivating themselves
        if user.id == current_user.id:
            flash('You cannot deactivate your own account.', 'error')
            return redirect(url_for('admin_user_detail', user_id=user_id))
        
        user.is_active = not user.is_active
        db.session.commit()
        
        status = "activated" if user.is_active else "deactivated"
        flash(f'User {user.email} has been {status}.', 'success')
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Admin toggle user status error: {e}")
        flash('Error updating user status.', 'error')
    
    return redirect(url_for('admin_user_detail', user_id=user_id))

@app.route('/admin/user/<int:user_id>/toggle-admin', methods=['POST'])
@admin_required
def admin_toggle_admin_status(user_id):
    """Toggle user admin status"""
    try:
        user = User.query.get_or_404(user_id)
        
        # Prevent admin from removing their own admin status
        if user.id == current_user.id:
            flash('You cannot remove your own admin privileges.', 'error')
            return redirect(url_for('admin_user_detail', user_id=user_id))
        
        user.is_admin = not user.is_admin
        db.session.commit()
        
        status = "granted" if user.is_admin else "removed"
        flash(f'Admin privileges {status} for {user.email}.', 'success')
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Admin toggle admin status error: {e}")
        flash('Error updating admin status.', 'error')
    
    return redirect(url_for('admin_user_detail', user_id=user_id))

@app.route('/admin/user/<int:user_id>/update-subscription', methods=['POST'])
@admin_required
def admin_update_user_subscription(user_id):
    """Update user's subscription and cryptocurrency selection"""
    try:
        user = User.query.get_or_404(user_id)
        plan_type = request.form.get('plan_type')
        coins = request.form.getlist('coins')  # Get list of selected coins
        
        # Validate plan type
        valid_plans = ['v3', 'v6', 'v9', 'elite', 'none']
        if plan_type not in valid_plans:
            flash('Invalid plan type selected.', 'error')
            return redirect(url_for('admin_user_detail', user_id=user_id))
        
        # Handle subscription removal
        if plan_type == 'none':
            # Remove/cancel all active subscriptions for this user
            active_subscriptions = Subscription.query.filter_by(user_id=user_id, status='active').all()
            for subscription in active_subscriptions:
                subscription.status = 'cancelled'
                subscription.updated_at = datetime.utcnow()
            
            db.session.commit()
            flash(f'All subscriptions removed for {user.email}.', 'success')
            return redirect(url_for('admin_user_detail', user_id=user_id))
        
        # Validate coin selection (max 2 for non-elite plans)
        if plan_type != 'elite' and len(coins) > 2:
            flash('Non-elite plans are limited to 2 cryptocurrency pairs.', 'error')
            return redirect(url_for('admin_user_detail', user_id=user_id))
        
        # Find or create subscription
        subscription = Subscription.query.filter_by(user_id=user_id, status='active').first()
        if not subscription:
            subscription = Subscription(user_id=user_id)
            db.session.add(subscription)
        
        # Update subscription
        subscription.plan_type = plan_type
        subscription.coins = json.dumps(coins)
        subscription.status = 'active'
        subscription.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        flash(f'Subscription updated for {user.email}: {plan_type} plan with {len(coins)} coins.', 'success')
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Admin update subscription error: {e}")
        flash('Error updating subscription.', 'error')
    
    return redirect(url_for('admin_user_detail', user_id=user_id))

@app.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    """Delete user account (admin only)"""
    try:
        user = User.query.get_or_404(user_id)
        
        # Prevent admin from deleting themselves
        if user.id == current_user.id:
            flash('You cannot delete your own account.', 'error')
            return redirect(url_for('admin_user_detail', user_id=user_id))
        
        # Store email for flash message
        user_email = user.email
        
        # Delete associated subscriptions first
        Subscription.query.filter_by(user_id=user_id).delete()
        
        # Delete user
        db.session.delete(user)
        db.session.commit()
        
        flash(f'User {user_email} has been permanently deleted.', 'success')
        return redirect(url_for('admin_dashboard'))
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Admin delete user error: {e}")
        flash('Error deleting user.', 'error')
        return redirect(url_for('admin_user_detail', user_id=user_id))

@app.route('/admin/alerts')
@admin_required
def admin_alerts():
    """Admin view for managing all trading alerts"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 50
        
        alerts = TradingAlert.query.join(User)\
            .order_by(TradingAlert.created_at.desc())\
            .paginate(page=page, per_page=per_page, error_out=False)
        
        # Get alert statistics
        total_alerts = TradingAlert.query.count()
        alerts_today = TradingAlert.query.filter(
            TradingAlert.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0)
        ).count()
        unread_alerts = TradingAlert.query.filter_by(is_read=False).count()
        
        stats = {
            'total_alerts': total_alerts,
            'alerts_today': alerts_today,
            'unread_alerts': unread_alerts
        }
        
        return render_template('admin/alerts.html', alerts=alerts, stats=stats)
        
    except Exception as e:
        logger.error(f"Admin alerts error: {e}")
        flash('Error loading alerts.', 'error')
        return redirect(url_for('admin_dashboard'))

@app.route('/admin/bot-status')
@admin_required
def admin_bot_status():
    """View all user bot statuses"""
    try:
        # Get all users with their bot status and subscription info
        users = db.session.query(User).outerjoin(Subscription).filter(
            Subscription.status == 'active'
        ).add_columns(
            User.id,
            User.email,
            User.display_name,
            User.is_admin,
            User.bot_status,
            User.bot_last_active,
            User.bot_activated_at,
            Subscription.plan_type,
            Subscription.coins
        ).all()
        
        # Separate admin and customer bots
        admin_bots = []
        customer_bots = []
        
        for user_data in users:
            user_info = {
                'id': user_data.id,
                'email': user_data.email,
                'display_name': user_data.display_name,
                'bot_status': user_data.bot_status,
                'bot_last_active': user_data.bot_last_active,
                'bot_activated_at': user_data.bot_activated_at,
                'plan_type': user_data.plan_type if not user_data.is_admin else 'free',
                'coins': []
            }
            
            if user_data.is_admin:
                user_info['coins'] = ['SOL', 'RAY']  # Admin's default coins
                admin_bots.append(user_info)
            else:
                try:
                    if user_data.coins:
                        user_info['coins'] = json.loads(user_data.coins)
                    customer_bots.append(user_info)
                except json.JSONDecodeError:
                    user_info['coins'] = []
                    customer_bots.append(user_info)
        
        return render_template('admin/bot_status.html', 
                             admin_bots=admin_bots,
                             customer_bots=customer_bots)
        
    except Exception as e:
        logger.error(f"Admin bot status error: {e}")
        flash('Error loading bot statuses.', 'error')
        return redirect(url_for('admin_dashboard'))

@app.route('/admin/alerts/create', methods=['GET', 'POST'])
@admin_required
def admin_create_alert():
    """Create a new trading alert for individual user based on their subscription"""
    if request.method == 'POST':
        try:
            user_id = request.form.get('user_id')
            coin_pair = request.form.get('coin_pair')
            alert_type = request.form.get('alert_type')
            price = float(request.form.get('price', 0))
            confidence = int(request.form.get('confidence', 85))
            message = request.form.get('message')
            expires_hours = int(request.form.get('expires_hours', 24))
            
            # Validation
            if not all([user_id, coin_pair, alert_type, message]):
                flash('All fields are required.', 'error')
                return render_template('admin/create_alert.html', users=get_users_with_subscriptions())
            
            if alert_type not in ['buy', 'sell', 'hold']:
                flash('Invalid alert type.', 'error')
                return render_template('admin/create_alert.html', users=get_users_with_subscriptions())
            
            # Get user and validate subscription
            user = User.query.get(user_id)
            if not user:
                flash('User not found.', 'error')
                return render_template('admin/create_alert.html', users=get_users_with_subscriptions())
            
            subscription = user.get_active_subscription()
            if not subscription:
                flash(f'User {user.email} does not have an active subscription.', 'error')
                return render_template('admin/create_alert.html', users=get_users_with_subscriptions())
            
            # Validate coin selection against user's subscription
            try:
                user_coins = json.loads(subscription.coins) if subscription.coins else []
            except (json.JSONDecodeError, TypeError):
                user_coins = []
            
            if coin_pair not in user_coins:
                flash(f'Selected coin {coin_pair} is not in {user.email}\'s subscription. They only have: {", ".join(user_coins)}', 'error')
                return render_template('admin/create_alert.html', users=get_users_with_subscriptions())
            
            # Use the user's plan algorithm
            algorithm = subscription.plan_type
            
            # Create alert
            alert = TradingAlert(
                user_id=user.id,
                coin_pair=coin_pair.upper(),
                alert_type=alert_type,
                price=price,
                confidence=confidence,
                algorithm=algorithm,
                message=message,
                expires_at=datetime.utcnow() + timedelta(hours=expires_hours)
            )
            
            db.session.add(alert)
            db.session.commit()
            
            flash(f'Alert created successfully for {user.email}: {coin_pair} ({alert_type.upper()}) using {algorithm.upper()} algorithm!', 'success')
            return redirect(url_for('admin_alerts'))
            
        except ValueError as ve:
            flash('Invalid price value. Please enter a valid number.', 'error')
            return render_template('admin/create_alert.html', users=get_users_with_subscriptions())
        except Exception as e:
            db.session.rollback()
            logger.error(f"Create alert error: {e}")
            flash('Error creating alert.', 'error')
    
    # Get users with active subscriptions and their coins
    users_with_subs = get_users_with_subscriptions()
    return render_template('admin/create_alert.html', users=users_with_subs)

def get_users_with_subscriptions():
    """Helper function to get users with their active subscriptions and coin data"""
    users_data = []
    
    # Get users with active subscriptions
    users = User.query.join(Subscription)\
        .filter(Subscription.status == 'active')\
        .filter(User.is_active == True)\
        .order_by(User.email).all()
    
    for user in users:
        subscription = user.get_active_subscription()
        if subscription:
            try:
                coins = json.loads(subscription.coins) if subscription.coins else []
            except (json.JSONDecodeError, TypeError):
                coins = []
            
            users_data.append({
                'id': user.id,
                'email': user.email,
                'display_name': user.display_name,
                'plan_type': subscription.plan_type,
                'coins': coins,
                'coin_count': len(coins)
            })
    
    return users_data

@app.route('/admin/alerts/broadcast', methods=['GET', 'POST'])
@admin_required
def admin_broadcast_alert():
    """Broadcast an alert to all active subscribers"""
    if request.method == 'POST':
        try:
            coin_pair = request.form.get('coin_pair')
            alert_type = request.form.get('alert_type')
            price = float(request.form.get('price', 0))
            confidence = int(request.form.get('confidence', 85))
            algorithm = request.form.get('algorithm')
            message = request.form.get('message')
            expires_hours = int(request.form.get('expires_hours', 24))
            plan_filter = request.form.get('plan_filter', 'all')
            
            # Get users based on plan filter
            if plan_filter == 'all':
                users = User.query.join(Subscription)\
                    .filter(Subscription.status == 'active')\
                    .filter(User.is_active == True).all()
            else:
                users = User.query.join(Subscription)\
                    .filter(Subscription.status == 'active')\
                    .filter(Subscription.plan_type == plan_filter)\
                    .filter(User.is_active == True).all()
            
            # Create alerts for all matching users
            alert_count = 0
            for user in users:
                alert = TradingAlert(
                    user_id=user.id,
                    coin_pair=coin_pair.upper(),
                    alert_type=alert_type,
                    price=price,
                    confidence=confidence,
                    algorithm=algorithm,
                    message=message,
                    expires_at=datetime.utcnow() + timedelta(hours=expires_hours)
                )
                db.session.add(alert)
                alert_count += 1
            
            db.session.commit()
            
            flash(f'Alert broadcasted to {alert_count} users!', 'success')
            return redirect(url_for('admin_alerts'))
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Broadcast alert error: {e}")
            flash('Error broadcasting alert.', 'error')
    
    return render_template('admin/broadcast_alert.html')

@app.route('/admin/user/<int:user_id>/send-welcome', methods=['POST'])
@admin_required
def admin_send_welcome_email(user_id):
    """Send welcome email to a user (admin only)"""
    try:
        user = User.query.get_or_404(user_id)
        
        if not user.email_verified:
            flash('Cannot send welcome email to unverified user.', 'error')
            return redirect(url_for('admin_user_detail', user_id=user_id))
        
        if send_welcome_email(user):
            flash(f'Welcome email sent to {user.email}.', 'success')
        else:
            flash(f'Failed to send welcome email to {user.email}.', 'error')
        
    except Exception as e:
        logger.error(f"Admin send welcome email error: {e}")
        flash('Error sending welcome email.', 'error')
    
    return redirect(url_for('admin_user_detail', user_id=user_id))

# Push Notification API Routes
@app.route('/api/push/vapid-public-key')
def get_vapid_public_key():
    """Get the VAPID public key for push notification subscription"""
    try:
        public_key = push_service.get_vapid_public_key()
        return jsonify({'publicKey': public_key})
    except Exception as e:
        logger.error(f"Error getting VAPID public key: {e}")
        return jsonify({'error': 'Failed to get public key'}), 500

@app.route('/api/push/subscribe', methods=['POST'])
@login_required
def subscribe_to_push():
    """Subscribe user to push notifications"""
    try:
        subscription_data = request.json
        
        if not subscription_data:
            return jsonify({'error': 'No subscription data provided'}), 400
        
        # Extract subscription details
        endpoint = subscription_data.get('endpoint')
        keys = subscription_data.get('keys', {})
        p256dh = keys.get('p256dh')
        auth = keys.get('auth')
        
        if not all([endpoint, p256dh, auth]):
            return jsonify({'error': 'Invalid subscription data'}), 400
        
        # Update user with subscription details
        current_user.push_subscription_endpoint = endpoint
        current_user.push_subscription_p256dh = p256dh
        current_user.push_subscription_auth = auth
        current_user.push_notifications_enabled = True
        
        db.session.commit()
        
        logger.info(f"User {current_user.email} subscribed to push notifications")
        return jsonify({'success': True, 'message': 'Successfully subscribed to push notifications'})
        
    except Exception as e:
        logger.error(f"Error subscribing to push notifications: {e}")
        db.session.rollback()
        return jsonify({'error': 'Failed to subscribe'}), 500

@app.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def unsubscribe_from_push():
    """Unsubscribe user from push notifications"""
    try:
        # Clear user's subscription details
        current_user.push_subscription_endpoint = None
        current_user.push_subscription_p256dh = None
        current_user.push_subscription_auth = None
        current_user.push_notifications_enabled = False
        
        db.session.commit()
        
        logger.info(f"User {current_user.email} unsubscribed from push notifications")
        return jsonify({'success': True, 'message': 'Successfully unsubscribed from push notifications'})
        
    except Exception as e:
        logger.error(f"Error unsubscribing from push notifications: {e}")
        db.session.rollback()
        return jsonify({'error': 'Failed to unsubscribe'}), 500

@app.route('/api/push/test', methods=['POST'])
@login_required
def test_push_notification():
    """Send a test push notification to the current user"""
    try:
        if not current_user.push_notifications_enabled:
            return jsonify({'error': 'Push notifications not enabled'}), 400
        
        # Send test notification
        result = push_service.send_trading_alert_notification(
            user=current_user,
            symbol="TEST",
            price="$50,000",
            change="+5.2%",
            algorithm="Test",
            alert_type="test",
            confidence="95%"
        )
        
        if result:
            return jsonify({'success': True, 'message': 'Test notification sent successfully'})
        else:
            return jsonify({'error': 'Failed to send test notification'}), 500
            
    except Exception as e:
        logger.error(f"Error sending test push notification: {e}")
        return jsonify({'error': 'Failed to send test notification'}), 500

# New Simple Email Alert System
@app.route('/api/test-email-alert', methods=['POST'])
@login_required
def test_email_alert():
    """Send a test trading alert via email - works on ALL devices"""
    try:
        user = current_user
        
        # Send test email alert
        subject = "🚀 Trading Alert Test - MalachiteBionics"
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: linear-gradient(135deg, #3b82f6, #8b5cf6); padding: 20px; text-align: center; color: white;">
                <h1 style="margin: 0; font-size: 24px;">🚀 Trading Alert Test</h1>
                <p style="margin: 10px 0 0; opacity: 0.9;">MalachiteBionics Trading Bot</p>
            </div>
            
            <div style="padding: 30px; background: #f8fafc;">
                <h2 style="color: #1e293b; margin-top: 0;">Hi {user.display_name or user.username}!</h2>
                
                <p style="color: #475569; font-size: 16px; line-height: 1.6;">
                    This is a test trading alert to confirm your email notifications are working perfectly.
                </p>
                
                <div style="background: #dcfce7; border: 1px solid #16a34a; border-radius: 8px; padding: 15px; margin: 20px 0;">
                    <h3 style="color: #15803d; margin: 0 0 10px 0; font-size: 18px;">
                        ✅ Email Alerts Active
                    </h3>
                    <p style="color: #166534; margin: 0; font-size: 14px;">
                        You'll receive instant notifications when trading opportunities are detected.
                        <strong>No browser setup required!</strong>
                    </p>
                </div>
                
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{request.url_root}dashboard" 
                       style="background: #3b82f6; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                        View Dashboard
                    </a>
                </div>
                
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
                
                <p style="color: #64748b; font-size: 12px; text-align: center; margin: 0;">
                    MalachiteBionics Trading Bot • Automated Cryptocurrency Trading
                </p>
            </div>
        </div>
        """
        
        msg = Message(
            subject,
            recipients=[user.email],
            html=html_body
        )
        
        mail.send(msg)
        logger.info(f"Test email alert sent to user {user.username} ({user.email})")
        
        return jsonify({
            'success': True, 
            'message': 'Test alert sent to your email!'
        })
        
    except Exception as e:
        logger.error(f"Error sending test email alert: {e}")
        return jsonify({
            'success': False, 
            'message': 'Failed to send test email. Please check your email settings.'
        }), 500

@app.route('/api/update-phone', methods=['POST'])
@login_required
def update_phone():
    """Update user's phone number for SMS alerts"""
    try:
        data = request.get_json()
        phone = data.get('phone', '').strip()
        
        if not phone:
            return jsonify({'success': False, 'message': 'Phone number is required'}), 400
        
        # Basic phone validation (US format)
        import re
        phone_clean = re.sub(r'[^\d]', '', phone)
        if not re.match(r'^\+?1?\d{10,11}$', phone_clean):
            return jsonify({'success': False, 'message': 'Please enter a valid US phone number'}), 400
        
        # Update user's phone number
        current_user.phone = phone
        db.session.commit()
        
        logger.info(f"Phone number updated for user {current_user.username}: {phone}")
        
        return jsonify({
            'success': True,
            'message': 'Phone number updated! SMS alerts will be available soon.'
        })
        
    except Exception as e:
        logger.error(f"Error updating phone number: {e}")
        return jsonify({
            'success': False,
            'message': 'Failed to update phone number'
        }), 500

# Function to send trading alert emails (used by trading bot)
def send_trading_alert_email(user, alert_type, details):
    """Send trading alert via email - called by trading bot"""
    try:
        if alert_type == 'buy_signal':
            subject = f"🟢 BUY Signal Detected - {details.get('pair', 'Unknown')}"
            color = "#16a34a"
            emoji = "🟢"
        elif alert_type == 'sell_signal':
            subject = f"🔴 SELL Signal Detected - {details.get('pair', 'Unknown')}"
            color = "#dc2626"
            emoji = "🔴"
        else:
            subject = f"📊 Trading Update - {details.get('pair', 'Unknown')}"
            color = "#3b82f6"
            emoji = "📊"
        
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: linear-gradient(135deg, {color}, #8b5cf6); padding: 20px; text-align: center; color: white;">
                <h1 style="margin: 0; font-size: 24px;">{emoji} Trading Alert</h1>
                <p style="margin: 10px 0 0; opacity: 0.9;">MalachiteBionics Bot • Live Signal</p>
            </div>
            
            <div style="padding: 30px; background: #f8fafc;">
                <h2 style="color: #1e293b; margin-top: 0;">Alert Details</h2>
                
                <div style="background: white; border-radius: 8px; padding: 20px; margin: 15px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                    <p style="margin: 0 0 10px 0; font-size: 16px;"><strong>Pair:</strong> {details.get('pair', 'N/A')}</p>
                    <p style="margin: 0 0 10px 0; font-size: 16px;"><strong>Action:</strong> {alert_type.replace('_', ' ').title()}</p>
                    <p style="margin: 0 0 10px 0; font-size: 16px;"><strong>Price:</strong> ${details.get('price', 'N/A')}</p>
                    <p style="margin: 0; font-size: 14px; color: #64748b;"><strong>Time:</strong> {details.get('time', 'Just now')}</p>
                </div>
                
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{request.url_root}dashboard" 
                       style="background: {color}; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">
                        View Dashboard
                    </a>
                </div>
                
                <p style="color: #64748b; font-size: 12px; text-align: center; margin: 20px 0 0 0;">
                    This alert was sent instantly to your email • No complex setup required
                </p>
            </div>
        </div>
        """
        
        msg = Message(
            subject,
            recipients=[user.email],
            html=html_body
        )
        
        mail.send(msg)
        logger.info(f"Trading alert email sent to {user.username}: {alert_type}")
        return True
        
    except Exception as e:
        logger.error(f"Error sending trading alert email: {e}")
        return False

@app.route('/admin/fix-database')
def fix_database():
    """Admin route to fix database schema issues"""
    try:
        logger.info("Manual database fix initiated")
        
        # Comprehensive database repair
        repair_results = []
        
        with app.app_context():
            # Step 1: Test basic connection
            try:
                db.session.execute(text('SELECT 1'))
                repair_results.append("✅ Database connection: OK")
            except Exception as conn_error:
                repair_results.append(f"❌ Database connection failed: {conn_error}")
                return f"Database repair failed - connection issue: {conn_error}"
            
            # Step 2: Check if tables exist
            try:
                from sqlalchemy import inspect
                inspector = inspect(db.engine)
                tables = inspector.get_table_names()
                repair_results.append(f"✅ Existing tables: {', '.join(tables) if tables else 'None'}")
            except Exception as inspect_error:
                repair_results.append(f"❌ Table inspection failed: {inspect_error}")
            
            # Step 3: Force table recreation
            try:
                db.create_all()
                repair_results.append("✅ Tables created/updated")
            except Exception as create_error:
                repair_results.append(f"❌ Table creation failed: {create_error}")
            
            # Step 4: Add missing columns
            column_fixes = []
            
            # Check and add phone column
            try:
                db.session.execute(text("SELECT phone FROM user LIMIT 1"))
                column_fixes.append("✅ Phone column: exists")
            except Exception:
                try:
                    db.session.execute(text("ALTER TABLE user ADD COLUMN phone VARCHAR(20)"))
                    db.session.commit()
                    column_fixes.append("✅ Phone column: added")
                except Exception as phone_error:
                    if "duplicate column" in str(phone_error).lower():
                        column_fixes.append("✅ Phone column: already exists")
                    else:
                        column_fixes.append(f"❌ Phone column: failed to add ({phone_error})")
            
            # Check and add uuid column
            try:
                db.session.execute(text("SELECT uuid FROM user LIMIT 1"))
                column_fixes.append("✅ UUID column: exists")
            except Exception:
                try:
                    db.session.execute(text("ALTER TABLE user ADD COLUMN uuid VARCHAR(36)"))
                    db.session.commit()
                    column_fixes.append("✅ UUID column: added")
                except Exception as uuid_error:
                    if "duplicate column" in str(uuid_error).lower():
                        column_fixes.append("✅ UUID column: already exists")
                    else:
                        column_fixes.append(f"❌ UUID column: failed to add ({uuid_error})")
            
            # Step 5: Test user table structure
            try:
                result = db.session.execute(text("SELECT id, email, password_hash FROM user LIMIT 1"))
                repair_results.append("✅ User table structure: OK")
            except Exception as struct_error:
                repair_results.append(f"❌ User table structure issue: {struct_error}")
            
            # Step 6: Final verification
            try:
                if create_tables():
                    repair_results.append("✅ Final database verification: PASSED")
                else:
                    repair_results.append("❌ Final database verification: FAILED")
            except Exception as verify_error:
                repair_results.append(f"❌ Final verification error: {verify_error}")
            
            # Generate comprehensive report
            report = f"""
            <html>
            <head><title>Database Repair Report</title></head>
            <body style="font-family: Arial, sans-serif; margin: 40px;">
                <h1 style="color: #333;">🔧 Database Repair Report</h1>
                <h2 style="color: #666;">Basic Checks:</h2>
                <ul>
                    {"".join(f"<li>{result}</li>" for result in repair_results)}
                </ul>
                <h2 style="color: #666;">Column Migrations:</h2>
                <ul>
                    {"".join(f"<li>{fix}</li>" for fix in column_fixes)}
                </ul>
                <h2 style="color: #666;">Next Steps:</h2>
                <p>1. Try logging in again</p>
                <p>2. If issues persist, contact support with this report</p>
                <p>3. <a href="/login">Return to Login Page</a></p>
                <hr>
                <p style="color: #999; font-size: 12px;">
                    Report generated on {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
                </p>
            </body>
            </html>
            """
            
            logger.info("Database repair completed")
            return report
            
    except Exception as e:
        error_msg = f"Database repair critical error: {e}"
        logger.error(error_msg)
        return f"""
        <html>
        <body style="font-family: Arial, sans-serif; margin: 40px;">
            <h1 style="color: red;">❌ Database Repair Failed</h1>
            <p><strong>Error:</strong> {error_msg}</p>
            <p><a href="/login">Return to Login Page</a></p>
            <p>Please contact support if this error persists.</p>
        </body>
        </html>
        """

@app.route('/emergency-db-reset')
def emergency_database_reset():
    """Emergency route to completely reset the database - USE WITH CAUTION"""
    try:
        logger.warning("EMERGENCY DATABASE RESET INITIATED")
        
        with app.app_context():
            # Drop all tables
            db.drop_all()
            logger.info("All tables dropped")
            
            # Recreate all tables
            db.create_all()
            logger.info("All tables recreated")
            
            # Create admin user
            admin_email = "malachitebionics@gmail.com"
            existing_admin = User.query.filter_by(email=admin_email).first()
            
            if not existing_admin:
                admin_user = User(
                    email=admin_email,
                    display_name="Admin",
                    is_admin=True,
                    email_verified=True,
                    is_active=True
                )
                admin_user.set_password("admin123")  # Default password - CHANGE IMMEDIATELY
                db.session.add(admin_user)
                db.session.commit()
                logger.info("Admin user created with default password")
            
            return f"""
            <html>
            <body style="font-family: Arial, sans-serif; margin: 40px;">
                <h1 style="color: orange;">⚠️ Emergency Database Reset Complete</h1>
                <p><strong>Database has been completely reset!</strong></p>
                <p>Admin account created:</p>
                <ul>
                    <li>Email: {admin_email}</li>
                    <li>Password: admin123 (CHANGE IMMEDIATELY)</li>
                </ul>
                <p><a href="/login" style="background: #007bff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Login Now</a></p>
                <p style="color: red; font-size: 14px;">
                    <strong>WARNING:</strong> All user data has been lost. This should only be used in emergency situations.
                </p>
            </body>
            </html>
            """
            
    except Exception as e:
        error_msg = f"Emergency reset failed: {e}"
        logger.error(error_msg)
        return f"""
        <html>
        <body style="font-family: Arial, sans-serif; margin: 40px;">
            <h1 style="color: red;">❌ Emergency Reset Failed</h1>
            <p><strong>Error:</strong> {error_msg}</p>
            <p>Please contact technical support immediately.</p>
        </body>
        </html>
        """

# Service Worker route with correct MIME type
@app.route('/static/sw.js')
def service_worker():
    """Serve service worker with correct Content-Type header"""
    from flask import send_from_directory, make_response
    
    try:
        response = make_response(send_from_directory('static', 'sw.js'))
        response.headers['Content-Type'] = 'application/javascript'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Service-Worker-Allowed'] = '/'
        return response
    except Exception as e:
        logger.error(f"Error serving service worker: {e}")
        return "Service worker not found", 404

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
        # Start the trading bot as a background service
        try:
            from start_bot_service import start_trading_bot
            start_trading_bot()
            logger.info("🤖 Trading bot started successfully")
        except Exception as bot_error:
            logger.error(f"Failed to start trading bot: {bot_error}")
            # Continue running Flask app even if bot fails to start
        
        # Database tables are already created by create_tables() function above
        app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        print(f"Error: {e}")
        raise
