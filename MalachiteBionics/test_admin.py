"""
Simple test script to check if admin user exists and can be queried
Run this to test database connectivity and user model
"""

from app import app, db, User

def test_admin_user():
    """Test if we can query the admin user"""
    with app.app_context():
        try:
            print("🔍 Testing database connection...")
            
            # Test basic query
            user_count = User.query.count()
            print(f"✅ Total users in database: {user_count}")
            
            # Test admin user
            admin = User.query.filter_by(email="admin@tradingbot.com").first()
            if admin:
                print(f"✅ Admin user found:")
                print(f"   - Email: {admin.email}")
                print(f"   - Display Name: {admin.display_name if hasattr(admin, 'display_name') else 'Not set'}")
                print(f"   - Is Admin: {admin.is_admin if hasattr(admin, 'is_admin') else 'Not set'}")
                print(f"   - Email Verified: {admin.email_verified if hasattr(admin, 'email_verified') else 'Not set'}")
                print(f"   - UUID: {admin.uuid if hasattr(admin, 'uuid') else 'Not set'}")
                print(f"   - Has password: {'Yes' if admin.password_hash else 'No'}")
                
                # Test password check
                try:
                    password_valid = admin.check_password("admin123")
                    print(f"   - Password 'admin123' valid: {password_valid}")
                except Exception as e:
                    print(f"   - Password check error: {e}")
                
            else:
                print("❌ Admin user not found!")
                print("Available users:")
                users = User.query.all()
                for user in users:
                    print(f"   - {user.email}")
            
        except Exception as e:
            print(f"❌ Database error: {e}")
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    test_admin_user()
