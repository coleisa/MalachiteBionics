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
            admin = User(email=admin_email)
            admin.set_password("admin123")  # Change this password!
            db.session.add(admin)
            db.session.commit()
            print(f"Admin user created: {admin_email}")

if __name__ == '__main__':
    init_database()
