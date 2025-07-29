from app import app, db, User, Subscription

def init_database():
    """Initialize the database with tables"""
    with app.app_context():
        # Create all tables
        db.create_all()
        print("Database tables created successfully!")
        
        # Create a test admin user (optional)
        admin_email = "admin@tradingbot.com"
        if not User.query.filter_by(email=admin_email).first():
            admin = User(
                email=admin_email,
                display_name="System Administrator",
                is_admin=True,
                email_verified=True,  # Admin is pre-verified
                is_active=True,
                bot_status='offline'  # Default bot status
            )
            admin.set_password("admin123")  # Change this password!
            db.session.add(admin)
            db.session.commit()
            print(f"Admin user created: {admin_email} (password: admin123)")

if __name__ == '__main__':
    init_database()
