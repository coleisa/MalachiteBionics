#!/usr/bin/env python3
"""
Simple migration script to add phone column to User table
Run this once to update existing database
"""

import os
import sys
from sqlalchemy import create_engine, text

def add_phone_column():
    """Add phone column to User table if it doesn't exist"""
    try:
        # Get database URL from environment
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            print("❌ DATABASE_URL environment variable not set")
            return False
        
        # Fix PostgreSQL URL format for SQLAlchemy
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        
        engine = create_engine(database_url)
        
        with engine.connect() as conn:
            # Check if phone column already exists
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'user' AND column_name = 'phone'
            """))
            
            if result.fetchone():
                print("✅ Phone column already exists")
                return True
            
            # Add phone column
            conn.execute(text("""
                ALTER TABLE "user" 
                ADD COLUMN phone VARCHAR(20)
            """))
            
            conn.commit()
            print("✅ Phone column added successfully")
            return True
            
    except Exception as e:
        print(f"❌ Error adding phone column: {e}")
        return False

if __name__ == "__main__":
    print("🔄 Adding phone column to User table...")
    success = add_phone_column()
    sys.exit(0 if success else 1)
