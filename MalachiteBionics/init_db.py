from app import app, db, User, Subscription

def init_database():
    """Initialize the database with tables"""
    with app.app_context():
        # Create all tables
        db.create_all()
        print("Database tables created successfully!")
        
        # Create admin user with correct email
        admin_email = "malachitebionics@gmail.com"
        if not User.query.filter_by(email=admin_email).first():
            admin = User(
                email=admin_email,
                display_name="MalachiteBionics Admin",
                is_admin=True,
                email_verified=True,  # Admin is pre-verified
                is_active=True,
                bot_status='offline'  # Default bot status
            )
            admin.set_password("admin123")  # Change this password after first login!
            db.session.add(admin)
            db.session.commit()
            print(f"Admin user created: {admin_email} (password: admin123)")
            print("IMPORTANT: Change the admin password after first login!")

if __name__ == '__main__':
    init_database()
