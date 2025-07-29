"""
Web Push Notification Service for Trading Alerts
Sends push notifications directly to users' phones/devices
"""

from flask import jsonify, request
import json
import requests
from pywebpush import webpush, WebPushException
import os
import logging

logger = logging.getLogger(__name__)

class PushNotificationService:
    def __init__(self):
        # You'll need to generate these VAPID keys
        self.vapid_private_key = os.environ.get('VAPID_PRIVATE_KEY')
        self.vapid_public_key = os.environ.get('VAPID_PUBLIC_KEY') 
        self.vapid_email = os.environ.get('VAPID_EMAIL', 'mailto:malachitebionics@gmail.com')
        
        # Generate VAPID keys if they don't exist
        if not self.vapid_private_key or not self.vapid_public_key:
            self._generate_vapid_keys()
    
    def _generate_vapid_keys(self):
        """Generate VAPID keys if they don't exist"""
        try:
            from pywebpush import vapid
            vapid_data = vapid.Vapid()
            vapid_data.generate_keys()
            
            self.vapid_private_key = vapid_data.private_key
            self.vapid_public_key = vapid_data.public_key
            
            logger.info("Generated new VAPID keys for push notifications")
            logger.warning("Please set VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY environment variables for production")
            
        except Exception as e:
            logger.error(f"Failed to generate VAPID keys: {e}")
            # Set dummy keys as fallback
            self.vapid_private_key = "dummy-private-key"
            self.vapid_public_key = "dummy-public-key"
    
    def get_vapid_public_key(self):
        """Get the VAPID public key for client subscription"""
        return self.vapid_public_key
        
    def send_trading_alert_notification(self, user, symbol, price, change, algorithm, alert_type, confidence):
        """Send push notification for trading alert to a specific user"""
        try:
            if not user.push_notifications_enabled:
                return False
                
            # Create subscription info from user data
            subscription_info = {
                "endpoint": user.push_subscription_endpoint,
                "keys": {
                    "p256dh": user.push_subscription_p256dh,
                    "auth": user.push_subscription_auth
                }
            }
            
            notification_payload = {
                "title": f"🚨 {alert_type.upper()} Signal - {symbol}",
                "body": f"{algorithm.upper()} Algorithm | Price: {price} | Change: {change} | Confidence: {confidence}",
                "icon": "/static/icon-192x192.png",
                "badge": "/static/badge-72x72.png",
                "data": {
                    "url": "/alerts",
                    "type": "trading_alert",
                    "symbol": symbol,
                    "algorithm": algorithm
                },
                "actions": [
                    {
                        "action": "view",
                        "title": "View Alert"
                    },
                    {
                        "action": "dismiss",
                        "title": "Dismiss"
                    }
                ],
                "requireInteraction": True,
                "tag": f"trading-alert-{symbol}"
            }
            
            try:
                webpush(
                    subscription_info=subscription_info,
                    data=json.dumps(notification_payload),
                    vapid_private_key=self.vapid_private_key,
                    vapid_claims={
                        "sub": self.vapid_email
                    }
                )
                logger.info(f"Push notification sent successfully to {user.email}")
                return True
                
            except WebPushException as e:
                logger.error(f"Failed to send push notification to {user.email}: {e}")
                if hasattr(e, 'response') and e.response and e.response.status_code == 410:
                    # Subscription expired, should disable notifications
                    logger.info(f"Subscription expired for {user.email}, disabling push notifications")
                    user.push_notifications_enabled = False
                return False
                
        except Exception as e:
            logger.error(f"Error sending push notification to {user.email}: {e}")
            return False

# Global instance
push_service = PushNotificationService()
