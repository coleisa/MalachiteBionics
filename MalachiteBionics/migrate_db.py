"""
Database migration script to add missing columns
Run this if you're getting 500 errors during login
"""

from app import app, db, User
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def migrate_database():
    """Add missing columns to existing database"""
    with app.app_context():
        try:
            # Get database connection
            connection = db.engine.connect()
            
            # List of columns to add if they don't exist
            columns_to_add = [
                ("uuid", "VARCHAR(36) UNIQUE DEFAULT gen_random_uuid()::text"),
                ("email_verified", "BOOLEAN DEFAULT FALSE"),
                ("email_verification_token", "VARCHAR(100) UNIQUE"),
                ("email_verification_sent_at", "TIMESTAMP"),
                ("last_login", "TIMESTAMP"),
                ("last_seen", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                ("login_count", "INTEGER DEFAULT 0"),
                ("is_admin", "BOOLEAN DEFAULT FALSE"),
                ("bot_status", "VARCHAR(20) DEFAULT 'offline'"),
                ("bot_last_active", "TIMESTAMP"),
                ("bot_activated_at", "TIMESTAMP")
            ]
            
            # Check which columns exist
            result = connection.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'user'
            """)
            existing_columns = [row[0] for row in result]
            
            logger.info(f"Existing columns: {existing_columns}")
            
            # Add missing columns
            for column_name, column_def in columns_to_add:
                if column_name not in existing_columns:
                    try:
                        # Special handling for UUID column
                        if column_name == "uuid":
                            connection.execute(f"ALTER TABLE \"user\" ADD COLUMN {column_name} {column_def}")
                            # Update existing users with UUIDs
                            connection.execute("""
                                UPDATE "user" 
                                SET uuid = gen_random_uuid()::text 
                                WHERE uuid IS NULL
                            """)
                        else:
                            connection.execute(f"ALTER TABLE \"user\" ADD COLUMN {column_name} {column_def}")
                        
                        logger.info(f"✅ Added column: {column_name}")
                    except Exception as e:
                        logger.warning(f"⚠️  Could not add {column_name}: {e}")
                else:
                    logger.info(f"⏭️  Column {column_name} already exists")
            
            connection.close()
            
            # Now update the admin user to ensure it has all required fields
            admin = User.query.filter_by(email="admin@tradingbot.com").first()
            if admin:
                # Ensure admin has all required fields
                if not admin.display_name:
                    admin.display_name = "System Administrator"
                if not admin.is_admin:
                    admin.is_admin = True
                if not admin.email_verified:
                    admin.email_verified = True
                if not admin.is_active:
                    admin.is_active = True
                if not admin.bot_status:
                    admin.bot_status = 'offline'
                
                db.session.commit()
                logger.info("✅ Updated admin user with required fields")
            else:
                logger.warning("⚠️  Admin user not found")
            
            logger.info("🎉 Database migration completed successfully!")
            
        except Exception as e:
            logger.error(f"❌ Migration failed: {e}")
            db.session.rollback()
            raise

if __name__ == '__main__':
    migrate_database()
