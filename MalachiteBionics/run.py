#!/usr/bin/env python3
"""
Development server launcher
"""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

if __name__ == '__main__':
    from app import app
    
    # Initialize database
    with app.app_context():
        from app import db
        db.create_all()
        print("Database initialized!")
    
    # Run development server
    app.run(
        debug=True,
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000))
    )
